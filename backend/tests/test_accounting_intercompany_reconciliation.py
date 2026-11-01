"""Intercompany reconciliation tests over the real immutable dataset.

Derivations (all from ``data/northwind``):

``intercompany.csv`` records five US→UK management-fee invoices (rows 2-6) and
four UK→US counterparts (rows 7-10). The US receivable is the *sum* of all
five invoices; the UK payable omits ``INV-US-2291`` (row 6, dated 2024-12-31),
so that invoice is an item in transit at the year end (policy §5).

The four matched references book different amounts on each side because each
side used its own booking-date rate, so the matched pairs carry genuine FX
residuals which policy §5 sends to account 7300.

Closing USD->GBP = 1 / 1.2530 (GBP/USD closing = 1.2530).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.fx import FXService
from app.accounting.intercompany import (
    IntercompanyReconciler,
    MatchType,
)
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"
CLOSING_USD_GBP = Decimal("1") / Decimal("1.2530")


@pytest.fixture
def real_service() -> SourceDataService:
    if not (REPO_DATA / "northwind").is_dir():
        pytest.skip("real dataset not present")
    return SourceDataService(REPO_DATA)


@pytest.fixture
def reconciler(real_service) -> IntercompanyReconciler:
    return IntercompanyReconciler(real_service, fx=FXService(real_service))


def test_records_load_with_source_rows(reconciler):
    records = reconciler.load_records()
    # 9 data rows in intercompany.csv (rows 2-10 of the file).
    assert len(records) == 9
    assert records[0].source_row == 1
    assert records[0].reporting_entity == "US"
    assert records[4].doc_ref == "INV-US-2291"
    assert records[4].doc_amount == Decimal("10160.00")


def test_reconciliation_matches_four_references(reconciler):
    result = reconciler.reconcile()
    assert result.matched_count == 4
    refs = {p.doc_ref for p in result.pairs}
    assert refs == {
        "INV-US-2201",
        "INV-US-2224",
        "INV-US-2257",
        "INV-US-2268",
    }


def test_in_transit_invoice_is_not_force_matched(reconciler):
    result = reconciler.reconcile()
    assert result.unmatched_count == 1
    item = result.unmatched[0]
    assert item.doc_ref == "INV-US-2291"
    assert item.classification == "in_transit"
    assert item.reporting_entity == "US"
    # In transit at the closing rate: 10,160 USD * (1 / 1.2530).
    assert item.gbp_amount == Decimal("10160.00") * CLOSING_USD_GBP


def test_matched_pair_residual_is_the_booking_rate_difference(reconciler):
    result = reconciler.reconcile()
    pair = next(p for p in result.pairs if p.doc_ref == "INV-US-2201")
    # Pair orientation follows sorted (entity, counterparty) order, so the UK
    # leg is stored first here (UK < US). The GBP amounts are each side's own
    # booked/converted value at the closing rate.
    uk_gbp = Decimal("124770.21")
    us_gbp = Decimal("157460.00") * CLOSING_USD_GBP
    assert {pair.left_gbp, pair.right_gbp} == {uk_gbp, us_gbp}
    # residual_gbp is left - right, full precision, never rounded.
    assert pair.residual_gbp == pair.left_gbp - pair.right_gbp
    assert abs(pair.residual_gbp) == abs(us_gbp - uk_gbp)
    assert pair.match_type in (MatchType.EXACT, MatchType.FX_RESIDUAL, MatchType.RESIDUAL)


def test_total_residual_matches_hand_computation(reconciler):
    # Hand-computed from the four pairs (see module docstring). The residual
    # is the sum of each pair's (left - right) at the closing rate; the
    # magnitude is what policy §5 sends to account 7300.
    result = reconciler.reconcile()
    per_pair_residuals = [
        Decimal("124770.21") - Decimal("157460.00") * CLOSING_USD_GBP,
        Decimal("124474.31") - Decimal("157460.00") * CLOSING_USD_GBP,
        Decimal("117771.13") - Decimal("157460.00") * CLOSING_USD_GBP,
        Decimal("123692.07") - Decimal("157460.00") * CLOSING_USD_GBP,
    ]
    expected = sum(per_pair_residuals, Decimal("0"))
    assert result.residual_gbp == expected
    # Magnitude sanity: ~11,957.88 GBP from the four matched pairs.
    assert abs(result.residual_gbp).quantize(Decimal("0.01")) == Decimal("11957.88")


def test_reconciliation_is_deterministic(reconciler):
    first = reconciler.reconcile()
    second = reconciler.reconcile()
    assert first.residual_gbp == second.residual_gbp
    assert [p.doc_ref for p in first.pairs] == [p.doc_ref for p in second.pairs]
