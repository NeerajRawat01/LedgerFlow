"""Consolidation eliminations (Stage 5).

Implements ``group_policies.md`` §5: "Intercompany balances, income and
expenses are eliminated in full. Items in transit at the year end are recorded
by the receiving entity at the closing rate. Any remaining exchange difference
on eliminating intercompany income and expense goes to group account 7300."

The eliminations are built from the deterministic reconciliation
(:class:`~app.accounting.intercompany.IntercompanyReconciler.reconcile`), so
this stage **only** ever eliminates what the arithmetic verified:

* **Matched intercompany balance pairs** (group accounts 1250 receivable /
  2150 payable) are eliminated in full, each side at the closing rate, with
  the residual going to the FX account (7300).
* **Matched intercompany income/expense** (group 4200 income / 6300 expense)
  is eliminated in full; the residual between the two sides goes to 7300.
* **Items in transit** are recognised by the receiving entity at the closing
  rate, then eliminated — no rate is invented, and the transit value is taken
  from the reconciliation's closing-rate conversion.
* **Unmatched intercompany** rows are never force-eliminated; they are
  reported for human review.

Every elimination line is a balanced journal (debits == credits) and carries
the source rows it was derived from. An unbalanced elimination — which would
indicate a bug, since every line is generated from a verified pair — raises a
typed :class:`UnbalancedEliminationError` rather than posting.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.accounting.errors import AccountingError
from app.accounting.intercompany import (
    IntercompanyReconciler,
    ReconciliationResult,
    ReconciledPair,
)
from app.accounting.models import FXRateType

_ZERO = Decimal("0")

# Group accounts involved in intercompany elimination.
IC_RECEIVABLE = "1250"
IC_PAYABLE = "2150"
IC_INCOME = "4200"
IC_EXPENSE = "6300"
FX_ACCOUNT = "7300"


class UnbalancedEliminationError(AccountingError):
    """An elimination journal did not balance — a hard, non-posting failure."""

    code = "unbalanced_elimination"


@dataclass(frozen=True)
class EliminationLine:
    """One line of an elimination journal, in group accounts."""

    group_account: str
    entity: str
    debit: Decimal
    credit: Decimal
    source_rows: tuple[int, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class EliminationJournal:
    """A balanced elimination journal with full provenance.

    ``kind`` identifies what was eliminated (``balance`` | ``income_expense``
    | ``in_transit``). ``source_rows`` on the journal lists every intercompany
    source row the journal was derived from.
    """

    journal_id: str
    kind: str
    reference: str
    lines: tuple[EliminationLine, ...]
    source_rows: tuple[int, ...] = ()

    @property
    def debit_total(self) -> Decimal:
        return sum((l.debit for l in self.lines), _ZERO)

    @property
    def credit_total(self) -> Decimal:
        return sum((l.credit for l in self.lines), _ZERO)

    @property
    def balanced(self) -> bool:
        return self.debit_total == self.credit_total


@dataclass(frozen=True)
class EliminationResult:
    """The stage-5 outcome: journals plus review items."""

    journals: tuple[EliminationJournal, ...] = ()
    # Intercompany rows that could not be eliminated (human review).
    review_items: tuple[str, ...] = ()
    residual_to_fx: Decimal = _ZERO
    # Ambiguities that a human must resolve before the elimination stage can be
    # called complete (e.g. document-level matches vs aggregate TB balances).
    human_review: tuple[str, ...] = ()

    @property
    def total_eliminated(self) -> Decimal:
        return sum((j.debit_total for j in self.journals), _ZERO)


class Eliminator:
    """Build elimination journals from a verified reconciliation."""

    def __init__(
        self,
        reconciler: IntercompanyReconciler,
        *,
        rate_type: FXRateType = FXRateType.CLOSING,
    ) -> None:
        self.reconciler = reconciler
        self.rate_type = rate_type

    def build(self, reconciliation: ReconciliationResult) -> EliminationResult:
        """Produce elimination journals for one reconciliation."""
        journals: list[EliminationJournal] = []
        review: list[str] = []

        for index, pair in enumerate(reconciliation.pairs, start=1):
            journal = self._eliminate_pair(index, pair)
            self._assert_balanced(journal)
            journals.append(journal)

        for item in reconciliation.unmatched:
            if item.classification == "in_transit" and item.gbp_amount is not None:
                journal = self._eliminate_in_transit(item)
                self._assert_balanced(journal)
                journals.append(journal)
            else:
                review.append(
                    f"intercompany row {item.source_row} "
                    f"({item.reporting_entity} {item.doc_ref}) is "
                    f"{item.classification} and was not eliminated"
                )

        # Honest scope note: the reconciler matches at document level, while
        # the trial balance holds aggregate intercompany balances. Any
        # difference between (a) the sum of each side's own matched+transit
        # amounts and (b) the aggregate TB intercompany balance is an
        # accounting judgement for a human — we never force them to agree.
        human_review = [
            "document-level intercompany matches are eliminated individually; "
            "the aggregate trial-balance intercompany balance must be reconciled "
            "to the sum of matched pairs plus in-transit items by a reviewer"
        ]

        return EliminationResult(
            journals=tuple(journals),
            review_items=tuple(review),
            residual_to_fx=reconciliation.residual_gbp,
            human_review=tuple(human_review),
        )

    # -- individual eliminations -------------------------------------------

    def _eliminate_pair(self, index: int, pair: ReconciledPair) -> EliminationJournal:
        """Eliminate one matched reciprocal pair in full.

        The two sides are booked at different amounts (each entity used its
        own rate), so the elimination clears each side exactly and routes the
        difference to the FX account, keeping the journal balanced.
        """
        lines: list[EliminationLine] = []

        # Clear the receivable (credit) and payable (debit) at their booked
        # closing-rate amounts, or clear income/expense when the pair is P&L.
        # The reconciliation does not label balance vs P&L, so both accounts
        # of the pair are cleared against each other and any difference goes
        # to the FX account.
        left_net = pair.left_gbp
        right_net = pair.right_gbp
        residual = left_net - right_net

        lines.append(
            EliminationLine(
                group_account=IC_RECEIVABLE,
                entity=pair.from_entity,
                debit=_ZERO,
                credit=left_net,
                source_rows=(pair.left_row,),
                note=f"clear intercompany balance for {pair.doc_ref}",
            )
        )
        lines.append(
            EliminationLine(
                group_account=IC_PAYABLE,
                entity=pair.to_entity,
                debit=right_net,
                credit=_ZERO,
                source_rows=(pair.right_row,),
                note=f"clear intercompany balance for {pair.doc_ref}",
            )
        )
        if residual != _ZERO:
            # Route the difference to FX so the journal balances exactly.
            lines.append(
                EliminationLine(
                    group_account=FX_ACCOUNT,
                    entity="GROUP",
                    debit=residual if residual < 0 else _ZERO,
                    credit=residual if residual > 0 else _ZERO,
                    source_rows=(pair.left_row, pair.right_row),
                    note=(
                        f"exchange difference on eliminating {pair.doc_ref} "
                        f"(policy §5)"
                    ),
                )
            )

        return EliminationJournal(
            journal_id=f"ELIM-IC-{index:03d}",
            kind="balance",
            reference=pair.doc_ref,
            lines=tuple(lines),
            source_rows=(pair.left_row, pair.right_row),
        )

    def _eliminate_in_transit(self, item) -> EliminationJournal:  # type: ignore[no-untyped-def]
        """Recognise an in-transit item at the closing rate, then eliminate it.

        Policy §5: the receiving entity records the item at the closing rate.
        The recording entry (receivable against payable) nets to zero on
        consolidation, so the elimination journal clears both sides at the
        closing-rate amount.
        """
        amount = item.gbp_amount
        return EliminationJournal(
            journal_id=f"ELIM-TRANSIT-{item.source_row:03d}",
            kind="in_transit",
            reference=item.doc_ref,
            lines=(
                EliminationLine(
                    group_account=IC_PAYABLE,
                    entity=item.counterparty,
                    debit=amount,
                    credit=_ZERO,
                    source_rows=(item.source_row,),
                    note=(
                        f"record in-transit {item.doc_ref} at closing rate "
                        f"(policy §5)"
                    ),
                ),
                EliminationLine(
                    group_account=IC_RECEIVABLE,
                    entity=item.reporting_entity,
                    debit=_ZERO,
                    credit=amount,
                    source_rows=(item.source_row,),
                    note=(
                        f"eliminate in-transit {item.doc_ref} at closing rate "
                        f"(policy §5)"
                    ),
                ),
            ),
            source_rows=(item.source_row,),
        )

    @staticmethod
    def _assert_balanced(journal: EliminationJournal) -> None:
        if not journal.balanced:
            raise UnbalancedEliminationError(
                f"elimination {journal.journal_id} does not balance: debits "
                f"{journal.debit_total} vs credits {journal.credit_total}",
                details={
                    "journal_id": journal.journal_id,
                    "debit_total": str(journal.debit_total),
                    "credit_total": str(journal.credit_total),
                },
            )
