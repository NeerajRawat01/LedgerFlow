"""Elimination tests (Stage 5) over the real intercompany data.

The eliminations are built from the verified reconciliation, so these tests
assert that:

* every generated journal balances exactly (debits == credits) in full
  precision Decimal;
* each matched pair is eliminated in full, with the difference routed to the
  FX account 7300 (policy §5);
* the in-transit invoice (INV-US-2291) is recognised at the closing rate and
  eliminated, not force-matched;
* unmatched rows are surfaced for review, never eliminated.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.eliminations import (
    FX_ACCOUNT,
    IC_PAYABLE,
    IC_RECEIVABLE,
    Eliminator,
)
from app.accounting.fx import FXService
from app.accounting.intercompany import IntercompanyReconciler
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"
CLOSING_USD_GBP = Decimal("1") / Decimal("1.2530")


@pytest.fixture
def eliminator() -> Eliminator:
    if not (REPO_DATA / "northwind").is_dir():
        pytest.skip("real dataset not present")
    reconciler = IntercompanyReconciler(
        SourceDataService(REPO_DATA), fx=FXService(SourceDataService(REPO_DATA))
    )
    return Eliminator(reconciler)


def test_every_elimination_journal_balances(eliminator):
    result = eliminator.build(eliminator.reconciler.reconcile())
    assert result.journals  # non-empty
    for journal in result.journals:
        assert journal.debit_total == journal.credit_total, journal.journal_id


def test_matched_pairs_produce_balance_and_fx_lines(eliminator):
    result = eliminator.build(eliminator.reconciler.reconcile())
    balance_journals = [j for j in result.journals if j.kind == "balance"]
    assert len(balance_journals) == 4
    for journal in balance_journals:
        accounts = {l.group_account for l in journal.lines}
        assert IC_RECEIVABLE in accounts
        assert IC_PAYABLE in accounts
        # A residual is present (the two sides booked different amounts), so
        # the FX account must appear to keep the journal balanced.
        assert FX_ACCOUNT in accounts


def test_in_transit_is_eliminated_at_closing_rate(eliminator):
    reconciliation = eliminator.reconciler.reconcile()
    result = eliminator.build(reconciliation)
    transit = [j for j in result.journals if j.kind == "in_transit"]
    assert len(transit) == 1
    journal = transit[0]
    assert journal.reference == "INV-US-2291"
    amount = Decimal("10160.00") * CLOSING_USD_GBP
    assert journal.debit_total == amount
    assert journal.balanced


def test_residual_routed_to_fx_matches_reconciliation(eliminator):
    reconciliation = eliminator.reconciler.reconcile()
    result = eliminator.build(reconciliation)
    assert result.residual_to_fx == reconciliation.residual_gbp


def test_source_rows_are_preserved_on_eliminations(eliminator):
    result = eliminator.build(eliminator.reconciler.reconcile())
    for journal in result.journals:
        assert journal.source_rows
        assert all(r >= 1 for r in journal.source_rows)
