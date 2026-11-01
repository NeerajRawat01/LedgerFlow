"""Trial-balance ingestion for US, UK and India ledgers.

The three entities record fundamentally different CSV schemas (see
``data/northwind``):

* **US** — ``account_code, account_name, debit, credit``: two non-negative
  magnitude columns.
* **UK** — ``nominal_code, description, cost_centre, txn_currency,
  txn_amount, balance``: a single signed ``balance`` (negative = credit),
  plus optional foreign-currency transaction detail.
* **IN** — ``ledger_code, ledger_name, period, amount, dr_cr``: a positive
  ``amount`` with an explicit ``Dr``/``Cr`` flag.

Every parser normalises to :class:`~app.accounting.models.TrialBalanceRow`
with **debit/credit non-negative magnitudes**, keeps the original source
values in ``raw``, and records the 1-based source row number. Rows are never
silently dropped: a row that cannot be parsed raises a typed
:class:`InvalidNumberError`.

This module reads through the existing :class:`SourceDataService` (which
already parses CSV/JSON read-only) rather than duplicating source readers.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.accounting.errors import InvalidNumberError, UnknownEntityError, UnknownSchemaError
from app.accounting.models import TrialBalance, TrialBalanceRow
from app.services.source_data import SourceDataService

# The column signature each entity's TB file must present, so an
# unexpected file is rejected loudly rather than mis-parsed.
_US_COLUMNS = ["account_code", "account_name", "debit", "credit"]
_UK_COLUMNS = [
    "nominal_code",
    "description",
    "cost_centre",
    "txn_currency",
    "txn_amount",
    "balance",
]
_IN_COLUMNS = ["ledger_code", "ledger_name", "period", "amount", "dr_cr"]

# Entity id -> required column set (in file order).
_SCHEMAS = {
    "US": _US_COLUMNS,
    "UK": _UK_COLUMNS,
    "IN": _IN_COLUMNS,
}

_NEGATIVE_ONE = Decimal("-1")


def _parse_decimal(value: str | None, *, entity: str, source_file: str, row: int, column: str) -> Decimal:
    """Parse a source cell into a Decimal, or raise a typed error.

    An empty/whitespace cell is treated as zero (the US file records ``0.00``
    but blank cells must not crash ingestion). Any non-numeric non-empty cell
    is a hard :class:`InvalidNumberError` — never silently coerced.
    """
    if value is None:
        return Decimal("0")
    text = value.strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise InvalidNumberError(
            f"cannot parse numeric value {value!r} for {entity} "
            f"{source_file} row {row} column {column!r}",
            details={
                "entity": entity,
                "source_file": source_file,
                "source_row": row,
                "column": column,
                "value": value,
            },
        ) from exc


def _require_schema(entity: str, source_file: str, columns: list[str]) -> None:
    expected = _SCHEMAS[entity]
    if columns != expected:
        raise UnknownSchemaError(
            f"{source_file} does not match the {entity} trial-balance schema",
            details={
                "entity": entity,
                "source_file": source_file,
                "expected_columns": expected,
                "actual_columns": columns,
            },
        )


def _parse_us_row(entity: str, source_file: str, row_no: int, raw: dict[str, str]) -> TrialBalanceRow:
    debit = _parse_decimal(raw.get("debit"), entity=entity, source_file=source_file, row=row_no, column="debit")
    credit = _parse_decimal(raw.get("credit"), entity=entity, source_file=source_file, row=row_no, column="credit")
    return TrialBalanceRow(
        entity=entity,
        source_file=source_file,
        source_row=row_no,
        local_account_code=(raw.get("account_code") or "").strip(),
        account_name=(raw.get("account_name") or "").strip() or None,
        currency="USD",
        debit=debit,
        credit=credit,
        raw=dict(raw),
    )


def _parse_uk_row(entity: str, source_file: str, row_no: int, raw: dict[str, str]) -> TrialBalanceRow:
    balance = _parse_decimal(raw.get("balance"), entity=entity, source_file=source_file, row=row_no, column="balance")
    # Negative balance is a credit; split into non-negative debit/credit.
    if balance < 0:
        debit, credit = Decimal("0"), balance * _NEGATIVE_ONE
    else:
        debit, credit = balance, Decimal("0")

    txn_currency = (raw.get("txn_currency") or "").strip() or None
    txn_amount_raw = raw.get("txn_amount")
    txn_amount: Decimal | None = None
    if (txn_amount_raw or "").strip():
        txn_amount = _parse_decimal(
            txn_amount_raw, entity=entity, source_file=source_file, row=row_no, column="txn_amount"
        )

    return TrialBalanceRow(
        entity=entity,
        source_file=source_file,
        source_row=row_no,
        local_account_code=(raw.get("nominal_code") or "").strip(),
        account_name=(raw.get("description") or "").strip() or None,
        currency="GBP",
        debit=debit,
        credit=credit,
        signed_balance=balance,
        cost_centre=(raw.get("cost_centre") or "").strip() or None,
        txn_currency=txn_currency,
        txn_amount=txn_amount,
        raw=dict(raw),
    )


def _parse_in_row(entity: str, source_file: str, row_no: int, raw: dict[str, str]) -> TrialBalanceRow:
    amount = _parse_decimal(raw.get("amount"), entity=entity, source_file=source_file, row=row_no, column="amount")
    dr_cr = (raw.get("dr_cr") or "").strip().lower()
    if dr_cr == "dr":
        debit, credit = amount, Decimal("0")
    elif dr_cr == "cr":
        debit, credit = Decimal("0"), amount
    else:
        raise InvalidNumberError(
            f"unrecognised dr_cr flag {raw.get('dr_cr')!r} for {entity} "
            f"{source_file} row {row_no}",
            details={
                "entity": entity,
                "source_file": source_file,
                "source_row": row_no,
                "column": "dr_cr",
                "value": raw.get("dr_cr"),
            },
        )
    return TrialBalanceRow(
        entity=entity,
        source_file=source_file,
        source_row=row_no,
        local_account_code=(raw.get("ledger_code") or "").strip(),
        account_name=(raw.get("ledger_name") or "").strip() or None,
        currency="INR",
        debit=debit,
        credit=credit,
        raw=dict(raw),
    )


_ROW_PARSERS = {
    "US": _parse_us_row,
    "UK": _parse_uk_row,
    "IN": _parse_in_row,
}


class TrialBalanceIngestor:
    """Ingest one entity's trial balance from the immutable source dataset."""

    def __init__(self, service: SourceDataService | None = None) -> None:
        self.service = service if service is not None else SourceDataService()

    def ingest(self, entity: str) -> TrialBalance:
        """Return the normalised trial balance for ``entity``.

        Args:
            entity: ``US``, ``UK`` or ``IN`` (case-insensitive).

        Raises:
            UnknownEntityError: The entity id is not a known TB schema.
            UnknownSchemaError: The file's columns do not match the schema.
            InvalidNumberError: A numeric cell could not be parsed.
        """
        entity_id = (entity or "").strip().upper()
        if entity_id not in _SCHEMAS:
            raise UnknownEntityError(
                f"no trial-balance schema for entity {entity!r}",
                details={"requested": entity, "supported": sorted(_SCHEMAS)},
            )

        source = self.service.get_trial_balance(entity_id)
        source_file = str(source["source_file"])
        columns = [str(c) for c in source["columns"]]
        _require_schema(entity_id, source_file, columns)

        parser = _ROW_PARSERS[entity_id]
        rows: list[TrialBalanceRow] = []
        # Source row is 1-based excluding the header, so the first data line
        # is row 1 — matching what a human sees when counting file lines.
        for offset, raw in enumerate(source["rows"]):
            rows.append(parser(entity_id, source_file, offset + 1, {k: (v or "") for k, v in raw.items()}))

        meta = self._entity_meta(entity_id)
        return TrialBalance(
            entity=entity_id,
            legal_name=meta.get("legal_name") or None,
            functional_currency=meta.get("functional_currency") or "",
            source_file=source_file,
            rows=rows,
        )

    def _entity_meta(self, entity_id: str) -> dict[str, str]:
        for row in self.service.list_entities():
            if row.get("entity_id", "").strip().upper() == entity_id:
                return row
        return {}
