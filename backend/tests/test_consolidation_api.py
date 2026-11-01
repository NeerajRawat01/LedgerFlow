"""Consolidation run API tests.

Exercises the three endpoints end to end through a real ``AgentRunner`` →
LangGraph → ``ConsolidationSkill`` execution, with the MCP tool gateway
backed by an in-memory transport (no sockets, no network, no LLM, no
database). The runner and store are injected onto the app so no loopback
HTTP server is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from app.accounting.fx import FXService
from app.accounting.ingestion import TrialBalanceIngestor
from app.accounting.mapping import AccountMapper
from app.accounting.validation import TrialBalanceValidator
from app.agents.graph import AgentRuntime, build_graph
from app.agents.runner import AgentRunner
from app.agents.skills.consolidation import ConsolidationSkill
from app.agents.skills.registry import SkillRegistry
from app.agents.thread_service import ThreadService
from app.core.config import settings as app_settings
from app.core.errors import LLMNotConfiguredError
from app.main import create_app
from app.mcp.client import MCPClient
from app.mcp.gateway import ToolGateway
from app.mcp.server import build_mcp_server
from app.services.consolidation_runs import ConsolidationRunStore
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"
REAL_DATASET = REPO_DATA / "northwind"


# --- in-memory MCP transport (no sockets) -----------------------------------


@asynccontextmanager
async def _memory_transport(server: MCPServer):
    async with create_client_server_memory_streams() as (client, srv):
        task = asyncio.create_task(
            server._lowlevel_server.run(  # noqa: SLF001 - SDK test API
                srv[0],
                srv[1],
                server._lowlevel_server.create_initialization_options(),  # noqa: SLF001
            )
        )
        try:
            yield (client[0], client[1])
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _no_llm():
    raise LLMNotConfiguredError("GEMINI_API_KEY is not set")


def _real_skill() -> ConsolidationSkill:
    svc = SourceDataService(REPO_DATA)
    return ConsolidationSkill(
        ingestor=TrialBalanceIngestor(svc),
        validator=TrialBalanceValidator(),
        mapper=AccountMapper(svc),
        fx=FXService(svc),
        source=svc,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A TestClient whose consolidation route runs the real agent offline."""
    if not REAL_DATASET.is_dir():
        pytest.skip("real dataset not present")

    # A runtime over an in-memory MCP gateway and the real dataset.
    server = build_mcp_server(SourceDataService(REPO_DATA))
    client_mcp = MCPClient(
        transport_factory=lambda: _memory_transport(server),
        settings=app_settings,
    )
    storage_root = tmp_path / "storage"
    runtime = AgentRuntime(
        registry=SkillRegistry([_real_skill()]),
        llm_provider=_no_llm,
        thread_service=ThreadService(storage_root / "threads"),
        traces_dir=storage_root / "traces",
        settings=app_settings,
        tool_gateway=ToolGateway(client_mcp, settings=app_settings),
    )

    def runner_factory() -> AgentRunner:
        return AgentRunner(runtime, build_graph(runtime))

    app = create_app(runner_factory=runner_factory)
    # Override the store/traces dirs to the temp storage for isolation.
    app.state.run_store = ConsolidationRunStore(storage_root / "runs")
    app.state.traces_dir = runtime.traces_dir
    storage_root.mkdir(parents=True, exist_ok=True)

    with TestClient(app) as test_client:
        yield test_client


# --- successful run ---------------------------------------------------------


def test_start_run_returns_readiness_result(client: TestClient) -> None:
    response = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["run_id"].startswith("run_")
    assert body["trace_id"].startswith("trace_")
    assert body["mode"] == "consolidation"
    assert body["skill_name"] == "consolidation"
    assert body["status"] == "completed"

    readiness = body["readiness"]
    assert readiness is not None
    assert readiness["period"] == "2024"
    assert readiness["functional_currency"] == "GBP"
    assert readiness["complete"] is True

    # every required stage completed
    assert "fx_translation" in readiness["completed_stages"]
    assert readiness["blocked_stages"] == []
    assert readiness["missing_required_stages"] == []

    # per-entity statuses come from the real backend, not fixtures
    detail = readiness["entities_detail"]
    assert set(detail) == {"US", "UK", "IN"}
    assert detail["US"]["row_count"] == 25
    assert detail["US"]["balanced"] is True
    assert detail["US"]["functional_currency"] == "USD"
    assert detail["UK"]["functional_currency"] == "GBP"
    assert detail["IN"]["functional_currency"] == "INR"
    assert detail["US"]["unmapped_row_count"] == 0


def test_run_returns_control_figures(client: TestClient) -> None:
    body = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()
    figures = body["control_figures"]
    assert figures is not None
    assert figures["group_profit"] is not None
    assert figures["total_assets"] is not None
    assert figures["journals"] == {"posted": 15, "rejected": 2, "held_for_review": 2}


def test_run_records_mcp_tool_calls(client: TestClient) -> None:
    body = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()

    names = [t["name"] for t in body["tool_calls"]]
    assert "list_entities" in names
    assert names.count("get_trial_balance") == 3
    assert all(t["ok"] for t in body["tool_calls"])
    assert all(t["duration_ms"] is not None for t in body["tool_calls"])


def test_run_propagates_known_issue(client: TestClient) -> None:
    body = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()

    codes = {i["code"] for i in body["issues"]}
    assert "group_account_name_mismatch" in codes
    issue = next(i for i in body["issues"] if i["code"] == "group_account_name_mismatch")
    assert issue["severity"] == "warning"
    assert issue["entity"] == "UK"


def test_run_works_without_gemini_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    response = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


# --- refresh / persistence --------------------------------------------------


def test_get_run_returns_persisted_result(client: TestClient) -> None:
    created = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()

    fetched = client.get(f"/api/consolidations/runs/{created['run_id']}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["run_id"] == created["run_id"]
    assert body["readiness"]["complete"] is True
    assert set(body["readiness"]["entities_detail"]) == {"US", "UK", "IN"}


def test_get_unknown_run_returns_404(client: TestClient) -> None:
    response = client.get("/api/consolidations/runs/run_does_not_exist")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_list_runs_returns_history_newest_first(client: TestClient) -> None:
    first = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()
    second = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()

    listing = client.get("/api/consolidations/runs")
    assert listing.status_code == 200
    runs = listing.json()["runs"]
    ids = [r["run_id"] for r in runs]
    assert first["run_id"] in ids and second["run_id"] in ids
    # Newest first.
    assert ids.index(second["run_id"]) <= ids.index(first["run_id"])

    row = next(r for r in runs if r["run_id"] == second["run_id"])
    assert row["status"] == "completed"
    assert row["complete"] is True
    assert row["group_profit"] is not None
    assert row["total_assets"] is not None
    # The listing stays compact: no heavy statement payloads.
    assert "statements" not in row


def test_trace_endpoint_returns_events_and_tools(client: TestClient) -> None:
    created = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()

    trace = client.get(
        f"/api/consolidations/runs/{created['run_id']}/trace"
    )
    assert trace.status_code == 200
    body = trace.json()
    assert body["run_id"] == created["run_id"]
    assert body["trace_id"] == created["trace_id"]

    events = [e["event"] for e in body["events"]]
    assert events[0] == "execution_started"
    assert "skill_selected" in events
    assert "tool_call" in events
    assert events[-1] == "execution_completed"
    # tool calls are also exposed directly on the trace payload
    assert any(t["name"] == "get_trial_balance" for t in body["tool_calls"])


def test_trace_does_not_leak_secrets(client: TestClient) -> None:
    created = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()
    body = client.get(
        f"/api/consolidations/runs/{created['run_id']}/trace"
    ).json()
    blob = str(body)
    assert "GEMINI_API_KEY" not in blob
    assert "AIza" not in blob


# --- invalid request --------------------------------------------------------


def test_invalid_period_returns_400(client: TestClient) -> None:
    response = client.post(
        "/api/consolidations/runs", json={"period": "FY2020", "scope": "group"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "failed"
    # the skill rejected the unsupported period with a structured issue
    assert [i["code"] for i in body["issues"]] == ["invalid_period"]


def test_missing_period_defaults_and_runs(client: TestClient) -> None:
    # period has a default (FY2024), so an empty body is a valid run
    response = client.post("/api/consolidations/runs", json={})
    assert response.status_code == 200
    assert response.json()["readiness"]["period"] == "2024"


def test_extra_unknown_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/consolidations/runs",
        json={"period": "FY2024", "scope": "group", "sneaky": True},
    )
    assert response.status_code == 422  # pydantic extra=forbid


# --- execution error --------------------------------------------------------


def test_execution_error_is_preserved(tmp_path: Path) -> None:
    """A skill that raises surfaces as a typed failure, not a 500 crash."""

    class ExplodingSkill(ConsolidationSkill):
        async def execute(self, context):  # noqa: ANN001
            raise RuntimeError("kaboom")

    server = build_mcp_server(SourceDataService(REPO_DATA))
    mcp_client = MCPClient(
        transport_factory=lambda: _memory_transport(server), settings=app_settings
    )
    storage_root = tmp_path / "s2"
    runtime = AgentRuntime(
        registry=SkillRegistry([ExplodingSkill()]),
        llm_provider=_no_llm,
        thread_service=ThreadService(storage_root / "threads"),
        traces_dir=storage_root / "traces",
        settings=app_settings,
        tool_gateway=ToolGateway(mcp_client, settings=app_settings),
    )
    app = create_app(
        runner_factory=lambda: AgentRunner(runtime, build_graph(runtime))
    )
    app.state.run_store = ConsolidationRunStore(storage_root / "runs")
    app.state.traces_dir = runtime.traces_dir
    storage_root.mkdir(parents=True, exist_ok=True)

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/consolidations/runs",
            json={"period": "FY2024", "scope": "group"},
        )

    assert response.status_code == 500
    body = response.json()
    assert [e["code"] for e in body["errors"]] == ["skill_execution_failed"]
    assert "kaboom" in body["errors"][0]["message"]


# --- AI-assisted observability ----------------------------------------------

_PLAN_JSON = (
    '{"period":"2024","entities":["US","UK","IN"],"presentation_currency":"GBP",'
    '"workflow":"group_consolidation","summary":"plan","steps":[],'
    '"required_documents":[],"approval_stages":[],"confidence":"high"}'
)

_MAPPING_JSON = (
    '{"entity":"UK","local_code":"2310","local_name":"Deferred income",'
    '"current_group_code":"4100","proposed_group_code":"2300",'
    '"proposed_group_name":"Deferred income","confidence":"high",'
    '"reasoning":"name matches 2300","evidence":[],"needs_human_review":true}'
)

_ANOMALY_JSON = '{"summary":"advisory","findings":[],"note":"advisory only"}'

_MATCHING_JSON = (
    '{"proposals":[{"left_row":1,"right_row":6,"match_type":"reciprocal",'
    '"confidence":"high","reasoning":"same ref","proposed_difference_reason":"fx"}],'
    '"unmatched_notes":["row 5 unmatched"],"summary":"one proposal"}'
)


def _scripted_llm_provider():
    """A provider returning scripted structured JSON (never any network)."""
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    responses = [_PLAN_JSON, _MAPPING_JSON, _ANOMALY_JSON, _MATCHING_JSON]

    def provider():
        return FakeMessagesListChatModel(
            responses=[AIMessage(content=r) for r in responses]
        )

    return provider


@pytest.fixture
def ai_client(tmp_path: Path) -> TestClient:
    """A TestClient whose agent runtime has a (fake) LLM configured."""
    if not REAL_DATASET.is_dir():
        pytest.skip("real dataset not present")

    server = build_mcp_server(SourceDataService(REPO_DATA))
    mcp_client = MCPClient(
        transport_factory=lambda: _memory_transport(server), settings=app_settings
    )
    storage_root = tmp_path / "storage"
    runtime = AgentRuntime(
        registry=SkillRegistry([_real_skill()]),
        llm_provider=_scripted_llm_provider(),
        thread_service=ThreadService(storage_root / "threads"),
        traces_dir=storage_root / "traces",
        settings=app_settings,
        tool_gateway=ToolGateway(mcp_client, settings=app_settings),
    )
    app = create_app(
        runner_factory=lambda: AgentRunner(runtime, build_graph(runtime))
    )
    app.state.run_store = ConsolidationRunStore(storage_root / "runs")
    app.state.traces_dir = runtime.traces_dir
    storage_root.mkdir(parents=True, exist_ok=True)

    with TestClient(app) as test_client:
        yield test_client


def test_run_without_llm_reports_not_configured(client: TestClient) -> None:
    body = client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()
    assert body["ai_configured"] is False
    statuses = {s["stage"]: s["status"] for s in body["ai_stages"]}
    assert statuses.get("ai_execution_plan") == "not_configured"
    assert body["ai_reasoning"] == {}


def test_run_with_llm_reports_ai_activity(ai_client: TestClient) -> None:
    body = ai_client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()

    assert body["ai_configured"] is True
    statuses = {s["stage"]: s["status"] for s in body["ai_stages"]}
    assert statuses.get("ai_execution_plan") == "completed"
    assert statuses.get("ai_intercompany_matching") == "completed"

    reasoning = body["ai_reasoning"]
    assert reasoning["execution_plan"]["period"] == "2024"
    proposal = reasoning["mapping_proposals"][0]
    assert proposal["entity"] == "UK"
    assert proposal["local_code"] == "2310"
    assert proposal["proposed_group_code"] == "2300"
    assert proposal["needs_human_review"] is True
    # deterministic issue is still present alongside the AI proposal
    codes = {i["code"] for i in body["issues"]}
    assert "group_account_name_mismatch" in codes


def test_trace_records_llm_invocations(ai_client: TestClient) -> None:
    created = ai_client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()
    body = ai_client.get(
        f"/api/consolidations/runs/{created['run_id']}/trace"
    ).json()
    events = [e["event"] for e in body["events"]]
    assert "llm_invocation_started" in events
    assert "llm_invocation_completed" in events


def test_trace_never_leaks_prompt_or_key_with_llm(ai_client, monkeypatch) -> None:
    secret = "AIzaSyFAKEKEYFAKEKEYFAKEKEYFAKEKEY12345"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    created = ai_client.post(
        "/api/consolidations/runs", json={"period": "FY2024", "scope": "group"}
    ).json()
    body = ai_client.get(
        f"/api/consolidations/runs/{created['run_id']}/trace"
    ).json()
    blob = str(body)
    assert secret not in blob
    assert "GEMINI_API_KEY" not in blob
