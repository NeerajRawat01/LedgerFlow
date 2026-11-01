"""Typed agent state and result models for the Core LangGraph Agent.

One shared graph powers both the Consolidation Agent and the AI Copilot.
The execution ``mode`` is carried in state; everything downstream (intent
identification, skill selection, execution) branches on it. The LLM only
orchestrates — all financial calculation happens inside Python services.
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class AgentMode(str, Enum):
    """The two entry points that share the core agent."""

    CONSOLIDATION = "consolidation"
    COPILOT = "copilot"


class RunStatus(str, Enum):
    """Lifecycle status for an agent execution (mirrors consolidation runs)."""

    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    BLOCKED = "blocked"
    FAILED = "failed"
    AWAITING_REVIEW = "awaiting_review"


class Intent(BaseModel):
    """Structured intent derived from a request (deterministically or by LLM)."""

    kind: str
    topic: str | None = None
    period: str | None = None
    scope: str | None = None
    entities: list[str] = Field(default_factory=list)
    skill_hint: str | None = None
    requires_tools: bool = False
    confidence: float = 1.0
    source: str = "deterministic"
    user_goal: str | None = None


class ToolCallRecord(BaseModel):
    """One recorded tool invocation (name, args, outcome)."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result_summary: str | None = None
    error: str | None = None
    duration_ms: float | None = None


class AgentError(BaseModel):
    """A typed error surfaced during agent execution."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SkillResult(BaseModel):
    """Outcome returned by a skill after it executes."""

    status: RunStatus = RunStatus.COMPLETED
    summary: str = ""
    answer: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class AgentState(TypedDict, total=False):
    """LangGraph state flowing through the shared core agent."""

    mode: AgentMode
    run_id: str
    thread_id: str | None
    trace_id: str
    user_input: str | None
    payload: dict[str, Any]
    # Prior conversation turns, serialised for filesystem persistence.
    history: list[dict[str, Any]]
    messages: Annotated[list[AnyMessage], add_messages]
    intent: Intent | None
    skill_name: str | None
    skill_result: SkillResult | None
    tool_calls: Annotated[list[ToolCallRecord], operator.add]
    errors: Annotated[list[AgentError], operator.add]
    warnings: Annotated[list[str], operator.add]
    status: RunStatus
    summary: str | None


class ExecutionResult(BaseModel):
    """Final, API-ready result of one agent execution."""

    run_id: str
    trace_id: str
    thread_id: str | None
    mode: AgentMode
    status: RunStatus
    summary: str | None
    intent: Intent | None
    skill_name: str | None
    skill_result: SkillResult | None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    errors: list[AgentError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
