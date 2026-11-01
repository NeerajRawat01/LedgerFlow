"""Deterministic intercompany reconciliation (verification half of the workflow).

This module is the **Python half** of the AI-assisted intercompany workflow.
It performs every step that must be exact and auditable:

* **normalise** the raw ``intercompany.csv`` rows into typed records;
* **validate** the entity/counterparty relationship (both must be known group
  entities, and an entity never transacts with itself);
* **convert** amounts to a common comparison currency using the *approved*
  :class:`FXService` rates and Decimal arithmetic;
* **verify** a proposed match against the accounting rules (reciprocal
  direction, same reference or reconcilable reference, and an exact Decimal
  difference), returning a typed verdict with the residual.

It never guesses a match and never invents a figure. The LLM only proposes
candidate pairs; this service is the authority on whether a proposal is a
genuine, reconciling match. Nothing here posts an elimination — that stage
remains unimplemented and is reported as blocked elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from app.accounting.errors import UnknownEntityError
from app.accounting.models import FXRateType
from app.core.errors import ServiceError
from app.services.source_data import SourceDataService

_ZERO = Decimal("0")


class MatchType(str, Enum):
    """How a verified pair reconciles."""

    EXACT = "exact"                     # same reference, no residual
    FX_RESIDUAL = "fx_residual"         # reconciling within an FX tolerance
    REFERENCE_MISMATCH = "reference_mismatch"  # different refs, not verifiable
    DIRECTION_INVALID = "direction_invalid"    # not reciprocal
    RESIDUAL = "residual"               # reciprocal but a genuine difference


@dataclass(frozen=True)
class IntercompanyRecord:
    """One normalised ``intercompany.csv`` row with 1-based source row number."""

    source_row: int
    reporting_entity: str
    counterparty: str
    doc_ref: str
    doc_date: str
    doc_currency: str
    doc_amount: Decimal
    booked_currency: str
    booked_amount: Decimal
    description: str


@dataclass(frozen=True)
class MatchVerdict:
    """The deterministic verdict on a proposed pair.

    ``accepted`` is True only when the pair genuinely reconciles. ``residual``
    is the exact Decimal difference in the reporting currency (never rounded
    to hide a discrepancy). ``reason`` always carries the audit explanation.
    """

    left_row: int
    right_row: int
    accepted: bool
    match_type: MatchType
    residual: Decimal | None
    reporting_currency: str | None
    reason: str


@dataclass(frozen=True)
class ReconciledPair:
    """One reciprocal pair of intercompany rows (one per entity)."""

    doc_ref: str
    doc_currency: str
    # Direction of the outward leg (the row that bills).
    from_entity: str
    to_entity: str
    left_row: int
    right_row: int
    # Closing-rate comparison amounts (GBP), full precision.
    left_gbp: Decimal
    right_gbp: Decimal
    residual_gbp: Decimal
    match_type: MatchType
    reason: str


@dataclass(frozen=True)
class UnmatchedItem:
    """One intercompany row that did not pair with a reciprocal row."""

    source_row: int
    reporting_entity: str
    counterparty: str
    doc_ref: str
    doc_currency: str
    doc_amount: Decimal
    booked_currency: str
    booked_amount: Decimal
    gbp_amount: Decimal | None
    classification: str  # "in_transit" | "unmatched"
    reason: str


@dataclass(frozen=True)
class ReconciliationResult:
    """The deterministic intercompany reconciliation outcome.

    ``residual_gbp`` is the sum of matched-pair differences in GBP, computed
    with Decimal at closing rates; it is the amount policy §5 sends to the
    foreign-exchange account. Nothing here is rounded.
    """

    reporting_currency: str
    pairs: tuple[ReconciledPair, ...] = ()
    unmatched: tuple[UnmatchedItem, ...] = ()
    residual_gbp: Decimal = Decimal("0")

    @property
    def matched_count(self) -> int:
        return len(self.pairs)

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched)


class IntercompanyReconciler:
    """Normalise intercompany rows and verify proposed matches deterministically."""

    def __init__(
        self,
        service: SourceDataService,
        *,
        fx: Any = None,
        reporting_currency: str = "GBP",
        tolerance: Decimal = Decimal("0.01"),
    ) -> None:
        self.service = service
        self._fx = fx
        self.reporting_currency = reporting_currency
        # Policy §7 rounds only reported figures; matching tolerates at most
        # the reporting quantum so a genuine sub-cent rounding difference is
        # not mistaken for a real mismatch.
        self.tolerance = tolerance

    # -- normalisation -----------------------------------------------------

    def _known_entities(self) -> set[str]:
        return {
            (row.get("entity_id") or "").strip().upper()
            for row in self.service.list_entities()
            if (row.get("entity_id") or "").strip()
        }

    @staticmethod
    def _dec(value: str) -> Decimal:
        """Parse a source amount as Decimal; raise a typed error if invalid."""
        text = (value or "").strip()
        if not text:
            return _ZERO
        try:
            return Decimal(text)
        except Exception as exc:  # noqa: BLE001 - surfaced as typed ServiceError
            raise ServiceError(
                f"invalid intercompany amount {value!r}",
                details={"value": value},
            ) from exc

    def load_records(self) -> list[IntercompanyRecord]:
        """Return typed records for every intercompany row (order preserved)."""
        rows = self.service.get_intercompany()
        known = self._known_entities()
        records: list[IntercompanyRecord] = []
        for index, row in enumerate(rows, start=1):
            entity = (row.get("reporting_entity") or "").strip().upper()
            counterparty = (row.get("counterparty") or "").strip().upper()
            # Validate the relationship deterministically; never invent one.
            for party in (entity, counterparty):
                if party and party not in known:
                    raise UnknownEntityError(
                        f"intercompany row {index} references unknown entity {party!r}",
                        details={"source_row": index, "entity": party, "known": sorted(known)},
                    )
            records.append(
                IntercompanyRecord(
                    source_row=index,
                    reporting_entity=entity,
                    counterparty=counterparty,
                    doc_ref=(row.get("doc_ref") or "").strip(),
                    doc_date=(row.get("doc_date") or "").strip(),
                    doc_currency=(row.get("doc_currency") or "").strip().upper(),
                    doc_amount=self._dec(row.get("doc_amount")),
                    booked_currency=(row.get("booked_currency") or "").strip().upper(),
                    booked_amount=self._dec(row.get("booked_amount")),
                    description=(row.get("description") or "").strip(),
                )
            )
        return records

    def to_comparison_amount(
        self, record: IntercompanyRecord, *, rate_type: FXRateType = FXRateType.CLOSING
    ) -> Decimal:
        """Convert a record's booked amount to the reporting currency.

        Uses the approved :class:`FXService` (direct/inverse/cross) at full
        precision. Raises the typed ``MissingFXRateError`` when no rate exists —
        a rate is never fabricated.
        """
        if not self._fx:
            raise ServiceError("no FX service bound to the reconciler")
        if record.booked_currency == self.reporting_currency:
            return record.booked_amount
        return self._fx.convert(
            record.booked_amount,
            record.booked_currency,
            self.reporting_currency,
            rate_type,
        )

    # -- deterministic verification ---------------------------------------

    def verify(
        self,
        left: IntercompanyRecord,
        right: IntercompanyRecord,
        *,
        rate_type: FXRateType = FXRateType.CLOSING,
    ) -> MatchVerdict:
        """Verify whether ``left`` and ``right`` reconcile as a reciprocal pair.

        Checks, in order:

        1. **direction** — the pair must be reciprocal (A→B against B→A);
        2. **reference** — same ``doc_ref`` is required to call it a match; a
           differing reference is reported, never forced;
        3. **residual** — the reporting-currency difference is computed with
           Decimal and compared to the reporting quantum tolerance.

        Never accepts a match on the LLM's say-so: only the arithmetic decides.
        """
        if (
            left.reporting_entity != right.counterparty
            or right.reporting_entity != left.counterparty
        ):
            return MatchVerdict(
                left_row=left.source_row,
                right_row=right.source_row,
                accepted=False,
                match_type=MatchType.DIRECTION_INVALID,
                residual=None,
                reporting_currency=self.reporting_currency,
                reason=(
                    f"rows {left.source_row} and {right.source_row} are not "
                    f"reciprocal ({left.reporting_entity}->{left.counterparty} vs "
                    f"{right.reporting_entity}->{right.counterparty})"
                ),
            )

        if left.doc_ref and right.doc_ref and left.doc_ref != right.doc_ref:
            return MatchVerdict(
                left_row=left.source_row,
                right_row=right.source_row,
                accepted=False,
                match_type=MatchType.REFERENCE_MISMATCH,
                residual=None,
                reporting_currency=self.reporting_currency,
                reason=(
                    f"references differ: {left.doc_ref!r} vs {right.doc_ref!r}"
                ),
            )

        try:
            left_amt = self.to_comparison_amount(left, rate_type=rate_type)
            right_amt = self.to_comparison_amount(right, rate_type=rate_type)
        except Exception as exc:  # typed FX errors flow through
            return MatchVerdict(
                left_row=left.source_row,
                right_row=right.source_row,
                accepted=False,
                match_type=MatchType.RESIDUAL,
                residual=None,
                reporting_currency=self.reporting_currency,
                reason=f"cannot convert to compare: {exc}",
            )

        residual = left_amt - right_amt
        if residual == _ZERO:
            return MatchVerdict(
                left_row=left.source_row,
                right_row=right.source_row,
                accepted=True,
                match_type=MatchType.EXACT,
                residual=residual,
                reporting_currency=self.reporting_currency,
                reason=(
                    f"reciprocal rows reconcile exactly at {left_amt} "
                    f"{self.reporting_currency}"
                ),
            )

        if abs(residual) <= self.tolerance:
            return MatchVerdict(
                left_row=left.source_row,
                right_row=right.source_row,
                accepted=True,
                match_type=MatchType.FX_RESIDUAL,
                residual=residual,
                reporting_currency=self.reporting_currency,
                reason=(
                    f"reciprocal rows reconcile to within the reporting quantum "
                    f"(residual {residual})"
                ),
            )

        return MatchVerdict(
            left_row=left.source_row,
            right_row=right.source_row,
            accepted=False,
            match_type=MatchType.RESIDUAL,
            residual=residual,
            reporting_currency=self.reporting_currency,
            reason=(
                f"reciprocal rows differ by {residual} {self.reporting_currency} "
                f"(beyond the {self.tolerance} tolerance)"
            ),
        )

    def verify_pair(
        self,
        left_row: int,
        right_row: int,
        records: list[IntercompanyRecord] | None = None,
        *,
        rate_type: FXRateType = FXRateType.CLOSING,
    ) -> MatchVerdict:
        """Verify a proposed pair by 1-based source row numbers.

        Raises ``ServiceError`` when a referenced row does not exist, so a
        proposal citing a fabricated row is rejected explicitly.
        """
        rows = records if records is not None else self.load_records()
        by_row = {r.source_row: r for r in rows}
        if left_row not in by_row or right_row not in by_row:
            raise ServiceError(
                f"proposed match references unknown intercompany row(s) "
                f"{left_row}, {right_row}",
                details={
                    "left_row": left_row,
                    "right_row": right_row,
                    "available_rows": sorted(by_row),
                },
            )
        return self.verify(by_row[left_row], by_row[right_row], rate_type=rate_type)

    # -- full reconciliation ----------------------------------------------

    def reconcile(
        self,
        records: list[IntercompanyRecord] | None = None,
        *,
        rate_type: FXRateType = FXRateType.CLOSING,
    ) -> ReconciliationResult:
        """Reconcile all intercompany rows into matched pairs and unmatched items.

        Rows are paired deterministically by ``doc_ref`` across reciprocal
        entities (A→B matched to B→A with the same reference). Each pair is
        verified by :meth:`verify`; a pair that verifies is retained with its
        closing-rate residual, which is what policy §5 sends to the FX account.

        A row with **no** reciprocal partner is never force-matched. When a
        one-sided row exists while the counterparty has already recorded an
        aggregate balance, it is classified ``in_transit`` (policy §5: "items
        in transit at the year end are recorded by the receiving entity at the
        closing rate"); otherwise it is left ``unmatched`` for human review.

        The distinction is deterministic: a one-sided row is ``in_transit``
        only when its reference does **not** appear on the counterparty side
        *and* the counterparty has at least one recorded row for this
        relationship — i.e. the relationship exists in both books but this
        particular document is missing on one side. Purely unmatched rows
        (no counterparty activity at all) stay ``unmatched``.
        """
        rows = records if records is not None else self.load_records()
        by_row = {r.source_row: r for r in rows}

        # Group the rows of one direction by reference: {(from, to): {ref: row}}.
        by_direction_ref: dict[tuple[str, str], dict[str, list[IntercompanyRecord]]] = {}
        for record in rows:
            if not record.doc_ref:
                continue
            key = (record.reporting_entity, record.counterparty)
            by_direction_ref.setdefault(key, {}).setdefault(record.doc_ref, []).append(record)

        # Entities that appear on at least one side of a relationship.
        relationships: set[tuple[str, str]] = set()
        for entity, counterparty in by_direction_ref:
            relationships.add((entity, counterparty))
            relationships.add((counterparty, entity))

        used: set[int] = set()
        pairs: list[ReconciledPair] = []

        for (entity, counterparty), refs in sorted(by_direction_ref.items()):
            if (entity, counterparty) not in relationships:
                continue
            reverse = (counterparty, entity)
            for ref, left_rows in sorted(refs.items()):
                for left in left_rows:
                    if left.source_row in used:
                        continue
                    candidates = [
                        r
                        for r in by_direction_ref.get(reverse, {}).get(ref, [])
                        if r.source_row not in used
                    ]
                    if not candidates:
                        continue
                    right = candidates[0]
                    verdict = self.verify(left, right, rate_type=rate_type)
                    used.add(left.source_row)
                    used.add(right.source_row)
                    left_gbp = self._to_gbp(left, rate_type) if verdict.residual is not None else None
                    right_gbp = self._to_gbp(right, rate_type) if verdict.residual is not None else None
                    pairs.append(
                        ReconciledPair(
                            doc_ref=ref,
                            doc_currency=left.doc_currency,
                            from_entity=left.reporting_entity,
                            to_entity=left.counterparty,
                            left_row=left.source_row,
                            right_row=right.source_row,
                            left_gbp=left_gbp if left_gbp is not None else _ZERO,
                            right_gbp=right_gbp if right_gbp is not None else _ZERO,
                            residual_gbp=verdict.residual if verdict.residual is not None else _ZERO,
                            match_type=verdict.match_type,
                            reason=verdict.reason,
                        )
                    )

        # Unmatched rows: classify in-transit vs genuinely unmatched.
        unmatched: list[UnmatchedItem] = []
        for record in rows:
            if record.source_row in used or not record.doc_ref:
                continue
            reverse = (record.counterparty, record.reporting_entity)
            counterparty_active = reverse in by_direction_ref
            reverse_refs = by_direction_ref.get(reverse, {})
            same_ref_present = record.doc_ref in reverse_refs
            if counterparty_active and not same_ref_present:
                classification = "in_transit"
                reason = (
                    f"{record.reporting_entity} {record.doc_ref} has no "
                    f"counterpart row; the counterparty relationship exists but "
                    f"this document is missing — treated as an item in transit "
                    f"(policy §5)."
                )
            else:
                classification = "unmatched"
                reason = (
                    f"{record.reporting_entity} {record.doc_ref} has no "
                    f"reciprocal counterparty row"
                )
            gbp = self._to_gbp(record, rate_type)
            unmatched.append(
                UnmatchedItem(
                    source_row=record.source_row,
                    reporting_entity=record.reporting_entity,
                    counterparty=record.counterparty,
                    doc_ref=record.doc_ref,
                    doc_currency=record.doc_currency,
                    doc_amount=record.doc_amount,
                    booked_currency=record.booked_currency,
                    booked_amount=record.booked_amount,
                    gbp_amount=gbp,
                    classification=classification,
                    reason=reason,
                )
            )

        residual = sum((p.residual_gbp for p in pairs), _ZERO)
        return ReconciliationResult(
            reporting_currency=self.reporting_currency,
            pairs=tuple(pairs),
            unmatched=tuple(unmatched),
            residual_gbp=residual,
        )

    def _to_gbp(self, record: IntercompanyRecord, rate_type: FXRateType) -> Decimal | None:
        """GBP comparison amount for a record, or ``None`` if no rate exists."""
        try:
            return self.to_comparison_amount(record, rate_type=rate_type)
        except Exception:  # noqa: BLE001 - missing rate -> no fabricated figure
            return None
