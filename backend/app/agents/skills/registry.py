"""Skill Registry: the single source of truth for agent capabilities.

Exactly one registry instance is shared by the Consolidation Agent and the
AI Copilot. Skills declare which modes they serve; the registry enforces
that declaration so a copilot request can never reach a consolidation-only
skill (and vice versa).
"""

from __future__ import annotations

from typing import Iterable

from app.agents.skills.base import Skill, SkillSpec
from app.agents.state import AgentMode
from app.core.errors import (
    SkillNotAllowedForModeError,
    SkillNotFoundError,
)


class SkillRegistry:
    """Register, look up and validate skills for the core agent."""

    def __init__(self, skills: Iterable[Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills or ():
            self.register(skill)

    # -- registration -------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """Add a skill. Raises on duplicates or incomplete metadata."""
        spec = skill.spec
        spec.validate()
        if spec.name in self._skills:
            raise ValueError(f"skill already registered: {spec.name}")
        self._skills[spec.name] = skill

    def register_spec(
        self, skill: Skill, spec: SkillSpec
    ) -> None:
        """Attach a spec to a skill then register it."""
        skill.spec = spec
        self.register(skill)

    def replace(self, skill: Skill) -> None:
        """Register or overwrite (used to swap in mocks during tests)."""
        skill.spec.validate()
        self._skills[skill.spec.name] = skill

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    # -- lookup -------------------------------------------------------------

    def get(self, name: str) -> Skill:
        """Return a skill by name.

        Raises:
            SkillNotFoundError: When no skill has that name.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(
                f"skill not found: {name}", details={"skill": name}
            )
        return skill

    def has(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        return sorted(self._skills)

    def all(self) -> list[Skill]:
        return [self._skills[n] for n in self.names()]

    def for_mode(self, mode: AgentMode) -> list[Skill]:
        """Skills permitted to run in ``mode``, sorted by name."""
        return [s for s in self.all() if s.allows_mode(mode)]

    def resolve(self, name: str, mode: AgentMode) -> Skill:
        """Look up ``name`` and assert it is allowed to run in ``mode``.

        Raises:
            SkillNotFoundError: When no skill has that name.
            SkillNotAllowedForModeError: When the skill exists but does not
                serve this execution mode.
        """
        if name not in self._skills:
            raise SkillNotFoundError(
                f"skill not found: {name}",
                details={"skill": name, "mode": mode.value},
            )
        skill = self._skills[name]
        if not skill.allows_mode(mode):
            allowed = sorted(m.value for m in skill.spec.allowed_modes)
            raise SkillNotAllowedForModeError(
                f"skill '{name}' is not allowed in mode '{mode.value}'",
                details={"skill": name, "mode": mode.value, "allowed_modes": allowed},
            )
        return skill

    def select_by_intent(
        self, intent_kind: str, mode: AgentMode, *, hint: str | None = None
    ) -> Skill | None:
        """Pick the skill for an intent.

        A ``hint`` (the LLM's or request's proposed skill) is validated by
        the registry: an unknown hint raises ``SkillNotFoundError`` and a
        hint that does not serve this mode raises
        ``SkillNotAllowedForModeError``. Without a hint we match the intent
        kind, then fall back to the single mode-allowed skill when there is
        exactly one. Returns ``None`` when nothing matches (the caller
        decides whether that is fatal).
        """
        if hint:
            if not self.has(hint):
                raise SkillNotFoundError(
                    f"skill not found: {hint}",
                    details={"skill": hint, "mode": mode.value},
                )
            return self.resolve(hint, mode)
        candidates = self.for_mode(mode)
        if not candidates:
            return None
        for skill in candidates:
            if skill.spec.name == intent_kind:
                return skill
        return candidates[0] if len(candidates) == 1 else None

    def describe(self, mode: AgentMode | None = None) -> list[dict]:
        """Return skill metadata (for API responses and agent prompts)."""
        skills = self.all() if mode is None else self.for_mode(mode)
        return [
            {
                "name": s.spec.name,
                "description": s.spec.description,
                "purpose": s.spec.purpose,
                "allowed_modes": sorted(m.value for m in s.spec.allowed_modes),
                "permitted_tools": list(s.spec.permitted_tools),
                "execution_procedure": list(s.spec.execution_procedure),
                "validation_requirements": list(s.spec.validation_requirements),
                "output_expectations": list(s.spec.output_expectations),
            }
            for s in skills
        ]


def build_default_registry() -> SkillRegistry:
    """Registry with the core skills.

    Consolidation Skill v1 is the first real skill. It executes the
    accounting stages that exist today (ingestion, validation, account
    mapping, FX-rate readiness) and reports the remaining consolidation
    stages as blocked. The Financial Analysis (copilot) skill is a later
    milestone and is intentionally not registered yet.
    """
    from app.agents.skills.consolidation import ConsolidationSkill

    return SkillRegistry([ConsolidationSkill()])
