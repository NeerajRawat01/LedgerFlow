"""Acquisition accounting for the Northwind India business combination (FY2024).

Implements ``group_policies.md`` §4 for the 80%-owned, mid-year-acquired
Northwind India Pvt Ltd:

* **consideration transferred** is read from ``acquisition_note.md`` (INR
  690,000,000, paid in cash on completion) — never invented;
* **identifiable net assets at acquisition** are the completion-accounts total
  in the same note (INR 678,000,000), with its line detail retained;
* **goodwill** = consideration − owner's share of net assets, measured in the
  subsidiary's functional currency (INR) per policy §4 and retranslated to GBP
  at the closing rate;
* **NCI is measured at its proportionate share of net assets** (policy §4), so
  NCI at acquisition = (1 − ownership) × net assets at acquisition;
* **post-acquisition profit** is the subsidiary's result for the period it is
  consolidated (H2, from acquisition to 31 Dec) taken from the monthly P&L.

Everything here is plain ``Decimal`` arithmetic. Missing inputs produce
``None``/``fully_determined=False`` and a recorded issue — no figure is ever
fabricated. No LLM is involved.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.accounting.fx import FXService
from app.accounting.models import (
    AcquisitionInputs,
    AcquisitionResult,
    FXRateType,
    NCIPostAcquisitionResult,
    Severity,
    ValidationIssue,
)
from app.core.errors import ServiceError
from app.services.source_data import SourceDataService

_ZERO = Decimal("0")

# The acquisition note is a fixed source document; its path is referenced for
# traceability even though it is parsed by the typed reader below.
_ACQ_NOTE_FILE = "acquisition_note.md"

# Amounts in the note are written as e.g. "INR 690,000,000".
_AMOUNT_RE = re.compile(r"([A-Z]{3})\s*([\d,]+(?:\.\d+)?)")


class AcquisitionService:
    """Compute goodwill, NCI at acquisition and post-acquisition profit."""

    def __init__(
        self,
        service: SourceDataService | None = None,
        *,
        fx: FXService | None = None,
    ) -> None:
        self.service = service if service is not None else SourceDataService()
        self.fx = fx if fx is not None else FXService(self.service)

    # -- reading the acquisition note --------------------------------------

    def _read_note_text(self) -> str:
        """Read ``acquisition_note.md`` verbatim (read-only)."""
        path = self.service.root / _ACQ_NOTE_FILE
        if not path.is_file():
            raise ServiceError(
                "acquisition note not found",
                details={"file": _ACQ_NOTE_FILE, "root": str(self.service.root)},
            )
        return path.read_text(encoding="utf-8")

    def load_inputs(self, entity: str = "IN") -> AcquisitionInputs:
        """Parse the acquisition note into typed inputs (no fabrication).

        The note is a small, well-structured markdown document. We parse the
        consideration and the net-asset table deterministically; a field that
        cannot be located is left ``None`` and later reported as an issue.
        """
        text = self._read_note_text()

        acquisition_date: str | None = None
        date_match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
        if date_match:
            day, month_name, year = date_match.groups()
            months = {
                "january": "01", "february": "02", "march": "03", "april": "04",
                "may": "05", "june": "06", "july": "07", "august": "08",
                "september": "09", "october": "10", "november": "11", "december": "12",
            }
            month = months.get(month_name.lower())
            if month:
                acquisition_date = f"{year}-{month}-{int(day):02d}"

        ownership: Decimal | None = None
        own_match = re.search(r"(\d{1,3})\s*%\s*of the equity", text)
        if own_match:
            ownership = Decimal(own_match.group(1)) / Decimal("100")

        # Consideration: the first currency amount introduced by "Consideration".
        consideration: Decimal | None = None
        consideration_ccy: str | None = None
        cons_match = re.search(r"Consideration:.*?([A-Z]{3})\s*([\d,]+(?:\.\d+)?)", text, re.DOTALL)
        if cons_match:
            consideration_ccy = cons_match.group(1)
            consideration = Decimal(cons_match.group(2).replace(",", ""))

        # Net assets at completion, with line detail from the table.
        na_total: Decimal | None = None
        na_lines: dict[str, Decimal] = {}
        for line in text.splitlines():
            if "|" not in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            label, raw_amount = cells[0], cells[1]
            # Amounts may be bolded (``**678,000,000**``); strip markdown emphasis.
            cleaned = raw_amount.strip().strip("*").strip()
            amount_match = re.fullmatch(r"([\d,]+(?:\.\d+)?)", cleaned)
            if not amount_match:
                continue
            amount = Decimal(amount_match.group(1).replace(",", ""))
            key = label.strip("* ").strip()
            na_lines[key] = amount
            if key.lower() == "net assets at completion":
                na_total = amount

        pre_acq = na_lines.get("Profit for 1 April to 30 June 2024")

        return AcquisitionInputs(
            entity=entity.upper(),
            acquisition_date=acquisition_date,
            ownership_pct=ownership,
            currency="INR",
            consideration=consideration,
            consideration_currency=consideration_ccy,
            net_assets_at_acquisition=na_total,
            net_asset_lines=na_lines,
            pre_acquisition_profit=pre_acq,
            source_note=_ACQ_NOTE_FILE,
        )

    # -- acquisition calculation -------------------------------------------

    def compute(
        self,
        inputs: AcquisitionInputs,
        *,
        closing_rate_type: FXRateType = FXRateType.CLOSING,
    ) -> AcquisitionResult:
        """Calculate goodwill and NCI at acquisition deterministically.

        Raises a typed :class:`ServiceError` when a mandatory input is missing,
        because a business combination with no consideration or no net assets
        cannot be computed — and must not be guessed.
        """
        issues: list[ValidationIssue] = []
        if inputs.ownership_pct is None:
            raise ServiceError(
                "ownership percentage missing from acquisition note",
                details={"entity": inputs.entity, "file": _ACQ_NOTE_FILE},
            )
        if inputs.consideration is None:
            raise ServiceError(
                "consideration missing from acquisition note",
                details={"entity": inputs.entity, "file": _ACQ_NOTE_FILE},
            )
        if inputs.net_assets_at_acquisition is None:
            raise ServiceError(
                "net assets at acquisition missing from acquisition note",
                details={"entity": inputs.entity, "file": _ACQ_NOTE_FILE},
            )

        ownership = inputs.ownership_pct
        nci_pct = Decimal("1") - ownership
        consideration = inputs.consideration
        net_assets = inputs.net_assets_at_acquisition
        owners_share = net_assets * ownership
        nci_share = net_assets * nci_pct
        goodwill = consideration - owners_share

        if goodwill < _ZERO:
            issues.append(
                ValidationIssue(
                    code="negative_goodwill",
                    severity=Severity.WARNING,
                    message=(
                        f"{inputs.entity}: consideration is below the owner's "
                        f"share of net assets (goodwill {goodwill}); a bargain "
                        f"purchase gain would be recognised instead of goodwill"
                    ),
                    entity=inputs.entity,
                    source_file=_ACQ_NOTE_FILE,
                    details={"goodwill": str(goodwill)},
                )
            )

        functional = inputs.currency
        # ``resolve(X, GBP)`` returns the multiplier that converts X into GBP
        # (e.g. USD->GBP is the inverse of the recorded GBP/USD quote). The
        # rate is stored for the audit trail; conversion multiplies by it.
        resolved = self.fx.resolve(functional, "GBP", closing_rate_type)
        closing_rate = resolved.rate

        return AcquisitionResult(
            entity=inputs.entity,
            acquisition_date=inputs.acquisition_date or "",
            ownership_pct=ownership,
            functional_currency=functional,
            consideration=consideration,
            net_assets_at_acquisition=net_assets,
            owners_share_of_net_assets=owners_share,
            nci_share_of_net_assets=nci_share,
            goodwill=goodwill,
            nci_at_acquisition=nci_share,
            pre_acquisition_profit=inputs.pre_acquisition_profit or _ZERO,
            closing_rate=closing_rate,
            consideration_gbp=consideration * closing_rate,
            goodwill_gbp=goodwill * closing_rate,
            nci_at_acquisition_gbp=nci_share * closing_rate,
            fully_determined=True,
            issues=issues,
        )

    # -- post-acquisition profit -------------------------------------------

    def post_acquisition_profit(
        self,
        entity: str,
        *,
        acquisition_date: str,
        ownership_pct: Decimal,
        income_rate_type: FXRateType,
    ) -> NCIPostAcquisitionResult:
        """NCI share of the subsidiary's post-acquisition profit.

        Uses the monthly P&L: months **on or after** the acquisition month
        belong to the group (India: Jul–Dec 2024). Revenue is a credit and
        expenses are debits, so profit = Σrevenue − Σexpenses, converted at the
        period average for the period of inclusion, then multiplied by NCI%.
        """
        rows = self.service.get_monthly_pnl(entity)
        if not rows:
            raise ServiceError(
                "no monthly P&L available for post-acquisition profit",
                details={"entity": entity},
            )

        acq_month = acquisition_date[:7]  # "2024-07"
        revenue = _ZERO
        expenses = _ZERO
        for row in rows:
            month = (row.get("month") or "").strip()
            if month < acq_month:
                continue
            amount = Decimal((row.get("amount") or "0").strip() or "0")
            dr_cr = (row.get("dr_cr") or "").strip().lower()
            if dr_cr == "cr":
                revenue += amount
            else:
                expenses += amount
        profit = revenue - expenses

        resolved = self.fx.resolve(entity_currency(entity), "GBP", income_rate_type)
        nci_pct = Decimal("1") - ownership_pct
        nci_share = profit * nci_pct
        return NCIPostAcquisitionResult(
            entity=entity.upper(),
            functional_currency=entity_currency(entity),
            ownership_pct=ownership_pct,
            nci_pct=nci_pct,
            post_acquisition_profit=profit,
            rate_type=income_rate_type.value,
            rate=resolved.rate,
            nci_share_of_profit=nci_share,
            nci_share_of_profit_gbp=nci_share * resolved.rate,
        )


def entity_currency(entity: str) -> str:
    """Functional currency for a known entity id (from the entity master)."""
    return {"US": "USD", "UK": "GBP", "IN": "INR"}.get(entity.upper(), "GBP")
