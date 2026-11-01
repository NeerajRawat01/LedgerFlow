"""Core LangGraph agent tests (Milestone 2).

Covers the shared graph: intent identification for both modes, skill
selection, execution, failure routing, and the typed statuses/errors the
API layer will expose. All LLMs are fakes — no network access.
"""

from __future__ import annotations

import pytest

from app.agents.graph import (
    _coerce_intent,
    _extract_json,
    consolidation_intent,
    heuristic_copilot_intent,
)
from app.agents.skills.registry import SkillRegistry
from app.agents.state import AgentMode, RunStatus, SkillResult
from app.agents.tracing import TraceRecorder
from app.core.errors import LLMNotConfiguredError
from tests.conftest import StubAnalysisSkill, StubConsolidationSkill, fake_llm


# --- intent helpers ---------------------------------------------------------


def test_extract_json_plain_and_fenced():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('Sure!\n```json\n{"a": 1}\n```\nthanks') == {"a": 1}
    assert _extract_json("no json here") is None
    assert _extract_json("{broken}") is None


def test_coerce_intent_normalises_types():
    intent = _coerce_intent(
        {
            "kind": 123,
            "period": 2024,
            "entities": "US",
            "confidence": "1.7",
            "requires_tools": "yes",
        }
    )
    assert intent.kind == "123"
    assert intent.period == "2024"
    assert intent.entities == ["US"]
    assert intent.confidence == 1.0
    assert intent.source == "llm"


def test_consolidation_intent_is_deterministic():
    intent = consolidation_intent(
        {"period": 2024, "scope": "group", "entities": ["US", "UK"]}
    )
    assert intent.source == "deterministic"
    assert intent.period == "2024"
    assert intent.entities == ["US", "UK"]
    assert intent.confidence == 1.0


def test_heuristic_intent_fallback_extracts_period_and_entities():
    intent = heuristic_copilot_intent("Compare UK vs US margin for 2023")
    assert intent.source == "heuristic"
    assert intent.period == "2023"
    assert set(intent.entities) == {"UK", "US"}
    assert intent.confidence < 0.5


# --- consolidation mode -----------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_run_is_deterministic_without_llm(make_runner):
    runner, runtime = make_runner(llm_provider=_no_llm)

    result = await runner.run(
        mode="consolidation",
        payload={"period": "2024", "scope": "group", "entities": ["US", "UK"]},
    )

    assert result.status is RunStatus.BLOCKED
    assert result.skill_name == "consolidation"
    assert result.intent is not None and result.intent.source == "deterministic"
    assert "fx_translation" in result.summary
    # stateless run: no thread
    assert result.thread_id is None


@pytest.mark.asyncio
async def test_consolidation_without_period_fails_with_typed_error(make_runner):
    runner, _ = make_runner()

    result = await runner.run(mode="consolidation", payload={"scope": "group"})

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["intent_identification_failed"]
    assert result.intent is None
    assert result.skill_name is None


@pytest.mark.asyncio
async def test_consolidation_skill_hint_unknown_skill_fails(make_runner):
    runner, _ = make_runner()

    result = await runner.run(
        mode="consolidation",
        payload={"period": "2024", "skill": "does_not_exist"},
    )

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["skill_not_found"]


@pytest.mark.asyncio
async def test_consolidation_skill_failure_is_typed(make_runner):
    class ExplodingSkill(StubConsolidationSkill):
        async def execute(self, context):
            raise RuntimeError("kaboom")

    registry = SkillRegistry([ExplodingSkill(), StubAnalysisSkill()])
    runner, _ = make_runner(registry=registry)

    result = await runner.run(mode="consolidation", payload={"period": "2024"})

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["skill_execution_failed"]
    assert "kaboom" in result.errors[0].message


@pytest.mark.asyncio
async def test_consolidation_status_reflects_skill_result(make_runner):
    ok = SkillResult(
        status=RunStatus.COMPLETED_WITH_WARNINGS,
        summary="consolidated with 2 warnings",
        issues=[{"severity": "warning", "code": "unmapped_account"}],
    )
    registry = SkillRegistry([StubConsolidationSkill(ok), StubAnalysisSkill()])
    runner, _ = make_runner(registry=registry)

    result = await runner.run(mode="consolidation", payload={"period": "2024"})

    assert result.status is RunStatus.COMPLETED_WITH_WARNINGS
    assert result.skill_result is not None
    assert result.skill_result.issues[0]["code"] == "unmapped_account"


# --- copilot mode -----------------------------------------------------------


@pytest.mark.asyncio
async def test_copilot_uses_llm_intent_and_completes(make_runner):
    runner, _ = make_runner()

    result = await runner.run(
        mode="copilot", user_input="Why did revenue grow in 2024?"
    )

    assert result.status is RunStatus.COMPLETED
    assert result.intent is not None
    assert result.intent.source == "llm"
    assert result.intent.kind == "financial_analysis"
    assert result.skill_name == "financial_analysis"
    assert result.thread_id is not None
    # assistant reply is in the returned message list
    assert result.skill_result.answer is not None


@pytest.mark.asyncio
async def test_copilot_without_llm_returns_llm_not_configured(make_runner):
    runner, _ = make_runner(llm_provider=_no_llm)

    result = await runner.run(mode="copilot", user_input="hello?")

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["llm_not_configured"]
    assert isinstance(result.errors[0].details, dict)


@pytest.mark.asyncio
async def test_copilot_llm_garbage_response_falls_back_to_heuristic(make_runner):
    runner, _ = make_runner(llm_provider=fake_llm("I refuse to answer."))

    result = await runner.run(
        mode="copilot", user_input="UK margin trend for 2023?"
    )

    # unparseable LLM output -> heuristic intent, still selects the skill
    assert result.status is RunStatus.COMPLETED
    assert result.intent.source == "heuristic"
    assert result.intent.period == "2023"
    assert "UK" in result.intent.entities


@pytest.mark.asyncio
async def test_copilot_requires_user_input(make_runner):
    runner, _ = make_runner()

    from app.core.errors import ServiceError

    with pytest.raises(ServiceError):
        await runner.run(mode="copilot", user_input="   ")


@pytest.mark.asyncio
async def test_copilot_llm_network_failure_is_typed(make_runner):
    def broken_provider():
        raise ConnectionError("provider unreachable")

    runner, _ = make_runner(llm_provider=broken_provider)

    result = await runner.run(mode="copilot", user_input="how is cash?")

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["llm_invocation_failed"]


@pytest.mark.asyncio
async def test_copilot_unknown_skill_hint_fails_typed(make_runner):
    runner, _ = make_runner(
        llm_provider=fake_llm(
            '{"kind": "financial_analysis", "skill_hint": "ghost_skill", '
            '"confidence": 0.9, "user_goal": "x"}'
        )
    )

    result = await runner.run(mode="copilot", user_input="anything")

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["skill_not_found"]


@pytest.mark.asyncio
async def test_copilot_skill_hallucinated_mode_is_rejected(make_runner):
    # LLM hints the consolidation-only skill from copilot mode.
    runner, _ = make_runner(
        llm_provider=fake_llm(
            '{"kind": "financial_analysis", "skill_hint": "consolidation", '
            '"confidence": 0.9, "user_goal": "x"}'
        )
    )

    result = await runner.run(mode="copilot", user_input="anything")

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["skill_not_allowed_for_mode"]


@pytest.mark.asyncio
async def test_copilot_no_matching_skill_fails_typed(make_runner):
    empty = SkillRegistry([StubConsolidationSkill()])
    runner, _ = make_runner(
        registry=empty,
        llm_provider=fake_llm('{"kind": "unknown_kind", "confidence": 0.9}'),
    )

    result = await runner.run(mode="copilot", user_input="anything")

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["skill_not_found"]


@pytest.mark.asyncio
async def test_failed_turn_does_not_append_assistant_message(make_runner):
    runner, runtime = make_runner(llm_provider=_no_llm)

    result = await runner.run(mode="copilot", user_input="hi")

    assert result.status is RunStatus.FAILED
    saved = runtime.thread_service.load(result.thread_id)
    # question recorded, no fabricated assistant reply
    assert [m.type for m in saved] == ["human"]


# --- conversation continuity ------------------------------------------------


@pytest.mark.asyncio
async def test_thread_history_loads_on_next_turn(make_runner):
    runner, runtime = make_runner()

    first = await runner.run(mode="copilot", user_input="What was revenue?")
    second = await runner.run(
        mode="copilot",
        user_input="And the year before?",
        thread_id=first.thread_id,
    )

    saved = runtime.thread_service.load(first.thread_id)
    assert [m.type for m in saved] == ["human", "ai", "human", "ai"]
    # history handed to the graph for turn 2 includes turn 1
    assert second.thread_id == first.thread_id


@pytest.mark.asyncio
async def test_thread_isolation_between_conversations(make_runner):
    runner, runtime = make_runner()

    a = await runner.run(mode="copilot", user_input="question A")
    b = await runner.run(mode="copilot", user_input="question B")

    assert a.thread_id != b.thread_id
    assert [m.type for m in runtime.thread_service.load(a.thread_id)] == [
        "human",
        "ai",
    ]
    assert [m.type for m in runtime.thread_service.load(b.thread_id)] == [
        "human",
        "ai",
    ]


# --- tracing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_records_full_execution_sequence(make_runner):
    runner, _ = make_runner()

    result = await runner.run(mode="consolidation", payload={"period": "2024"})

    events = TraceRecorder(
        runner.runtime.traces_dir, result.trace_id
    ).read_events()
    names = [e["event"] for e in events]

    assert names[0] == "execution_started"
    assert "intent_identified" in names
    assert "skill_selected" in names
    assert "skill_executed" in names
    assert names[-1] == "execution_completed"
    # sequence numbers are dense and ascending
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["trace_id"] == result.trace_id for e in events)


@pytest.mark.asyncio
async def test_trace_written_even_on_intent_failure(make_runner):
    runner, _ = make_runner()

    result = await runner.run(mode="consolidation", payload={})

    events = TraceRecorder(
        runner.runtime.traces_dir, result.trace_id
    ).read_events()
    names = [e["event"] for e in events]
    assert "intent_failed" in names
    assert names[-1] == "execution_completed"


@pytest.mark.asyncio
async def test_graph_recursion_limit_is_enforced(make_runner):
    from langgraph.errors import GraphRecursionError

    runner, _ = make_runner()

    with pytest.raises(GraphRecursionError):
        await runner.graph.ainvoke(
            {
                "mode": AgentMode.CONSOLIDATION,
                "run_id": "r",
                "thread_id": None,
                "trace_id": "t_rec",
                "user_input": None,
                "payload": {"period": "2024"},
                "history": [],
                "messages": [],
                "errors": [],
                "warnings": [],
                "tool_calls": [],
                "status": RunStatus.RUNNING,
            },
            config={"recursion_limit": 2},
        )


@pytest.mark.asyncio
async def test_runner_maps_crash_to_failed_result(make_runner):
    # A defect escaping the node boundary still returns a typed failure
    # and records a crash event rather than blowing up the caller.
    runner, _ = make_runner()

    async def boom(state, config=None, **kwargs):
        raise RuntimeError("graph exploded")

    runner.graph.ainvoke = boom  # type: ignore[method-assign]

    result = await runner.run(mode="consolidation", payload={"period": "2024"})

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.errors] == ["unexpected_error"]
    events = TraceRecorder(
        runner.runtime.traces_dir, result.trace_id
    ).read_events()
    assert events[-1]["event"] == "execution_crashed"


# --- llm factory ------------------------------------------------------------


def test_create_chat_model_without_key_raises():
    from dataclasses import replace

    from app.agents.llm import create_chat_model
    from app.core.config import settings as app_settings

    with pytest.raises(LLMNotConfiguredError) as exc:
        create_chat_model(replace(app_settings, gemini_api_key=None))
    assert exc.value.code == "llm_not_configured"


def test_create_chat_model_with_key_builds_model():
    from dataclasses import replace

    from app.agents.llm import create_chat_model, is_llm_configured
    from app.core.config import settings as app_settings

    keyed = replace(app_settings, gemini_api_key="test-key-not-real")
    assert is_llm_configured(keyed)
    model = create_chat_model(keyed)
    # the key must never appear in the model's repr
    assert "test-key-not-real" not in repr(model)


def test_settings_repr_never_leaks_key():
    from dataclasses import replace

    from app.core.config import settings as app_settings

    keyed = replace(app_settings, gemini_api_key="AIzaSyFakeKey12345678901234567890123456789")
    assert "AIzaSyFakeKey" not in repr(keyed)
    assert "AIzaSyFakeKey" not in repr(
        replace(app_settings, llm_api_key="gsk_fake_for_repr_check_1234567890")
    )


def test_groq_provider_builds_model():
    from dataclasses import replace

    from app.agents.llm import create_chat_model, is_llm_configured
    from app.core.config import settings as app_settings

    cfg = replace(
        app_settings,
        llm_provider="groq",
        llm_model="openai/gpt-oss-120b",
        llm_api_key="gsk_fake_key_not_real",
    )
    assert is_llm_configured(cfg)
    model = create_chat_model(cfg)
    assert "gsk_fake_key_not_real" not in repr(model)


def test_provider_agnostic_key_fallback(monkeypatch):
    """LLM_API_KEY wins; otherwise the provider-specific env var is used."""
    from dataclasses import replace

    from app.core.config import settings as app_settings

    # Neutral key takes precedence for either provider.
    assert replace(
        app_settings, llm_api_key="neutral", gemini_api_key="legacy"
    ).resolved_llm_api_key == "neutral"

    # Gemini falls back to the legacy GEMINI_API_KEY.
    assert replace(
        app_settings,
        llm_provider="gemini",
        llm_api_key=None,
        gemini_api_key="legacy",
    ).resolved_llm_api_key == "legacy"

    # Groq falls back to GROQ_API_KEY from the environment.
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env_fallback")
    assert replace(
        app_settings, llm_provider="groq", llm_api_key=None
    ).resolved_llm_api_key == "gsk_env_fallback"


def test_unsupported_provider_raises():
    from dataclasses import replace

    from app.agents.llm import create_chat_model
    from app.core.config import settings as app_settings

    with pytest.raises(LLMNotConfiguredError):
        create_chat_model(
            replace(app_settings, llm_provider="anthropic", llm_api_key="x")
        )


def _no_llm():
    raise LLMNotConfiguredError("GEMINI_API_KEY is not set")
