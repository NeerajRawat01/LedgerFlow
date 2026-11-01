"""Schemas for consolidation-run endpoints.

These mirror the agent execution result and the consolidation-readiness
payload the skill already produces — they add no accounting meaning of their
own and never fabricate a figure. Everything returned is exactly what the
AgentRunner → LangGraph → ConsolidationSkill execution produced.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConsolidationRunRequest(BaseModel):
    """Request body for POST /api/consolidations/runs.

    ``period`` and ``scope`` are the only inputs the API accepts; they are
    forwarded verbatim to the shared agent as a deterministic consolidation
    intent. Unknown fields are rejected so a caller cannot smuggle in extra
    execution parameters.
    """

    model_config = {"extra": "forbid"}

    period: str = Field(
        default="FY2024",
        description="Reporting period for the run (FY2024 is supported).",
    )
    scope: str = Field(
        default="group",
        description="Consolidation scope; only 'group' is supported.",
    )
    entities: list[str] | None = Field(
        default=None,
        description="Optional subset of entities; defaults to all group entities.",
    )


class StageStatusModel(BaseModel):
    """One consolidation stage and whether it actually executed."""

    stage: str
    status: str
    detail: str = ""
    required_for_complete: bool = True


class EntityStatusModel(BaseModel):
    """Per-entity ingestion / validation / mapping summary (no invented values)."""

    entity: str
    functional_currency: str
    source_file: str | None = None
    row_count: int = 0
    balanced: bool = False
    debit_total: str = "0"
    credit_total: str = "0"
    difference: str = "0"
    mapped_row_count: int = 0
    unmapped_row_count: int = 0
    error_count: int = 0
    warning_count: int = 0


class IssueModel(BaseModel):
    """A structured issue raised by a deterministic check."""

    code: str
    severity: str
    message: str
    entity: str | None = None
    source_file: str | None = None
    source_row: int | None = None
    account_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ToolCallModel(BaseModel):
    """One recorded MCP tool invocation."""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result_summary: str | None = None
    error: str | None = None
    duration_ms: float | None = None


class AgentErrorModel(BaseModel):
    """A typed agent error surfaced by the execution."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class AICallModel(BaseModel):
    """One recorded LLM invocation (observability only; no prompt content)."""

    template_id: str
    prompt_version: str = "v1"
    model: str | None = None
    ok: bool = False
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    error: str | None = None
    structured_output: dict[str, Any] | None = None


class AIStageModel(BaseModel):
    """One AI-assisted stage and its explicit availability status.

    ``status`` is ``completed`` only when the LLM actually produced usable
    output; ``not_configured`` when no key is present; ``failed`` when the
    output could not be used. Artificial activity is never reported.
    """

    stage: str
    status: str
    detail: str = ""
    call_records: list[dict[str, Any]] = Field(default_factory=list)


class ReadinessModel(BaseModel):
    """The ConsolidationReadiness payload as returned by the skill."""

    period: str
    scope: str
    entities: list[str] = Field(default_factory=list)
    functional_currency: str
    complete: bool = False
    stages: list[StageStatusModel] = Field(default_factory=list)
    completed_stages: list[str] = Field(default_factory=list)
    blocked_stages: list[str] = Field(default_factory=list)
    pending_stages: list[str] = Field(default_factory=list)
    missing_required_stages: list[str] = Field(default_factory=list)
    entities_detail: dict[str, EntityStatusModel] = Field(default_factory=dict)


class ConsolidationRunResponse(BaseModel):
    """Full result of one consolidation run.
    Combines run identity, the agent execution result (status, summary,
    errors), the structured readiness result, the issues and the recorded
    MCP tool calls.
    """

    run_id: str
    trace_id: str
    mode: str
    status: str
    summary: str | None = None
    skill_name: str | None = None
    created_at: str

    readiness: ReadinessModel | None = None
    issues: list[IssueModel] = Field(default_factory=list)
    tool_calls: list[ToolCallModel] = Field(default_factory=list)
    errors: list[AgentErrorModel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    # Consolidated results produced by the deterministic engine. ``summary``
    # carries the four headline figures; ``control_figures`` the twelve
    # control figures; ``statements`` the three group statements; and
    # ``output_files`` the deliverables written to /output.
    consolidated_summary: dict[str, Any] | None = None
    control_figures: dict[str, Any] | None = None
    statements: dict[str, Any] | None = None
    output_files: list[dict[str, Any]] = Field(default_factory=list)
    # The consolidated trial balance (GBP) and every elimination entry, so the
    # UI can display and trace the actual figures.
    trial_balance: list[dict[str, Any]] = Field(default_factory=list)
    eliminations: list[dict[str, Any]] = Field(default_factory=list)
    integrity_checks: list[dict[str, Any]] = Field(default_factory=list)

    # AI-assisted observability. ``ai_configured`` reports whether an LLM was
    # actually available for this run; ``ai_stages`` reports each AI stage's
    # explicit status; ``ai_reasoning`` carries the structured proposals.
    ai_configured: bool = False
    ai_stages: list[AIStageModel] = Field(default_factory=list)
    ai_reasoning: dict[str, Any] = Field(default_factory=dict)


class ConsolidationRunSummary(BaseModel):
    """A compact row for the run-history list (no heavy statement payloads)."""

    run_id: str
    trace_id: str
    status: str
    summary: str | None = None
    period: str | None = None
    created_at: str = ""
    updated_at: str = ""
    issue_count: int = 0
    complete: bool = False
    group_revenue: Any | None = None
    group_profit: Any | None = None
    total_assets: Any | None = None
    total_equity: Any | None = None


class ConsolidationRunListResponse(BaseModel):
    """The persisted run history, newest first."""

    runs: list[ConsolidationRunSummary] = Field(default_factory=list)


class TraceEventModel(BaseModel):
    """One persisted agent trace event."""

    ts: str
    seq: int
    trace_id: str
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ConsolidationTraceResponse(BaseModel):
    """The execution trace (trace events + MCP tool calls) for one run."""

    run_id: str
    trace_id: str
    events: list[TraceEventModel] = Field(default_factory=list)
    tool_calls: list[ToolCallModel] = Field(default_factory=list)
