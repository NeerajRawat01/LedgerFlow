"""Consolidation readiness models (Consolidation Skill v1).

The accounting engine is deliberately incomplete: only ingestion, validation,
FX-rate resolution and account mapping are implemented. This module records,
deterministically, **which** consolidation stages have actually run and which
are blocked/pending, so a skill can never claim a consolidation is complete
when a required stage has not executed.

Nothing here performs financial arithmetic. The readiness model only carries
the typed outputs of the Stage 1/2 services plus a per-stage status ledger.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.accounting.models import (
    Severity,
    ValidationIssue,
)


class StageStatus(str, Enum):
    """Whether one consolidation stage actually executed."""

    COMPLETED = "completed"   # ran successfully on the real services
    BLOCKED = "blocked"       # a required dependency is missing / not built
    PENDING = "pending"       # not attempted yet in this increment
    FAILED = "failed"         # attempted and raised / produced an error


class StageOutcome(BaseModel):
    """The recorded outcome of one consolidation stage."""

    model_config = {"frozen": True}

    stage: str
    status: StageStatus
    detail: str = ""
    # True when the stage is mandatory before a consolidation can be *called*
    # complete. Every stage in STAGE_ORDER is required for a complete run.
    required_for_complete: bool = True


# The full stage ledger, in execution order. ``implemented`` reflects the
# actual repository state; every stage below now has a service.
STAGE_ORDER: tuple[tuple[str, bool, bool], ...] = (
    # (stage, implemented, required_for_complete)
    ("trial_balance_ingestion", True, True),
    ("trial_balance_validation", True, True),
    ("account_mapping", True, True),
    ("journal_validation", True, True),
    ("intercompany_reconciliation", True, True),
    ("fx_translation", True, True),
    ("intercompany_elimination", True, True),
    ("goodwill", True, True),
    ("nci", True, True),
    ("translation_reserve", True, True),
    ("financial_statement_assembly", True, True),
    ("output_generation", True, True),
)


class EntityConsolidationResult(BaseModel):
    """Per-entity outputs of ingestion, validation and mapping."""

    entity: str
    functional_currency: str
    source_file: str | None = None
    row_count: int = 0
    balanced: bool = False
    # Full-precision functional-currency totals (never rounded here).
    debit_total: str = "0"
    credit_total: str = "0"
    difference: str = "0"
    mapped_row_count: int = 0
    unmapped_row_count: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.WARNING)


class ConsolidationReadiness(BaseModel):
    """Typed consolidation-readiness result returned by the skill.

    This records what actually ran, what is missing (if anything), and the
    validated/mapped per-entity position, alongside the assembled statements.
    The ``complete`` flag is only ever True when every required stage is
    ``COMPLETED``.
    """

    period: str
    scope: str = "group"
    entities: list[str] = Field(default_factory=list)
    functional_currency: str = "GBP"

    stages: list[StageOutcome] = Field(default_factory=list)
    entity_results: dict[str, EntityConsolidationResult] = Field(
        default_factory=dict
    )
    issues: list[ValidationIssue] = Field(default_factory=list)

    # Deterministic summary flags (no figures are fabricated to fill gaps).
    complete: bool = False

    # --- derived views --------------------------------------------------

    @property
    def blocked_stages(self) -> list[str]:
        return [
            s.stage
            for s in self.stages
            if s.status in (StageStatus.BLOCKED, StageStatus.FAILED)
        ]

    @property
    def pending_stages(self) -> list[str]:
        return [s.stage for s in self.stages if s.status is StageStatus.PENDING]

    @property
    def completed_stages(self) -> list[str]:
        return [
            s.stage for s in self.stages if s.status is StageStatus.COMPLETED
        ]

    @property
    def missing_required_stages(self) -> list[str]:
        """Required stages that are not ``COMPLETED``."""
        return [
            s.stage
            for s in self.stages
            if s.required_for_complete and s.status is not StageStatus.COMPLETED
        ]

    def to_payload(self) -> dict[str, Any]:
        """A serialisable view for ``SkillResult.data`` / traces.

        Decimal totals are stringified (full precision preserved), and
        issues are expanded to plain dicts. No accounting figure is invented:
        every value here was produced by an executed stage.
        """
        return {
            "period": self.period,
            "scope": self.scope,
            "entities": list(self.entities),
            "functional_currency": self.functional_currency,
            "complete": self.complete,
            "stages": [
                {
                    "stage": s.stage,
                    "status": s.status.value,
                    "detail": s.detail,
                    "required_for_complete": s.required_for_complete,
                }
                for s in self.stages
            ],
            "completed_stages": self.completed_stages,
            "blocked_stages": self.blocked_stages,
            "pending_stages": self.pending_stages,
            "missing_required_stages": self.missing_required_stages,
            "entities_detail": {
                name: {
                    "functional_currency": r.functional_currency,
                    "source_file": r.source_file,
                    "row_count": r.row_count,
                    "balanced": r.balanced,
                    "debit_total": r.debit_total,
                    "credit_total": r.credit_total,
                    "difference": r.difference,
                    "mapped_row_count": r.mapped_row_count,
                    "unmapped_row_count": r.unmapped_row_count,
                    "error_count": r.error_count,
                    "warning_count": r.warning_count,
                }
                for name, r in self.entity_results.items()
            },
        }


def build_stage_ledger(
    *,
    ingestion: bool,
    validation: bool,
    mapping: bool,
    journals: bool = False,
    intercompany: bool = False,
    translation: bool = False,
    elimination: bool = False,
    goodwill: bool = False,
    nci: bool = False,
    translation_reserve: bool = False,
    statements: bool = False,
    outputs: bool = False,
) -> list[StageOutcome]:
    """Compose the stage ledger from what actually executed.

    Any stage not marked implemented in :data:`STAGE_ORDER` is reported
    ``BLOCKED`` (missing dependency) rather than silently skipped, so the
    caller can always see the unimplemented remainder explicitly. All stages
    are implemented today, so this fallback is currently unexercised.
    """
    executed = {
        "trial_balance_ingestion": ingestion,
        "trial_balance_validation": validation,
        "account_mapping": mapping,
        "journal_validation": journals,
        "intercompany_reconciliation": intercompany,
        "fx_translation": translation,
        "intercompany_elimination": elimination,
        "goodwill": goodwill,
        "nci": nci,
        "translation_reserve": translation_reserve,
        "financial_statement_assembly": statements,
        "output_generation": outputs,
    }
    ledger: list[StageOutcome] = []
    for stage, implemented, required in STAGE_ORDER:
        if not implemented:
            ledger.append(
                StageOutcome(
                    stage=stage,
                    status=StageStatus.BLOCKED,
                    detail="service not implemented",
                    required_for_complete=required,
                )
            )
        elif executed.get(stage):
            ledger.append(
                StageOutcome(
                    stage=stage,
                    status=StageStatus.COMPLETED,
                    detail="executed",
                    required_for_complete=required,
                )
            )
        else:
            ledger.append(
                StageOutcome(
                    stage=stage,
                    status=StageStatus.FAILED,
                    detail="stage did not complete",
                    required_for_complete=required,
                )
            )
    return ledger
