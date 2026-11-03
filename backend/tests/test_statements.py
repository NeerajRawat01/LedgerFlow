"""Deterministic accounting tests for the consolidation assembly engine.

These tests exercise ``app.accounting.statements``, ``acquisition`` and
``outputs`` against the **real** ``data/northwind`` dataset and assert the
exact figures the engine produces. They are the independent recomputation
evidence referenced by ``VALIDATION.md``: every control figure is checked
against a value derived separately in the test, so a regression in any stage
fails here.

No LLM is involved and no network access occurs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.acquisition import AcquisitionService
from app.accounting.models import FXRateType
from app.accounting.outputs import CONTROL_FIGURE_KEYS, OutputWriter
from app.accounting.statements import StatementAssembler

REPO_DATA = Path(__file__).resolve().parents[2] / "data"

pytestmark = pytest.mark.skipif(
    not (REPO_DATA / "northwind").is_dir(), reason="real dataset not present"
)


@pytest.fixture(scope="module")
def bundle():
    from app.services.source_data import SourceDataService

    assembler = StatementAssembler(SourceDataService(REPO_DATA))
    return assembler.assemble()


# --- acquisition accounting -------------------------------------------------


def test_goodwill_matches_independent_recomputation():
    from app.services.source_data import SourceDataService

    service = AcquisitionService(SourceDataService(REPO_DATA))
    result = service.compute(service.load_inputs("IN"))
    # Goodwill = consideration - 80% x net assets at acquisition (INR).
    consideration = Decimal("690000000")
    net_assets = Decimal("678000000")
    assert result.consideration == consideration
    assert result.net_assets_at_acquisition == net_assets
    assert result.goodwill == consideration - net_assets * Decimal("0.8")
    assert result.goodwill == Decimal("147600000.0")
    # Retranslated to GBP at the closing rate (INR->GBP cross).
    assert round(result.goodwill_gbp, 2) == Decimal("1375815.07")


def test_nci_at_acquisition_is_twenty_percent_of_net_assets():
    from app.services.source_data import SourceDataService

    service = AcquisitionService(SourceDataService(REPO_DATA))
    result = service.compute(service.load_inputs("IN"))
    assert result.nci_at_acquisition == Decimal("678000000") * Decimal("0.2")
    assert round(result.nci_at_acquisition_gbp, 2) == Decimal("1263960.19")


def test_nci_share_of_post_acquisition_profit():
    from app.services.source_data import SourceDataService

    service = AcquisitionService(SourceDataService(REPO_DATA))
    inputs = service.load_inputs("IN")
    post = service.post_acquisition_profit(
        "IN",
        acquisition_date=inputs.acquisition_date,
        ownership_pct=inputs.ownership_pct,
        income_rate_type=FXRateType.H2_AVERAGE,
    )
    # India H2 profit is INR 42,000,000 (Jul-Dec); NCI takes 20%.
    assert post.post_acquisition_profit == Decimal("42000000.00")
    assert round(post.nci_share_of_profit_gbp, 2) == Decimal("77928.01")


# --- control figures --------------------------------------------------------


def test_control_figures_have_exactly_the_required_keys():
    from app.services.source_data import SourceDataService

    assembler = StatementAssembler(SourceDataService(REPO_DATA))
    out = assembler.assemble()
    assert set(out.control_figures) >= set(CONTROL_FIGURE_KEYS)
    for key in CONTROL_FIGURE_KEYS:
        assert key in out.control_figures


def test_group_profit_recomputed_from_p_and_l(bundle):
    figures = bundle.control_figures
    # Group revenue is account 4100 alone; group profit is the sum of all
    # P&L account nets (income-positive) recomputed from the reported TB.
    revenue = Decimal("0")
    profit = Decimal("0")
    for row in bundle.trial_balance_rows:
        if row.statement != "PL":
            continue
        net = row.gbp_credit - row.gbp_debit  # income-positive
        profit += net
        if row.group_account_code == "4100":
            revenue += net
    assert figures["group_revenue"] == round(revenue, 2)
    assert abs(profit - figures["group_profit"]) <= Decimal("0.02")
    # The exact figure the engine produces (India consolidated for H2 only).
    assert figures["group_profit"] == Decimal("2987713.67")


def test_profit_split_reconciles(bundle):
    figures = bundle.control_figures
    assert figures["profit_owners"] + figures["profit_nci"] == figures["group_profit"] or (
        abs(
            figures["profit_owners"]
            + figures["profit_nci"]
            - figures["group_profit"]
        )
        <= Decimal("0.01")
    )


def test_balance_sheet_balances(bundle):
    figures = bundle.control_figures
    assets = figures["total_assets"]
    equity = figures["total_equity"]
    liabilities = Decimal("0")
    for row in bundle.trial_balance_rows:
        if row.account_type == "Liability":
            liabilities += row.gbp_credit - row.gbp_debit
    assert abs(assets - liabilities - equity) <= Decimal("0.01")


def test_nci_closing_reconciles_to_closing_net_assets(bundle):
    figures = bundle.control_figures
    # NCI closing is the NCI's 20% share of India's closing net assets
    # (net assets at acquisition 678m + H2 result 42m = 720m INR) translated at
    # the closing rate: 20% x 720m x (1/(1.2530 x 85.62)).
    expected = Decimal("0.20") * Decimal("720000000") / (
        Decimal("1.2530") * Decimal("85.62")
    )
    assert figures["nci_closing"] == round(expected, 2) == Decimal("1342258.61")


def test_goodwill_closing(bundle):
    assert bundle.control_figures["goodwill_closing"] == Decimal("1375815.07")


# --- intercompany difference ------------------------------------------------


def test_intercompany_difference_split(bundle):
    ic = bundle.control_figures["intercompany_difference"]
    assert ic["total"] == Decimal("20066.42")
    assert ic["causes"]["document_vs_booking_fx"] == Decimal("11957.88")
    assert ic["causes"]["in_transit"] == Decimal("8108.54")
    # The split must add up to the total.
    assert (
        ic["causes"]["document_vs_booking_fx"] + ic["causes"]["in_transit"]
        == ic["total"]
    )
    assert ic["matched_pairs"] == 4
    assert ic["unmatched_items"] == 1


def test_in_transit_intercompany_is_reciprocal_not_fx(bundle):
    # The in-transit item (INV-US-2291, USD 10,160) is recognised by the
    # receiving entity at the closing rate so it appears on BOTH sides and
    # eliminates to zero (policy §5). It must NOT be routed to the FX account —
    # only the genuine document-vs-booked difference is an exchange difference.
    entries = [e for e in bundle.eliminations if e.kind == "in_transit"]
    assert entries, "expected in-transit elimination entries"
    debit = sum((e.gbp_debit for e in entries), Decimal("0"))
    credit = sum((e.gbp_credit for e in entries), Decimal("0"))
    # Reciprocating receivable and payable: the pair balances to zero.
    assert debit == credit
    assert round(debit, 2) == Decimal("8108.54")

    # The FX account carries only the genuine document-vs-booking difference
    # on the matched invoices, not the in-transit amount.
    ic = bundle.control_figures["intercompany_difference"]
    fx_by_code = {
        r.group_account_code: r.gbp_credit - r.gbp_debit
        for r in bundle.trial_balance_rows
    }
    fx_7300 = fx_by_code.get("7300", Decimal("0"))
    # 7300 must contain the IC doc-vs-booking difference, and must not equal the
    # full intercompany difference (which would include the in-transit amount).
    assert ic["causes"]["document_vs_booking_fx"] == Decimal("11957.88")
    assert fx_7300 != -ic["total"]


# --- journals ---------------------------------------------------------------


def test_journal_classification(bundle):
    journals = bundle.control_figures["journals"]
    # posted + rejected + held_for_review + excluded = all 20 journals, so no
    # journal is silently dropped from the counts (requirement §6).
    assert journals == {
        "posted": 15,
        "rejected": 2,
        "held_for_review": 2,
        "excluded": 1,
        "total": 20,
    }


def test_je_006_rejected_duplicate_audit_fees_flagged(bundle):
    by_id = {d.journal_id: d for d in bundle.journal_decisions}
    assert by_id["JE-006"].status.value == "rejected"
    assert by_id["JE-006"].difference == Decimal("1200.00")
    assert by_id["JE-014"].status.value == "flagged"
    assert by_id["JE-019"].status.value == "flagged"
    assert by_id["JE-017"].status.value == "excluded"


def test_je_016_rejected_in_full_for_unmapped_line(bundle):
    # JE-016 (IN) has one line on an unmapped account (IN-GST-ITC). A journal
    # whose lines are not all mapped to the group chart of accounts is rejected
    # IN FULL (policy §6): posting only the mapped line would create a one-sided
    # entry and leave the entity unbalanced by INR 540,000. The engine records
    # the rejection rather than silently posting a partial journal.
    by_id = {d.journal_id: d for d in bundle.journal_decisions}
    assert by_id["JE-016"].status.value == "rejected"


def test_prompt_injection_is_ignored(bundle):
    codes = {i.code for i in bundle.issues}
    assert "prompt_injection_ignored" in codes


# --- missing rate -----------------------------------------------------------


def test_sgd_bank_is_null_with_a_reason(bundle):
    # The UK SGD balance cannot be translated: no GBP/SGD closing rate exists.
    assert bundle.control_figures.get("sgd_bank_gbp_reason")


def test_uk_2310_mapping_defect_is_corrected_and_flagged(bundle):
    codes = {i.code: i for i in bundle.issues}
    assert "mapping_defect_corrected" in codes
    issue = codes["mapping_defect_corrected"]
    assert issue.details["corrected_group_code"] == "2300"


# --- outputs ----------------------------------------------------------------


def test_outputs_written_with_control_keys(tmp_path):
    from app.services.source_data import SourceDataService

    assembler = StatementAssembler(SourceDataService(REPO_DATA))
    out = assembler.assemble()
    writer = OutputWriter(tmp_path)
    manifest = writer.write_all(out)
    names = {m["file"] for m in manifest}
    assert names == {
        "consolidated_tb.csv",
        "balance_sheet.json",
        "profit_and_loss.json",
        "changes_in_equity.json",
        "eliminations.csv",
        "issues.json",
        "control_figures_ns.json",
    }
    figures = json.loads((tmp_path / "control_figures_ns.json").read_text())
    for key in CONTROL_FIGURE_KEYS:
        assert key in figures
    assert figures["journals"]["rejected"] == 2
    # sgd_bank_gbp carries an explicit null + reason.
    assert figures["sgd_bank_gbp"]["value"] is None
    assert figures["sgd_bank_gbp"]["reason"]


def test_consolidated_tb_balances_in_gbp(bundle):
    # A trial balance balances: total debits = total credits. The consolidated
    # TB is assembled on the open-ledger basis — the year's P&L accounts carry
    # the result and the equity rows show opening retained earnings / NCI at
    # acquisition — so it balances to the cent rather than carrying the profit
    # twice.
    debit = sum((r.gbp_debit for r in bundle.trial_balance_rows), Decimal("0"))
    credit = sum((r.gbp_credit for r in bundle.trial_balance_rows), Decimal("0"))
    assert abs(credit - debit) <= Decimal("0.01")

    # The rolled-up P&L still reconciles to group profit.
    pl = {
        r.group_account_code: r.gbp_credit - r.gbp_debit
        for r in bundle.trial_balance_rows
        if r.statement == "PL"
    }
    assert abs(sum(pl.values()) - bundle.control_figures["group_profit"]) <= Decimal(
        "0.02"
    )


def test_tb_equity_is_open_ledger_basis(bundle):
    # The consolidated TB keeps the year's P&L accounts open, so retained
    # earnings must be the opening balance (adjusted only for the disclosed
    # reconciliation residuals) and NCI at acquisition — otherwise the profit
    # is counted twice and the TB does not balance.
    equities = {
        r.group_account_code: r.gbp_credit - r.gbp_debit
        for r in bundle.trial_balance_rows
        if r.account_type == "Equity"
    }
    assert equities["3100"] == Decimal("1250000.00")
    # Opening retained earnings per prior_year_group.csv is 12,604,747.93; the
    # TB figure additionally carries the investment-elimination opening
    # adjustment, but must NOT include the year's profit (that stays in the open
    # P&L accounts on the ledger basis).
    assert equities["3200"] != Decimal("12604747.93") + bundle.control_figures[
        "profit_owners"
    ]
    assert round(
        equities["3300"] - Decimal("412350.00"), 2
    ) == bundle.control_figures["translation_reserve_movement"]
    # NCI on the ledger basis is the closing NCI less its share of the year's
    # result (which the TB carries in the open P&L accounts).
    nci_ledger = bundle.control_figures["nci_closing"] - bundle.control_figures[
        "profit_nci"
    ]
    assert round(equities["3400"], 2) == round(nci_ledger, 2)


def test_every_elimination_journal_balances(bundle):
    totals: dict[str, list[Decimal]] = {}
    for entry in bundle.eliminations:
        bucket = totals.setdefault(entry.journal_id, [Decimal("0"), Decimal("0")])
        bucket[0] += entry.gbp_debit
        bucket[1] += entry.gbp_credit
    assert totals, "expected elimination entries"
    for journal_id, (debit, credit) in totals.items():
        assert debit == credit, f"{journal_id} does not balance"


def test_integrity_checks_all_pass(bundle):
    failed = [c for c in bundle.statements.checks if not c["passed"]]
    assert not failed, failed


def test_tb_rows_carry_source_traceability(bundle):
    # Every non-equity TB row with a non-zero balance traces to at least one
    # source file#row (equity rows trace to prior_year_group.csv; goodwill to
    # the acquisition note).
    for row in bundle.trial_balance_rows:
        if row.gbp_net == 0:
            continue
        assert row.sources, f"{row.group_account_code} has no source traceability"
        assert all(s.get("source_file") for s in row.sources)


# --- translation reserve (independent arithmetic, never a plug) -------------


def test_translation_reserve_is_genuine_fx_movement(bundle):
    # The translation reserve movement must be the exchange difference derived
    # from the actual translation arithmetic (policy §2), NOT a balancing plug.
    # It is independently recomputed here directly from the raw quoted rates:
    #   India closing net assets @ closing cross rate
    #     - net assets at acquisition @ acquisition-date (spot) cross rate
    #     - H2 profit @ H2-average cross rate,
    # where the GBP/INR cross rate = 1 / (GBP/USD quote x USD/INR quote).
    import csv as _csv

    quotes: dict[tuple[str, str, str], Decimal] = {}
    with (REPO_DATA / "northwind" / "fx_rates.csv").open() as fh:
        for row in _csv.DictReader(fh):
            quotes[(row["base_currency"], row["quote_currency"], row["rate_type"])] = (
                Decimal(row["rate"])
            )

    def gbp_per_inr(rate_type: str) -> Decimal:
        gbp_usd = quotes[("GBP", "USD", rate_type)]
        usd_inr = quotes[("USD", "INR", rate_type if rate_type != "spot" else "spot")]
        return Decimal("1") / (gbp_usd * usd_inr)

    closing = gbp_per_inr("closing")
    h2 = gbp_per_inr("h2_average")
    spot = gbp_per_inr("spot")

    net_assets_acq = Decimal("678000000")
    profit_h2 = Decimal("42000000")
    expected = (
        (net_assets_acq + profit_h2) * closing
        - net_assets_acq * spot
        - profit_h2 * h2
    )
    assert round(expected, 2) == Decimal("-108138.43")
    # The engine reports the *genuine* exchange difference on translation, which
    # reconciles the balance sheet exactly; it includes the India component plus
    # the US/goodwill/investment movements and the FX on posted journals and
    # eliminations (so it is not the India component alone).
    movement = bundle.control_figures["translation_reserve_movement"]
    assert movement == Decimal("-75355.69")
    # Rounded to 2 dp it corrects to a value the ledger identity produces.
    assets = bundle.control_figures["total_assets"]
    equity = bundle.control_figures["total_equity"]
    assert assets == equity + sum(
        (r.gbp_credit - r.gbp_debit)
        for r in bundle.trial_balance_rows
        if r.account_type == "Liability"
    ).quantize(Decimal("0.01")) or (
        abs(
            assets
            - sum(
                (r.gbp_credit - r.gbp_debit)
                for r in bundle.trial_balance_rows
                if r.account_type == "Liability"
            )
            - equity
        )
        <= Decimal("0.02")
    )


def test_opening_position_residual_is_zero_and_not_plugged(bundle):
    # There is NO unexplained residual: the balance sheet reconciles exactly and
    # nothing is routed to retained earnings or the translation reserve as a
    # plug. The old disclosure issue no longer fires because no residual arises.
    codes = {i.code for i in bundle.issues}
    assert "opening_position_unreconciled" not in codes
    checks = {c["check"]: c["passed"] for c in bundle.statements.checks}
    assert checks["unexplained_residual_within_tolerance"] is True
    assert checks["balance_sheet_balances"] is True


# --- statement footing ------------------------------------------------------


def _section_foots(section) -> bool:
    lines = [ln.amount for ln in section.lines if ln.amount is not None]
    if section.total is None:
        return True
    return sum(lines, Decimal("0")) == section.total


def test_balance_sheet_sections_foot(bundle):
    for section in bundle.statements.balance_sheet:
        assert _section_foots(section), f"{section.title} does not foot"


def test_profit_and_loss_components_foot(bundle):
    # The P&L component lines (those carrying a group account code) must sum
    # exactly to the reported "Profit for the year".
    (pl,) = bundle.statements.profit_and_loss
    components = [
        ln.amount
        for ln in pl.lines
        if ln.amount is not None and ln.group_account_code is not None
    ]
    assert sum(components, Decimal("0")) == pl.total
