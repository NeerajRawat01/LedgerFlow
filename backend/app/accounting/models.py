"""Typed financial models for the accounting engine.

Everything here is a plain data model. No financial arithmetic happens in
this module — it only describes the shapes the ingestion, validation, FX and
mapping services exchange.

Precision rules (see ``data/northwind/group_policies.md`` §7):

* intermediate values keep full precision (``Decimal`` never rounds),
* rounding to 2 decimal places happens only at the reporting boundary, via
  :func:`round_reported`.

Every financial row carries enough source traceability (entity, source file,
1-based source row number, local account code, currency and the original
debit/credit values) that a reported figure can be walked back to the exact
line of the exact input file.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The policy's reporting precision boundary: two decimal places.
REPORTING_QUANTUM = Decimal("0.01")


def round_reported(value: Decimal) -> Decimal:
    """Round a value to the policy's reporting precision (2 dp, HALF_UP).

    Policy: "Translate at full precision and round each reported figure to 2
    decimal places." Use this ONLY at the reporting boundary; keep full
    precision everywhere else.
    """
    return value.quantize(REPORTING_QUANTUM, rounding=ROUND_HALF_UP)


class DebitCredit(str, Enum):
    """The side a balance sits on."""

    DEBIT = "debit"
    CREDIT = "credit"


class Severity(str, Enum):
    """Severity attached to a :class:`ValidationIssue`."""

    ERROR = "error"
    WARNING = "warning"


class FXRateType(str, Enum):
    """Rate types present in ``fx_rates.csv``.

    Only the types actually recorded in the source dataset are listed; no
    further types are invented.
    """

    OPENING = "opening"
    AVERAGE = "average"
    H2_AVERAGE = "h2_average"
    SPOT = "spot"
    CLOSING = "closing"


# --- trial balance ----------------------------------------------------------


class TrialBalanceRow(BaseModel):
    """One trial-balance line, normalised across the three entity schemas.

    ``debit`` and ``credit`` are both non-negative magnitudes expressed in the
    entity's functional currency. Exactly one is normally non-zero, but both
    may be zero (and both may be populated for a genuinely two-sided line).

    Source traceability is mandatory: ``entity``, ``source_file``,
    ``source_row`` (1-based, header excluded), and ``local_account_code``.
    ``raw`` preserves the original source values verbatim for auditability.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    source_file: str
    source_row: int = Field(ge=1)
    local_account_code: str
    account_name: str | None = None
    currency: str
    debit: Decimal
    credit: Decimal
    # UK keeps a signed balance and optional foreign-currency detail.
    signed_balance: Decimal | None = None
    cost_centre: str | None = None
    txn_currency: str | None = None
    txn_amount: Decimal | None = None
    raw: dict[str, str] = Field(default_factory=dict)

    @property
    def net(self) -> Decimal:
        """Net balance as debit minus credit (full precision)."""
        return self.debit - self.credit

    @property
    def side(self) -> DebitCredit:
        """Which side the row nets to (a zero row is treated as debit)."""
        return DebitCredit.CREDIT if self.net < 0 else DebitCredit.DEBIT


class TrialBalance(BaseModel):
    """A single entity's trial balance with its source context."""

    model_config = ConfigDict(frozen=True)

    entity: str
    legal_name: str | None = None
    functional_currency: str
    source_file: str
    rows: list[TrialBalanceRow] = Field(default_factory=list)


# --- FX ---------------------------------------------------------------------


class FXRate(BaseModel):
    """One FX quote exactly as recorded in ``fx_rates.csv``.

    ``convention`` states the quote direction, e.g. "USD per 1 GBP", so the
    rate is always interpreted as ``quote_currency`` per 1 ``base_currency``.
    """

    model_config = ConfigDict(frozen=True)

    base_currency: str
    quote_currency: str
    rate_type: FXRateType
    rate: Decimal
    as_of: str
    convention: str
    source_row: int = Field(ge=1)


# --- account mapping --------------------------------------------------------


class MappingEntry(BaseModel):
    """One local-account → group-account mapping record.

    Shape matches ``map_uk.csv`` / ``map_in.csv`` (``local_code, local_name,
    group_code``). US has no mapping file because its trial balance already
    posts to the group chart; the US mapping is therefore the identity
    mapping, represented by :class:`MappingEntry` objects whose
    ``group_code`` equals ``local_code`` (see :mod:`app.accounting.mapping`).

    Source traceability: ``entity``, ``source_file`` and 1-based
    ``source_row`` (header excluded) locate the exact mapping line, and
    ``raw`` preserves the original cells verbatim.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    source_file: str | None = None
    source_row: int | None = Field(default=None, ge=1)
    local_code: str
    local_name: str | None = None
    group_code: str
    raw: dict[str, str] = Field(default_factory=dict)


class GroupAccount(BaseModel):
    """One row of the group chart of accounts (``group_coa.csv``)."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    account_type: str
    statement: str
    cash_flow_category: str
    normal_balance: str


class Statement(str, Enum):
    """Which primary statement a group account belongs to."""

    BALANCE_SHEET = "BS"
    PROFIT_AND_LOSS = "PL"


class MappedRow(BaseModel):
    """A trial-balance row resolved to a group account.

    Carries the original :class:`TrialBalanceRow` fields (unrounded, original
    debit/credit and full source traceability) plus the resolved group account.
    Mapping never performs FX conversion; the functional-currency values are
    carried through unchanged.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    source_file: str
    source_row: int = Field(ge=1)
    local_account_code: str
    group_account_code: str | None = None
    group_account_name: str | None = None
    currency: str
    debit: Decimal
    credit: Decimal
    cost_centre: str | None = None
    mapped: bool = False
    raw: dict[str, str] = Field(default_factory=dict)

    @property
    def net(self) -> Decimal:
        return self.debit - self.credit


# --- validation (defined before MappedTrialBalance references it) -----------


class ValidationIssue(BaseModel):
    """A structured finding produced by a deterministic validation check."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Severity
    message: str
    entity: str | None = None
    source_file: str | None = None
    source_row: int | None = None
    account_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    """The outcome of validating one entity's trial balance.

    ``debit_total`` / ``credit_total`` / ``difference`` are reported in the
    entity's functional currency at full precision (callers apply
    :func:`round_reported` at the reporting boundary).
    """

    entity: str
    source_file: str | None = None
    row_count: int = 0
    debit_total: Decimal = Decimal("0")
    credit_total: Decimal = Decimal("0")
    difference: Decimal = Decimal("0")
    balanced: bool = False
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.WARNING)


class MappedTrialBalance(BaseModel):
    """An entity's trial balance with every row resolved to a group account."""

    entity: str
    functional_currency: str
    source_file: str
    mapping_source_file: str | None = None
    rows: list[MappedRow] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def unmapped_row_count(self) -> int:
        return sum(1 for r in self.rows if not r.mapped)


# --- FX translation ---------------------------------------------------------


class TranslatedRow(BaseModel):
    """A mapped trial-balance row translated into the presentation currency.

    Carries the full provenance chain: original entity, source file, 1-based
    source row, local *and* group account codes, the functional-currency
    amounts **and** the GBP amounts, plus the exact rate applied and how it
    was resolved. This is what makes a consolidated figure walkable back to
    the source line that produced it.

    ``rate_type`` is the policy rate used (closing for assets/liabilities,
    average for income/expense). ``rate`` is the resolved rate at full
    precision; ``rate_method`` records direct/inverse/cross/identity.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    functional_currency: str
    group_account_code: str
    group_account_name: str | None = None
    statement: str | None = None
    # Functional-currency magnitudes (unrounded, as mapped).
    debit: Decimal
    credit: Decimal
    # Presentation-currency magnitudes (GBP, full precision).
    gbp_debit: Decimal
    gbp_credit: Decimal
    # Applied rate provenance.
    rate_type: str
    rate: Decimal
    rate_method: str
    # Source traceability (preserved end to end).
    source_file: str
    source_row: int = Field(ge=1)
    local_account_code: str
    cost_centre: str | None = None
    raw: dict[str, str] = Field(default_factory=dict)

    @property
    def gbp_net(self) -> Decimal:
        return self.gbp_debit - self.gbp_credit


class TranslationResult(BaseModel):
    """One entity's translation outcome, with an explicit status.

    ``translated`` is True only when every mapped row was translated. When a
    required rate is missing the row is left un-translated, the reason is
    recorded as a :class:`ValidationIssue`, and ``translated`` is False — no
    rate is ever fabricated to force a translation.
    """

    entity: str
    functional_currency: str
    presentation_currency: str = "GBP"
    translated: bool = False
    rows: list[TranslatedRow] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def total_gbp_debit(self) -> Decimal:
        return sum((r.gbp_debit for r in self.rows), Decimal("0"))

    @property
    def total_gbp_credit(self) -> Decimal:
        return sum((r.gbp_credit for r in self.rows), Decimal("0"))


# --- acquisition accounting (business combination) --------------------------


class AcquisitionInputs(BaseModel):
    """The inputs a business-combination calculation needs, with provenance.

    Every field is optional so a missing input can be reported as ``None``
    (and recorded as an issue) rather than defaulted to a fabricated number.
    ``source_*`` fields preserve where each figure came from.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    acquisition_date: str | None = None
    ownership_pct: Decimal | None = None
    currency: str = "INR"
    consideration: Decimal | None = None
    consideration_currency: str | None = None
    net_assets_at_acquisition: Decimal | None = None
    # Per-line net-asset detail (label -> amount) for traceability.
    net_asset_lines: dict[str, Decimal] = Field(default_factory=dict)
    pre_acquisition_profit: Decimal | None = None
    source_note: str | None = None


class AcquisitionResult(BaseModel):
    """The deterministic acquisition-accounting outcome for one subsidiary.

    All amounts are held in the subsidiary's **functional currency** (the
    policy holds goodwill in that currency) plus the closing-rate GBP
    equivalent. Nothing is rounded until the reporting boundary.
    """

    entity: str
    acquisition_date: str
    ownership_pct: Decimal
    functional_currency: str
    consideration: Decimal
    net_assets_at_acquisition: Decimal
    # Owner's share of net assets at acquisition (ownership_pct x net assets).
    owners_share_of_net_assets: Decimal
    nci_share_of_net_assets: Decimal
    goodwill: Decimal
    # NCI measured at its proportionate share of net assets (policy §4).
    nci_at_acquisition: Decimal
    pre_acquisition_profit: Decimal
    # Closing-rate GBP equivalents + the rate applied.
    closing_rate: Decimal
    consideration_gbp: Decimal
    goodwill_gbp: Decimal
    nci_at_acquisition_gbp: Decimal
    fully_determined: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)


class NCIPostAcquisitionResult(BaseModel):
    """NCI share of a subsidiary's post-acquisition result."""

    entity: str
    functional_currency: str
    ownership_pct: Decimal
    nci_pct: Decimal
    post_acquisition_profit: Decimal
    rate_type: str
    rate: Decimal
    nci_share_of_profit: Decimal
    nci_share_of_profit_gbp: Decimal
    issues: list[ValidationIssue] = Field(default_factory=list)


# --- consolidated statement assembly ----------------------------------------


class ConsolidatedTBRow(BaseModel):
    """One consolidated trial-balance line in GBP with full provenance.

    ``gbp_debit`` / ``gbp_credit`` are non-negative magnitudes in the
    presentation currency. ``sources`` enumerates every contributing record
    (entity, file, row) so a reported figure walks back to its source.
    """

    group_account_code: str
    group_account_name: str
    account_type: str
    statement: str
    gbp_debit: Decimal
    gbp_credit: Decimal
    sources: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def gbp_net(self) -> Decimal:
        return self.gbp_debit - self.gbp_credit


class EliminationEntry(BaseModel):
    """One elimination journal line in GBP with provenance."""

    journal_id: str
    kind: str
    reference: str
    group_account_code: str
    group_account_name: str
    entity: str
    gbp_debit: Decimal
    gbp_credit: Decimal
    source_rows: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""
    note: str = ""


class LineItem(BaseModel):
    """One reported statement line (already rounded at the boundary)."""

    group_account_code: str | None = None
    label: str
    amount: Decimal | None = None
    # ``None`` amount always carries a reason (never an invented figure).
    reason: str | None = None
    note: str | None = None


class StatementSection(BaseModel):
    """A titled group of statement lines plus its total."""

    title: str
    lines: list[LineItem] = Field(default_factory=list)
    total: Decimal | None = None
    total_reason: str | None = None


class FinancialStatements(BaseModel):
    """The assembled consolidated statements (all figures rounded to 2 dp)."""

    presentation_currency: str = "GBP"
    period: str = "FY2024"
    balance_sheet: list[StatementSection] = Field(default_factory=list)
    profit_and_loss: list[StatementSection] = Field(default_factory=list)
    changes_in_equity: list[StatementSection] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)


class ConsolidationIssue(BaseModel):
    """One problem found, with file/line/amount and the action taken.

    ``action`` is one of ``blocked`` | ``assumed`` | ``flagged`` | ``posted``
    | ``rejected`` | ``excluded`` | ``held_for_review`` (assignment §4:
    "what your system did").
    """

    code: str
    severity: Severity
    message: str
    action: str = "flagged"
    entity: str | None = None
    source_file: str | None = None
    source_row: int | None = None
    account_code: str | None = None
    affected_amount: Decimal | None = None
    treatment: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
