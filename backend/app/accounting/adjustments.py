"""Deterministic validation and posting eligibility of manual journals.

Implements ``group_policies.md`` §6: "Manual journals are posted only if they
are valid for FY2024. Invalid journals are not posted."

A journal is **valid** when:

1. it balances — total debits equal total credits in the entity's functional
   currency (checked with Decimal, or any explicit ``debit``/``credit``;
   any imbalance is a hard rejection);
2. it is dated within the reporting period (FY2024); a journal dated outside
   the period is excluded, not posted;
3. it is not duplicated — two journal entries posting the same accounts and
   the same amount in the same direction are flagged as a probable duplicate
   for **human review** (never auto-posted and never silently dropped).

Everything here is bookkeeping around the source ``adjustments.json``. No
amount is recomputed or invented: the journal's own lines are used verbatim.
The suspicious free-text ``memo`` fields sometimes present in the source are
treated as *data*, never as instructions — they cannot alter any check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum

from app.core.errors import ServiceError
from app.services.source_data import SourceDataService

_ZERO = Decimal("0")


class JournalStatus(str, Enum):
    """The posting decision for one journal."""

    POSTED = "posted"                 # valid for FY2024 -> applied
    REJECTED = "rejected"             # unbalanced / malformed -> not applied
    EXCLUDED = "excluded"             # out of period -> not applied
    FLAGGED = "flagged"               # valid but needs human review (duplicate)


@dataclass(frozen=True)
class JournalLine:
    """One line of a manual journal, in the entity's own account codes."""

    account: str
    debit: Decimal
    credit: Decimal
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Journal:
    """One normalised manual journal with source traceability."""

    journal_id: str
    entity: str
    date: str
    description: str
    prepared_by: str
    lines: tuple[JournalLine, ...]
    source_index: int  # 0-based position in adjustments.json
    memo_present: bool = False  # a suspicious free-text memo was recorded

    @property
    def debit_total(self) -> Decimal:
        return sum((l.debit for l in self.lines), _ZERO)

    @property
    def credit_total(self) -> Decimal:
        return sum((l.credit for l in self.lines), _ZERO)

    @property
    def difference(self) -> Decimal:
        return self.debit_total - self.credit_total


@dataclass(frozen=True)
class JournalDecision:
    """The deterministic decision for one journal, with the reason."""

    journal_id: str
    entity: str
    status: JournalStatus
    reason: str
    debit_total: Decimal
    credit_total: Decimal
    difference: Decimal
    source_index: int
    duplicate_of: str | None = None


class JournalValidator:
    """Validate and classify manual journals against the FY2024 policy."""

    def __init__(
        self,
        service: SourceDataService | None = None,
        *,
        reporting_year: str = "2024",
    ) -> None:
        self.service = service if service is not None else SourceDataService()
        self.reporting_year = str(reporting_year)

    # -- loading -----------------------------------------------------------

    def load_journals(self) -> list[Journal]:
        """Normalise every journal in ``adjustments.json`` (order preserved)."""
        payload = self.service.get_adjustments()
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        journals: list[Journal] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            lines: list[JournalLine] = []
            memo_present = False
            for raw_line in entry.get("lines") or []:
                if not isinstance(raw_line, dict):
                    continue
                # A memo is captured as data; it never influences a decision.
                if (raw_line.get("memo") or "").strip():
                    memo_present = True
                lines.append(
                    JournalLine(
                        account=str(raw_line.get("account") or "").strip(),
                        debit=self._dec(raw_line.get("debit")),
                        credit=self._dec(raw_line.get("credit")),
                        raw={k: str(v) for k, v in raw_line.items() if v is not None},
                    )
                )
            journals.append(
                Journal(
                    journal_id=str(entry.get("id") or f"JE-{index + 1:03d}").strip(),
                    entity=str(entry.get("entity") or "").strip().upper(),
                    date=str(entry.get("date") or "").strip(),
                    description=str(entry.get("description") or "").strip(),
                    prepared_by=str(entry.get("prepared_by") or "").strip(),
                    lines=tuple(lines),
                    source_index=index,
                    memo_present=memo_present,
                )
            )
        return journals

    @staticmethod
    def _dec(value: object) -> Decimal:
        if value is None:
            return _ZERO
        text = str(value).strip()
        if not text:
            return _ZERO
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise ServiceError(
                f"invalid journal amount {value!r}", details={"value": str(value)}
            ) from exc

    # -- validation --------------------------------------------------------

    def _in_period(self, date: str) -> bool:
        """True when ``date`` falls within the reporting year (FY2024)."""
        # ISO dates only; anything unparseable is treated as out of period so
        # it is never posted on an assumption.
        return date.startswith(f"{self.reporting_year}-")

    @staticmethod
    def _signature(journal: Journal) -> tuple:
        """A direction-sensitive signature used to detect duplicate journals."""
        return tuple(
            sorted(
                (l.account, l.debit, l.credit)
                for l in journal.lines
            )
        )

    def validate(self) -> list[JournalDecision]:
        """Classify every journal as posted / rejected / excluded / flagged.

        Order of precedence: an out-of-period journal is excluded; an
        unbalanced journal is rejected; a balanced, in-period journal that
        duplicates another is flagged for review; otherwise it is posted.
        """
        journals = self.load_journals()
        decisions: list[JournalDecision] = []

        # Index by signature to detect duplicates across the whole batch.
        by_signature: dict[tuple, list[Journal]] = {}
        for journal in journals:
            by_signature.setdefault(self._signature(journal), []).append(journal)

        seen_signatures: set[tuple] = set()
        for journal in journals:
            signature = self._signature(journal)
            difference = journal.difference

            if not self._in_period(journal.date):
                decisions.append(
                    self._decision(
                        journal,
                        JournalStatus.EXCLUDED,
                        f"dated {journal.date!r}, outside FY{self.reporting_year}; "
                        f"not posted (policy §6)",
                    )
                )
                continue

            if difference != _ZERO:
                decisions.append(
                    self._decision(
                        journal,
                        JournalStatus.REJECTED,
                        f"unbalanced: debits {journal.debit_total} minus credits "
                        f"{journal.credit_total} = {difference}; not posted "
                        f"(policy §6)",
                    )
                )
                continue

            group = by_signature.get(signature, [])
            if len(group) > 1:
                duplicate_of = next(
                    (j.journal_id for j in group if j.journal_id != journal.journal_id),
                    None,
                )
                decisions.append(
                    self._decision(
                        journal,
                        JournalStatus.FLAGGED,
                        f"appears to duplicate {duplicate_of} (identical accounts "
                        f"and amounts); held for human review, not auto-posted",
                        duplicate_of=duplicate_of,
                    )
                )
                seen_signatures.add(signature)
                continue

            decisions.append(
                self._decision(
                    journal,
                    JournalStatus.POSTED,
                    "valid for FY2024 and balanced",
                )
            )

        return decisions

    @staticmethod
    def _decision(
        journal: Journal,
        status: JournalStatus,
        reason: str,
        *,
        duplicate_of: str | None = None,
    ) -> JournalDecision:
        return JournalDecision(
            journal_id=journal.journal_id,
            entity=journal.entity,
            status=status,
            reason=reason,
            debit_total=journal.debit_total,
            credit_total=journal.credit_total,
            difference=journal.difference,
            source_index=journal.source_index,
            duplicate_of=duplicate_of,
        )

    def posted_journals(self) -> list[Journal]:
        """The journals that are eligible to be posted (valid & in period).

        Duplicates are excluded from this set: they are held for review and
        must not be silently applied.
        """
        posted_ids = {
            d.journal_id
            for d in self.validate()
            if d.status is JournalStatus.POSTED
        }
        return [j for j in self.load_journals() if j.journal_id in posted_ids]
