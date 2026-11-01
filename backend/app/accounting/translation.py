"""FX translation of an entity's mapped trial balance into GBP (Stage 3).

This is the deterministic, authoritative translation step required before any
elimination or statement assembly. It implements ``group_policies.md`` §2:

* **assets and liabilities** translate at the **closing** rate;
* **income and expenses** translate at the **average** rate for the period the
  entity is included in the group (FY2024 average for the US/UK; the H2
  average for India, acquired 1 July 2024);
* **equity** translates at **historical** rates. Historical equity comes from
  the prior-year group balances and the acquisition record, not from a market
  rate — so equity rows are classified and routed to the assembly stage rather
  than converted with a spot rate here;
* **exchange differences** on translation go to the translation reserve
  (group account 3300), computed at the assembly stage from the translated
  asset/liability and profit-or-loss position.

The service converts each row using the approved :class:`FXService` (which
resolves direct/inverse/cross rates from ``fx_rates.csv``), applies **no
rounding** (policy §7 rounds only reported figures), and **never fabricates a
rate**: a missing rate leaves the row un-translated and records a
``missing_fx_rate`` issue.

A rate is decided per row from its **statement classification** (assets and
liabilities close; income and expenses average). Where an entity's period is
partial (India, included from 1 July), the ``h2_average`` rate type is used
for income and expenses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.accounting.errors import MissingFXRateError
from app.accounting.fx import FXService
from app.accounting.models import (
    FXRateType,
    GroupAccount,
    MappedRow,
    MappedTrialBalance,
    Severity,
    Statement,
    TranslatedRow,
    TranslationResult,
    ValidationIssue,
)

_ZERO = Decimal("0")


@dataclass(frozen=True)
class EntityTranslationPolicy:
    """How one entity's rows are translated (period + rate selection).

    ``income_rate_type`` is the average variant the policy requires for the
    period the entity is included in the group: ``average`` for a full-year
    entity, ``h2_average`` for one consolidated only for H2 (India).
    """

    entity: str
    functional_currency: str
    income_rate_type: FXRateType
    closing_rate_type: FXRateType = FXRateType.CLOSING


class FXTranslator:
    """Translate a mapped trial balance into the presentation currency."""

    def __init__(
        self,
        *,
        fx: FXService,
        group_coa: dict[str, GroupAccount],
        presentation_currency: str = "GBP",
    ) -> None:
        self.fx = fx
        self.group_coa = group_coa
        self.presentation_currency = presentation_currency.strip().upper()

    # -- policy ------------------------------------------------------------

    @staticmethod
    def policy_for_entity(
        entity: str,
        functional_currency: str,
        *,
        acquisition_date: str | None,
        reporting_year_end: str = "2024-12-31",
    ) -> EntityTranslationPolicy:
        """Derive the translation rate policy for one entity.

        A subsidiary acquired *during* the reporting year contributes only
        from its acquisition date, so its income and expenses translate at the
        period-of-inclusion average (``h2_average`` here, since India was
        acquired 1 July 2024). A full-year entity uses the FY2024 average.

        The ``h2_average`` type is chosen only when the acquisition falls in
        the second half of the reporting year, which is what the source
        actually records; the rate itself is still read from ``fx_rates.csv``,
        never assumed.
        """
        entity_id = entity.strip().upper()
        income_type = FXRateType.AVERAGE
        if acquisition_date:
            acq = str(acquisition_date).strip()
            # A mid-year acquisition (after 1 July of the reporting year)
            # restricts the inclusion period to H2.
            if acq >= f"{reporting_year_end[:4]}-07-01":
                income_type = FXRateType.H2_AVERAGE
        return EntityTranslationPolicy(
            entity=entity_id,
            functional_currency=functional_currency.strip().upper(),
            income_rate_type=income_type,
        )

    # -- rate selection per row -------------------------------------------

    def _rate_type_for(
        self, statement: str | None, policy: EntityTranslationPolicy
    ) -> FXRateType:
        """Assets/liabilities close; income/expenses average (policy §2).

        Equity is returned unchanged by the caller (historical-rate rule), so
        this never needs to decide equity. Anything not clearly a P&L account
        is treated as a balance-sheet item and translated at closing — the
        conservative choice, since a balance sheet item at the wrong rate
        would overstate the reserve.
        """
        if statement == Statement.PROFIT_AND_LOSS.value:
            return policy.income_rate_type
        return policy.closing_rate_type

    # -- translation -------------------------------------------------------

    def translate(
        self,
        mapped: MappedTrialBalance,
        policy: EntityTranslationPolicy,
    ) -> TranslationResult:
        """Translate every mapped row of ``mapped`` into GBP.

        ``policy`` carries the entity's period-dependent rate selection (an
        H2-average entity such as India uses ``h2_average`` for income and
        expenses). Rows whose group account is unknown (``mapped=False``) are
        skipped: they cannot be classified, so they are reported by mapping,
        not translated here. A row whose rate cannot be resolved is recorded
        via a ``missing_fx_rate`` issue and omitted from ``rows`` — never
        converted with a fabricated rate.
        """
        entity = mapped.entity.strip().upper()
        functional = mapped.functional_currency.strip().upper()

        issues: list[ValidationIssue] = []
        rows: list[TranslatedRow] = []
        translated_all = True

        for row in mapped.rows:
            if not row.mapped or not row.group_account_code:
                # Unmapped rows are a mapping concern; do not translate.
                continue
            account = self.group_coa.get(row.group_account_code)
            statement = account.statement if account else None

            # Equity is translated at historical rates by the assembly stage;
            # it is not converted here.
            if account and account.account_type == "Equity":
                continue

            rate_type = self._rate_type_for(statement, policy)
            try:
                resolved = self.fx.resolve(
                    functional, self.presentation_currency, rate_type
                )
            except MissingFXRateError as exc:
                translated_all = False
                issues.append(
                    ValidationIssue(
                        code="missing_fx_rate",
                        severity=Severity.ERROR,
                        message=(
                            f"{entity} {row.source_file} row {row.source_row}: "
                            f"cannot translate {row.local_account_code} "
                            f"({row.group_account_code}) from {functional} to "
                            f"{self.presentation_currency} at {rate_type.value} "
                            f"rate — {exc.message}"
                        ),
                        entity=entity,
                        source_file=row.source_file,
                        source_row=row.source_row,
                        account_code=row.local_account_code,
                        details={
                            "group_account_code": row.group_account_code,
                            "functional_currency": functional,
                            "rate_type": rate_type.value,
                            "reason": "missing_fx_rate",
                        },
                    )
                )
                continue

            rows.append(
                TranslatedRow(
                    entity=entity,
                    functional_currency=functional,
                    group_account_code=row.group_account_code,
                    group_account_name=row.group_account_name,
                    statement=statement,
                    debit=row.debit,
                    credit=row.credit,
                    gbp_debit=row.debit * resolved.rate,
                    gbp_credit=row.credit * resolved.rate,
                    rate_type=rate_type.value,
                    rate=resolved.rate,
                    rate_method=resolved.method,
                    source_file=row.source_file,
                    source_row=row.source_row,
                    local_account_code=row.local_account_code,
                    cost_centre=row.cost_centre,
                    raw=row.raw,
                )
            )

        return TranslationResult(
            entity=entity,
            functional_currency=functional,
            presentation_currency=self.presentation_currency,
            translated=translated_all,
            rows=rows,
            issues=issues,
        )
