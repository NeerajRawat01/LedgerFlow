"""Deterministic trial-balance validation.

All checks are pure arithmetic over :class:`~app.accounting.models.TrialBalance`
rows. Nothing is auto-corrected: an unbalanced trial balance is reported, never
silently patched, because a silent correction would destroy the audit trail.

Checks implemented (Stage 1 scope):

1. **Debit and credit totals**, per entity.
2. **Difference** between the two totals.
3. **Entity-level balancing status** (difference is zero).
4. **Invalid or missing numeric values** — surfaced as row-level issues when a
   caller validates rows whose magnitudes carry a non-finite/degenerate
   marker; ingestion raises ``InvalidNumberError`` for unparseable cells, so
   this check additionally guards negative magnitudes and empty account codes.
5. **Duplicate or malformed source rows** — a duplicate is two rows with the
   same identifying composite key (entity-specific; see ``_row_key``).
"""

from __future__ import annotations

from decimal import Decimal

from app.accounting.models import (
    Severity,
    TrialBalance,
    TrialBalanceRow,
    ValidationIssue,
    ValidationResult,
)

_ZERO = Decimal("0")


def _row_key(row: TrialBalanceRow) -> tuple[str, str | None]:
    """Identifying key for duplicate detection.

    Per the reviewed ambiguity decision: for the UK the key is
    ``(nominal_code, cost_centre)`` so that two rows sharing a nominal code
    but split across cost centres (7000 / CC-OPS and 7000 / CC-SALES) are
    distinct, not duplicates. Every entity uses
    ``(local_account_code, cost_centre)`` — for US/IN ``cost_centre`` is
    always ``None``, so the key reduces to the account code.
    """
    return (row.local_account_code, row.cost_centre)


class TrialBalanceValidator:
    """Validate a single entity's ingested trial balance."""

    def validate(self, tb: TrialBalance) -> ValidationResult:
        issues: list[ValidationIssue] = []

        debit_total = _ZERO
        credit_total = _ZERO
        seen: dict[tuple[str, str | None], TrialBalanceRow] = {}

        for row in tb.rows:
            debit_total += row.debit
            credit_total += row.credit

            self._check_row_values(tb, row, issues)

            key = _row_key(row)
            if key in seen:
                first = seen[key]
                issues.append(
                    ValidationIssue(
                        code="duplicate_row",
                        severity=Severity.WARNING,
                        message=(
                            f"{tb.entity} {tb.source_file}: duplicate row key "
                            f"{key!r} at source rows {first.source_row} and "
                            f"{row.source_row}"
                        ),
                        entity=tb.entity,
                        source_file=tb.source_file,
                        source_row=row.source_row,
                        account_code=row.local_account_code,
                        details={"first_row": first.source_row, "key": list(key)},
                    )
                )
            else:
                seen[key] = row

        difference = debit_total - credit_total
        if difference != _ZERO:
            issues.append(
                ValidationIssue(
                    code="unbalanced_trial_balance",
                    severity=Severity.ERROR,
                    message=(
                        f"{tb.entity} trial balance does not balance: debits "
                        f"({debit_total}) minus credits ({credit_total}) = "
                        f"{difference}"
                    ),
                    entity=tb.entity,
                    source_file=tb.source_file,
                    details={
                        "debit_total": str(debit_total),
                        "credit_total": str(credit_total),
                        "difference": str(difference),
                    },
                )
            )

        if not tb.rows:
            issues.append(
                ValidationIssue(
                    code="empty_trial_balance",
                    severity=Severity.ERROR,
                    message=f"{tb.entity} trial balance has no rows",
                    entity=tb.entity,
                    source_file=tb.source_file,
                )
            )

        return ValidationResult(
            entity=tb.entity,
            source_file=tb.source_file,
            row_count=len(tb.rows),
            debit_total=debit_total,
            credit_total=credit_total,
            difference=difference,
            balanced=difference == _ZERO and bool(tb.rows),
            issues=issues,
        )

    @staticmethod
    def _check_row_values(
        tb: TrialBalance, row: TrialBalanceRow, issues: list[ValidationIssue]
    ) -> None:
        """Flag degenerate row values (check 4)."""
        if not row.local_account_code:
            issues.append(
                ValidationIssue(
                    code="missing_account_code",
                    severity=Severity.ERROR,
                    message=(
                        f"{tb.entity} {tb.source_file} row {row.source_row} "
                        f"has no account code"
                    ),
                    entity=tb.entity,
                    source_file=tb.source_file,
                    source_row=row.source_row,
                )
            )
        if row.debit < _ZERO or row.credit < _ZERO:
            issues.append(
                ValidationIssue(
                    code="negative_magnitude",
                    severity=Severity.ERROR,
                    message=(
                        f"{tb.entity} {tb.source_file} row {row.source_row} "
                        f"has a negative debit/credit magnitude "
                        f"(debit={row.debit}, credit={row.credit})"
                    ),
                    entity=tb.entity,
                    source_file=tb.source_file,
                    source_row=row.source_row,
                    account_code=row.local_account_code,
                    details={"debit": str(row.debit), "credit": str(row.credit)},
                )
            )
