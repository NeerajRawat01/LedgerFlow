"""Account mapping tests: local chart of accounts -> group chart of accounts.

Real source files under ``data/northwind`` are used for the UK/IN/US mapping
behaviour; expected group codes are read directly from ``map_uk.csv`` /
``map_in.csv`` / ``group_coa.csv`` and written into each test. Synthetic
per-entity fixtures (via ``write_northwind_fixture``) exercise isolated edge
cases such as conflicting or malformed mapping records.

No expected mapping is invented.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.ingestion import TrialBalanceIngestor
from app.accounting.mapping import AccountMapper
from app.accounting.models import Severity, TrialBalance, TrialBalanceRow
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
def ingestor(real_service: SourceDataService) -> TrialBalanceIngestor:
    return TrialBalanceIngestor(real_service)


# --- 1. UK mapping ----------------------------------------------------------


def test_uk_mapping_uses_source_codes(mapper: AccountMapper, ingestor: TrialBalanceIngestor):
    # map_uk.csv: 1200->1100, 4000->4100, 8210->6300, 9500->8100
    mtb = mapper.map_trial_balance(ingestor.ingest("UK"))
    by_local = {r.local_account_code: r for r in mtb.rows}

    assert mtb.mapping_source_file == "map_uk.csv"
    assert by_local["1200"].group_account_code == "1100"
    assert by_local["1200"].group_account_name == "Cash and cash equivalents"
    assert by_local["4000"].group_account_code == "4100"
    assert by_local["8210"].group_account_code == "6300"
    assert by_local["9500"].group_account_code == "8100"
    assert mtb.unmapped_row_count == 0


# --- 2. India mapping -------------------------------------------------------


def test_in_mapping_uses_source_codes(mapper: AccountMapper, ingestor: TrialBalanceIngestor):
    # map_in.csv: IN-BNK-01->1100, IN-REV->4100, IN-TXE->8100
    mtb = mapper.map_trial_balance(ingestor.ingest("IN"))
    by_local = {r.local_account_code: r for r in mtb.rows}

    assert mtb.mapping_source_file == "map_in.csv"
    assert by_local["IN-BNK-01"].group_account_code == "1100"
    assert by_local["IN-REV"].group_account_code == "4100"
    assert by_local["IN-TXE"].group_account_code == "8100"
    assert mtb.unmapped_row_count == 0


# --- 3. US direct group-account mapping -------------------------------------


def test_us_uses_identity_mapping(mapper: AccountMapper, ingestor: TrialBalanceIngestor):
    # US posts directly to the group chart: every account_code is already a
    # group code, and SourceDataService reports no mapping file.
    mtb = mapper.map_trial_balance(ingestor.ingest("US"))

    assert mtb.mapping_source_file is None
    assert mtb.unmapped_row_count == 0
    for row in mtb.rows:
        assert row.mapped is True
        assert row.group_account_code == row.local_account_code
        assert row.group_account_name == mapper.group_coa[row.local_account_code].account_name


def test_us_reports_no_mapping_file(mapper: AccountMapper):
    entries, source_file = mapper.load_mapping("US")
    assert entries == []
    assert source_file is None


# --- 4. Unmapped local accounts ---------------------------------------------


def test_unmapped_local_account_is_reported_not_dropped(
    real_service: SourceDataService,
):
    # A UK TB with a code absent from map_uk.csv must be kept and flagged.
    tb = TrialBalance(
        entity="UK",
        functional_currency="GBP",
        source_file="tb_uk.csv",
        rows=[
            TrialBalanceRow(
                entity="UK",
                source_file="tb_uk.csv",
                source_row=1,
                local_account_code="9999",
                account_name="Mystery account",
                currency="GBP",
                debit=Decimal("100.00"),
                credit=Decimal("0.00"),
            )
        ],
    )
    mtb = AccountMapper(real_service).map_trial_balance(tb)

    assert len(mtb.rows) == 1  # not dropped
    assert mtb.rows[0].mapped is False
    assert mtb.rows[0].group_account_code is None
    codes = [i.code for i in mtb.issues]
    assert "unmapped_account" in codes


# --- 5. Missing group account codes -----------------------------------------


def test_missing_group_account_flagged(northwind_data_dir):
    # Point a UK local account at a group code that is not in the COA.
    path = northwind_data_dir / "northwind" / "map_uk.csv"
    path.write_text(
        "local_code,local_name,group_code\n"
        "7000,Sales,9999\n",
        encoding="utf-8",
    )
    mapper = AccountMapper(SourceDataService(northwind_data_dir))
    entries, _ = mapper.load_mapping("UK")
    issues = mapper.validate_mapping("UK", entries)

    missing = [i for i in issues if i.code == "missing_group_account"]
    assert len(missing) == 1
    assert missing[0].details["group_code"] == "9999"
    assert missing[0].severity is Severity.ERROR


# --- 6. Duplicate mapping records -------------------------------------------


def test_duplicate_mapping_record_flagged(northwind_data_dir):
    path = northwind_data_dir / "northwind" / "map_uk.csv"
    path.write_text(
        "local_code,local_name,group_code\n"
        "7000,Sales,6100\n"
        "7000,Sales,6100\n",  # identical repeat
        encoding="utf-8",
    )
    mapper = AccountMapper(SourceDataService(northwind_data_dir))
    entries, _ = mapper.load_mapping("UK")
    issues = mapper.validate_mapping("UK", entries)

    dupes = [i for i in issues if i.code == "duplicate_mapping_record"]
    assert len(dupes) == 1
    assert dupes[0].severity is Severity.WARNING
    assert dupes[0].details["source_rows"] == [1, 2]
    # identical records do not constitute a conflict
    assert not [i for i in issues if i.code == "conflicting_mapping"]


# --- 7. Conflicting mappings ------------------------------------------------


def test_conflicting_mapping_flagged_and_not_resolved(northwind_data_dir):
    path = northwind_data_dir / "northwind" / "map_uk.csv"
    path.write_text(
        "local_code,local_name,group_code\n"
        "7000,Sales,6100\n"
        "7000,Sales,6200\n",  # same local code, different group codes
        encoding="utf-8",
    )
    mapper = AccountMapper(SourceDataService(northwind_data_dir))
    entries, _ = mapper.load_mapping("UK")
    issues = mapper.validate_mapping("UK", entries)

    conflicts = [i for i in issues if i.code == "conflicting_mapping"]
    assert len(conflicts) == 1
    assert conflicts[0].severity is Severity.ERROR
    assert conflicts[0].details["group_codes"] == ["6100", "6200"]

    # And the conflict is NOT auto-resolved when mapping a TB row.
    tb = TrialBalance(
        entity="UK",
        functional_currency="GBP",
        source_file="tb_uk.csv",
        rows=[
            TrialBalanceRow(
                entity="UK",
                source_file="tb_uk.csv",
                source_row=1,
                local_account_code="7000",
                currency="GBP",
                debit=Decimal("10.00"),
                credit=Decimal("0.00"),
            )
        ],
    )
    mtb = mapper.map_trial_balance(tb)
    assert mtb.rows[0].mapped is False
    assert mtb.rows[0].group_account_code is None


# --- 8. Legitimate many-to-one mappings -------------------------------------


def test_many_to_one_mapping_is_valid_aggregation(
    mapper: AccountMapper, ingestor: TrialBalanceIngestor
):
    # map_uk.csv maps BOTH 1200 and 1210 to 1100 (two banks -> Cash).
    mtb = mapper.map_trial_balance(ingestor.ingest("UK"))
    to_cash = [r for r in mtb.rows if r.group_account_code == "1100"]
    assert {r.local_account_code for r in to_cash} == {"1200", "1210"}
    # many-to-one is NOT flagged as any kind of error
    assert not [i for i in mtb.issues if i.code == "conflicting_mapping"]
    assert all(r.mapped for r in to_cash)


# --- 9. Source-row traceability ---------------------------------------------


def test_mapped_rows_preserve_source_traceability(
    mapper: AccountMapper, ingestor: TrialBalanceIngestor
):
    tb = ingestor.ingest("UK")
    mtb = mapper.map_trial_balance(tb)

    # row count and source identity are preserved 1:1, in order
    assert len(mtb.rows) == len(tb.rows)
    for mapped, original in zip(mtb.rows, tb.rows):
        assert mapped.source_row == original.source_row
        assert mapped.source_file == original.source_file
        assert mapped.local_account_code == original.local_account_code
        assert mapped.debit == original.debit
        assert mapped.credit == original.credit
        assert mapped.currency == original.currency
        assert mapped.raw == original.raw

    # spot-check a known UK row: 2310 is source data row 11
    deferred = next(r for r in mtb.rows if r.local_account_code == "2310")
    assert deferred.source_row == 11
    assert deferred.raw["nominal_code"] == "2310"


# --- 10. Known UK 2310 mapping concern --------------------------------------


def test_known_uk_2310_mapping_concern_is_reported(
    mapper: AccountMapper, ingestor: TrialBalanceIngestor
):
    # map_uk.csv row 12: 2310,Deferred income,4100
    # group_coa.csv:     2300,Deferred income,Liability,... ; 4100,Revenue,...
    # The mapping sends a deferred-income account to Revenue 4100 while the
    # group chart keeps 'Deferred income' at 2300.
    mtb = mapper.map_trial_balance(ingestor.ingest("UK"))

    row = next(r for r in mtb.rows if r.local_account_code == "2310")
    assert row.group_account_code == "4100"  # mapping NOT silently changed
    assert row.group_account_name == "Revenue"

    mismatches = [i for i in mtb.issues if i.code == "group_account_name_mismatch"]
    assert len(mismatches) == 1
    issue = mismatches[0]
    assert issue.severity is Severity.WARNING
    assert issue.account_code == "2310"
    assert issue.details["name_matched_group_accounts"] == ["2300"]
    assert issue.details["group_code"] == "4100"


def test_only_2310_triggers_the_name_mismatch(
    mapper: AccountMapper, ingestor: TrialBalanceIngestor
):
    # Across the real UK and IN mappings, only 2310 contradicts the COA name.
    uk_mismatch = [
        i
        for i in mapper.map_trial_balance(ingestor.ingest("UK")).issues
        if i.code == "group_account_name_mismatch"
    ]
    in_mismatch = [
        i
        for i in mapper.map_trial_balance(ingestor.ingest("IN")).issues
        if i.code == "group_account_name_mismatch"
    ]
    assert [i.account_code for i in uk_mismatch] == ["2310"]
    assert in_mismatch == []


# --- 11. Empty / malformed mapping files ------------------------------------


def test_empty_mapping_file_yields_no_entries(northwind_data_dir):
    path = northwind_data_dir / "northwind" / "map_uk.csv"
    path.write_text(
        "local_code,local_name,group_code\n", encoding="utf-8"
    )
    mapper = AccountMapper(SourceDataService(northwind_data_dir))
    entries, source_file = mapper.load_mapping("UK")

    assert entries == []
    assert source_file == "map_uk.csv"
    assert mapper.validate_mapping("UK", entries) == []


def test_missing_mapping_file_is_treated_as_empty(northwind_data_dir):
    # Remove the UK mapping file entirely.
    (northwind_data_dir / "northwind" / "map_uk.csv").unlink()
    mapper = AccountMapper(SourceDataService(northwind_data_dir))
    entries, source_file = mapper.load_mapping("UK")

    assert entries == []
    assert source_file is None


def test_incomplete_mapping_record_flagged(northwind_data_dir):
    path = northwind_data_dir / "northwind" / "map_uk.csv"
    path.write_text(
        "local_code,local_name,group_code\n"
        "7000,Sales,\n"  # missing group code
        ",Orphan,6100\n",  # missing local code
        encoding="utf-8",
    )
    mapper = AccountMapper(SourceDataService(northwind_data_dir))
    entries, _ = mapper.load_mapping("UK")
    issues = mapper.validate_mapping("UK", entries)

    invalid = [i for i in issues if i.code == "invalid_mapping_record"]
    assert len(invalid) == 2
    assert all(i.severity is Severity.ERROR for i in invalid)


def test_mapping_validation_issue_codes_are_stable(mapper: AccountMapper):
    # Guards the structured contract other layers depend on, against the real
    # source dataset: exactly one warning (2310) and no errors for UK.
    entries, _ = mapper.load_mapping("UK")
    real_issues = mapper.validate_mapping("UK", entries)
    codes = sorted(i.code for i in real_issues)
    assert codes == ["group_account_name_mismatch"]
    assert all(i.severity is Severity.WARNING for i in real_issues)
