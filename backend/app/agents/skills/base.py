"""Skill contract for the shared core agent.

A Skill bundles everything the agent needs to run one capability safely:
its purpose, the instructions injected into the shared agent's prompt, its
execution procedure, the MCP tools it is permitted to call, its validation
requirements and its output expectations. Each skill declares which
execution modes may use it.

The LLM orchestrates; the skill's ``execute`` does the work by calling typed
backend services (never by doing financial arithmetic in the prompt).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.agents.state import AgentMode, SkillResult, ToolCallRecord

# Sentinel for "instruction doc not yet loaded" (distinct from "no doc").
_UNSET: Any = object()


class ToolPolicy(str, Enum):
    """What a skill is allowed to do through the MCP tool gateway."""

    READ_ONLY = "read_only"
    ALLOWED_TOOLS = "allowed_tools"   # subset of tools, enforced by name
    NO_TOOLS = "no_tools"


@dataclass(frozen=True)
class SkillSpec:
    """Static metadata describing a skill.

    This is what the agent (and a human reviewer) can inspect to understand
    a skill before it runs.
    """

    name: str
    purpose: str
    instructions: str
    execution_procedure: tuple[str, ...]
    permitted_tools: tuple[str, ...] = ()
    validation_requirements: tuple[str, ...] = ()
    output_expectations: tuple[str, ...] = ()
    allowed_modes: frozenset[AgentMode] = frozenset()
    tool_policy: ToolPolicy = ToolPolicy.NO_TOOLS
    description: str = ""

    def validate(self) -> None:
        """Reject an incomplete skill definition early."""
        missing = [
            f
            for f in ("name", "purpose", "instructions")
            if not getattr(self, f).strip()
        ]
        if not self.execution_procedure:
            missing.append("execution_procedure")
        if not self.allowed_modes:
            missing.append("allowed_modes")
        if missing:
            raise ValueError(
                f"skill '{self.name or '<unnamed>'}' is missing: {', '.join(missing)}"
            )


class SkillContext(BaseModel):
    """Everything a skill receives when it executes.

    Deliberately explicit: the skill gets the structured intent, the raw
    user input, the LLM handle (for composing answers only), the trace
    recorder, and a tool gateway (MCP) when one is configured. The skill
    must call backend services for all calculations.

    The tool gateway is the **per-execution, policy-bound** view the graph
    creates from the skill's static spec and the current mode: the skill
    invokes tools with ``await context.tool_gateway.call(name, args)`` and
    every call is checked against ``permitted_tools`` / ``ToolPolicy`` (and
    read-only annotations) *before* it reaches MCP. Gateway calls are
    recorded automatically — skills must not re-add them to
    ``SkillResult.tool_calls`` (the graph merges and de-duplicates).
    """

    mode: AgentMode
    intent: Any
    user_input: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    llm: Any = None
    trace: Any = None
    tool_gateway: Any = None
    settings: Any = None
    # The selected skill's agent-facing SKILL.md document (if it ships one).
    # Populated by the graph when a skill is selected, so a skill's own
    # instructions are available to it and no other skill's are exposed.
    skill_instructions: str | None = None
    # A zero-argument callable returning the skill catalogue (name/purpose/...)
    # as plain dicts, so an AI planning step can inspect *available* skills.
    # Supplied by the graph; it exposes registry metadata only, never tool
    # access, and never widens this skill's permissions.
    registry_describe: Any = None

    model_config = {"arbitrary_types_allowed": True}


class Skill(ABC):
    """Base class for every skill registered with the core agent."""

    spec: SkillSpec

    @property
    def name(self) -> str:
        return self.spec.name

    @abstractmethod
    async def execute(self, context: SkillContext) -> SkillResult:
        """Run this skill and return its typed result."""

    def permitted_tool_names(self) -> tuple[str, ...]:
        return self.spec.permitted_tools

    def allows_mode(self, mode: AgentMode) -> bool:
        return mode in self.spec.allowed_modes

    def instructions_doc(self) -> str | None:
        """The skill's agent-facing ``SKILL.md`` description, if present.

        Loaded lazily and cached per instance, so an agent only ever receives
        the instructions of the skill that was actually selected — never
        another skill's. Returns ``None`` when the skill ships no document.
        """
        if getattr(self, "_instructions_doc", _UNSET) is _UNSET:
            from app.agents.skills.skill_instructions import (
                SkillInstructionsNotFound,
                load_skill_instructions,
            )

            try:
                self._instructions_doc = load_skill_instructions(self.spec.name)
            except SkillInstructionsNotFound:
                self._instructions_doc = None
        return self._instructions_doc


@dataclass
class ExecutionReport:
    """Convenience accumulator a skill can build while executing."""

    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
