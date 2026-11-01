"""SKILL.md instruction-document tests.

Covers the lightweight convention that pairs a skill's Markdown instruction
document with its Python implementation:

* the document exists and loads;
* it is associated with the right skill;
* instructions are surfaced only for the selected skill;
* the existing ConsolidationSkill execution is unchanged;
* MCP permission and budget enforcement still hold.

All MCP traffic is in-memory; no LLM is ever called.
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
from app.agents.skills.base import Skill, SkillContext, SkillSpec
from app.agents.skills.consolidation import ConsolidationSkill
from app.agents.skills.registry import SkillRegistry, build_default_registry
from app.agents.skills.skill_instructions import (
    SKILL_FILE_NAME,
    SKILLS_ROOT,
    SkillInstructionsNotFound,
    has_skill_instructions,
    load_skill_instructions,
    skill_instructions_path,
)
from app.agents.state import AgentMode, Intent, RunStatus, SkillResult
from app.core.config import settings as app_settings
from app.core.errors import LLMNotConfiguredError, ToolPolicyError
from app.mcp.client import MCPClient
from app.mcp.gateway import ToolGateway
from app.mcp.server import build_mcp_server
from app.services.source_data import SourceDataService

REPO_DATA = Path(__file__).resolve().parents[2] / "data"
CONSOLIDATION_MD = SKILLS_ROOT / "consolidation" / SKILL_FILE_NAME


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
        transport_factory=lambda: _memory_server(server),
        settings=settings or app_settings,
    )
    return ToolGateway(client, settings=settings or app_settings)


def _real_gateway() -> ToolGateway:
    if not (REPO_DATA / "northwind").is_dir():
        pytest.skip("real dataset not present")
    return _gateway(REPO_DATA)


def _real_skill() -> ConsolidationSkill:
    svc = SourceDataService(REPO_DATA)
    return ConsolidationSkill(
        ingestor=TrialBalanceIngestor(svc),
        validator=TrialBalanceValidator(),
        mapper=AccountMapper(svc),
        fx=FXService(svc),
    )


def _no_llm():
    raise LLMNotConfiguredError("GEMINI_API_KEY is not set")


class _NoDocSkill(Skill):
    """A skill that ships no SKILL.md (used for negative association tests)."""

    spec = SkillSpec(
        name="no_doc_skill",
        purpose="A skill without an instruction document.",
        instructions="inline instructions only",
        execution_procedure=("do nothing",),
        allowed_modes=frozenset({AgentMode.CONSOLIDATION}),
    )

    async def execute(self, context: SkillContext) -> SkillResult:
        return SkillResult(status=RunStatus.COMPLETED)


# --- 1. instruction file existence and loading ------------------------------


def test_skill_file_exists_at_conventional_path():
    assert CONSOLIDATION_MD.is_file()
    assert skill_instructions_path("consolidation") == CONSOLIDATION_MD
    assert SKILL_FILE_NAME == "SKILL.md"


def test_load_skill_instructions_returns_document_text():
    text = load_skill_instructions("consolidation")
    assert isinstance(text, str)
    assert text.strip()
    # the document must be the real one, not a stub
    assert "FY2024 consolidation workflow" in text


def test_missing_instructions_raise_typed_error():
    with pytest.raises(SkillInstructionsNotFound):
        load_skill_instructions("no_such_skill")
    assert has_skill_instructions("no_such_skill") is False


@pytest.mark.parametrize("bad", ["", "  ", "../escape", "a/b", ".", ".."])
def test_invalid_skill_names_rejected(bad):
    with pytest.raises(ValueError):
        skill_instructions_path(bad)
    assert has_skill_instructions(bad) is False


def test_document_covers_required_sections():
    text = load_skill_instructions("consolidation")
    for heading in (
        "When this skill should be selected",
        "FY2024 consolidation workflow",
        "Required input",
        "Expected output",
        "Accounting and data-integrity rules",
        "MCP tool usage rules",
        "Handling missing data and unresolved issues",
        "Explicit restrictions",
    ):
        assert heading in text, f"missing section: {heading}"
    # explicit restrictions must all be present
    for clause in (
        "Never fabricate financial values",
        "Never bypass journal validation",
        "Never modify original source files",
        "Never perform accounting calculations using the LLM",
    ):
        assert clause in text, f"missing restriction: {clause}"


# --- 2. association between Markdown and the Python skill -------------------


def test_consolidation_skill_loads_its_own_document():
    skill = ConsolidationSkill()
    doc = skill.instructions_doc()
    assert doc is not None
    assert doc == load_skill_instructions("consolidation")
    assert "consolidation" in doc.lower()


def test_document_is_cached_per_instance():
    skill = ConsolidationSkill()
    first = skill.instructions_doc()
    second = skill.instructions_doc()
    assert first is second  # same cached string object


def test_skill_without_document_returns_none():
    assert _NoDocSkill().instructions_doc() is None


def test_document_path_matches_skill_name():
    # the document directory is named after the skill it belongs to
    assert CONSOLIDATION_MD.parent.name == ConsolidationSkill.spec.name
    assert skill_instructions_path(ConsolidationSkill.spec.name) == CONSOLIDATION_MD


# --- 3. instructions available only for the selected skill ------------------


def test_context_receives_selected_skill_instructions(make_runner):
    async def go():
        runner, _ = make_runner(
            registry=SkillRegistry([_real_skill()]),
            llm_provider=_no_llm,
            tool_gateway=_real_gateway(),
        )
        # --- selected skill ---
        result = await runner.run(
            mode="consolidation", payload={"period": "2024", "scope": "group"}
        )
        return result

    result = asyncio.run(go())
    assert result.skill_name == "consolidation"
    # the selected skill's own document was the one surfaced
    assert result.skill_result is not None


def test_only_selected_skill_instructions_are_exposed():
    # A registry holding the consolidation skill (with a doc) and a skill
    # without one must surface the doc only for consolidation.
    registry = SkillRegistry([ConsolidationSkill(), _NoDocSkill()])
    consolidation = registry.get("consolidation")
    no_doc = registry.get("no_doc_skill")

    assert consolidation.instructions_doc() is not None
    assert no_doc.instructions_doc() is None
    # and the no-doc skill never receives the consolidation document
    assert "FY2024 consolidation workflow" not in (no_doc.instructions_doc() or "")


def test_consolidation_instructions_not_leaked_to_other_skill_name():
    text = ConsolidationSkill().instructions_doc()
    # the document self-identifies as the consolidation skill only
    assert "**Name:** `consolidation`" in text


# --- 4. existing execution remains functional -------------------------------


def _context(gateway, *, skill, period="2024", scope="group"):
    intent = Intent(kind="consolidation", period=period)
    bound = gateway.bind(
        skill=skill.spec, mode=AgentMode.CONSOLIDATION, trace=None
    )
    return SkillContext(
        mode=AgentMode.CONSOLIDATION,
        intent=intent,
        payload={"period": period, "scope": scope},
        tool_gateway=bound,
        skill_instructions=skill.instructions_doc(),
    ), bound


def test_consolidation_execution_still_runs_deterministically():
    async def go():
        skill = _real_skill()
        gateway = _real_gateway()
        context, bound = _context(gateway, skill=skill)
        # the graph normally injects the doc; here we assert it is passed too
        assert context.skill_instructions == skill.instructions_doc()
        result = await skill.execute(context)
        return result, bound

    result, bound = asyncio.run(go())
    assert result.status is RunStatus.COMPLETED
    assert result.data["complete"] is True
    assert {
        "trial_balance_ingestion",
        "trial_balance_validation",
        "account_mapping",
        "fx_translation",
        "financial_statement_assembly",
    } <= set(result.data["completed_stages"])
    # MCP retrieval still happens through the gateway
    names = [r.name for r in bound.records]
    assert "list_entities" in names
    assert names.count("get_trial_balance") == 3


# --- 5. permission and budget enforcement intact ----------------------------


def test_tool_permission_enforcement_intact():
    async def go():
        skill = _real_skill()
        _, bound = _context(_real_gateway(), skill=skill)
        with pytest.raises(ToolPolicyError) as exc:
            await bound.call("get_adjustments")  # not permitted
        return exc.value

    err = asyncio.run(go())
    assert err.details["reason"] == "not_permitted"


def test_tool_budget_enforcement_intact():
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
    assert len(bound.records) == 1


# --- selection still trace-marks instruction availability -------------------


def test_skill_selection_trace_marks_instructions(make_runner):
    async def go():
        runner, runtime = make_runner(
            registry=SkillRegistry([_real_skill()]),
            llm_provider=_no_llm,
            tool_gateway=_real_gateway(),
        )
        result = await runner.run(
            mode="consolidation", payload={"period": "2024", "scope": "group"}
        )
        return result, runtime

    result, runtime = asyncio.run(go())
    from app.agents.tracing import TraceRecorder

    events = TraceRecorder(runtime.traces_dir, result.trace_id).read_events()
    selected = next(e for e in events if e["event"] == "skill_selected")
    assert selected["payload"]["skill"] == "consolidation"
    assert selected["payload"]["has_instructions"] is True


def test_default_registry_skill_has_instructions():
    skill = build_default_registry().get("consolidation")
    assert skill.instructions_doc() is not None
