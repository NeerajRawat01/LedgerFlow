"""FX translation tests (Stage 3) over the real immutable dataset.

Expected values are derived by hand from ``data/northwind`` and the derivation
is written into each test. No figure is invented:

* UK functional currency == presentation currency, so translation is the
  identity and every GBP amount equals the mapped functional amount.
* US converts USD -> GBP. The dataset records GBP/USD (USD per 1 GBP), so the
  USD -> GBP rate is its inverse: ``1 / 1.2530`` at closing, ``1 / 1.2780`` at
  the FY2024 average.
* India is acquired 1 July 2024, so its income/expense rows use the
  ``h2_average`` rate type (policy §2), resolved USD->INR give INR per USD, so
  INR -> GBP is a cross: ``(1 / GBP_USD) * (1 / USD_INR)``.

No rounding is applied (policy §7 rounds only reported figures).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.fx import FXService
from app.accounting.ingestion import TrialBalanceIngestor
from app.accounting.mapping import AccountMapper
from app.accounting.models import FXRateType, Statement
from app.accounting.translation import FXTranslator
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def real_service() -> SourceDataService:
    if not (REPO_DATA / "northwind").is_dir():
        pytest.skip("real dataset not present")
    return SourceDataService(REPO_DATA)


@pytest.fixture
def mapper(real_service: SourceDataService) -> AccountMapper:
    return AccountMapper(real_service)


@pytest.fixture
def fx(real_service: SourceDataService) -> FXService:
    return FXService(real_service)


@pytest.fixture
def translator(real_service, fx, mapper) -> FXTranslator:
    return FXTranslator(fx=fx, group_coa=mapper.group_coa)


def _mapped(real_service, mapper, entity: str):
    tb = TrialBalanceIngestor(real_service).ingest(entity)
    return mapper.map_trial_balance(tb)


# --- policy selection -------------------------------------------------------


def test_full_year_entity_uses_average_rate():
    policy = FXTranslator.policy_for_entity(
        "US", "USD", acquisition_date=None
    )
    assert policy.income_rate_type is FXRateType.AVERAGE


def test_uk_is_included_for_full_year():
    # entities.csv records UK acquisition 2016-03-01 (well before FY2024).
    policy = FXTranslator.policy_for_entity(
        "UK", "GBP", acquisition_date="2016-03-01"
    )
    assert policy.income_rate_type is FXRateType.AVERAGE


def test_india_midyear_acquisition_uses_h2_average():
    # entities.csv records IN acquisition 2024-07-01, i.e. a mid-year (H2)
    # inclusion, so income/expenses translate at the H2 average (policy §2).
    policy = FXTranslator.policy_for_entity(
        "IN", "INR", acquisition_date="2024-07-01"
    )
    assert policy.income_rate_type is FXRateType.H2_AVERAGE


# --- UK: identity translation ----------------------------------------------


def test_uk_translation_is_identity(real_service, mapper, translator):
    mapped = _mapped(real_service, mapper, "UK")
    policy = FXTranslator.policy_for_entity(
        "UK", "GBP", acquisition_date="2016-03-01"
    )
    result = translator.translate(mapped, policy)

    assert result.translated
    assert result.presentation_currency == "GBP"
    # Every translated GBP amount must equal its functional amount exactly.
    for row in result.rows:
        assert row.gbp_debit == row.debit
        assert row.gbp_credit == row.credit
        assert row.rate == Decimal("1")


def test_uk_equity_rows_are_not_translated(real_service, mapper, translator):
    # UK 3000 Share capital and 3200 P&L reserve are Equity; the assembly
    # stage handles them at historical rates, so the translator omits them.
    mapped = _mapped(real_service, mapper, "UK")
    policy = FXTranslator.policy_for_entity(
        "UK", "GBP", acquisition_date="2016-03-01"
    )
    result = translator.translate(mapped, policy)
    equity_codes = {
        c
        for c, a in mapper.group_coa.items()
        if a.account_type == "Equity"
    }
    assert not any(r.group_account_code in equity_codes for r in result.rows)


# --- US: USD -> GBP at inverse rates ---------------------------------------


def test_us_cash_translates_at_closing_inverse(real_service, mapper, translator):
    # US 1100 Cash = debit USD 4,875,000 (tb_us.csv row 1).
    # GBP/USD closing = 1.2530 (USD per GBP) -> USD->GBP = 1 / 1.2530.
    mapped = _mapped(real_service, mapper, "US")
    policy = FXTranslator.policy_for_entity("US", "USD", acquisition_date=None)
    result = translator.translate(mapped, policy)

    cash = next(r for r in result.rows if r.group_account_code == "1100")
    expected_rate = Decimal("1") / Decimal("1.2530")
    assert cash.rate_type == "closing"
    assert cash.rate == expected_rate
    assert cash.gbp_debit == Decimal("4875000.00") * expected_rate


def test_us_revenue_translates_at_average_inverse(real_service, mapper, translator):
    # US 4100 Revenue = credit USD 18,400,000 (tb_us.csv row 18).
    # Average GBP/USD = 1.2780 -> USD->GBP = 1 / 1.2780.
    mapped = _mapped(real_service, mapper, "US")
    policy = FXTranslator.policy_for_entity("US", "USD", acquisition_date=None)
    result = translator.translate(mapped, policy)

    revenue = next(r for r in result.rows if r.group_account_code == "4100")
    expected_rate = Decimal("1") / Decimal("1.2780")
    assert revenue.rate_type == "average"
    assert revenue.gbp_credit == Decimal("18400000.00") * expected_rate


def test_pl_rows_use_average_bs_rows_use_closing(real_service, mapper, translator):
    mapped = _mapped(real_service, mapper, "US")
    policy = FXTranslator.policy_for_entity("US", "USD", acquisition_date=None)
    result = translator.translate(mapped, policy)

    for row in result.rows:
        if row.statement == Statement.PROFIT_AND_LOSS.value:
            assert row.rate_type == "average"
        elif row.statement == Statement.BALANCE_SHEET.value:
            assert row.rate_type == "closing"


# --- India: cross-currency translation at H2 average for P&L ---------------


def test_india_revenue_uses_cross_and_h2_average(real_service, mapper, translator):
    # IN IN-REV (-> group 4100) = credit INR 642,000,000 (tb_in.csv row 14).
    # Policy §2 (cross): INR->GBP = (GBP/USD) * (USD/INR) using the same type.
    # GBP/USD h2_average = 1.2840; USD/INR h2_average = 83.95.
    # INR->GBP = (1 / 1.2840) * (1 / 83.95).
    mapped = _mapped(real_service, mapper, "IN")
    policy = FXTranslator.policy_for_entity(
        "IN", "INR", acquisition_date="2024-07-01"
    )
    result = translator.translate(mapped, policy)

    revenue = next(r for r in result.rows if r.group_account_code == "4100")
    expected_rate = (Decimal("1") / Decimal("1.2840")) * (Decimal("1") / Decimal("83.95"))
    assert revenue.rate_type == "h2_average"
    assert revenue.rate == expected_rate
    assert revenue.gbp_credit == Decimal("642000000.00") * expected_rate


def test_india_ppe_uses_closing_cross(real_service, mapper, translator):
    # IN IN-PPE (-> 1500) = debit INR 520,000,000 (tb_in.csv row 5).
    # GBP/USD closing 1.2530; USD/INR closing 85.62.
    # INR->GBP closing = (1 / 1.2530) * (1 / 85.62).
    mapped = _mapped(real_service, mapper, "IN")
    policy = FXTranslator.policy_for_entity(
        "IN", "INR", acquisition_date="2024-07-01"
    )
    result = translator.translate(mapped, policy)

    ppe = next(r for r in result.rows if r.group_account_code == "1500")
    expected_rate = (Decimal("1") / Decimal("1.2530")) * (Decimal("1") / Decimal("85.62"))
    assert ppe.rate_type == "closing"
    assert ppe.rate == expected_rate


# --- traceability & honesty -------------------------------------------------


def test_translated_rows_preserve_source_traceability(real_service, mapper, translator):
    mapped = _mapped(real_service, mapper, "US")
    policy = FXTranslator.policy_for_entity("US", "USD", acquisition_date=None)
    result = translator.translate(mapped, policy)
    for row in result.rows:
        assert row.entity == "US"
        assert row.source_file == "tb_us.csv"
        assert row.source_row >= 1
        assert row.local_account_code


def test_missing_rate_is_reported_never_invented(real_service, mapper, fx):
    """A missing closing rate must leave rows un-translated and raise an issue.

    The source records GBP/SGD opening and average but NO closing rate. A
    translator asked to convert a SGD balance at closing must therefore fail
    honestly rather than fabricate a rate. We exercise this with a synthetic
    mapped balance in SGD.
    """
    from app.accounting.models import MappedRow, MappedTrialBalance
    from app.accounting.models import GroupAccount

    coa = dict(mapper.group_coa)
    coa["1100"] = GroupAccount(
        account_code="1100",
        account_name="Cash and cash equivalents",
        account_type="Asset",
        statement="BS",
        cash_flow_category="Cash",
        normal_balance="Debit",
    )
    translator = FXTranslator(fx=fx, group_coa=coa)
    mapped = MappedTrialBalance(
        entity="XX",
        functional_currency="SGD",
        source_file="synthetic.csv",
        rows=[
            MappedRow(
                entity="XX",
                source_file="synthetic.csv",
                source_row=1,
                local_account_code="9900",
                group_account_code="1100",
                group_account_name="Cash and cash equivalents",
                currency="SGD",
                debit=Decimal("40000.00"),
                credit=Decimal("0"),
                mapped=True,
            )
        ],
    )
    policy = FXTranslator.policy_for_entity("XX", "SGD", acquisition_date=None)
    result = translator.translate(mapped, policy)

    assert not result.translated
    assert result.rows == []
    assert any(i.code == "missing_fx_rate" for i in result.issues)
    # And crucially: no GBP figure was produced for the row.
    assert result.total_gbp_debit == Decimal("0")
