"""Consolidation run endpoints.

Three read/write endpoints drive the existing shared agent for one
consolidation run:

* ``POST /api/consolidations/runs``        — start a run, return its result;
* ``GET  /api/consolidations/runs/{id}``   — fetch a persisted run;
* ``GET  /api/consolidations/runs/{id}/trace`` — the execution trace.

Execution is **synchronous**: the response is the completed AgentRunner
result. This is the simplest reliable model that fits the existing
architecture (no background workers, no queue, no database). The runner is
the real ``AgentRunner`` over ``build_default_registry()``; the API never
calls the skill directly and never fabricates a figure.

The runner factory is resolved from ``app.state.runner_factory`` so tests
can inject a runner backed by an in-memory MCP gateway; production uses the
loopback MCP gateway from ``default_runtime``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import settings

from app.agents.runner import AgentRunner
from app.agents.state import AgentMode, ExecutionResult
from app.agents.tracing import TraceRecorder
from app.core.errors import NotFoundError, ServiceError
from app.schemas.consolidation import (
    AgentErrorModel,
    AIStageModel,
    ConsolidationRunListResponse,
    ConsolidationRunRequest,
    ConsolidationRunResponse,
    ConsolidationRunSummary,
    ConsolidationTraceResponse,
    EntityStatusModel,
    IssueModel,
    ReadinessModel,
    StageStatusModel,
    ToolCallModel,
    TraceEventModel,
)
from app.services.consolidation_runs import ConsolidationRunStore

router = APIRouter(prefix="/api/consolidations", tags=["consolidation"])


def _runner(request: Request) -> AgentRunner:
    factory = getattr(request.app.state, "runner_factory", None)
    if factory is None:
        raise ServiceError(
            "consolidation runner is not configured on the application"
        )
    return factory()


def _store(request: Request) -> ConsolidationRunStore:
    store = getattr(request.app.state, "run_store", None)
    if store is None:
        raise ServiceError("consolidation run store is not configured")
    return store


def _readiness(result: ExecutionResult) -> ReadinessModel | None:
    """Extract the structured readiness payload from the skill result."""
    if result.skill_result is None:
        return None
    data = result.skill_result.data or {}
    if "stages" not in data:
        return None
    return ReadinessModel(
        period=str(data.get("period", "")),
        scope=str(data.get("scope", "group")),
        entities=list(data.get("entities", [])),
        functional_currency=str(data.get("functional_currency", "GBP")),
        complete=bool(data.get("complete", False)),
        stages=[StageStatusModel(**s) for s in data.get("stages", [])],
        completed_stages=list(data.get("completed_stages", [])),
        blocked_stages=list(data.get("blocked_stages", [])),
        pending_stages=list(data.get("pending_stages", [])),
        missing_required_stages=list(data.get("missing_required_stages", [])),
        entities_detail={
            name: EntityStatusModel(entity=name, **detail)
            for name, detail in (data.get("entities_detail") or {}).items()
        },
    )


def _issues(result: ExecutionResult) -> list[IssueModel]:
    if result.skill_result is None:
        return []
    return [IssueModel(**issue) for issue in result.skill_result.issues]


def _jsonable(value: Any) -> Any:
    """Recursively convert Decimal (and nested containers) to JSON numbers."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _result_payload(
    run_id: str, result: ExecutionResult, created_at: str
) -> dict:
    """Serialise an ExecutionResult into the persisted/returned run record.

    The ``readiness`` block carries the full skill result data (including the
    readiness view) plus a plain ``consolidation_readiness`` alias, so the
    persisted record is self-contained for the refresh path.
    """
    readiness = _readiness(result)
    skill_data = result.skill_result.data if result.skill_result else None
    return {
        "run_id": run_id,
        "trace_id": result.trace_id,
        "mode": result.mode.value,
        "status": result.status.value,
        "summary": result.summary,
        "skill_name": result.skill_name,
        "created_at": created_at,
        "readiness": readiness.model_dump() if readiness else None,
        "skill_data": _jsonable(skill_data),
        "consolidated_summary": (skill_data or {}).get("consolidated_summary"),
        "control_figures": _jsonable((skill_data or {}).get("control_figures")),
        "statements": (skill_data or {}).get("statements"),
        "output_files": list((skill_data or {}).get("output_files", [])),
        "trial_balance": list((skill_data or {}).get("trial_balance", [])),
        "eliminations": list((skill_data or {}).get("eliminations", [])),
        "integrity_checks": list((skill_data or {}).get("integrity_checks", [])),
        "issues": [i.model_dump() for i in _issues(result)],
        "tool_calls": [t.model_dump() for t in result.tool_calls],
        "errors": [e.model_dump() for e in result.errors],
        "warnings": list(result.warnings),
        # AI-assisted observability (never fabricated; explicit statuses).
        "ai_configured": bool((skill_data or {}).get("ai_configured", False)),
        "ai_stages": list((skill_data or {}).get("ai_stages", [])),
        "ai_reasoning": (skill_data or {}).get("ai_reasoning", {}) or {},
    }


def _to_response(record: dict) -> ConsolidationRunResponse:
    readiness = record.get("readiness")
    return ConsolidationRunResponse(
        run_id=record["run_id"],
        trace_id=record["trace_id"],
        mode=record["mode"],
        status=record["status"],
        summary=record.get("summary"),
        skill_name=record.get("skill_name"),
        created_at=record.get("created_at", ""),
        readiness=ReadinessModel(**readiness) if readiness else None,
        issues=[IssueModel(**i) for i in record.get("issues", [])],
        tool_calls=[ToolCallModel(**t) for t in record.get("tool_calls", [])],
        errors=[AgentErrorModel(**e) for e in record.get("errors", [])],
        warnings=list(record.get("warnings", [])),
        consolidated_summary=record.get("consolidated_summary"),
        control_figures=record.get("control_figures"),
        statements=record.get("statements"),
        output_files=list(record.get("output_files", [])),
        trial_balance=list(record.get("trial_balance", [])),
        eliminations=list(record.get("eliminations", [])),
        integrity_checks=list(record.get("integrity_checks", [])),
        ai_configured=bool(record.get("ai_configured", False)),
        ai_stages=[AIStageModel(**s) for s in record.get("ai_stages", [])],
        ai_reasoning=record.get("ai_reasoning", {}) or {},
    )


@router.post("/runs", response_model=ConsolidationRunResponse)
async def start_run(
    request: Request, body: ConsolidationRunRequest
) -> ConsolidationRunResponse:
    """Execute one consolidation run and return its full result.

    A typed failure (invalid period, skill error, ...) is preserved in the
    response body with an HTTP status that reflects it: 400 for an invalid
    request, 500 for an execution failure. The run is persisted either way so
    the trace stays inspectable.
    """
    runner = _runner(request)
    store = _store(request)

    payload: dict = {"period": body.period, "scope": body.scope}
    if body.entities:
        payload["entities"] = body.entities

    result = await runner.run(mode=AgentMode.CONSOLIDATION, payload=payload)

    record = _result_payload(result.run_id, result, created_at="")
    stored = store.save(record)
    response = _to_response(stored)

    failure_codes = {e.code for e in result.errors}
    skill_data = result.skill_result.data if result.skill_result else {}
    invalid_request = (
        bool(skill_data.get("validation_error"))
        or "intent_identification_failed" in failure_codes
    )
    if invalid_request:
        return JSONResponse(status_code=400, content=response.model_dump())
    if result.status.value == "failed":
        return JSONResponse(status_code=500, content=response.model_dump())
    return response


@router.get("/output/{filename}")
async def download_output(filename: str) -> FileResponse:
    """Download one generated deliverable from the output directory.

    Only plain file names are accepted (no path separators), so a caller
    cannot traverse outside the configured output directory.
    """
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or ".." in filename
    ):
        raise HTTPException(status_code=400, detail="invalid file name")
    output_dir = Path(settings.output_dir).resolve()
    path = (output_dir / filename).resolve()
    if output_dir not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    media_type = (
        "application/json" if filename.endswith(".json") else "text/csv"
    )
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/runs", response_model=ConsolidationRunListResponse)
async def list_runs(request: Request, limit: int = 50) -> ConsolidationRunListResponse:
    """Return the persisted run history, newest first.

    Only compact summary fields are returned so the history list stays cheap;
    the full record is available from ``GET /runs/{run_id}``.
    """
    store = _store(request)
    summaries: list[ConsolidationRunSummary] = []
    for record in store.list(limit=limit):
        readiness = record.get("readiness") or {}
        figures = record.get("control_figures") or {}
        summary_block = record.get("consolidated_summary") or {}
        summaries.append(
            ConsolidationRunSummary(
                run_id=record.get("run_id", ""),
                trace_id=record.get("trace_id", ""),
                status=record.get("status", ""),
                summary=record.get("summary"),
                period=(readiness or {}).get("period"),
                created_at=record.get("created_at", ""),
                updated_at=record.get("updated_at", ""),
                issue_count=len(record.get("issues", [])),
                complete=bool((readiness or {}).get("complete", False)),
                group_revenue=summary_block.get("group_revenue")
                or figures.get("group_revenue"),
                group_profit=summary_block.get("group_profit")
                or figures.get("group_profit"),
                total_assets=summary_block.get("total_assets")
                or figures.get("total_assets"),
                total_equity=summary_block.get("total_equity")
                or figures.get("total_equity"),
            )
        )
    return ConsolidationRunListResponse(runs=summaries)


@router.get("/runs/{run_id}", response_model=ConsolidationRunResponse)
async def get_run(request: Request, run_id: str) -> ConsolidationRunResponse:
    """Return a previously executed run (the refresh path)."""
    store = _store(request)
    try:
        record = store.load(run_id)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )
    return _to_response(record)


@router.get("/runs/{run_id}/trace", response_model=ConsolidationTraceResponse)
async def get_run_trace(
    request: Request, run_id: str
) -> ConsolidationTraceResponse:
    """Return the persisted trace events and MCP tool calls for a run."""
    store = _store(request)
    try:
        record = store.load(run_id)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )

    traces_dir = getattr(request.app.state, "traces_dir", None)
    events: list[TraceEventModel] = []
    if traces_dir is not None:
        recorder = TraceRecorder(traces_dir, record["trace_id"])
        for event in recorder.read_events():
            events.append(
                TraceEventModel(
                    ts=event.get("ts", ""),
                    seq=event.get("seq", 0),
                    trace_id=event.get("trace_id", record["trace_id"]),
                    event=event.get("event", ""),
                    payload=event.get("payload", {}) or {},
                )
            )

    return ConsolidationTraceResponse(
        run_id=run_id,
        trace_id=record["trace_id"],
        events=events,
        tool_calls=[ToolCallModel(**t) for t in record.get("tool_calls", [])],
    )
