"""Accounting Stage 1 tests: TB ingestion, validation and FX infrastructure.

Two data sources are used deliberately:

* the **real** immutable dataset under ``data/northwind`` (read-only) for the
  schema-faithful ingestion, balancing and FX tests — expected values are
  derived by hand from the actual source rows and the derivation is written
  into each test;
* the **synthetic** ``northwind_data_dir`` fixture for malformed-input and
  edge-case tests, so error behaviour is exercised without depending on
  (or touching) the real files.

No expected financial figure is invented: every expected Decimal below is
either read directly from a source cell or computed by an explicit formula
shown in a comment.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.errors import (
    InvalidNumberError,
    MissingFXRateError,
    UnknownEntityError,
    UnknownSchemaError,
)
from app.accounting.fx import FXService
from app.accounting.ingestion import TrialBalanceIngestor
from app.accounting.models import FXRateType, round_reported
from app.accounting.validation import TrialBalanceValidator
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def real_service() -> SourceDataService:
    """SourceDataService over the real immutable dataset."""
    if not (REPO_DATA / "northwind").is_dir():
        pytest.skip("real dataset not present")
    return SourceDataService(REPO_DATA)


@pytest.fixture
def ingestor(real_service: SourceDataService) -> TrialBalanceIngestor:
    return TrialBalanceIngestor(real_service)


@pytest.fixture
def fx(real_service: SourceDataService) -> FXService:
    return FXService(real_service)


# --- US ingestion -----------------------------------------------------------


def test_us_ingestion_parses_schema_and_rows(ingestor: TrialBalanceIngestor):
    tb = ingestor.ingest("US")

    assert tb.entity == "US"
    assert tb.functional_currency == "USD"
    assert tb.source_file == "tb_us.csv"
    # tb_us.csv has 25 data rows (header excluded).
    assert len(tb.rows) == 25

    first = tb.rows[0]
    assert first.local_account_code == "1100"
    assert first.account_name == "Cash and cash equivalents"
    assert first.currency == "USD"
    # Source row 1 is the first data line: account_code 1100, debit 4875000.00
    assert first.source_row == 1
    assert first.debit == Decimal("4875000.00")
    assert first.credit == Decimal("0.00")
    # original values preserved verbatim
    assert first.raw["account_code"] == "1100"
    assert first.raw["debit"] == "4875000.00"


def test_us_credit_row_maps_to_credit_column(ingestor: TrialBalanceIngestor):
    tb = ingestor.ingest("US")
    # Counted from tb_us.csv: header is file line 1, so 4100,Revenue,,18400000.00
    # (file line 18) is data row 17.
    revenue = next(r for r in tb.rows if r.local_account_code == "4100")
    assert revenue.debit == Decimal("0.00")
    assert revenue.credit == Decimal("18400000.00")
    assert revenue.source_row == 17


# --- UK ingestion -----------------------------------------------------------


def test_uk_ingestion_signed_balance_split(ingestor: TrialBalanceIngestor):
    tb = ingestor.ingest("UK")

    assert tb.functional_currency == "GBP"
    assert tb.source_file == "tb_uk.csv"
    assert len(tb.rows) == 23

    # First row: 1200,Bank - Barclays GBP,,GBP,,1240000.00  -> debit 1240000.00
    bank = tb.rows[0]
    assert bank.local_account_code == "1200"
    assert bank.debit == Decimal("1240000.00")
    assert bank.credit == Decimal("0.00")
    assert bank.signed_balance == Decimal("1240000.00")

    # A negative balance is a credit: 0031,Plant and machinery - depreciation,
    # ,GBP,,-1060000.00
    dep = next(r for r in tb.rows if r.local_account_code == "0031")
    assert dep.signed_balance == Decimal("-1060000.00")
    assert dep.debit == Decimal("0.00")
    assert dep.credit == Decimal("1060000.00")


def test_uk_foreign_currency_row_preserves_txn_detail(ingestor: TrialBalanceIngestor):
    tb = ingestor.ingest("UK")
    # 1210,Bank - DBS SGD,,SGD,40000.00,23010.00
    dbs = next(r for r in tb.rows if r.local_account_code == "1210")
    assert dbs.currency == "GBP"  # functional
    assert dbs.txn_currency == "SGD"
    assert dbs.txn_amount == Decimal("40000.00")
    assert dbs.debit == Decimal("23010.00")


def test_uk_two_cost_centres_are_distinct_rows(ingestor: TrialBalanceIngestor):
    tb = ingestor.ingest("UK")
    wages = [r for r in tb.rows if r.local_account_code == "7000"]
    assert len(wages) == 2
    assert {r.cost_centre for r in wages} == {"CC-OPS", "CC-SALES"}
    assert [r.source_row for r in wages] == [17, 18]


# --- India ingestion --------------------------------------------------------


def test_in_ingestion_dr_cr_flag_mapping(ingestor: TrialBalanceIngestor):
    tb = ingestor.ingest("IN")

    assert tb.functional_currency == "INR"
    assert tb.source_file == "tb_in.csv"
    assert len(tb.rows) == 20

    # IN-BNK-01,HDFC Bank current account,FY25 YTD,285000000.00,Dr
    bank = tb.rows[0]
    assert bank.local_account_code == "IN-BNK-01"
    assert bank.debit == Decimal("285000000.00")
    assert bank.credit == Decimal("0.00")

    # IN-REV,Revenue from operations,FY25 YTD,642000000.00,Cr
    rev = next(r for r in tb.rows if r.local_account_code == "IN-REV")
    assert rev.debit == Decimal("0.00")
    assert rev.credit == Decimal("642000000.00")


# --- Decimal precision ------------------------------------------------------


def test_values_are_decimal_not_float(ingestor: TrialBalanceIngestor):
    tb = ingestor.ingest("IN")
    for row in tb.rows:
        assert isinstance(row.debit, Decimal)
        assert isinstance(row.credit, Decimal)
    # 8274373.43 (US investment) must survive exactly, with no binary drift.
    us = TrialBalanceIngestor(SourceDataService(REPO_DATA)).ingest("US")
    inv = next(r for r in us.rows if r.local_account_code == "1610")
    assert inv.debit == Decimal("8274373.43")


# --- Validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "entity,expected_total",
    [
        # Derived by summing the debit column of each source file (and, for
        # UK/IN, the debit side of the signed amount / dr_cr flag).
        ("US", Decimal("43534373.43")),
        ("UK", Decimal("15688717.72")),
        ("IN", Decimal("1805000000.00")),
    ],
)
def test_all_entities_balance(ingestor: TrialBalanceIngestor, entity, expected_total):
    tb = ingestor.ingest(entity)
    result = TrialBalanceValidator().validate(tb)

    assert result.balanced is True
    assert result.difference == Decimal("0.00")
    assert result.debit_total == expected_total
    assert result.credit_total == expected_total
    assert result.error_count == 0


def test_unbalanced_trial_balance_is_detected_not_corrected(northwind_data_dir):
    # Replace US TB with an unbalanced one and assert the engine reports it.
    tb_path = northwind_data_dir / "northwind" / "tb_us.csv"
    tb_path.write_text(
        "account_code,account_name,debit,credit\n"
        "1000,Cash,100.00,\n"
        "4000,Revenue,,80.00\n",
        encoding="utf-8",
    )
    ingestor = TrialBalanceIngestor(SourceDataService(northwind_data_dir))
    tb = ingestor.ingest("US")
    result = TrialBalanceValidator().validate(tb)

    assert result.balanced is False
    assert result.difference == Decimal("20.00")  # 100.00 - 80.00
    codes = [i.code for i in result.issues]
    assert "unbalanced_trial_balance" in codes
    # not silently corrected: rows and totals preserved as-is
    assert result.row_count == 2
    assert result.debit_total == Decimal("100.00")


def test_empty_cell_treated_as_zero(northwind_data_dir):
    # A blank numeric cell must parse as Decimal zero, never crash. The
    # synthetic US fixture stores a blank credit on its debit row.
    ingestor = TrialBalanceIngestor(SourceDataService(northwind_data_dir))
    tb = ingestor.ingest("US")
    cash = next(r for r in tb.rows if r.local_account_code == "1000")
    assert cash.debit == Decimal("125000.00")
    assert cash.credit == Decimal("0")
    assert cash.raw["credit"] == ""  # source cell really was empty


def test_invalid_numeric_value_raises_typed_error(northwind_data_dir):
    tb_path = northwind_data_dir / "northwind" / "tb_us.csv"
    tb_path.write_text(
        "account_code,account_name,debit,credit\n"
        "1000,Cash,not-a-number,\n",
        encoding="utf-8",
    )
    ingestor = TrialBalanceIngestor(SourceDataService(northwind_data_dir))
    with pytest.raises(InvalidNumberError) as exc:
        ingestor.ingest("US")
    assert exc.value.code == "invalid_number"
    assert exc.value.details["source_row"] == 1
    assert exc.value.details["column"] == "debit"


def test_invalid_dr_cr_flag_raises_typed_error(northwind_data_dir):
    tb_path = northwind_data_dir / "northwind" / "tb_in.csv"
    tb_path.write_text(
        "ledger_code,ledger_name,period,amount,dr_cr\n"
        "IN-X,Thing,FY25,100.00,Maybe\n",
        encoding="utf-8",
    )
    ingestor = TrialBalanceIngestor(SourceDataService(northwind_data_dir))
    with pytest.raises(InvalidNumberError):
        ingestor.ingest("IN")


def test_duplicate_rows_flagged_but_cost_centre_splits_are_not(northwind_data_dir):
    tb_path = northwind_data_dir / "northwind" / "tb_uk.csv"
    tb_path.write_text(
        "nominal_code,description,cost_centre,txn_currency,txn_amount,balance\n"
        "7000,Sales,CC-A,GBP,1.00,-1.00\n"
        "7000,Sales,CC-A,GBP,1.00,-1.00\n"  # exact duplicate key (7000, CC-A)
        "7000,Sales,CC-B,GBP,2.00,-2.00\n",  # same code, different cost centre
        encoding="utf-8",
    )
    ingestor = TrialBalanceIngestor(SourceDataService(northwind_data_dir))
    result = TrialBalanceValidator().validate(ingestor.ingest("UK"))

    dupes = [i for i in result.issues if i.code == "duplicate_row"]
    assert len(dupes) == 1
    assert dupes[0].details["key"] == ["7000", "CC-A"]
    assert dupes[0].details["first_row"] == 1
    assert dupes[0].source_row == 2


def test_unknown_entity_raises_typed_error(ingestor: TrialBalanceIngestor):
    with pytest.raises(UnknownEntityError) as exc:
        ingestor.ingest("FR")
    assert exc.value.code == "unknown_entity"


def test_wrong_schema_raises_typed_error(northwind_data_dir):
    tb_path = northwind_data_dir / "northwind" / "tb_us.csv"
    tb_path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    ingestor = TrialBalanceIngestor(SourceDataService(northwind_data_dir))
    with pytest.raises(UnknownSchemaError) as exc:
        ingestor.ingest("US")
    assert exc.value.code == "unknown_tb_schema"


# --- FX: direct -------------------------------------------------------------


def test_fx_direct_conversion_from_source(fx: FXService):
    # fx_rates.csv: GBP,USD,closing,1.2530,2024-12-31,USD per 1 GBP
    resolved = fx.resolve("GBP", "USD", FXRateType.CLOSING)
    assert resolved.rate == Decimal("1.2530")
    assert resolved.method == "direct"
    # 1000 GBP -> 1253.0 USD
    assert fx.convert(Decimal("1000"), "GBP", "USD", "closing") == Decimal("1253.0")


def test_fx_identity_rate(fx: FXService):
    resolved = fx.resolve("GBP", "GBP", FXRateType.CLOSING)
    assert resolved.rate == Decimal("1")
    assert resolved.method == "identity"


# --- FX: inverse ------------------------------------------------------------


def test_fx_inverse_conversion(fx: FXService):
    # No USD->GBP closing row exists; it is the exact reciprocal of the
    # recorded GBP->USD closing 1.2530.
    resolved = fx.resolve("USD", "GBP", FXRateType.CLOSING)
    assert resolved.method == "inverse"
    assert resolved.rate == Decimal("1") / Decimal("1.2530")
    # round-trip: 1253.0 USD * (1/1.2530) = 1000 GBP (full precision)
    back = fx.convert(Decimal("1253.0"), "USD", "GBP", "closing")
    assert back == Decimal("1000")


# --- FX: cross-rate (policy example) ---------------------------------------


def test_fx_cross_rate_matches_policy_example(fx: FXService):
    # group_policies.md §2: GBP/INR closing = GBP/USD closing x USD/INR closing
    #                      = 1.2530 x 85.62
    resolved = fx.resolve("GBP", "INR", FXRateType.CLOSING)
    assert resolved.method == "cross"
    assert resolved.via == "USD"
    assert resolved.rate == Decimal("1.2530") * Decimal("85.62")
    assert resolved.rate == Decimal("107.281860")
    # conversion keeps full precision (no rounding in the engine)
    assert fx.convert(Decimal("1000"), "GBP", "INR", "closing") == Decimal("107281.860000")


def test_fx_cross_rate_h2_average(fx: FXService):
    # GBP/USD h2_average 1.2840 x USD/INR h2_average 83.95 = 107.7918
    resolved = fx.resolve("GBP", "INR", FXRateType.H2_AVERAGE)
    assert resolved.method == "cross"
    assert resolved.rate == Decimal("1.2840") * Decimal("83.95")
    assert resolved.rate == Decimal("107.791800")


# --- FX: missing rates ------------------------------------------------------


def test_fx_missing_rate_raises_typed_error(fx: FXService):
    # fx_rates.csv records GBP/SGD for opening and average only — there is no
    # SGD closing leg at all, so SGD->GBP closing cannot be resolved directly,
    # inversely or via a cross rate.
    with pytest.raises(MissingFXRateError) as exc:
        fx.resolve("SGD", "GBP", FXRateType.CLOSING)
    assert exc.value.code == "missing_fx_rate"


def test_fx_zero_rate_is_not_missing(northwind_data_dir):
    # A recorded zero rate is a valid Decimal zero, distinct from missing.
    fx_path = northwind_data_dir / "northwind" / "fx_rates.csv"
    fx_path.write_text(
        "base_currency,quote_currency,rate_type,rate,as_of,convention\n"
        "GBP,USD,closing,0.0000,2024-12-31,USD per 1 GBP\n",
        encoding="utf-8",
    )
    fx = FXService(SourceDataService(northwind_data_dir))
    resolved = fx.resolve("GBP", "USD", FXRateType.CLOSING)
    assert resolved.rate == Decimal("0.0000")
    # inverting a zero rate is impossible -> typed error, not a fabricated rate
    with pytest.raises(MissingFXRateError):
        fx.resolve("USD", "GBP", FXRateType.CLOSING)


# --- Rounding policy --------------------------------------------------------


def test_round_reported_to_two_decimals_half_up():
    # policy §7: round each reported figure to 2 dp.
    assert round_reported(Decimal("107.281860")) == Decimal("107.28")
    assert round_reported(Decimal("1.2530") * Decimal("85.62")) == Decimal("107.28")
    # HALF_UP: 2.675 -> 2.68 (not banker's rounding)
    assert round_reported(Decimal("2.675")) == Decimal("2.68")
    assert round_reported(Decimal("107.281860")).as_tuple().exponent == -2


def test_engine_keeps_full_precision_until_reporting_boundary(fx: FXService):
    raw = fx.convert(Decimal("1000"), "GBP", "INR", "closing")
    # full precision preserved
    assert raw == Decimal("107281.860000")
    # only the reporting boundary rounds
    assert round_reported(raw) == Decimal("107281.86")
