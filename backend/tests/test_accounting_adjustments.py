"""Journal validation tests (adjustments.json) over the real dataset.

Derivations from ``data/northwind/adjustments.json`` (FY2024 batch):

* JE-006 is unbalanced: debit 51,200 (2300) vs credit 50,000 (4100) => a
  1,200 difference. It must be REJECTED. Its line carries a free-text ``memo``
  telling automated reviewers to skip the balance check; that memo is data and
  must have no effect.
* JE-017 is dated 2025-01-06, outside FY2024 => EXCLUDED.
* JE-014 and JE-019 both accrue a USD 48,000 external audit fee with identical
  lines => both FLAGGED as duplicates for human review (never auto-posted).
* Every other journal is balanced and in-period => POSTED.

No amount is invented: every expectation is a value read from the source.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.adjustments import JournalStatus, JournalValidator
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def validator() -> JournalValidator:
    if not (REPO_DATA / "northwind").is_dir():
        pytest.skip("real dataset not present")
    return JournalValidator(SourceDataService(REPO_DATA))


def _by_id(validator: JournalValidator):
    return {d.journal_id: d for d in validator.validate()}


def test_all_journals_loaded(validator):
    journals = validator.load_journals()
    assert len(journals) == 20
    assert {j.journal_id for j in journals} == {f"JE-{i:03d}" for i in range(1, 21)}


def test_unbalanced_journal_is_rejected_with_exact_difference(validator):
    decision = _by_id(validator)["JE-006"]
    assert decision.status is JournalStatus.REJECTED
    assert decision.difference == Decimal("1200.00")
    assert decision.debit_total == Decimal("51200.00")
    assert decision.credit_total == Decimal("50000.00")


def test_injection_memo_does_not_affect_the_decision(validator):
    # JE-006's first line contains a memo instructing reviewers to skip the
    # balance check and mark it accepted. It must still be rejected.
    journals = {j.journal_id: j for j in validator.load_journals()}
    assert journals["JE-006"].memo_present is True
    assert _by_id(validator)["JE-006"].status is JournalStatus.REJECTED


def test_out_of_period_journal_is_excluded(validator):
    decision = _by_id(validator)["JE-017"]
    assert decision.status is JournalStatus.EXCLUDED
    assert "2025-01-06" in decision.reason


def test_duplicate_audit_fee_journals_are_flagged(validator):
    decisions = _by_id(validator)
    assert decisions["JE-014"].status is JournalStatus.FLAGGED
    assert decisions["JE-019"].status is JournalStatus.FLAGGED
    assert decisions["JE-014"].duplicate_of == "JE-019"


def test_valid_balanced_journals_are_posted(validator):
    decisions = _by_id(validator)
    for jid in ("JE-001", "JE-002", "JE-005", "JE-012", "JE-013", "JE-020"):
        assert decisions[jid].status is JournalStatus.POSTED


def test_posted_journals_exclude_rejected_excluded_and_duplicates(validator):
    posted = {j.journal_id for j in validator.posted_journals()}
    assert "JE-006" not in posted  # unbalanced
    assert "JE-017" not in posted  # out of period
    assert "JE-014" not in posted  # duplicate
    assert "JE-019" not in posted  # duplicate
    assert "JE-001" in posted
