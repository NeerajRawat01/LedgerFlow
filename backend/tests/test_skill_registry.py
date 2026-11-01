"""Skill Registry tests (Milestone 2).

The registry is the single source of truth shared by both agents: it must
validate skill metadata on registration, enforce the mode allowlist, and
produce typed errors for unknown or misplaced skills.
"""

from __future__ import annotations

import pytest

from app.agents.skills.base import Skill, SkillContext, SkillSpec
from app.agents.skills.registry import SkillRegistry, build_default_registry
from app.agents.state import AgentMode, RunStatus, SkillResult
from app.core.errors import (
    SkillNotAllowedForModeError,
    SkillNotFoundError,
)
from tests.conftest import StubAnalysisSkill, StubConsolidationSkill


def _skill(
    name: str,
    modes: set[AgentMode],
    *,
    purpose: str = "does a thing",
    procedure: tuple[str, ...] = ("step 1",),
    tools: tuple[str, ...] = (),
) -> Skill:
    class _S(Skill):
        spec = SkillSpec(
            name=name,
            purpose=purpose,
            instructions=f"instructions for {name}",
            execution_procedure=procedure,
            permitted_tools=tools,
            allowed_modes=frozenset(modes),
        )

        async def execute(self, context: SkillContext) -> SkillResult:
            return SkillResult(status=RunStatus.COMPLETED)

    return _S()


# --- registration & validation ---------------------------------------------


def test_register_and_get_skill():
    registry = SkillRegistry()
    skill = StubConsolidationSkill()
    registry.register(skill)

    assert registry.get("consolidation") is skill
    assert registry.has("consolidation")
    assert registry.names() == ["consolidation"]


def test_register_duplicate_name_rejected():
    registry = SkillRegistry([StubConsolidationSkill()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(StubConsolidationSkill())


def test_registry_accepts_iterable_at_construction():
    registry = SkillRegistry([StubConsolidationSkill(), StubAnalysisSkill()])
    assert registry.names() == ["consolidation", "financial_analysis"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"purpose": "", "name": "x"},
        {"instructions": "", "name": "x"},
        {"execution_procedure": (), "name": "x"},
        {"allowed_modes": frozenset(), "name": "x"},
    ],
)
def test_incomplete_skill_spec_rejected(kwargs):
    registry = SkillRegistry()
    base = dict(
        name="x",
        purpose="p",
        instructions="i",
        execution_procedure=("s",),
        allowed_modes=frozenset({AgentMode.COPILOT}),
    )
    base.update(kwargs)
    with pytest.raises(ValueError, match="missing"):
        registry.register(_wrap(spec=SkillSpec(**base)))


def _wrap(spec: SkillSpec) -> Skill:
    class _S(Skill):
        async def execute(self, context: SkillContext) -> SkillResult:
            return SkillResult(status=RunStatus.COMPLETED)

    _S.spec = spec
    return _S()


def test_replace_allows_swapping_in_mocks():
    registry = SkillRegistry([StubConsolidationSkill()])
    registry.replace(StubConsolidationSkill(SkillResult(status=RunStatus.BLOCKED)))
    assert registry.get("consolidation").result.status is RunStatus.BLOCKED


def test_unregister_removes_skill():
    registry = SkillRegistry([StubConsolidationSkill()])
    registry.unregister("consolidation")
    assert not registry.has("consolidation")


# --- lookup & mode allowlist ------------------------------------------------


def test_get_unknown_skill_raises_typed_error():
    registry = SkillRegistry()
    with pytest.raises(SkillNotFoundError) as exc:
        registry.get("nope")
    assert exc.value.code == "skill_not_found"
    assert exc.value.details["skill"] == "nope"


def test_resolve_enforces_mode_allowlist():
    registry = SkillRegistry([StubConsolidationSkill()])

    # allowed mode
    assert registry.resolve("consolidation", AgentMode.CONSOLIDATION).name == (
        "consolidation"
    )

    # wrong mode -> typed refusal
    with pytest.raises(SkillNotAllowedForModeError) as exc:
        registry.resolve("consolidation", AgentMode.COPILOT)
    assert exc.value.code == "skill_not_allowed_for_mode"
    assert exc.value.details["allowed_modes"] == ["consolidation"]


def test_resolve_unknown_skill_raises_typed_error():
    registry = SkillRegistry()
    with pytest.raises(SkillNotFoundError):
        registry.resolve("ghost", AgentMode.CONSOLIDATION)


def test_for_mode_filters_skills():
    registry = SkillRegistry([StubConsolidationSkill(), StubAnalysisSkill()])

    names_c = [s.name for s in registry.for_mode(AgentMode.CONSOLIDATION)]
    names_k = [s.name for s in registry.for_mode(AgentMode.COPILOT)]

    assert names_c == ["consolidation"]
    assert names_k == ["financial_analysis"]


def test_for_mode_empty_registry_returns_empty():
    assert SkillRegistry().for_mode(AgentMode.COPILOT) == []


# --- intent-based selection -------------------------------------------------


def test_select_by_intent_uses_hint_when_valid():
    registry = SkillRegistry([StubConsolidationSkill(), StubAnalysisSkill()])
    skill = registry.select_by_intent(
        "anything", AgentMode.COPILOT, hint="financial_analysis"
    )
    assert skill is not None and skill.name == "financial_analysis"


def test_select_by_intent_unknown_hint_raises():
    registry = SkillRegistry([StubAnalysisSkill()])
    with pytest.raises(SkillNotFoundError):
        registry.select_by_intent(
            "financial_analysis", AgentMode.COPILOT, hint="typo_skill"
        )


def test_select_by_intent_hint_wrong_mode_raises():
    registry = SkillRegistry([StubConsolidationSkill(), StubAnalysisSkill()])
    with pytest.raises(SkillNotAllowedForModeError):
        registry.select_by_intent(
            "financial_analysis", AgentMode.COPILOT, hint="consolidation"
        )


def test_select_by_intent_matches_kind_then_single_candidate():
    registry = SkillRegistry([StubConsolidationSkill(), StubAnalysisSkill()])
    # kind matches skill name
    skill = registry.select_by_intent("consolidation", AgentMode.CONSOLIDATION)
    assert skill is not None and skill.name == "consolidation"


def test_select_by_intent_single_candidate_fallback():
    registry = SkillRegistry([StubAnalysisSkill()])
    skill = registry.select_by_intent("unrelated_kind", AgentMode.COPILOT)
    assert skill is not None and skill.name == "financial_analysis"


def test_select_by_intent_ambiguous_returns_none():
    registry = SkillRegistry(
        [
            _skill("a", {AgentMode.COPILOT}),
            _skill("b", {AgentMode.COPILOT}),
        ]
    )
    assert registry.select_by_intent("neither", AgentMode.COPILOT) is None


def test_select_by_intent_no_candidates_returns_none():
    registry = SkillRegistry([StubConsolidationSkill()])
    assert (
        registry.select_by_intent("financial_analysis", AgentMode.COPILOT) is None
    )


# --- description payloads ---------------------------------------------------


def test_describe_exposes_skill_contract_fields():
    registry = SkillRegistry([StubConsolidationSkill()])
    described = registry.describe(AgentMode.CONSOLIDATION)

    assert len(described) == 1
    entry = described[0]
    assert entry["name"] == "consolidation"
    assert entry["allowed_modes"] == ["consolidation"]
    assert "get_trial_balance" in entry["permitted_tools"]
    for field in (
        "purpose",
        "execution_procedure",
        "validation_requirements",
        "output_expectations",
    ):
        assert field in entry


def test_describe_respects_mode_filter():
    registry = SkillRegistry([StubConsolidationSkill(), StubAnalysisSkill()])
    only_c = registry.describe(AgentMode.CONSOLIDATION)
    assert [e["name"] for e in only_c] == ["consolidation"]


def test_default_registry_registers_consolidation_skill():
    registry = build_default_registry()
    assert registry.names() == ["consolidation"]
    skill = registry.get("consolidation")
    assert skill.allows_mode(AgentMode.CONSOLIDATION)
    assert not skill.allows_mode(AgentMode.COPILOT)


def test_spec_validate_reports_all_missing_fields():
    spec = SkillSpec(
        name="",
        purpose="",
        instructions="",
        execution_procedure=(),
        allowed_modes=frozenset(),
    )
    with pytest.raises(ValueError) as exc:
        spec.validate()
    message = str(exc.value)
    for missing in ("name", "purpose", "instructions", "execution_procedure"):
        assert missing in message
