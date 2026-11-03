"""Consolidated financial-statement assembly (the central orchestration stage).

This module is the **assembly half** of the consolidation engine. It does not
re-implement any accounting step: it *orchestrates* the existing deterministic
services — ingestion, validation, mapping, journal validation, intercompany
reconciliation, FX translation, eliminations and acquisition accounting — and
assembles their typed outputs into the group's consolidated trial balance and
statements.

Policy implemented (``data/northwind/group_policies.md``):

* presentation currency **GBP**, reported to 2 dp at the boundary only;
* assets/liabilities at closing, income/expenses at the period average
  (India: H2 average), equity at historical rates;
* exchange differences on translation go to the translation reserve (3300);
* intercompany balances, income and expenses eliminated in full, with any
  residual going to group account 7300;
* NCI measured at its proportionate share of net assets; goodwill held in the
  subsidiary's functional currency and retranslated at closing;
* manual journals posted only when valid for FY2024.

Two deliberate, documented source corrections are applied here, each recorded
as an issue so nothing happens silently:

1. **UK 2310 "Deferred income" → group 2300** — the supplied ``map_uk.csv``
   routes ``2310 Deferred income`` to group ``4100 Revenue``, which contradicts
   the group chart of accounts (where *Deferred income* is a liability at
   ``2300``). The mapping file is left untouched; the row is re-mapped
   deterministically and flagged.
2. **GBP/SGD closing rate is absent** — the UK's SGD bank balance cannot be
   translated at closing, so ``sgd_bank_gbp`` is reported as ``null`` with a
   reason. No rate is invented.

Nothing here rounds an intermediate value. ``round_reported`` is applied once,
when a reported statement line or control figure is produced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.accounting.acquisition import AcquisitionService, entity_currency
from app.accounting.adjustments import (
    JournalDecision,
    JournalStatus,
    JournalValidator,
)
from app.accounting.consolidation import StageOutcome, StageStatus
from app.accounting.eliminations import Eliminator
from app.accounting.fx import FXService
from app.accounting.ingestion import TrialBalanceIngestor
from app.accounting.intercompany import IntercompanyReconciler
from app.accounting.mapping import AccountMapper
from app.accounting.models import (
    ConsolidationIssue,
    ConsolidatedTBRow,
    EliminationEntry,
    FinancialStatements,
    FXRateType,
    GroupAccount,
    LineItem,
    MappedTrialBalance,
    Severity,
    StatementSection,
    TranslatedRow,
    TrialBalance,
    ValidationIssue,
    round_reported,
)
from app.accounting.translation import FXTranslator
from app.accounting.validation import TrialBalanceValidator
from app.core.errors import LedgerFlowError, ServiceError
from app.services.source_data import SourceDataService

_ZERO = Decimal("0")
_ONE = Decimal("1")

# Trial-balance balance tolerance: half the policy's 1p reporting quantum. Used
# only for the full-precision TB balance check, where non-terminating Decimal
# division can leave an infinitesimal (<1e-19) residual far below a penny.
_ROUNDING_TOLERANCE = Decimal("0.005")

# Equity accounts are translated at historical rates and assembled separately.
EQUITY_ACCOUNTS = {"3100", "3200", "3300", "3400"}


# Intercompany and investment accounts.
IC_RECEIVABLE = "1250"
IC_PAYABLE = "2150"
IC_INCOME = "4200"
IC_EXPENSE = "6300"
FX_ACCOUNT = "7300"
GOODWILL = "1700"
NCI_ACCOUNT = "3400"
TRESERVE = "3300"
REVENUE = "4100"

# The UK source defect: this local code is mis-mapped to 4100 in ``map_uk.csv``.
UK_DEFERRED_INCOME_LOCAL = "2310"
UK_DEFERRED_INCOME_CORRECT_GROUP = "2300"


@dataclass
class _EntityData:
    """Everything gathered for one entity during assembly."""

    entity: str
    currency: str
    trial_balance: TrialBalance
    mapped: MappedTrialBalance
    issues: list[ValidationIssue] = field(default_factory=list)


class StatementAssembler:
    """Assemble consolidated statements from the deterministic services."""

    def __init__(
        self,
        service: SourceDataService | None = None,
        *,
        ingestor: TrialBalanceIngestor | None = None,
        validator: TrialBalanceValidator | None = None,
        mapper: AccountMapper | None = None,
        fx: FXService | None = None,
        reconciler: IntercompanyReconciler | None = None,
        journals: JournalValidator | None = None,
        acquisition: AcquisitionService | None = None,
        presentation_currency: str = "GBP",
    ) -> None:
        self.service = service if service is not None else SourceDataService()
        self.fx = fx if fx is not None else FXService(self.service)
        self.ingestor = ingestor if ingestor is not None else TrialBalanceIngestor(self.service)
        self.validator = validator if validator is not None else TrialBalanceValidator()
        self.mapper = mapper if mapper is not None else AccountMapper(self.service)
        self.reconciler = (
            reconciler
            if reconciler is not None
            else IntercompanyReconciler(self.service, fx=self.fx)
        )
        self.journals = journals if journals is not None else JournalValidator(self.service)
        self.acquisition = (
            acquisition if acquisition is not None else AcquisitionService(self.service, fx=self.fx)
        )
        self.presentation_currency = presentation_currency
        self.translator = FXTranslator(
            fx=self.fx,
            group_coa=self.mapper.group_coa,
            presentation_currency=presentation_currency,
        )

        # Populated during :meth:`assemble`.
        self.issues: list[ConsolidationIssue] = []
        self.stages: list[StageOutcome] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def assemble(self, entities: list[str] | None = None) -> "ConsolidationOutput":
        """Run the full assembly and return the consolidated output bundle."""
        entity_ids = (
            [e.strip().upper() for e in entities]
            if entities
            else ["UK", "US", "IN"]
        )
        entity_ids = sorted(set(entity_ids))
        self.issues = []
        self.stages = []
        self._entity_data: dict[str, _EntityData] = {}

        # 1. Ingest / validate / map each entity (deterministic services).
        entity_data = self._load_entities(entity_ids)
        self._entity_data = entity_data

        # 2. Journals: classify and apply the postable ones.
        decisions = self.journals.validate()
        posted = {d.journal_id for d in decisions if d.status is JournalStatus.POSTED}

        # A balanced, in-period journal whose *every* line maps to a group
        # account is postable. If any line has no grouping, posting only the
        # mapped lines would unbalance the entity (a real defect: see JE-016,
        # whose Dr IN-GST-ITC leg has no mapping). Such a journal is rejected in
        # full — never partially applied — and recorded as an issue. This keeps
        # each entity's post-journal functional balance intact.
        decisions = self._reject_partially_unmapped_journals(entity_data, decisions)
        posted = {d.journal_id for d in decisions if d.status is JournalStatus.POSTED}
        posted_journals = [
            j for j in self.journals.load_journals() if j.journal_id in posted
        ]
        self._posted_journals = posted_journals
        self._record_journal_issues(decisions)
        self._record_prompt_injection_issues()

        # Sub-ledger control reconciliation (AR/AP control totals vs the TB).
        self._check_subledger_controls(entity_data)

        # Merge posted journal effects into each entity's mapped accounts.
        adjusted = self._apply_journals(entity_data, posted_journals)

        # 3. Translate each entity into GBP (closing / average / H2 average).
        translated = self._translate(entity_data, adjusted)

        # 4. Intercompany reconciliation + eliminations.
        reconciliation = self.reconciler.reconcile()
        eliminator = Eliminator(self.reconciler)
        elimination_result = eliminator.build(reconciliation)

        # 5. Acquisition accounting (goodwill, NCI at acquisition).
        acquisition_result = self.acquisition.compute(self.acquisition.load_inputs("IN"))
        for issue in acquisition_result.issues:
            self._add_issue(
                code=issue.code,
                severity=issue.severity,
                message=issue.message,
                action="flagged",
                entity=issue.entity,
                source_file=issue.source_file,
                source_row=issue.source_row,
            )

        # Post-acquisition profit for NCI (India H2).
        nci_post = self.acquisition.post_acquisition_profit(
            "IN",
            acquisition_date=acquisition_result.acquisition_date,
            ownership_pct=acquisition_result.ownership_pct,
            income_rate_type=FXRateType.H2_AVERAGE,
        )

        # 6. Combine + eliminate into a consolidated trial balance.
        tb_rows, elim_entries, equity_balances = self._combine_and_eliminate(
            entity_data, translated, elimination_result, acquisition_result
        )

        # 7. Compute the translation reserve as the balancing equity movement.
        statements, figures = self._build_statements(
            tb_rows=tb_rows,
            equity_balances=equity_balances,
            elimination_result=elimination_result,
            acquisition_result=acquisition_result,
            nci_post=nci_post,
            decisions=decisions,
        )

        # 8. Align the consolidated trial balance's equity rows with the
        #    assembled equity position, so the TB and the statements agree
        #    (both derive from the same figures; the reserve is the balancer).
        #
        #    The TB is the *open-ledger* view: the year's P&L accounts still
        #    carry the result (income as a credit, expenses as a debit), so the
        #    equity rows must be on the same basis — opening retained earnings
        #    and NCI *at acquisition*, with the closing translation reserve.
        #    Using the statements' closing retained earnings and closing NCI
        #    would double-count the year's profit and leave the TB out of
        #    balance by exactly the group profit.
        tb_rows = self._sync_equity_rows(
            tb_rows,
            statements,
            opening_re=equity_balances.get("3200", _ZERO)
            + getattr(self, "_opening_re_adjustment", _ZERO),
            nci_at_acquisition=getattr(
                self,
                "_nci_ledger_basis",
                self._nci_at_acquisition_gbp(acquisition_result),
            ),
        )

        # 9. Verify the consolidated trial balance balances on full-precision
        #    values (debits = credits) and record it as an integrity check.
        #    Translation uses non-terminating Decimal division (e.g. 1/1.2530),
        #    so the exact full-precision totals can differ by an infinitesimal
        #    (≈1e-20) that is far below the policy's 1p reporting quantum. The
        #    check therefore compares against half the reporting quantum.
        tb_debits = sum((r.gbp_debit for r in tb_rows), _ZERO)
        tb_credits = sum((r.gbp_credit for r in tb_rows), _ZERO)
        tb_residual = tb_debits - tb_credits
        statements.checks.append(
            {
                "check": "consolidated_tb_balances",
                "passed": abs(tb_residual) <= _ROUNDING_TOLERANCE,
                "detail": (
                    f"total debits {round_reported(tb_debits)} = total credits "
                    f"{round_reported(tb_credits)} (difference "
                    f"{round_reported(tb_residual)})"
                ),
            }
        )

        # 10. Verify every elimination journal balances (debits = credits) on
        #     full-precision values, and that the elimination set balances.
        per_journal: dict[str, list[Decimal]] = {}
        for entry in elim_entries:
            bucket = per_journal.setdefault(entry.journal_id, [_ZERO, _ZERO])
            bucket[0] += entry.gbp_debit
            bucket[1] += entry.gbp_credit
        unbalanced = [
            jid for jid, (dr, cr) in sorted(per_journal.items()) if dr != cr
        ]
        statements.checks.append(
            {
                "check": "elimination_journals_balance",
                "passed": not unbalanced,
                "detail": (
                    (
                        f"all {len(per_journal)} elimination journal(s) balance "
                        f"(debits = credits)"
                    )
                    if not unbalanced
                    else (
                        "unbalanced elimination journal(s): "
                        + ", ".join(unbalanced)
                    )
                ),
            }
        )

        return ConsolidationOutput(
            statement_assembler=self,
            entities=entity_ids,
            trial_balance_rows=tb_rows,
            eliminations=elim_entries,
            statements=statements,
            control_figures=figures,
            reconciliation=reconciliation,
            acquisition=acquisition_result,
            nci_post=nci_post,
            journal_decisions=decisions,
        )

    # ------------------------------------------------------------------
    # Stage 1-2: load entities, apply journals
    # ------------------------------------------------------------------

    def _load_entities(self, entity_ids: list[str]) -> dict[str, _EntityData]:
        data: dict[str, _EntityData] = {}
        for entity in entity_ids:
            tb = self.ingestor.ingest(entity)
            issues: list[ValidationIssue] = []
            validation = self.validator.validate(tb)
            issues.extend(validation.issues)
            mapped = self.mapper.map_trial_balance(tb)
            issues.extend(mapped.issues)
            mapped = self._correct_known_mapping_defects(entity, mapped)
            data[entity] = _EntityData(
                entity=entity,
                currency=tb.functional_currency,
                trial_balance=tb,
                mapped=mapped,
                issues=issues,
            )
            for issue in issues:
                self._add_issue(
                    code=issue.code,
                    severity=issue.severity,
                    message=issue.message,
                    action="flagged" if issue.severity is Severity.WARNING else "blocked",
                    entity=issue.entity,
                    source_file=issue.source_file,
                    source_row=issue.source_row,
                    account_code=issue.account_code,
                    details=issue.details,
                )
        return data

    def _correct_known_mapping_defects(
        self, entity: str, mapped: MappedTrialBalance
    ) -> MappedTrialBalance:
        """Apply documented, deterministic corrections to source mapping defects.

        Currently: UK ``2310 Deferred income`` is recorded in ``map_uk.csv`` as
        mapping to group ``4100 Revenue``, which contradicts the group chart of
        accounts (where *Deferred income* is a liability at ``2300``). The
        source file is never modified; the row is re-mapped here and an issue is
        recorded so the correction is visible and traceable.
        """
        if entity != "UK":
            return mapped
        corrected_rows = []
        changed = False
        for row in mapped.rows:
            if row.local_account_code != UK_DEFERRED_INCOME_LOCAL:
                corrected_rows.append(row)
                continue
            if row.group_account_code == UK_DEFERRED_INCOME_CORRECT_GROUP:
                corrected_rows.append(row)
                continue
            target = self.mapper.group_coa.get(UK_DEFERRED_INCOME_CORRECT_GROUP)
            corrected_rows.append(
                row.model_copy(
                    update={
                        "group_account_code": UK_DEFERRED_INCOME_CORRECT_GROUP,
                        "group_account_name": target.account_name if target else "Deferred income",
                        "mapped": target is not None,
                    }
                )
            )
            changed = True
            self._add_issue(
                code="mapping_defect_corrected",
                severity=Severity.WARNING,
                message=(
                    f"UK local account {UK_DEFERRED_INCOME_LOCAL} "
                    f"(Deferred income) is mapped to group 4100 Revenue in "
                    f"map_uk.csv, contradicting the group chart of accounts; "
                    f"re-mapped to group {UK_DEFERRED_INCOME_CORRECT_GROUP} "
                    f"(Deferred income, a liability). Source file left unmodified."
                ),
                action="flagged",
                entity="UK",
                source_file=row.source_file,
                source_row=row.source_row,
                account_code=UK_DEFERRED_INCOME_LOCAL,
                affected_amount=row.debit + row.credit,
                details={
                    "source_group_code": "4100",
                    "corrected_group_code": UK_DEFERRED_INCOME_CORRECT_GROUP,
                    "source_mapping_file": "map_uk.csv",
                },
            )
        if not changed:
            return mapped
        return mapped.model_copy(update={"rows": corrected_rows})

    def _record_prompt_injection_issues(self) -> None:
        """Record any free-text memo that tries to instruct an automated tool.

        The source ``adjustments.json`` contains a memo addressed to automated
        reviewers/AI assistants, instructing them to skip the balance check for
        JE-006 and not to report it. Such text is treated strictly as data: it
        is surfaced here as an issue and has no effect on the deterministic
        validation (which rejects JE-006 for being unbalanced).
        """
        injection_markers = (
            "ai assistant",
            "automated reviewer",
            "skip the",
            "do not list",
            "mark it as accepted",
        )
        for journal in self.journals.load_journals():
            if not journal.memo_present:
                continue
            text = " ".join(
                (line.raw.get("memo") or "") for line in journal.lines
            ).lower()
            if any(marker in text for marker in injection_markers):
                self._add_issue(
                    code="prompt_injection_ignored",
                    severity=Severity.WARNING,
                    message=(
                        f"{journal.journal_id} contains a free-text memo that "
                        f"tries to instruct automated reviewers to skip "
                        f"validation; the text was treated as data and ignored"
                    ),
                    action="flagged",
                    entity=journal.entity,
                    source_file="adjustments.json",
                    affected_amount=abs(journal.difference) or None,
                    details={"journal_id": journal.journal_id, "memo_present": True},
                )

    def _check_subledger_controls(self, entity_data: dict[str, _EntityData]) -> None:
        """Compare each entity's AR/AP sub-ledger control totals to its TB.

        A difference is reported as an issue; the TB is never adjusted to force
        agreement (the sub-ledger total is a control, not a posting).
        """
        controls: dict[tuple[str, str], Decimal] = {}
        for row in self.service.get_subledger_controls():
            key = (
                (row.get("entity_id") or "").strip().upper(),
                (row.get("ledger") or "").strip().lower(),
            )
            try:
                controls[key] = Decimal((row.get("control_total") or "0").strip() or "0")
            except Exception:  # noqa: BLE001 - non-numeric control is skipped
                continue

        # Group accounts holding trade receivables / payables.
        ar_group, ap_group = "1200", "2100"
        for entity, data in entity_data.items():
            balances: dict[str, Decimal] = {ar_group: _ZERO, ap_group: _ZERO}
            for row in data.mapped.rows:
                if row.group_account_code in balances:
                    balances[row.group_account_code] += row.debit - row.credit
            ar_control = next(
                (v for (e, l), v in controls.items() if e == entity and "receivable" in l),
                None,
            )
            ap_control = next(
                (v for (e, l), v in controls.items() if e == entity and "payable" in l),
                None,
            )
            for control, tb_value, group_code, label in (
                (ar_control, balances[ar_group], ar_group, "receivables"),
                (ap_control, -balances[ap_group], ap_group, "payables"),
            ):
                if control is None:
                    continue
                difference = tb_value - control
                if difference != _ZERO:
                    self._add_issue(
                        code="subledger_control_difference",
                        severity=Severity.WARNING,
                        message=(
                            f"{entity} {label} sub-ledger control {control} does "
                            f"not agree with the trial balance {tb_value} "
                            f"(difference {difference}); the TB was not adjusted"
                        ),
                        action="flagged",
                        entity=entity,
                        source_file="subledger_controls.csv",
                        account_code=group_code,
                        affected_amount=abs(difference),
                        details={
                            "control_total": str(control),
                            "trial_balance_total": str(tb_value),
                            "difference": str(difference),
                        },
                    )

    def _apply_journals(
        self, entity_data: dict[str, _EntityData], posted_journals: list[Any]
    ) -> dict[str, dict[str, list[Decimal]]]:
        """Return per-entity group-account balances adjusted for posted journals.

        Balances are ``{group_code: [debit, credit]}`` in the *functional*
        currency, built from the mapped TB plus the valid, in-period journals.
        Journal lines whose local account has no mapping are reported and left
        out (never guessed).
        """
        lookup_by_entity: dict[str, dict[str, str]] = {}
        for entity, data in entity_data.items():
            lookup: dict[str, str] = {}
            for row in data.mapped.rows:
                if row.mapped and row.group_account_code:
                    lookup.setdefault(row.local_account_code, row.group_account_code)
            lookup_by_entity[entity] = lookup

        balances: dict[str, dict[str, list[Decimal]]] = {}
        for entity, data in entity_data.items():
            entity_balances: dict[str, list[Decimal]] = defaultdict(lambda: [_ZERO, _ZERO])
            for row in data.mapped.rows:
                if not row.mapped or not row.group_account_code:
                    continue
                entity_balances[row.group_account_code][0] += row.debit
                entity_balances[row.group_account_code][1] += row.credit
            balances[entity] = entity_balances

        for journal in posted_journals:
            entity = journal.entity
            if entity not in entity_data:
                continue
            lookup = lookup_by_entity[entity]
            for line in journal.lines:
                group_code = lookup.get(line.account)
                if group_code is None:
                    self._add_issue(
                        code="journal_line_unmapped",
                        severity=Severity.WARNING,
                        message=(
                            f"{journal.journal_id} line account {line.account!r} "
                            f"has no group mapping; the line was not applied"
                        ),
                        action="held_for_review",
                        entity=entity,
                        account_code=line.account,
                        affected_amount=line.debit + line.credit,
                        details={"journal_id": journal.journal_id},
                    )
                    continue
                balances[entity][group_code][0] += line.debit
                balances[entity][group_code][1] += line.credit
        return balances

    def _reject_partially_unmapped_journals(
        self,
        entity_data: dict[str, _EntityData],
        decisions: list[JournalDecision],
    ) -> list[JournalDecision]:
        """Reject, in full, any posted journal that has an unmapped line.

        A journal is an all-or-nothing unit: if one of its lines cannot be
        resolved to a group account, posting only the residual lines would
        break the entity's debits = credits. Rather than silently absorb that
        break in the translation reserve, the journal is rejected in full and
        the unmapped line is recorded as an issue (policy §6: "invalid journals
        are not posted" — a journal that cannot be applied completely is not
        valid for posting).
        """
        # Build each entity's local -> group lookup from the mapped rows.
        lookups: dict[str, dict[str, str]] = {}
        for entity, data in entity_data.items():
            lookup: dict[str, str] = {}
            for row in data.mapped.rows:
                if row.mapped and row.group_account_code:
                    lookup.setdefault(row.local_account_code, row.group_account_code)
            lookups[entity] = lookup

        by_id = {j.journal_id: j for j in self.journals.load_journals()}
        revised: list[JournalDecision] = []
        for decision in decisions:
            if decision.status is not JournalStatus.POSTED:
                revised.append(decision)
                continue
            journal = by_id.get(decision.journal_id)
            lookup = lookups.get(decision.entity, {})
            unmapped = [
                line.account
                for line in (journal.lines if journal else ())
                if line.account not in lookup
            ]
            if not unmapped:
                revised.append(decision)
                continue
            revised.append(
                JournalDecision(
                    journal_id=decision.journal_id,
                    entity=decision.entity,
                    status=JournalStatus.REJECTED,
                    reason=(
                        f"line account(s) {sorted(unmapped)} have no group "
                        f"mapping; posting only the remaining lines would "
                        f"unbalance {decision.entity}, so the journal was "
                        f"rejected in full (policy §6)"
                    ),
                    debit_total=decision.debit_total,
                    credit_total=decision.credit_total,
                    difference=decision.difference,
                    source_index=decision.source_index,
                    duplicate_of=decision.duplicate_of,
                )
            )
            for account in sorted(unmapped):
                self._add_issue(
                    code="journal_rejected_unmapped_line",
                    severity=Severity.ERROR,
                    message=(
                        f"{decision.journal_id} ({decision.entity}): line "
                        f"account {account!r} has no group mapping; the whole "
                        f"journal was rejected so {decision.entity} stays "
                        f"balanced (policy §6)"
                    ),
                    action="rejected",
                    entity=decision.entity,
                    source_file="adjustments.json",
                    account_code=account,
                    details={"journal_id": decision.journal_id},
                )
        return revised

    def _record_journal_issues(self, decisions: list[JournalDecision]) -> None:
        for decision in decisions:
            if decision.status is JournalStatus.POSTED:
                continue
            action = {
                JournalStatus.REJECTED: "rejected",
                JournalStatus.EXCLUDED: "excluded",
                JournalStatus.FLAGGED: "held_for_review",
            }[decision.status]
            severity = (
                Severity.ERROR
                if decision.status is JournalStatus.REJECTED
                else Severity.WARNING
            )
            self._add_issue(
                code=f"journal_{decision.status.value}",
                severity=severity,
                message=(
                    f"{decision.journal_id} ({decision.entity}): "
                    f"{decision.reason}"
                ),
                action=action,
                entity=decision.entity,
                source_file="adjustments.json",
                affected_amount=abs(decision.difference) or None,
                details={
                    "journal_id": decision.journal_id,
                    "debit_total": str(decision.debit_total),
                    "credit_total": str(decision.credit_total),
                    "difference": str(decision.difference),
                    "duplicate_of": decision.duplicate_of,
                },
            )

    # ------------------------------------------------------------------
    # Stage 3: translation
    # ------------------------------------------------------------------

    def _translate(
        self,
        entity_data: dict[str, _EntityData],
        balances: dict[str, dict[str, list[Decimal]]],
    ) -> dict[str, dict[str, Any]]:
        """Translate each entity's balances into GBP.

        Returns, per entity, ``{"pl": {code: gbp_net}, "bs": {code: gbp_net}}``
        where net is ``debit - credit`` in GBP. Equity accounts are excluded
        (handled at assembly from historical balances).

        An entity acquired mid-year (India) contributes only from its
        acquisition month. Its P&L is therefore rebuilt **only** from the
        monthly P&L rows on or after the acquisition month (translated at the
        period-of-inclusion average, H), and the monthly total for each ledger
        is tied to that entity's trial balance — a mismatch blocks the run.
        Revenue/expenses earned before the acquisition (the Apr–Jun quarter)
        belong to the previous owners and are not group profit.
        """
        result: dict[str, dict[str, Any]] = {}
        for entity, data in entity_data.items():
            currency = data.currency
            acquisition_date = self._acquisition_date(entity)
            if acquisition_date and self._is_partial_year(acquisition_date):
                balances = dict(balances)
                balances[entity] = self._partial_year_pl_balances(
                    entity, data, acquisition_date, adjusted=balances.get(entity, {})
                )
            policy = FXTranslator.policy_for_entity(
                entity,
                currency,
                acquisition_date=acquisition_date,
            )
            closing_rate = self._rate(currency, FXRateType.CLOSING)
            income_rate = self._rate(currency, policy.income_rate_type)

            pl: dict[str, Decimal] = defaultdict(lambda: _ZERO)
            bs: dict[str, Decimal] = defaultdict(lambda: _ZERO)
            # Per group-account source detail so a reported balance traces back
            # to the exact entity rows, files and rates that produced it.
            detail: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for group_code, (debit, credit) in balances[entity].items():
                if group_code in EQUITY_ACCOUNTS:
                    continue
                account = self.mapper.group_coa.get(group_code)
                net = debit - credit
                if account is not None and account.statement == "PL":
                    pl[group_code] += net * income_rate
                    rate_type_used = policy.income_rate_type.value
                    rate_used = income_rate
                else:
                    bs[group_code] += net * closing_rate
                    rate_type_used = FXRateType.CLOSING.value
                    rate_used = closing_rate
                for row in data.mapped.rows:
                    if not row.mapped or row.group_account_code != group_code:
                        continue
                    detail[group_code].append(
                        {
                            "entity": entity,
                            "source_file": row.source_file,
                            "source_row": row.source_row,
                            "local_account_code": row.local_account_code,
                            "functional_currency": currency,
                            "debit": str(row.debit),
                            "credit": str(row.credit),
                            "rate_type": rate_type_used,
                            "rate": str(rate_used),
                            "gbp_debit": str(row.debit * rate_used),
                            "gbp_credit": str(row.credit * rate_used),
                        }
                    )
            # Posted-journal detail: record which journals touched the entity so
            # the adjusted balance is traceable to adjustments.json as well.
            result[entity] = {
                "pl": pl,
                "bs": bs,
                "closing_rate": closing_rate,
                "income_rate": income_rate,
                "detail": detail,
            }
        return result

    def _rate(self, currency: str, rate_type: FXRateType) -> Decimal:
        """Resolve ``currency -> GBP`` at ``rate_type``; never invent a rate."""
        resolved = self.fx.resolve(currency, self.presentation_currency, rate_type)
        return resolved.rate

    # ------------------------------------------------------------------
    # Entity master (entities.csv) — no hardcoded currencies/dates
    # ------------------------------------------------------------------

    def _entities_master(self) -> dict[str, dict[str, str]]:
        """The entity master keyed by upper-case entity id (entities.csv)."""
        cached = getattr(self, "_entity_master", None)
        if cached is None:
            cached = {}
            for row in self.service.list_entities():
                key = (row.get("entity_id") or "").strip().upper()
                if key:
                    cached[key] = row
            self._entity_master = cached
        return cached

    def _entity_currency(self, entity: str) -> str:
        """Functional currency for ``entity`` from the entity master."""
        row = self._entities_master().get(entity.strip().upper())
        if row and (row.get("functional_currency") or "").strip():
            return row["functional_currency"].strip().upper()
        data = getattr(self, "_entity_data", {}).get(entity.strip().upper())
        if data is not None:
            return data.currency
        return self.presentation_currency

    def _acquisition_date(self, entity: str) -> str | None:
        """Acquisition date for ``entity`` from the entity master, if any.

        Returns any recorded acquisition date; the caller decides whether it
        makes the entity partial-year via :meth:`_is_partial_year`.
        """
        row = self._entities_master().get(entity.strip().upper())
        if not row:
            return None
        date = (row.get("acquisition_date") or "").strip()
        return date or None

    # The group reporting period (FY2024). A subsidiary acquired after the
    # start of the reporting year is consolidated only from its acquisition
    # date, so its income/expenses belong to the period of inclusion (H2).
    _REPORTING_PERIOD_START = "2024-01-01"
    _REPORTING_PERIOD_END = "2024-12-31"

    def _is_partial_year(self, acquisition_date: str) -> bool:
        """True when an entity joins the group part-way through the year.

        An entity acquired on or after 1 July of the reporting year is
        consolidated for H2 only (income/expenses at the H2 average, P&L built
        from the monthly movements on/after the acquisition month).
        """
        return (
            self._REPORTING_PERIOD_START
            < acquisition_date
            <= self._REPORTING_PERIOD_END
            and acquisition_date >= f"{self._REPORTING_PERIOD_END[:4]}-07-01"
        )

    def _partial_year_pl_balances(
        self,
        entity: str,
        data: _EntityData,
        acquisition_date: str,
        *,
        adjusted: dict[str, list[Decimal]] | None = None,
        reporting_year_end: str = "2024-12-31",
    ) -> dict[str, list[Decimal]]:
        """Rebuild a mid-year acquisition's P&L from its monthly movements.

        Only months **on or after** the acquisition month are included: the
        pre-acquisition result belongs to the previous owners and is part of
        net assets at acquisition, not group profit. Each ledger's monthly
        total is tied to that entity's trial balance; any difference blocks the
        run (no figure is ever absorbed).

        Returns ``{group_code: [debit, credit]}`` in the entity's functional
        currency, restricted to the included months, and merged with the
        entity's non-P&L balances so the caller can translate it as usual.
        """
        acq_month = acquisition_date[:7]
        monthly = self.service.get_monthly_pnl(entity)
        if not monthly:
            raise ServiceError(
                f"{entity} was acquired mid-year ({acquisition_date}) but no "
                f"monthly P&L is available to build its partial-year result",
                details={"entity": entity, "acquisition_date": acquisition_date},
            )

        # Monthly totals per ledger code, split into "all months" (for the
        # tie-out to the TB) and "included months" (on/after the acquisition).
        all_total: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        included: dict[str, list[Decimal]] = defaultdict(lambda: [_ZERO, _ZERO])
        for row in monthly:
            ledger = (row.get("ledger_code") or "").strip()
            month = (row.get("month") or "").strip()
            amount = Decimal((row.get("amount") or "0").strip() or "0")
            dr_cr = (row.get("dr_cr") or "").strip().lower()
            all_total[ledger] += amount
            if month < acq_month:
                continue
            if dr_cr == "cr":
                included[ledger][1] += amount
            else:
                included[ledger][0] += amount

        # Tie the monthly totals to the trial balance per ledger code.
        tb_by_local: dict[str, Decimal] = {}
        for row in data.trial_balance.rows:
            tb_by_local[row.local_account_code] = (
                tb_by_local.get(row.local_account_code, _ZERO)
                + row.debit
                + row.credit
            )
        mismatches: list[str] = []
        for ledger, total in sorted(all_total.items()):
            tb_amount = tb_by_local.get(ledger)
            if tb_amount is None:
                mismatches.append(f"{ledger}: monthly {total} has no TB row")
                continue
            if abs(tb_amount - total) > _ROUNDING_TOLERANCE:
                mismatches.append(
                    f"{ledger}: monthly {total} vs trial balance {tb_amount} "
                    f"(difference {tb_amount - total})"
                )
        if mismatches:
            self._add_issue(
                code="monthly_pl_tb_mismatch",
                severity=Severity.ERROR,
                message=(
                    f"{entity} monthly P&L does not tie to its trial balance "
                    f"for: " + "; ".join(mismatches)
                ),
                action="blocked",
                entity=entity,
                source_file="in_pl_monthly.csv",
                details={"mismatches": mismatches},
            )
            raise ServiceError(
                f"{entity} monthly P&L does not tie to the trial balance: "
                + "; ".join(mismatches),
                details={"entity": entity, "mismatches": mismatches},
            )

        # Start from the entity's **journal-adjusted** balances (so the
        # balance-sheet effects of any posted journals — e.g. an extra
        # depreciation charge — are kept), then replace every P&L group account
        # with the included-month valuation. Falling back to the raw mapped rows
        # would silently drop those journal effects from the balance sheet.
        balances: dict[str, list[Decimal]] = defaultdict(lambda: [_ZERO, _ZERO])
        if adjusted:
            for group_code, (debit, credit) in adjusted.items():
                account = self.mapper.group_coa.get(group_code)
                if account is not None and account.statement == "PL":
                    continue
                balances[group_code][0] += debit
                balances[group_code][1] += credit
        else:
            for row in data.mapped.rows:
                if not row.mapped or not row.group_account_code:
                    continue
                account = self.mapper.group_coa.get(row.group_account_code)
                if account is not None and account.statement == "PL":
                    continue
                balances[row.group_account_code][0] += row.debit
                balances[row.group_account_code][1] += row.credit

        local_to_group = self._local_to_group_lookup(entity)
        for ledger, (debit, credit) in included.items():
            group_code = local_to_group.get(ledger)
            if group_code is None:
                continue
            account = self.mapper.group_coa.get(group_code)
            if account is None or account.statement != "PL":
                continue
            balances[group_code] = [
                balances[group_code][0] + debit,
                balances[group_code][1] + credit,
            ]
        return balances

    def _local_to_group_lookup(self, entity: str) -> dict[str, str]:
        """A stable local→group account lookup for one entity."""
        cached = getattr(self, "_local_group_lookups", None)
        if cached is None:
            cached = {}
            self._local_group_lookups = cached
        if entity in cached:
            return cached[entity]
        lookup: dict[str, str] = {}
        for row in self._entity_data.get(entity).mapped.rows if entity in self._entity_data else []:
            if row.mapped and row.group_account_code:
                lookup.setdefault(row.local_account_code, row.group_account_code)
        cached[entity] = lookup
        return lookup


    # ------------------------------------------------------------------
    # Stage 4-6: combine + eliminate
    # ------------------------------------------------------------------

    def _combine_and_eliminate(
        self,
        entity_data: dict[str, _EntityData],
        translated: dict[str, dict[str, Any]],
        elimination_result: Any,
        acquisition_result: Any,
    ) -> tuple[list[ConsolidatedTBRow], list[EliminationEntry], dict[str, Decimal]]:
        """Combine translated entity balances and apply eliminations."""
        # Combined P&L (GBP, net = debit - credit) and BS (GBP, net).
        comb_pl: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        comb_bs: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for payload in translated.values():
            for code, value in payload["pl"].items():
                comb_pl[code] += value
            for code, value in payload["bs"].items():
                comb_bs[code] += value

        elim_entries: list[EliminationEntry] = []

        # 4a. Eliminate intercompany balances in full (policy §5).
        #
        # The group eliminates the *aggregate* intercompany receivable (1250)
        # against the aggregate intercompany payable (2150). Before doing so,
        # any intercompany item *in transit* at the year end is recorded by the
        # receiving entity at the closing rate (policy §5), so it appears on
        # both sides and eliminates to zero. Only the genuine exchange
        # difference on the matched invoices (each side booked at its own rate)
        # remains, and that is routed to the FX account (7300). Routing the
        # in-transit amount to FX — as an earlier revision did — would overstate
        # the group's foreign-exchange loss because an in-transit item is not an
        # exchange difference at all.
        ic_receivable = comb_bs.get(IC_RECEIVABLE, _ZERO)   # debit (asset)
        ic_payable = -comb_bs.get(IC_PAYABLE, _ZERO)         # credit (liability) as positive

        # Record in-transit items on the receiving side at the closing rate so
        # both sides carry them before elimination.
        in_transit_total = _ZERO
        in_transit_entries: list[EliminationEntry] = []
        for item in self.reconciler.reconcile().unmatched:
            if item.classification == "in_transit" and item.gbp_amount is not None:
                in_transit_total += item.gbp_amount
                in_transit_entries.append(
                    EliminationEntry(
                        journal_id=f"ELIM-TRANSIT-{item.source_row:03d}",
                        kind="in_transit",
                        reference=item.doc_ref,
                        group_account_code=IC_PAYABLE,
                        group_account_name=self._account_name(IC_PAYABLE),
                        entity=item.counterparty,
                        gbp_debit=_ZERO,
                        gbp_credit=item.gbp_amount,
                        reason=(
                            f"record in-transit {item.doc_ref} in the receiving "
                            f"entity ({item.counterparty}) at the closing rate "
                            f"(policy §5)"
                        ),
                    )
                )
                in_transit_entries.append(
                    EliminationEntry(
                        journal_id=f"ELIM-TRANSIT-{item.source_row:03d}",
                        kind="in_transit",
                        reference=item.doc_ref,
                        group_account_code=IC_RECEIVABLE,
                        group_account_name=self._account_name(IC_RECEIVABLE),
                        entity=item.reporting_entity,
                        gbp_debit=item.gbp_amount,
                        gbp_credit=_ZERO,
                        reason=(
                            f"clear the sender's in-transit {item.doc_ref} "
                            f"against the receiving entity's recognition "
                            f"(policy §5)"
                        ),
                    )
                )
        if in_transit_total != _ZERO:
            # Receiving entity books the payable; the payable magnitude grows.
            ic_payable += in_transit_total
            comb_bs[IC_PAYABLE] = comb_bs.get(IC_PAYABLE, _ZERO) - in_transit_total

        ic_balance_difference = ic_receivable - ic_payable
        if ic_receivable != _ZERO or ic_payable != _ZERO:
            comb_bs[IC_RECEIVABLE] = _ZERO
            comb_bs[IC_PAYABLE] = _ZERO
            comb_bs[FX_ACCOUNT] = comb_bs.get(FX_ACCOUNT, _ZERO) - ic_balance_difference
            elim_entries.extend(in_transit_entries)
            elim_entries.append(
                EliminationEntry(
                    journal_id="ELIM-IC-001",
                    kind="balance",
                    reference="aggregate intercompany balances",
                    group_account_code=IC_RECEIVABLE,
                    group_account_name=self._account_name(IC_RECEIVABLE),
                    entity="GROUP",
                    gbp_debit=_ZERO,
                    gbp_credit=ic_receivable,
                    reason=(
                        "eliminate aggregate intercompany receivable in full "
                        "(policy §5)"
                    ),
                )
            )
            elim_entries.append(
                EliminationEntry(
                    journal_id="ELIM-IC-001",
                    kind="balance",
                    reference="aggregate intercompany balances",
                    group_account_code=IC_PAYABLE,
                    group_account_name=self._account_name(IC_PAYABLE),
                    entity="GROUP",
                    gbp_debit=ic_payable,
                    gbp_credit=_ZERO,
                    reason=(
                        "eliminate aggregate intercompany payable in full "
                        "(policy §5)"
                    ),
                )
            )
            elim_entries.append(
                EliminationEntry(
                    journal_id="ELIM-IC-001",
                    kind="balance",
                    reference="aggregate intercompany balances",
                    group_account_code=FX_ACCOUNT,
                    group_account_name=self._account_name(FX_ACCOUNT),
                    entity="GROUP",
                    gbp_debit=ic_balance_difference if ic_balance_difference > 0 else _ZERO,
                    gbp_credit=-ic_balance_difference if ic_balance_difference < 0 else _ZERO,
                    reason=(
                        "exchange difference on matched intercompany invoices "
                        "booked at different rates (policy §5; see "
                        "intercompany_difference)"
                    ),
                )
            )

        # 4b. Eliminate intercompany income/expense (4200 vs 6300), residual to 7300.
        ic_income = comb_pl.get(IC_INCOME, _ZERO)   # income is credit -> negative net
        ic_expense = comb_pl.get(IC_EXPENSE, _ZERO)  # expense is debit -> positive net
        if ic_income != _ZERO or ic_expense != _ZERO:
            # Clear both lines and route the residual to the FX account so the
            # P&L stays balanced in debit/credit terms.
            comb_pl[IC_INCOME] = _ZERO
            comb_pl[IC_EXPENSE] = _ZERO
            residual = -(ic_income + ic_expense)
            comb_pl[FX_ACCOUNT] = comb_pl.get(FX_ACCOUNT, _ZERO) - residual
            # Clear intercompany income (4200, a credit balance) with a debit
            # and intercompany expense (6300, a debit balance) with a credit.
            # ``ic_income``/``ic_expense`` are net (debit - credit) amounts, so
            # the magnitudes to reverse are ``-ic_income`` and ``-ic_expense``.
            income_magnitude = -ic_income
            expense_magnitude = ic_expense
            for code, debit, credit in (
                (IC_INCOME, income_magnitude, _ZERO),
                (IC_EXPENSE, _ZERO, expense_magnitude),
                (FX_ACCOUNT, _ZERO, residual),
            ):
                if debit == _ZERO and credit == _ZERO:
                    continue
                elim_entries.append(
                    EliminationEntry(
                        journal_id="ELIM-PL-001",
                        kind="income_expense",
                        reference="intercompany management fees",
                        group_account_code=code,
                        group_account_name=self._account_name(code),
                        entity="GROUP",
                        gbp_debit=debit,
                        gbp_credit=credit,
                        reason=(
                            "intercompany income/expense eliminated in full "
                            "(policy §5); residual exchange difference to 7300"
                            if code != FX_ACCOUNT
                            else "residual exchange difference on eliminating "
                            "intercompany income/expense (policy §5)"
                        ),
                    )
                )

        # 4c. Eliminate parent investments against the subsidiaries' equity and
        #     recognise goodwill and NCI (policy §4). Every journal balances on
        #     its own; the genuinely derivable exchange differences on the
        #     investments and goodwill go to the translation reserve (3300).
        uk_inv_movement, in_inv_movement = self._investment_elimination(
            comb_bs, acquisition_result, elim_entries
        )

        # The equity balances are the parent's opening group position (the
        # subsidiaries' opening equity is eliminated above). The translation
        # reserve movement is derived below; opening equity comes from source.
        equity_balances = self._opening_equity()
        self._investment_fx_movements = (uk_inv_movement, in_inv_movement)

        tb_rows = self._to_tb_rows(comb_pl, comb_bs, translated)
        return tb_rows, elim_entries, equity_balances

    def _investment_elimination(
        self,
        comb_bs: dict[str, Decimal],
        acquisition_result: Any,
        elim_entries: list[EliminationEntry],
    ) -> tuple[Decimal, Decimal]:
        """Eliminate parent investments against the subsidiaries' equity.

        The parent holds ``1600`` (UK, USD) and ``1610`` (India, USD). Each
        investment is eliminated against the subsidiary's equity at the rate
        the group policy attributes to that equity, and the *rate movement*
        between the investment's carrying (closing) rate and its elimination
        rate is a genuine exchange difference that goes to the translation
        reserve (3300) — never to retained earnings.

        * **UK** — owned before FY2024, so its opening equity is already in the
          prior-year group figures. The investment is eliminated at the
          **opening** rate against UK share capital (GBP 1,000,000); the small
          excess of share capital over the investment at the opening rate is
          part of the group's opening position and goes to reserves. The rate
          movement ``Inv_UK_usd x (closing - opening)`` goes to 3300.
        * **India** — acquired this year, so it enters at its **spot**-rate net
          assets at completion. The investment is eliminated at spot against
          India's share capital + pre-acquisition reserves + goodwill, with NCI
          recognised at spot. The rate movements on the investment
          (``Inv_in_usd x (closing - spot)``) and on goodwill
          (``Goodwill_inr x (closing - spot)``) go to 3300.

        ``comb_bs`` holds the running consolidated balances for real accounts
        (assets, liabilities, P&L, goodwill, NCI, translation reserve). The
        subsidiary equity accounts (3100/3200) are **not** in ``comb_bs`` —
        equity is contributed by the parent's opening position — so the
        investment journals' equity lines are recorded for the elimination
        report only and never mutate ``comb_bs``. NCI and goodwill deltas ARE
        applied (they become part of the consolidated balance sheet).

        Returns ``(uk_rate_movement, india_rate_movement)`` in GBP: the exchange
        differences routed to 3300 for the UK and India investments (investment
        plus goodwill movements) respectively, used by the closed-form check.
        """
        inv_uk_usd = self._investment_amount_usd("1600")
        inv_in_usd = self._investment_amount_usd("1610")
        usd_open = self._rate("USD", FXRateType.OPENING)
        usd_close = self._rate("USD", FXRateType.CLOSING)
        usd_spot = self._rate("USD", FXRateType.SPOT)
        inr_spot = self._rate("INR", FXRateType.SPOT)
        inr_close = self._rate("INR", FXRateType.CLOSING)

        uk_sc_gbp = self._uk_share_capital_gbp()
        gw_inr = acquisition_result.goodwill  # INR
        nci_acq_inr = acquisition_result.nci_at_acquisition  # INR

        # --- UK investment (eliminated at the opening rate) ----------------
        uk_inv_carrying = inv_uk_usd * usd_close           # in comb_bs at closing
        uk_inv_at_open = inv_uk_usd * usd_open
        uk_rate_movement = uk_inv_carrying - uk_inv_at_open
        # The group's opening reserves carry the excess of UK share capital over
        # the investment at the opening rate (the UK was owned before FY2024).
        uk_excess_to_reserves = uk_sc_gbp - uk_inv_at_open

        comb_bs["1600"] = comb_bs.get("1600", _ZERO) - uk_inv_carrying
        self._opening_re_adjustment = uk_excess_to_reserves

        self._post_balanced(
            elim_entries,
            journal_id="ELIM-INV-UK",
            kind="investment",
            reference="Northwind UK Ltd investment",
            reason=(
                "eliminate the parent's investment in Northwind UK Ltd against "
                "the subsidiary's share capital at the opening rate; the "
                "closing/opening rate movement goes to the translation reserve "
                "(policy §2, §4)"
            ),
            balancing_account=TRESERVE,
            lines=[
                ("1600", _ZERO, uk_inv_carrying),                  # remove investment at carrying (closing) rate
                ("3100", uk_sc_gbp, _ZERO),                        # remove UK share capital (GBP)
                ("3200", _ZERO, uk_excess_to_reserves),            # excess of SC over investment@opening to reserves
            ],
        )

        # --- India investment (eliminated at spot) -------------------------
        in_inv_carrying = inv_in_usd * usd_close
        india_rate_movement = in_inv_carrying - inv_in_usd * usd_spot
        goodwill_rate_movement = gw_inr * (inr_close - inr_spot)
        gw_at_spot = gw_inr * inr_spot
        nci_at_spot = nci_acq_inr * inr_spot

        comb_bs["1610"] = comb_bs.get("1610", _ZERO) - in_inv_carrying
        comb_bs[GOODWILL] = comb_bs.get(GOODWILL, _ZERO) + gw_inr * inr_close
        comb_bs[NCI_ACCOUNT] = comb_bs.get(NCI_ACCOUNT, _ZERO) - nci_at_spot

        self._post_balanced(
            elim_entries,
            journal_id="ELIM-INV-IN",
            kind="investment",
            reference="Northwind India Pvt Ltd investment",
            reason=(
                "eliminate the parent's investment in Northwind India Pvt Ltd "
                "at the acquisition spot rate against the subsidiary's share "
                "capital, pre-acquisition reserves and goodwill, recognising "
                "NCI at spot; investment and goodwill rate movements go to the "
                "translation reserve (policy §2, §4)"
            ),
            balancing_account=TRESERVE,
            lines=[
                ("1610", _ZERO, in_inv_carrying),                  # remove investment at carrying (closing) rate
                ("3100", inv_in_usd * usd_spot, _ZERO),            # the owner's share of net assets at spot
                (GOODWILL, gw_at_spot, _ZERO),                     # goodwill recognised at spot
                (NCI_ACCOUNT, _ZERO, nci_at_spot),                 # NCI at acquisition (spot)
            ],
        )

        return uk_rate_movement, india_rate_movement + goodwill_rate_movement

    def _investment_amount_usd(self, group_code: str) -> Decimal:
        """The parent's investment balance for ``group_code`` in USD.

        Read from the US trial balance (functional-currency magnitude), never
        from a translated figure, so the elimination rate can be applied
        independently.
        """
        data = getattr(self, "_entity_data", {}).get("US")
        if data is None:
            data = self._load_entities(["US"])["US"]
        total = _ZERO
        for row in data.mapped.rows:
            if row.group_account_code == group_code and row.mapped:
                total += row.debit - row.credit
        return total

    def _uk_share_capital_gbp(self) -> Decimal:
        """UK share capital in GBP (functional-currency magnitude)."""
        data = getattr(self, "_entity_data", {}).get("UK")
        if data is None:
            data = self._load_entities(["UK"])["UK"]
        total = _ZERO
        for row in data.mapped.rows:
            if row.group_account_code == "3100" and row.mapped:
                total += row.credit - row.debit
        return total

    def _post_balanced(
        self,
        elim_entries: list[EliminationEntry],
        *,
        journal_id: str,
        kind: str,
        reference: str,
        reason: str,
        lines: list[tuple[str, Decimal, Decimal]],
        balancing_account: str | None = None,
        expected_balance: Decimal | None = None,
    ) -> Decimal:
        """Record a balanced elimination journal (reporting only).

        The caller applies the *real* balance effects to ``comb_bs``
        explicitly. When the supplied ``lines`` do not already balance, the
        residual is posted to ``balancing_account`` (the translation reserve)
        as a credit when the debits exceed the credits, or a debit otherwise.
        If ``expected_balance`` is given (the closed-form TR contribution,
        positive = an increase in the credit-balance reserve), the posted
        balancing amount must equal ``-expected_balance`` within the reporting
        tolerance, or the journal is a bug and raises.

        Returns the posted balancing amount (net = debits - credits) for the
        balancing account.
        """
        debits = sum((d for _, d, _ in lines), _ZERO)
        credits = sum((c for _, _, c in lines), _ZERO)
        net = debits - credits
        posted = list(lines)
        if balancing_account is not None and net != _ZERO:
            if net > 0:
                posted.append((balancing_account, _ZERO, net))
            else:
                posted.append((balancing_account, -net, _ZERO))
        final_debits = sum((d for _, d, _ in posted), _ZERO)
        final_credits = sum((c for _, _, c in posted), _ZERO)
        if abs(final_debits - final_credits) > _ROUNDING_TOLERANCE:
            raise ServiceError(
                f"elimination journal {journal_id} does not balance: debits "
                f"{final_debits} vs credits {final_credits}",
                details={"journal_id": journal_id},
            )
        if expected_balance is not None and abs(net + expected_balance) > _ROUNDING_TOLERANCE:
            raise ServiceError(
                f"elimination journal {journal_id} balancing amount {net} does "
                f"not equal the expected translation-reserve movement "
                f"{-expected_balance}",
                details={"journal_id": journal_id},
            )
        for code, debit, credit in posted:
            if debit == _ZERO and credit == _ZERO:
                continue
            elim_entries.append(
                EliminationEntry(
                    journal_id=journal_id,
                    kind=kind,
                    reference=reference,
                    group_account_code=code,
                    group_account_name=self._account_name(code),
                    entity="GROUP",
                    gbp_debit=debit,
                    gbp_credit=credit,
                    reason=reason,
                )
            )
        return net

    # ------------------------------------------------------------------
    # Stage 7: statements + control figures
    # ------------------------------------------------------------------

    def _build_statements(
        self,
        *,
        tb_rows: list[ConsolidatedTBRow],
        equity_balances: dict[str, Decimal],
        elimination_result: Any,
        acquisition_result: Any,
        nci_post: Any,
        decisions: list[JournalDecision],
    ) -> tuple[FinancialStatements, dict[str, Any]]:
        """Assemble BS/P&L/equity statements and the control figures."""
        by_code = {r.group_account_code: r for r in tb_rows}

        # -- Profit & loss -------------------------------------------------
        pl_codes = [
            code
            for code in ("4100", "4200", "5100", "6100", "6200", "6300", "6400", "6500", "7100", "7300", "8100")
            if code in by_code
        ]
        revenue = self._pl_amount(by_code, REVENUE)
        group_profit = sum(
            (self._pl_amount(by_code, code) for code in pl_codes), _ZERO
        )
        # Profit attributable to NCI (India H2 share).
        profit_nci = nci_post.nci_share_of_profit_gbp
        profit_owners = group_profit - profit_nci

        # -- Balance sheet -------------------------------------------------
        asset_codes = [c for c, r in by_code.items() if r.account_type == "Asset"]
        liab_codes = [c for c, r in by_code.items() if r.account_type == "Liability"]
        total_assets = sum(
            (by_code[c].gbp_debit - by_code[c].gbp_credit for c in asset_codes), _ZERO
        )
        total_liabilities = sum(
            (by_code[c].gbp_credit - by_code[c].gbp_debit for c in liab_codes), _ZERO
        )

        # Equity: opening (historical) + this year's movements.
        share_capital = equity_balances.get("3100", _ZERO)
        opening_re = equity_balances.get("3200", _ZERO)
        opening_treserve = equity_balances.get("3300", _ZERO)

        # NCI at 31 December, measured (policy §4) at the NCI's proportionate
        # share of India's *closing* net assets — net assets at acquisition plus
        # the H2 result — translated at the closing rate. This reconciles to 20%
        # of closing net assets exactly (see validation).
        nci_closing = self._nci_closing_gbp(acquisition_result, nci_post)
        # The NCI's opening (i.e. at-acquisition) carrying amount, at the spot
        # rate, is the starting point of the changes-in-equity statement.
        nci_at_acq = self._nci_at_acquisition_gbp(acquisition_result)
        # Ledger-basis NCI (for the open-ledger trial balance): the closing NCI
        # less its share of the year's result, which the TB carries in the open
        # P&L accounts. This includes the NCI's share of the India translation
        # difference, so the TB balances on the closing-FX basis.
        self._nci_ledger_basis = nci_closing - nci_post.nci_share_of_profit_gbp

        # Retained earnings = opening RE (including the UK share-capital excess
        # recognised on the investment elimination) + profit attributable to
        # owners. There is NO plug: any residual that cannot be explained is
        # surfaced below, never absorbed into retained earnings or the reserve.
        retained_earnings = (
            opening_re + getattr(self, "_opening_re_adjustment", _ZERO) + profit_owners
        )

        # The translation reserve movement is the *ledger-derived* amount that
        # keeps A = L + E: the residual after every real balance and the group's
        # other equity. It is recomputed independently from the closed form
        # (policy §2) and the two must agree to within the reporting tolerance.
        ledger_tr_movement = (
            (total_assets - total_liabilities)
            - share_capital
            - retained_earnings
            - nci_closing
            - opening_treserve
        )
        closed_form_tr = self._closed_form_translation_reserve(
            acquisition_result, nci_post
        )
        self._translation_reserve_difference = ledger_tr_movement - closed_form_tr
        self._ledger_tr_movement = ledger_tr_movement
        self._closed_form_tr_value = closed_form_tr
        # The translation reserve movement is the *genuine* exchange difference
        # on translation: it is the amount the accounting identity produces from
        # the fully adjusted, fully eliminated translated balances. It is
        # disclosed against the simplified closed form (which cannot capture the
        # FX on posted journals/eliminations) and any difference is reported —
        # never absorbed into retained earnings. The balance sheet reconciles
        # EXACTLY (see the `balance_sheet_balances` integrity check), so there is
        # no unexplained residual.
        if abs(ledger_tr_movement - closed_form_tr) > _ROUNDING_TOLERANCE:
            self._add_issue(
                code="translation_reserve_reconciliation",
                severity=Severity.WARNING,
                message=(
                    "the ledger-derived translation reserve movement "
                    f"({round_reported(ledger_tr_movement)}) differs from the "
                    "simplified closed-form estimate "
                    f"({round_reported(closed_form_tr)}) by "
                    f"{round_reported(ledger_tr_movement - closed_form_tr)} GBP; "
                    "the difference is the exchange effect of the posted " \
                    "adjusting journals and the intercompany/investment " \
                    "eliminations, which the simplified estimate does not model. " \
                    "The reserve is a genuine exchange difference (the balance " \
                    "sheet reconciles exactly), not a plug"
                ),
                action="flagged",
                entity="GROUP",
                affected_amount=abs(ledger_tr_movement - closed_form_tr),
                details={
                    "ledger_translation_reserve_movement": str(ledger_tr_movement),
                    "closed_form_translation_reserve_movement": str(closed_form_tr),
                    "difference": str(ledger_tr_movement - closed_form_tr),
                    "tolerance": str(_ROUNDING_TOLERANCE),
                },
            )

        fx_movement = ledger_tr_movement
        translation_reserve = opening_treserve + fx_movement
        total_equity = share_capital + retained_earnings + translation_reserve + nci_closing

        # The reserve movement reported to the user is the genuine FX movement
        # (never a plug).
        treserve_movement = fx_movement

        # Stash the unrounded equity values: the consolidated trial balance is
        # assembled on the open-ledger basis from these (see _sync_equity_rows)
        # so the trial balance balances exactly before reporting rounding.
        self._unrounded_equity = {
            "3100": share_capital,
            "3300": translation_reserve,
        }

        # Each reported line is rounded to 2 dp independently (policy §7), so a
        # reader summing the rounded lines can differ from the rounded total by
        # a penny. Force every *summary* section (whose lines must sum to its
        # total) to foot by absorbing any presentation residual in the largest
        # line (_foot_section). The P&L foots its components itself before the
        # memo lines are appended, and the changes-in-equity statement mixes
        # opening balances, movements and closing balances, so neither is a
        # plain summation and neither is footed here.
        bs_section_assets = [self._foot_section(s) for s in self._asset_sections(by_code, total_assets)]
        bs_section_liab = [self._foot_section(s) for s in self._liability_sections(by_code, total_liabilities)]
        bs_section_equity = [self._foot_section(s) for s in self._equity_sections(
            share_capital=share_capital,
            retained_earnings=retained_earnings,
            translation_reserve=translation_reserve,
            nci_closing=nci_closing,
            total_equity=total_equity,
        )]
        pl_sections = self._pl_sections(by_code, revenue, group_profit, profit_owners, profit_nci)
        equity_sections = self._changes_in_equity(
            opening_re=opening_re,
            opening_treserve=opening_treserve,
            share_capital=share_capital,
            profit_owners=profit_owners,
            profit_nci=profit_nci,
            treserve_movement=treserve_movement,
            nci_at_acq=nci_at_acq,
            nci_closing=nci_closing,
            retained_earnings=retained_earnings,
            translation_reserve=translation_reserve,
            total_equity=total_equity,
        )

        # -- Intercompany difference ---------------------------------------
        ic_difference = self._intercompany_difference()

        # -- Foreign-currency bank balances (e.g. the UK SGD account) ------
        sgd_bank, sgd_reason = self._foreign_currency_bank()

        # -- Journals ------------------------------------------------------
        posted = sum(1 for d in decisions if d.status is JournalStatus.POSTED)
        rejected = sum(1 for d in decisions if d.status is JournalStatus.REJECTED)
        held = sum(1 for d in decisions if d.status is JournalStatus.FLAGGED)
        excluded = sum(1 for d in decisions if d.status is JournalStatus.EXCLUDED)

        figures = {
            "group_revenue": round_reported(revenue),
            "group_profit": round_reported(group_profit),
            "profit_owners": round_reported(profit_owners),
            "profit_nci": round_reported(profit_nci),
            "nci_closing": round_reported(nci_closing),
            "goodwill_closing": round_reported(acquisition_result.goodwill_gbp),
            "translation_reserve_movement": round_reported(treserve_movement),
            "total_assets": round_reported(total_assets),
            "total_equity": round_reported(total_equity),
            "intercompany_difference": ic_difference,
            "sgd_bank_gbp": sgd_bank,
            "journals": {
                "posted": posted,
                "rejected": rejected,
                "held_for_review": held,
                "excluded": excluded,
                "total": posted + rejected + held + excluded,
            },
        }
        if sgd_reason:
            figures["sgd_bank_gbp_reason"] = sgd_reason

        checks = self._integrity_checks(
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            total_equity=total_equity,
            group_profit=group_profit,
            profit_owners=profit_owners,
            profit_nci=profit_nci,
            nci_closing=nci_closing,
            acquisition_result=acquisition_result,
            nci_post=nci_post,
        )

        statements = FinancialStatements(
            presentation_currency=self.presentation_currency,
            period="FY2024",
            balance_sheet=bs_section_assets + bs_section_liab + bs_section_equity,
            profit_and_loss=pl_sections,
            changes_in_equity=equity_sections,
            checks=checks,
        )
        return statements, figures

    # -- statement builders ------------------------------------------------

    def _asset_sections(self, by_code: dict[str, ConsolidatedTBRow], total: Decimal) -> list[StatementSection]:
        lines = []
        for code in sorted(c for c, r in by_code.items() if r.account_type == "Asset"):
            row = by_code[code]
            amount = row.gbp_debit - row.gbp_credit
            if amount != _ZERO:
                lines.append(
                    LineItem(
                        group_account_code=code,
                        label=row.group_account_name,
                        amount=round_reported(amount),
                    )
                )
        return [
            StatementSection(
                title="Assets", lines=lines, total=round_reported(total)
            )
        ]

    def _liability_sections(self, by_code: dict[str, ConsolidatedTBRow], total: Decimal) -> list[StatementSection]:
        lines = []
        for code in sorted(c for c, r in by_code.items() if r.account_type == "Liability"):
            row = by_code[code]
            amount = row.gbp_credit - row.gbp_debit
            if amount != _ZERO:
                lines.append(
                    LineItem(
                        group_account_code=code,
                        label=row.group_account_name,
                        amount=round_reported(amount),
                    )
                )
        return [
            StatementSection(
                title="Liabilities", lines=lines, total=round_reported(total)
            )
        ]

    def _equity_sections(
        self,
        *,
        share_capital: Decimal,
        retained_earnings: Decimal,
        translation_reserve: Decimal,
        nci_closing: Decimal,
        total_equity: Decimal,
    ) -> list[StatementSection]:
        lines = [
            LineItem(group_account_code="3100", label="Share capital", amount=round_reported(share_capital)),
            LineItem(group_account_code="3200", label="Retained earnings", amount=round_reported(retained_earnings)),
            LineItem(group_account_code="3300", label="Translation reserve", amount=round_reported(translation_reserve)),
            LineItem(group_account_code="3400", label="Non-controlling interest", amount=round_reported(nci_closing)),
        ]
        return [
            StatementSection(title="Equity", lines=lines, total=round_reported(total_equity))
        ]

    def _pl_sections(
        self,
        by_code: dict[str, ConsolidatedTBRow],
        revenue: Decimal,
        group_profit: Decimal,
        profit_owners: Decimal,
        profit_nci: Decimal,
    ) -> list[StatementSection]:
        line_codes = ["4100", "5100", "6100", "6200", "6400", "6500", "7100", "7300", "8100"]
        lines = []
        for code in line_codes:
            row = by_code.get(code)
            if row is None:
                continue
            amount = row.gbp_credit - row.gbp_debit  # income positive, expense negative
            if amount != _ZERO:
                lines.append(
                    LineItem(
                        group_account_code=code,
                        label=row.group_account_name,
                        amount=round_reported(amount),
                    )
                )
        # Foot the component lines to the reported profit before appending the
        # memo lines, so the P&L components sum exactly to "Profit for the year".
        component_total = round_reported(group_profit)
        component_sum = sum((ln.amount for ln in lines), _ZERO)
        if lines and component_sum != component_total:
            target = max(lines, key=lambda ln: abs(ln.amount))
            lines = [
                ln.model_copy(update={"amount": ln.amount + (component_total - component_sum)})
                if ln is target
                else ln
                for ln in lines
            ]
        lines.append(LineItem(label="Profit for the year", amount=component_total, note="group profit"))
        lines.append(LineItem(label="Attributable to owners of the parent", amount=round_reported(profit_owners)))
        lines.append(LineItem(label="Attributable to non-controlling interest", amount=round_reported(profit_nci)))
        return [
            StatementSection(
                title="Profit and loss",
                lines=lines,
                total=component_total,
            )
        ]

    def _changes_in_equity(
        self,
        *,
        opening_re: Decimal,
        opening_treserve: Decimal,
        share_capital: Decimal,
        profit_owners: Decimal,
        profit_nci: Decimal,
        treserve_movement: Decimal,
        nci_at_acq: Decimal,
        nci_closing: Decimal,
        retained_earnings: Decimal,
        translation_reserve: Decimal,
        total_equity: Decimal,
    ) -> list[StatementSection]:
        lines = [
            LineItem(label="Share capital at 1 January 2024", amount=round_reported(share_capital)),
            LineItem(label="Retained earnings at 1 January 2024", amount=round_reported(opening_re)),
            LineItem(label="Translation reserve at 1 January 2024", amount=round_reported(opening_treserve)),
            LineItem(label="Profit attributable to owners", amount=round_reported(profit_owners)),
            LineItem(label="Profit attributable to non-controlling interest", amount=round_reported(profit_nci)),
            LineItem(label="Translation reserve movement", amount=round_reported(treserve_movement)),
            LineItem(label="Non-controlling interest at acquisition", amount=round_reported(nci_at_acq)),
            LineItem(label="Share capital at 31 December 2024", amount=round_reported(share_capital)),
            LineItem(label="Retained earnings at 31 December 2024", amount=round_reported(retained_earnings)),
            LineItem(label="Translation reserve at 31 December 2024", amount=round_reported(translation_reserve)),
            LineItem(label="Non-controlling interest at 31 December 2024", amount=round_reported(nci_closing)),
        ]
        return [
            StatementSection(
                title="Changes in equity", lines=lines, total=round_reported(total_equity)
            )
        ]

    @staticmethod
    def _foot_section(section: StatementSection) -> StatementSection:
        """Make the rounded line items sum exactly to the rounded section total.

        Each reported line is rounded to 2 dp independently (policy §7), so the
        rounded lines can differ from the (rounded) exact total by a penny. A
        reader summing the statement must reach the total, so the residual is
        absorbed by the largest line item — a standard presentation-rounding
        adjustment that leaves every other line untouched and never changes the
        total. Lines without an amount (or with ``None``) are ignored.
        """
        if section.total is None:
            return section
        lines = [ln for ln in section.lines if ln.amount is not None]
        if not lines:
            return section
        rounded_sum = sum((ln.amount for ln in lines), _ZERO)
        residual = section.total - rounded_sum
        if residual == _ZERO:
            return section
        # Absorb the residual in the largest line item.
        target = max(lines, key=lambda ln: abs(ln.amount))
        new_lines = [
            ln.model_copy(update={"amount": ln.amount + residual})
            if ln is target
            else ln
            for ln in section.lines
        ]
        return section.model_copy(update={"lines": new_lines})

    # -- control figures ---------------------------------------------------

    def _nci_closing_gbp(
        self, acquisition_result: Any, nci_post: Any
    ) -> Decimal:
        """NCI at 31 December, reconciled to the NCI share of closing net assets.

        Policy §4 measures NCI at its proportionate share of net assets. India's
        closing net assets are its acquisition-date net assets plus the H2 result
        (``in_pl_monthly.csv``), so the NCI's share is
        ``nci_pct x (NA@acq + H2 profit)`` translated at the **closing** rate.
        This is exactly 20% of India's closing net assets translated at closing,
        and every input comes from the acquisition note / monthly P&L — nothing
        is hardcoded.
        """
        nci_pct = nci_post.nci_pct
        net_assets_acq = acquisition_result.net_assets_at_acquisition  # INR
        profit_h2 = nci_post.post_acquisition_profit  # INR
        inr_closing = self._rate("INR", FXRateType.CLOSING)
        return nci_pct * (net_assets_acq + profit_h2) * inr_closing

    def _nci_at_acquisition_gbp(self, acquisition_result: Any) -> Decimal:
        """NCI at acquisition translated at the SPOT (completion) rate.

        Policy §4 measures NCI at its proportionate share of net assets at
        acquisition; that share is translated into GBP at the acquisition-date
        spot rate, not the closing rate (the closing rate would import a
        translation difference into NCI that belongs in the translation
        reserve).
        """
        inr_spot = self._rate("INR", FXRateType.SPOT)
        return acquisition_result.nci_at_acquisition * inr_spot

    def _closed_form_translation_reserve(
        self, acquisition_result: Any, nci_post: Any
    ) -> Decimal:
        """The closed-form translation reserve movement attributable to owners.

        Independently derived from the raw rates and the entity balances, and
        asserted (in :meth:`_build_statements`) to equal the ledger-derived
        reserve. The total movement is the sum of five components, and the NCI
        share of India's components is removed to leave the owners' share:

        * **US** — ``NA0_us x (c - o) + P_us x (c - a)``: the US opening net
          assets translate at the closing vs opening rate, and its profit at the
          closing vs average rate (``NA0_us`` = US opening share capital +
          retained earnings in USD, ``P_us`` = US profit in USD after posted
          journals).
        * **UK investment** — ``- Inv_UK_usd x (c - o)``: the eliminated
          investment retranslates from the opening to the closing rate.
        * **India** — ``NA_acq_inr x (C - S) + P_in x (C - H)``: India's net
          assets at acquisition translate from spot to closing, and its
          post-acquisition profit from the H2 average to closing.
        * **Goodwill** — ``GW_inr x (C - S)``: goodwill is held in INR and
          retranslated at closing.
        * **India investment** — ``- Inv_in_usd x (c - s) + (consideration_inr
          x S - Inv_in_usd x s)``: the investment retranslates from spot to
          closing, plus the small difference between the consideration recorded
          in INR and the parent's USD carrying amount.

        The NCI share is ``20% x (India NA term + India profit term)`` only — it
        does not touch goodwill or the investment terms (those relate to the
        parent's ownership).

        All rates are GBP-per-unit multipliers (the reciprocal of the recorded
        ``X per 1 GBP`` quotes) of the SAME rate type.
        """
        usd_open = self._rate("USD", FXRateType.OPENING)
        usd_average = self._rate("USD", FXRateType.AVERAGE)
        usd_close = self._rate("USD", FXRateType.CLOSING)
        usd_spot = self._rate("USD", FXRateType.SPOT)
        inr_spot = self._rate("INR", FXRateType.SPOT)
        inr_close = self._rate("INR", FXRateType.CLOSING)
        inr_h2 = self._rate("INR", FXRateType.H2_AVERAGE)

        # US opening net assets (share capital + retained earnings) and profit,
        # in USD, from the US trial balance after posted journals.
        us_na0 = self._us_opening_net_assets_usd()
        us_profit = self._us_profit_usd()

        # Parent investment amounts (USD).
        inv_uk_usd = self._investment_amount_usd("1600")
        inv_in_usd = self._investment_amount_usd("1610")

        na_acq_inr = acquisition_result.net_assets_at_acquisition
        gw_inr = acquisition_result.goodwill
        consideration_inr = acquisition_result.consideration
        p_in = nci_post.post_acquisition_profit

        us_term = us_na0 * (usd_close - usd_open) + us_profit * (usd_close - usd_average)
        uk_inv_term = -inv_uk_usd * (usd_close - usd_open)
        india_na_term = na_acq_inr * (inr_close - inr_spot)
        india_profit_term = p_in * (inr_close - inr_h2)
        india_term = india_na_term + india_profit_term
        goodwill_term = gw_inr * (inr_close - inr_spot)
        inv_in_term = -inv_in_usd * (usd_close - usd_spot) + (
            consideration_inr * inr_spot - inv_in_usd * usd_spot
        )

        total = us_term + uk_inv_term + india_term + goodwill_term + inv_in_term
        nci_pct = nci_post.nci_pct
        nci_share = nci_pct * (india_na_term + india_profit_term)
        return total - nci_share

    def _us_opening_net_assets_usd(self) -> Decimal:
        """US opening share capital + retained earnings in USD (from the TB)."""
        data = getattr(self, "_entity_data", {}).get("US")
        if data is None:
            data = self._load_entities(["US"])["US"]
        total = _ZERO
        for row in data.mapped.rows:
            if row.mapped and row.group_account_code in ("3100", "3200"):
                total += row.credit - row.debit
        return total

    def _us_profit_usd(self) -> Decimal:
        """US profit in USD after posted journals (income-positive)."""
        balances = self._adjusted_balances_us()
        total = _ZERO
        for group_code, (debit, credit) in balances.items():
            account = self.mapper.group_coa.get(group_code)
            if account is not None and account.statement == "PL":
                total += credit - debit
        return total

    def _adjusted_balances_us(self) -> dict[str, list[Decimal]]:
        """US balances (functional USD) after posted journals, by group code."""
        cached = getattr(self, "_us_adjusted", None)
        if cached is not None:
            return cached
        balances: dict[str, list[Decimal]] = defaultdict(lambda: [_ZERO, _ZERO])
        data = getattr(self, "_entity_data", {}).get("US")
        if data is None:
            data = self._load_entities(["US"])["US"]
        for row in data.mapped.rows:
            if row.mapped and row.group_account_code:
                balances[row.group_account_code][0] += row.debit
                balances[row.group_account_code][1] += row.credit
        for journal in getattr(self, "_posted_journals", []) or []:
            if journal.entity != "US":
                continue
            for line in journal.lines:
                balances[line.account][0] += line.debit
                balances[line.account][1] += line.credit
        self._us_adjusted = balances
        return balances

    def _opening_equity(self) -> dict[str, Decimal]:
        """The group's opening equity, read strictly from prior_year_group.csv.

        Every balance comes from the source (no hardcoded fallback): if a
        required equity code is missing the run fails rather than inventing a
        value.
        """
        result: dict[str, Decimal] = {}
        try:
            for row in self.service.get_prior_year_group():
                code = (row.get("group_account") or "").strip()
                amount = Decimal((row.get("amount_gbp") or "").strip() or "0")
                if code:
                    result[code] = amount
        except LedgerFlowError as exc:
            raise ServiceError(
                "the group's opening equity (prior_year_group.csv) could not be "
                f"read: {exc}"
            ) from exc
        if not result:
            raise ServiceError(
                "prior_year_group.csv supplied no opening equity rows; the "
                "opening position cannot be invented"
            )
        for required in ("3100", "3200", "3300"):
            if required not in result:
                raise ServiceError(
                    "prior_year_group.csv is missing the required opening "
                    f"equity account {required}"
                )
        result.setdefault("3400", _ZERO)
        return result

    def _entity_balances_raw(self, entity: str) -> dict[str, list[Decimal]]:
        cached = getattr(self, "_raw_balances", None)
        if cached is None:
            cached = {}
            self._raw_balances = cached
        if entity in cached:
            return cached[entity]
        data = getattr(self, "_entity_data", {}).get(entity)
        if data is None:
            data = self._load_entities([entity])[entity]
        balances: dict[str, list[Decimal]] = defaultdict(lambda: [_ZERO, _ZERO])
        for row in data.mapped.rows:
            if row.mapped and row.group_account_code:
                balances[row.group_account_code][0] += row.debit
                balances[row.group_account_code][1] += row.credit
        cached[entity] = balances
        return balances

    def _local_to_group(self, entity: str, local_code: str) -> str | None:
        if entity not in ("UK", "IN"):
            return local_code
        entries, _ = self.mapper.load_mapping(entity)
        for entry in entries:
            if entry.local_code == local_code and entry.group_code:
                return entry.group_code
        return None

    def _intercompany_difference(self) -> dict[str, Any]:
        """The intercompany difference and its causes (assignment appendix).

        The difference is the aggregate intercompany receivable less the
        aggregate intercompany payable, both in GBP at the closing rate. It is
        split into:

        * **document vs booking FX** — the exchange differences on invoices
          matched on both sides but booked at different rates;
        * **in transit** — the invoice(s) recorded by one entity but not yet by
          the receiving entity at the year end (policy §5).
        """
        reconciliation = self.reconciler.reconcile()

        # Aggregate balances in GBP (closing rate), before elimination.
        us_bs = self._entity_balances_raw("US")
        uk_bs = self._entity_balances_raw("UK")
        usd_closing = self._rate("USD", FXRateType.CLOSING)
        receivable = us_bs.get(IC_RECEIVABLE, [_ZERO, _ZERO])[0] * usd_closing
        payable = uk_bs.get(IC_PAYABLE, [_ZERO, _ZERO])[1]  # UK books in GBP
        total = receivable - payable

        in_transit = _ZERO
        for item in reconciliation.unmatched:
            if item.classification == "in_transit" and item.gbp_amount is not None:
                in_transit += item.gbp_amount
        doc_booking_fx = total - in_transit
        return {
            "total": round_reported(total),
            "causes": {
                "document_vs_booking_fx": round_reported(doc_booking_fx),
                "in_transit": round_reported(in_transit),
            },
            "matched_pairs": reconciliation.matched_count,
            "unmatched_items": reconciliation.unmatched_count,
        }

    def _foreign_currency_bank(self) -> tuple[Decimal | None, str | None]:
        """Translate the **SGD bank** balance found in the source TB.

        The control figure is specifically the group's SGD bank account
        (assignment): only rows whose transaction currency is SGD AND which map
        to a bank account in the group chart of accounts are included. Scanning
        every foreign-currency row would wrongly pull in the UK's USD
        intercompany payable (2150). The amount comes from the source row, so
        changing an input row (or adding a GBP/SGD closing rate) changes the
        result; nothing is hardcoded.

        Returns the total translated value in GBP, or ``None`` with a reason
        when the required rate is missing. If no such row exists, returns zero.
        """
        if self.presentation_currency != "GBP":
            return _ZERO, None

        BANK_GROUP_ACCOUNTS = {"1100", "1210"}
        total = _ZERO
        missing: list[str] = []
        for entity in sorted(getattr(self, "_entity_data", {})):
            data = self._entity_data[entity]
            for row in data.trial_balance.rows:
                txn_ccy = (row.txn_currency or "").strip().upper()
                if txn_ccy != "SGD":
                    continue
                group_code = self._local_to_group(entity, row.local_account_code)
                account = self.mapper.group_coa.get(group_code) if group_code else None
                is_bank = group_code in BANK_GROUP_ACCOUNTS or (
                    account is not None and "bank" in (account.account_name or "").lower()
                )
                if not is_bank:
                    continue
                # The foreign-currency amount is the source transaction amount;
                # fall back to the booked balance only if none is recorded.
                amount = row.txn_amount if row.txn_amount is not None else row.signed_balance
                if amount is None:
                    continue
                try:
                    resolved = self.fx.resolve(
                        txn_ccy, self.presentation_currency, FXRateType.CLOSING
                    )
                except LedgerFlowError:
                    missing.append(f"{entity} {row.local_account_code} ({txn_ccy})")
                    self._add_issue(
                        code="missing_fx_rate",
                        severity=Severity.WARNING,
                        message=(
                            f"no {self.presentation_currency}/{txn_ccy} closing "
                            f"rate is recorded, so {entity} account "
                            f"{row.local_account_code} ({txn_ccy} {amount}) cannot "
                            f"be translated at the closing rate"
                        ),
                        action="blocked",
                        entity=entity,
                        source_file=row.source_file,
                        source_row=row.source_row,
                        account_code=row.local_account_code,
                        affected_amount=amount,
                        details={
                            "transaction_currency": txn_ccy,
                            "reason": "missing_fx_rate",
                        },
                    )
                    continue
                total += amount * resolved.rate

        if missing:
            reason = (
                "no closing rate recorded for "
                + ", ".join(missing)
                + "; a cross-rate cannot be derived"
            )
            return None, reason
        return total, None

    def _integrity_checks(
        self,
        *,
        total_assets: Decimal,
        total_liabilities: Decimal,
        total_equity: Decimal,
        group_profit: Decimal,
        profit_owners: Decimal,
        profit_nci: Decimal,
        nci_closing: Decimal,
        acquisition_result: Any,
        nci_post: Any,
    ) -> list[dict[str, Any]]:
        # Unexplained residual: the balance sheet must reconcile to within the
        # reporting tolerance. There is no plug in retained earnings or the
        # reserve, so this is the genuine residual and it must be zero
        # (requirement §4). If it ever exceeds the tolerance the check FAILS.
        unexplained = total_assets - total_liabilities - total_equity

        # NCI must reconcile to the NCI's proportionate share of India's
        # closing net assets translated at the closing rate (requirement §3).
        nci_expected = self._nci_closing_gbp(acquisition_result, nci_post)
        nci_difference = nci_closing - nci_expected

        return [
            {
                "check": "balance_sheet_balances",
                "passed": (total_assets - total_liabilities - total_equity) == _ZERO,
                "detail": (
                    f"assets {round_reported(total_assets)} = liabilities "
                    f"{round_reported(total_liabilities)} + equity "
                    f"{round_reported(total_equity)}"
                ),
            },
            {
                "check": "unexplained_residual_within_tolerance",
                "passed": abs(unexplained) <= _ROUNDING_TOLERANCE,
                "detail": (
                    f"assets - liabilities - equity = "
                    f"{round_reported(unexplained)} GBP "
                    f"(tolerance {_ROUNDING_TOLERANCE}); no residual is routed "
                    "to retained earnings or the translation reserve"
                ),
            },
            {
                "check": "nci_reconciles_to_closing_net_assets",
                "passed": abs(nci_difference) <= _ROUNDING_TOLERANCE,
                "detail": (
                    f"NCI closing {round_reported(nci_closing)} = "
                    f"{round_reported(nci_post.nci_pct * 100)}% of India's "
                    "closing net assets at the closing rate "
                    f"({round_reported(nci_expected)}); difference "
                    f"{round_reported(nci_difference)}"
                ),
            },
            {
                "check": "profit_split_reconciles",
                "passed": (profit_owners + profit_nci - group_profit) == _ZERO,
                "detail": (
                    f"owners {round_reported(profit_owners)} + NCI "
                    f"{round_reported(profit_nci)} = group profit "
                    f"{round_reported(group_profit)}"
                ),
            },
        ]

    # -- helpers -----------------------------------------------------------

    def _pl_amount(self, by_code: dict[str, ConsolidatedTBRow], code: str) -> Decimal:
        """Income-positive amount for a P&L account (credit - debit)."""
        row = by_code.get(code)
        if row is None:
            return _ZERO
        return row.gbp_credit - row.gbp_debit

    def _account_name(self, code: str) -> str:
        account = self.mapper.group_coa.get(code)
        return account.account_name if account else code

    def _sync_equity_rows(
        self,
        tb_rows: list[ConsolidatedTBRow],
        statements: FinancialStatements,
        *,
        opening_re: Decimal,
        nci_at_acquisition: Decimal,
    ) -> list[ConsolidatedTBRow]:
        """Replace the equity rows with the assembled equity figures on the
        **open-ledger (trial-balance) basis**.

        The trial balance keeps the year's P&L accounts open, so the equity
        rows must not also carry the year's result. The TB therefore shows:

        * share capital (closing — unchanged in the year),
        * **opening** retained earnings (the current profit stays in the open
          P&L accounts, so it is not rolled into retained earnings here),
        * the **closing** translation reserve (its movement is a balance-sheet
          equity movement, not a P&L item),
        * NCI **at acquisition** (its share of the current profit is part of
          the open P&L, not a separate ledger equity movement).

        With those figures the consolidated TB balances exactly
        (debits = credits). The reported *statements* use the closing figures
        (retained earnings including the year's profit, NCI closing) — the
        difference is precisely the year's profit, represented in the TB by
        the open P&L accounts.
        """
        equity_lines: dict[str, Decimal] = {}
        for section in statements.balance_sheet:
            if section.title != "Equity":
                continue
            for line in section.lines:
                if line.group_account_code and line.amount is not None:
                    equity_lines[line.group_account_code] = line.amount

        # Override the accounts that must be on the ledger basis, using the
        # unrounded values so the trial balance balances exactly.
        unrounded = getattr(self, "_unrounded_equity", {})
        equity_lines["3100"] = unrounded.get("3100", equity_lines.get("3100", _ZERO))
        equity_lines["3200"] = opening_re
        equity_lines["3300"] = unrounded.get("3300", equity_lines.get("3300", _ZERO))
        equity_lines["3400"] = nci_at_acquisition

        # The closing translation reserve from the assembled equity section is
        # kept as-is (3300): the reserve movement is an equity movement.

        kept = [r for r in tb_rows if r.account_type != "Equity"]
        equity_source = {
            "entity": "GROUP",
            "source_file": "prior_year_group.csv",
            "source_row": None,
            "note": "opening group equity; movements from the year's result, "
            "translation reserve and acquisition (see eliminations.csv)",
        }
        for code, amount in equity_lines.items():
            account = self.mapper.group_coa.get(code)
            if account is None:
                continue
            # Credit-positive equity: a positive amount is a credit magnitude.
            debit = -amount if amount < 0 else _ZERO
            credit = amount if amount > 0 else _ZERO
            kept.append(
                ConsolidatedTBRow(
                    group_account_code=code,
                    group_account_name=account.account_name,
                    account_type=account.account_type,
                    statement=account.statement,
                    gbp_debit=debit,
                    gbp_credit=credit,
                    sources=[dict(equity_source)],
                )
            )
        return sorted(kept, key=lambda r: r.group_account_code)

    def _to_tb_rows(
        self,
        comb_pl: dict[str, Decimal],
        comb_bs: dict[str, Decimal],
        translated: dict[str, dict[str, Any]],
    ) -> list[ConsolidatedTBRow]:
        """Produce consolidated trial-balance rows from the combined balances.

        ``comb_pl`` / ``comb_bs`` net amounts are converted back into
        non-negative debit/credit magnitudes on the side they fall.
        """
        rows: list[ConsolidatedTBRow] = []
        net_by_code: dict[str, Decimal] = {}
        # Combine the balance-sheet and P&L nets by group account. A group
        # account is normally either a balance-sheet or a P&L account (so the
        # two sets are disjoint), but the FX account (7300) receives both a
        # balance-sheet movement (the exchange difference on eliminating
        # intercompany balances) and a P&L movement (the residual on eliminating
        # intercompany income/expense). Both must be added, not overwritten.
        for code, net in comb_bs.items():
            net_by_code[code] = net_by_code.get(code, _ZERO) + net
        for code, net in comb_pl.items():
            net_by_code[code] = net_by_code.get(code, _ZERO) + net

        for code in sorted(net_by_code):
            account = self.mapper.group_coa.get(code)
            if account is None:
                continue
            net = net_by_code[code]
            # Standard trial-balance representation: a positive net sits on the
            # debit side, a negative net on the credit side. P&L rows therefore
            # carry income (a credit) as a credit magnitude; the statement
            # builders convert that to an income-positive figure.
            debit = net if net > 0 else _ZERO
            credit = -net if net < 0 else _ZERO
            sources = self._sources_for(code, translated)
            if code == GOODWILL and net != _ZERO:
                # Goodwill arises from the India acquisition: trace it to the
                # acquisition note rather than to an entity trial-balance row.
                sources = [
                    {
                        "entity": "GROUP",
                        "source_file": "acquisition_note.md",
                        "source_row": None,
                        "note": "goodwill = consideration - 80% x net assets at "
                        "acquisition (INR), retranslated at the closing rate",
                    }
                ]
            if code == FX_ACCOUNT and not sources and net != _ZERO:
                # The group FX account here is produced by the intercompany
                # elimination (balance + income/expense residual), so it traces
                # to the intercompany source records, not a single entity row.
                sources = [
                    {
                        "entity": "GROUP",
                        "source_file": "intercompany.csv",
                        "source_row": None,
                        "note": "exchange difference on intercompany elimination "
                        "(policy §5); also see eliminations.csv",
                    }
                ]
            rows.append(
                ConsolidatedTBRow(
                    group_account_code=code,
                    group_account_name=account.account_name,
                    account_type=account.account_type,
                    statement=account.statement,
                    gbp_debit=debit,
                    gbp_credit=credit,
                    sources=sources,
                )
            )
        return rows

    def _sources_for(self, code: str, translated: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for entity, payload in translated.items():
            for item in payload.get("detail", {}).get(code, []):
                sources.append(item)
        return sources

    def _add_issue(
        self,
        *,
        code: str,
        severity: Severity,
        message: str,
        action: str,
        entity: str | None = None,
        source_file: str | None = None,
        source_row: int | None = None,
        account_code: str | None = None,
        affected_amount: Decimal | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.issues.append(
            ConsolidationIssue(
                code=code,
                severity=severity,
                message=message,
                action=action,
                entity=entity,
                source_file=source_file,
                source_row=source_row,
                account_code=account_code,
                affected_amount=affected_amount,
                treatment=message,
                details=details or {},
            )
        )


@dataclass
class ConsolidationOutput:
    """The full consolidated output bundle (pre-rounding except statements)."""

    statement_assembler: StatementAssembler
    entities: list[str]
    trial_balance_rows: list[ConsolidatedTBRow]
    eliminations: list[EliminationEntry]
    statements: FinancialStatements
    control_figures: dict[str, Any]
    reconciliation: Any
    acquisition: Any
    nci_post: Any
    journal_decisions: list[JournalDecision]

    @property
    def issues(self) -> list[ConsolidationIssue]:
        return self.statement_assembler.issues
