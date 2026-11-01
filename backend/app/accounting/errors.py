"""Typed accounting errors.

These extend the shared :class:`~app.core.errors.LedgerFlowError` hierarchy
so an accounting failure carries the same stable machine-readable ``code``
contract as every other backend error, and can cross the MCP transport via
``error_for_code`` like the source-data errors do.

Only errors the Stage 1 services actually raise are defined here — no
speculative error types.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import LedgerFlowError


class AccountingError(LedgerFlowError):
    """Base class for accounting-engine failures."""

    code = "accounting_error"


class UnknownSchemaError(AccountingError):
    """A trial-balance CSV does not match a known entity schema."""

    code = "unknown_tb_schema"


class InvalidNumberError(AccountingError):
    """A source cell that must be numeric could not be parsed as a Decimal."""

    code = "invalid_number"


class UnknownEntityError(AccountingError):
    """The requested entity id is not recorded in ``entities.csv``."""

    code = "unknown_entity"


class MissingFXRateError(AccountingError):
    """No direct, inverse or cross rate exists for the requested conversion.

    Distinct from a valid zero rate: a missing rate is an error, never a
    number. Financial code must not fabricate a rate it does not have.
    """

    code = "missing_fx_rate"


class AmbiguousFXRateError(AccountingError):
    """More than one distinct source rate matched a single FX lookup."""

    code = "ambiguous_fx_rate"


__all__ = [
    "AccountingError",
    "UnknownSchemaError",
    "InvalidNumberError",
    "UnknownEntityError",
    "MissingFXRateError",
    "AmbiguousFXRateError",
]
