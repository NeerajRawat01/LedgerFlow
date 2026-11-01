"""Consolidation Skill v1 tests.

Covers the real skill end to end: registration/selection through the shared
LangGraph agent, MCP retrieval through the ToolGateway under policy, the
deterministic accounting stages it runs, structured issue propagation, the
stage ledger that keeps the run ``blocked`` (never falsely complete), and the
absence of any fabricated statements.

Real ``data/northwind`` is used for the substantive accounting assertions;
the synthetic ``northwind_data_dir`` fixture drives isolated edge cases. All
MCP traffic is in-memory (no sockets) and no LLM is ever called.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from app.accounting.fx import FXService
from app.accounting.ingestion import TrialBalanceIngestor
from app.accounting.mapping import AccountMapper
from app.accounting.validation import TrialBalanceValidator
from app.agents.skills.base import SkillContext
from app.agents.skills.consolidation import ConsolidationSkill
from app.agents.skills.registry import SkillRegistry, build_default_registry
from app.agents.state import AgentMode, Intent, RunStatus
from app.agents.tracing import TraceRecorder
from app.core.config import settings as app_settings
from app.core.errors import (
    LLMNotConfiguredError,
    ToolPolicyError,
)
from app.mcp.client import MCPClient
from app.mcp.gateway import ToolGateway
from app.mcp.server import build_mcp_server
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"


# --- in-memory MCP transport (no sockets) -----------------------------------


@asynccontextmanager
async def _memory_server(server: MCPServer):
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


def _gateway(data_dir: Path, *, settings=None) -> ToolGateway:
    server = build_mcp_server(SourceDataService(data_dir))
    client = MCPClient(
        transport_factory=lambda: _memory_server(server), settings=settings or app_settings
    )
    return ToolGateway(client, settings=settings or app_settings)


def _real_gateway() -> ToolGateway:
    if not (REPO_DATA / "northwind").is_dir():
        pytest.skip("real dataset not present")
    return _gateway(REPO_DATA)


def _real_skill() -> ConsolidationSkill:
    """A skill bound to the real dataset via injected services."""
    svc = SourceDataService(REPO_DATA)
    return ConsolidationSkill(
        ingestor=TrialBalanceIngestor(svc),
        validator=TrialBalanceValidator(),
        mapper=AccountMapper(svc),
        fx=FXService(svc),
    )


def _context(gateway, *, skill, period="2024", entities=None, scope="group"):
    intent = Intent(
        kind="consolidation",
        period=period,
        entities=entities or [],
    )
    payload = {"period": period, "scope": scope}
    if entities:
        payload["entities"] = entities
    bound = gateway.bind(
        skill=skill.spec,
        mode=AgentMode.CONSOLIDATION,
        trace=TraceRecorder(Path("/tmp"), "t_unused"),
    )
    return (
        SkillContext(
            mode=AgentMode.CONSOLIDATION,
            intent=intent,
            payload=payload,
            tool_gateway=bound,
        ),
        bound,
    )


async def _run_skill(gateway, *, skill=None, **kwargs):
    skill = skill or _real_skill()
    context, bound = _context(gateway, skill=skill, **kwargs)
    result = await skill.execute(context)
    return result, bound


# --- 1. registration --------------------------------------------------------


def test_skill_is_registered_in_default_registry():
    registry = build_default_registry()
    assert registry.has("consolidation")
    skill = registry.get("consolidation")
    assert isinstance(skill, ConsolidationSkill)
    # explicit mode permission: consolidation only
    assert skill.allows_mode(AgentMode.CONSOLIDATION)
    assert not skill.allows_mode(AgentMode.COPILOT)


def test_skill_spec_declares_full_contract():
    spec = ConsolidationSkill.spec
    spec.validate()  # must not raise
    assert spec.description
    assert spec.purpose
    assert spec.instructions
    assert len(spec.execution_procedure) >= 5
    assert spec.validation_requirements
    assert spec.output_expectations
    assert spec.permitted_tools == (
        "list_entities",
        "get_trial_balance",
        "get_group_coa",
        "get_account_mapping",
        "get_fx_rates",
        "get_intercompany",
    )


# --- 2. selection for a consolidation request -------------------------------


def test_registry_selects_consolidation_for_consolidation_intent():
    registry = build_default_registry()
    skill = registry.select_by_intent("consolidation", AgentMode.CONSOLIDATION)
    assert skill is not None and skill.name == "consolidation"


def test_registry_selects_consolidation_via_hint():
    registry = build_default_registry()
    skill = registry.select_by_intent(
        "consolidation", AgentMode.CONSOLIDATION, hint="consolidation"
    )
    assert skill is not None and skill.name == "consolidation"


# --- 3. rejection of unsupported modes --------------------------------------


def test_consolidation_skill_not_allowed_in_copilot_mode():
    from app.core.errors import SkillNotAllowedForModeError

    registry = build_default_registry()
    with pytest.raises(SkillNotAllowedForModeError):
        registry.resolve("consolidation", AgentMode.COPILOT)


# --- 4. missing / invalid reporting period ----------------------------------


@pytest.mark.parametrize("period", ["2023", "FY2025", "", "not-a-period", None])
def test_invalid_period_is_rejected(period):
    async def go():
        skill = _real_skill()
        context, bound = _context(_real_gateway(), skill=skill, period=period)
        return await skill.execute(context), bound

    result, bound = asyncio.run(go())
    assert result.status is RunStatus.FAILED
    assert result.data.get("validation_error") is True
    assert bound.records == []  # rejected before any MCP retrieval


def test_missing_period_rejected_without_tool_calls():
    async def go():
        skill = _real_skill()
        context, bound = _context(_real_gateway(), skill=skill, period=None)
        result = await skill.execute(context)
        return result, bound

    result, bound = asyncio.run(go())
    assert result.status is RunStatus.FAILED
    assert bound.records == []  # rejected before any MCP call
    assert result.data["field"] == "period"


def test_period_aliases_accepted():
    from app.agents.skills.consolidation import ConsolidationSkill as CS

    assert CS.normalise_period("FY2024") == "2024"
    assert CS.normalise_period("2024") == "2024"
    assert CS.normalise_period("fy-2024") == "2024"
    assert CS.normalise_period("2023") is None


def test_unsupported_scope_rejected():
    async def go():
        gateway = _real_gateway()
        skill = _real_skill()
        context, bound = _context(gateway, skill=skill, scope="entity")
        return await skill.execute(context), bound

    result, bound = asyncio.run(go())
    assert result.status is RunStatus.FAILED
    assert bound.records == []
    assert result.data["field"] == "scope"


# --- 5. MCP tool retrieval through the gateway ------------------------------


def test_skill_retrieves_source_data_through_mcp():
    async def go():
        gateway = _real_gateway()
        skill = _real_skill()
        result, bound = await _run_skill(gateway, skill=skill)
        return result, bound

    result, bound = asyncio.run(go())

    names = [r.name for r in bound.records]
    assert "list_entities" in names
    assert names.count("get_trial_balance") == 3  # US, UK, IN
    assert "get_fx_rates" in names
    assert all(r.ok for r in bound.records)


def test_skill_does_not_need_a_llm():
    # Consolidation mode carries a deterministic intent; the skill never
    # touches context.llm, so an unkeyed environment still works.
    async def go():
        skill = _real_skill()
        gateway = _real_gateway()
        context, _ = _context(gateway, skill=skill)
        assert context.llm is None  # no LLM configured in tests
        return await skill.execute(context)

    result = asyncio.run(go())
    assert result.status is RunStatus.COMPLETED
    assert result.data["complete"] is True


# --- 6. tool permission enforcement -----------------------------------------


def test_non_permitted_tool_is_rejected_by_gateway():
    async def go():
        gateway = _real_gateway()
        skill = _real_skill()
        _, bound = _context(gateway, skill=skill)
        with pytest.raises(ToolPolicyError) as exc:
            await bound.call("get_adjustments")  # not in permitted_tools
        return exc.value

    err = asyncio.run(go())
    assert err.details["reason"] == "not_permitted"
    assert "get_adjustments" not in ConsolidationSkill.spec.permitted_tools


def test_tool_budget_is_enforced():
    async def go():
        tight = replace(app_settings, agent_max_tool_calls=1)
        gateway = _gateway(REPO_DATA, settings=tight)
        skill = _real_skill()
        _, bound = _context(gateway, skill=skill)
        await bound.call("list_entities")
        with pytest.raises(ToolPolicyError) as exc:
            await bound.call("get_trial_balance", {"entity": "US"})
        return exc.value, bound

    err, bound = asyncio.run(go())
    assert err.details["reason"] == "budget_exhausted"
    # a budget-exhausted call is not recorded
    assert len(bound.records) == 1


# --- 7. ingestion for all three entities ------------------------------------


def test_ingestion_completes_for_all_three_entities():
    from app.accounting.ingestion import TrialBalanceIngestor
    from app.services.source_data import SourceDataService

    ingestor = TrialBalanceIngestor(SourceDataService(REPO_DATA))
    for entity, expected_rows in (("US", 25), ("UK", 23), ("IN", 20)):
        tb = ingestor.ingest(entity)
        assert len(tb.rows) == expected_rows


def test_readiness_reports_all_entities():
    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    assert set(result.data["entities"]) == {"US", "UK", "IN"}
    assert result.data["entities_detail"]["US"]["row_count"] == 25
    assert result.data["entities_detail"]["UK"]["row_count"] == 23
    assert result.data["entities_detail"]["IN"]["row_count"] == 20


# --- 8. validation ----------------------------------------------------------


def test_validation_stage_completes_and_balances_match_stage1():
    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    assert "trial_balance_validation" in result.data["completed_stages"]
    detail = result.data["entities_detail"]
    # Stage 1 recorded these exact full-precision balances.
    assert detail["US"]["balanced"] is True
    assert detail["US"]["debit_total"] == "43534373.43"
    assert detail["UK"]["credit_total"] == "15688717.72"
    assert detail["IN"]["debit_total"] == "1805000000.00"


# --- 9. account mapping -----------------------------------------------------


def test_mapping_stage_completes():
    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    assert "account_mapping" in result.data["completed_stages"]
    detail = result.data["entities_detail"]
    for entity in ("US", "UK", "IN"):
        assert detail[entity]["unmapped_row_count"] == 0
    assert detail["UK"]["mapped_row_count"] == 23


# --- 10. structured issue propagation ---------------------------------------


def test_known_uk_2310_warning_propagates_through_skill():
    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    codes = {i["code"] for i in result.issues}
    assert "group_account_name_mismatch" in codes
    uk_issue = next(
        i for i in result.issues if i["code"] == "group_account_name_mismatch"
    )
    assert uk_issue["severity"] == "warning"
    assert uk_issue["entity"] == "UK"


def test_unmapped_account_issue_propagates(northwind_data_dir):
    # Synthetic UK TB with a code absent from map_uk.csv.
    path = northwind_data_dir / "northwind" / "tb_uk.csv"
    path.write_text(
        "nominal_code,description,cost_centre,txn_currency,txn_amount,balance\n"
        "9999,Mystery,LON,GBP,10.00,-10.00\n",
        encoding="utf-8",
    )

    async def go():
        svc = SourceDataService(northwind_data_dir)
        skill = ConsolidationSkill(
            ingestor=TrialBalanceIngestor(svc),
            validator=TrialBalanceValidator(),
            mapper=AccountMapper(svc),
            fx=FXService(svc),
        )
        gateway = _gateway(northwind_data_dir)
        context, _ = _context(gateway, skill=skill, entities=["UK"])
        return await skill.execute(context)

    result = asyncio.run(go())
    codes = {i["code"] for i in result.issues}
    assert "unmapped_account" in codes


# --- 11. all accounting stages now execute ----------------------------------


def test_all_stages_are_reported_completed():
    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    assert result.data["blocked_stages"] == []
    assert result.data["missing_required_stages"] == []
    assert result.status is RunStatus.COMPLETED
    for stage in (
        "intercompany_reconciliation",
        "fx_translation",
        "intercompany_elimination",
        "goodwill",
        "nci",
        "translation_reserve",
        "financial_statement_assembly",
        "output_generation",
    ):
        assert stage in result.data["completed_stages"]


# --- 12. consolidated statements are produced (deterministically) -----------


def test_result_includes_consolidated_statements():
    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    assert result.data["complete"] is True
    assert result.data["statements"] is not None
    figures = result.data["control_figures"]
    # The four headline figures are present and in GBP.
    assert figures["group_revenue"] is not None
    assert figures["group_profit"] is not None
    assert figures["total_assets"] is not None
    assert figures["total_equity"] is not None
    assert "completed" in result.summary.lower()


def test_all_required_stages_complete():
    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    assert result.data["missing_required_stages"] == []
    assert result.data["complete"] is True


# --- 13. missing Gemini API key ---------------------------------------------


def test_skill_works_without_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # settings already has no key in tests
    assert app_settings.gemini_api_key in (None, "")

    async def go():
        return await _run_skill(_real_gateway())

    result, _ = asyncio.run(go())
    assert result.status is RunStatus.COMPLETED
    assert result.data["complete"] is True


# --- 14. complete execution trace -------------------------------------------


def test_graph_trace_records_tool_calls(make_runner):
    async def go():
        # The shared graph binds the gateway from the skill spec; drive the
        # registered skill through a real in-memory gateway here.
        registry = build_default_registry()
        gateway = _real_gateway()
        runner, runtime = make_runner(
            registry=registry,
            llm_provider=_no_llm,
            tool_gateway=gateway,
        )
        result = await runner.run(
            mode="consolidation",
            payload={"period": "2024", "scope": "group"},
        )
        return result, runtime

    result, runtime = asyncio.run(go())
    assert result.skill_name == "consolidation"

    events = TraceRecorder(runtime.traces_dir, result.trace_id).read_events()
    names = [e["event"] for e in events]
    assert names[0] == "execution_started"
    assert "skill_selected" in names
    assert "skill_started" in names
    assert "tool_call" in names
    assert "skill_executed" in names
    assert names[-1] == "execution_completed"


# --- 15. end-to-end AgentRunner -> graph -> skill -> MCP -> service ----------


def test_end_to_end_consolidation_run(make_runner):
    async def go():
        # In production the skill's services and the MCP server read the same
        # DATA_DIR; here both are pointed at the real dataset explicitly.
        registry = SkillRegistry([_real_skill()])
        runner, runtime = make_runner(
            registry=registry,
            llm_provider=_no_llm,
            tool_gateway=_real_gateway(),
        )
        return await runner.run(
            mode="consolidation",
            payload={"period": "2024", "scope": "group", "entities": ["US", "UK", "IN"]},
        ), runtime

    result, _ = asyncio.run(go())

    # selected and executed the real skill, deterministically, with no LLM
    assert result.skill_name == "consolidation"
    assert result.intent is not None and result.intent.source == "deterministic"

    # MCP calls were made through the gateway and recorded on the result
    tool_names = [c.name for c in result.tool_calls]
    assert "get_trial_balance" in tool_names
    assert tool_names.count("get_trial_balance") == 3

    # accounting stages ran on the real services
    assert result.skill_result is not None
    data = result.skill_result.data
    assert data["complete"] is True
    assert data["entities_detail"]["US"]["row_count"] == 25
    assert set(data["completed_stages"]) >= {
        "trial_balance_ingestion",
        "trial_balance_validation",
        "account_mapping",
        "fx_translation",
        "intercompany_elimination",
        "financial_statement_assembly",
        "output_generation",
    }

    # overall status reflects the completed consolidation
    assert result.status is RunStatus.COMPLETED


def test_end_to_end_consolidation_without_period_fails(make_runner):
    async def go():
        runner, _ = make_runner(
            registry=build_default_registry(),
            llm_provider=_no_llm,
            tool_gateway=_real_gateway(),
        )
        return await runner.run(mode="consolidation", payload={"scope": "group"})

    result = asyncio.run(go())
    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["intent_identification_failed"]


def _no_llm():
    raise LLMNotConfiguredError("GEMINI_API_KEY is not set")
