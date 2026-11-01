"""The Core LangGraph Agent shared by both entry points.

One ``StateGraph`` powers the Consolidation Agent and the AI Copilot. The
``mode`` in state selects the branch behaviour:

* ``consolidation`` — intent is derived deterministically from the structured
  payload (period, scope, entities). No LLM is required to execute a run.
* ``copilot`` — intent is identified by the LLM (Gemini). An unkeyed
  environment produces the typed ``LLM_NOT_CONFIGURED`` error.

Flow::

    START -> load_context -> identify_intent -> select_skill
           -> execute_skill -> finalize -> END

Every node records trace events, and a failure at any step sets a typed
error in state and routes straight to ``finalize`` so the trace is always
completed. The LLM only orchestrates; skills call Python services for all
financial work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agents.skills.base import SkillContext
from app.agents.skills.registry import SkillRegistry
from app.agents.state import (
    AgentError,
    AgentMode,
    AgentState,
    Intent,
    RunStatus,
    SkillResult,
)
from app.agents.thread_service import ThreadService
from app.agents.tracing import TraceRecorder
from app.core.config import Settings, settings as app_settings
from app.core.errors import (
    IntentIdentificationError,
    LedgerFlowError,
    LLMNotConfiguredError,
    SkillExecutionError,
    SkillNotFoundError,
)

# Provider that returns a chat model (or raises LLMNotConfiguredError).
LLMProvider = Callable[[], BaseChatModel]


@dataclass
class AgentRuntime:
    """Everything the shared graph needs, injected once.

    Keeping services on a runtime object (rather than globals) lets tests
    swap in a fake LLM, a temp storage dir and a test skill registry.
    """

    registry: SkillRegistry
    llm_provider: LLMProvider
    thread_service: ThreadService
    traces_dir: Path
    settings: Settings = app_settings
    tool_gateway: Any = None

    def optional_llm(self) -> BaseChatModel | None:
        """Return an LLM when configured, else None (never raises)."""
        try:
            return self.llm_provider()
        except LLMNotConfiguredError:
            return None

    def require_llm(self) -> BaseChatModel:
        """Return an LLM or raise the typed not-configured error."""
        return self.llm_provider()

    def trace(self, trace_id: str) -> TraceRecorder:
        return TraceRecorder(self.traces_dir, trace_id)


# --- intent helpers ---------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of an LLM response."""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_intent(data: dict[str, Any]) -> Intent:
    """Build a validated Intent from loosely typed LLM JSON."""
    def as_str(value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    raw_entities = data.get("entities") or []
    if isinstance(raw_entities, str):
        raw_entities = [raw_entities]
    entities = [str(e).strip() for e in raw_entities if str(e).strip()]

    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    return Intent(
        kind=str(data.get("kind") or "financial_analysis"),
        topic=as_str(data.get("topic")),
        period=as_str(data.get("period")),
        scope=as_str(data.get("scope")),
        entities=entities,
        skill_hint=as_str(data.get("skill_hint")),
        requires_tools=bool(data.get("requires_tools", False)),
        confidence=max(0.0, min(1.0, confidence)),
        source="llm",
        user_goal=as_str(data.get("user_goal")) or as_str(data.get("question")),
    )


_PERIOD_RE = re.compile(r"\b(20\d{2})\b")
_ENTITY_RE = re.compile(r"\b(US|UK|IN)\b")


def heuristic_copilot_intent(user_input: str) -> Intent:
    """Keyword fallback when the LLM returns unusable intent JSON."""
    period_match = _PERIOD_RE.search(user_input)
    entities = sorted(set(_ENTITY_RE.findall(user_input)))
    lowered = user_input.lower()
    topic = "general"
    for keyword in ("revenue", "margin", "profit", "variance", "cash", "balance"):
        if keyword in lowered:
            topic = keyword
            break
    return Intent(
        kind="financial_analysis",
        topic=topic,
        period=period_match.group(1) if period_match else None,
        entities=entities,
        requires_tools=True,
        confidence=0.3,
        source="heuristic",
        user_goal=user_input,
    )


def consolidation_intent(payload: dict[str, Any]) -> Intent:
    """Deterministic intent for a consolidation run (no LLM involved)."""
    entities = payload.get("entities") or []
    if isinstance(entities, str):
        entities = [entities]
    return Intent(
        kind="consolidation",
        topic="consolidation_run",
        period=str(payload["period"]) if payload.get("period") else None,
        scope=str(payload.get("scope") or "group"),
        entities=[str(e) for e in entities],
        skill_hint=str(payload["skill"]) if payload.get("skill") else None,
        requires_tools=True,
        confidence=1.0,
        source="deterministic",
    )


COPILOT_INTENT_PROMPT = """\
You identify the financial intent of a user's question for LedgerFlow, a
multi-entity consolidation platform. Available skills:
{skills}

Respond with ONLY a JSON object (no prose, no code fences) matching:
{{"kind": "financial_analysis", "topic": string, "period": string|null,
  "scope": string|null, "entities": [string], "skill_hint": string|null,
  "requires_tools": boolean, "confidence": number between 0 and 1,
  "user_goal": string}}

Prior conversation turns (for context only):
{history}

User question:
{question}"""


# --- graph nodes ------------------------------------------------------------


def build_graph(runtime: AgentRuntime):  # noqa: C901 - node wiring is inherently linear
    """Compile the shared core agent graph bound to ``runtime``."""

    def _fail(
        state: AgentState, exc: LedgerFlowError, event: str
    ) -> dict[str, Any]:
        trace = runtime.trace(state["trace_id"])
        trace.event(event, exc.to_dict(), secrets=[_secret(runtime)])
        return {
            "status": RunStatus.FAILED,
            "errors": [AgentError(**exc.to_dict())],
            "summary": exc.message,
        }

    async def load_context(state: AgentState) -> dict[str, Any]:
        trace = runtime.trace(state["trace_id"])
        trace.event(
            "execution_started",
            {
                "run_id": state["run_id"],
                "mode": state["mode"].value,
                "thread_id": state.get("thread_id"),
                "skill": state.get("skill_name"),
            },
            secrets=[_secret(runtime)],
        )
        trace.event(
            "context_loaded",
            {"messages": len(state.get("messages", []))},
        )
        return {"status": RunStatus.RUNNING}

    async def identify_intent(state: AgentState) -> dict[str, Any]:
        trace = runtime.trace(state["trace_id"])
        mode = state["mode"]

        try:
            if mode is AgentMode.CONSOLIDATION:
                intent = consolidation_intent(state.get("payload") or {})
                if not intent.period:
                    raise IntentIdentificationError(
                        "consolidation requires a period",
                        details={"payload": sorted(state.get("payload") or {})},
                    )
            else:
                llm = runtime.require_llm()
                skills_json = json.dumps(
                    runtime.registry.describe(mode), indent=2
                )
                history = state.get("history") or []
                history_text = (
                    json.dumps(history[-6:], indent=2) if history else "(none)"
                )
                question = state.get("user_input") or ""
                response = await llm.ainvoke(
                    [
                        SystemMessage(
                            content=COPILOT_INTENT_PROMPT.format(
                                skills=skills_json,
                                history=history_text,
                                question=question,
                            )
                        ),
                        HumanMessage(content=question),
                    ]
                )
                data = _extract_json(
                    response.content
                    if isinstance(response.content, str)
                    else str(response.content)
                )
                if data is None:
                    intent = heuristic_copilot_intent(question)
                    trace.event(
                        "intent_fallback",
                        {"reason": "unparseable_llm_response"},
                    )
                else:
                    intent = _coerce_intent(data)
        except LedgerFlowError as exc:
            return _fail(state, exc, "intent_failed")
        except Exception as exc:  # provider/network failure
            from app.core.errors import LLMInvocationError

            return _fail(
                state,
                LLMInvocationError(
                    f"LLM intent identification failed: {exc}",
                    details={"mode": mode.value},
                ),
                "intent_failed",
            )

        trace.event(
            "intent_identified",
            {
                "kind": intent.kind,
                "source": intent.source,
                "confidence": intent.confidence,
                "period": intent.period,
                "entities": intent.entities,
            },
        )
        return {"intent": intent}

    def route_after_intent(state: AgentState) -> str:
        if state.get("status") is RunStatus.FAILED or not state.get("intent"):
            return "finalize"
        return "select_skill"

    async def select_skill(state: AgentState) -> dict[str, Any]:
        trace = runtime.trace(state["trace_id"])
        intent: Intent = state["intent"]
        mode = state["mode"]
        try:
            skill = runtime.registry.select_by_intent(
                intent.kind, mode, hint=intent.skill_hint
            )
            if skill is None:
                available = [s.spec.name for s in runtime.registry.for_mode(mode)]
                raise SkillNotFoundError(
                    f"no skill available for intent '{intent.kind}' in mode "
                    f"'{mode.value}'",
                    details={
                        "intent": intent.kind,
                        "mode": mode.value,
                        "available": available,
                    },
                )
        except LedgerFlowError as exc:
            return _fail(state, exc, "skill_selection_failed")

        trace.event(
            "skill_selected",
            {
                "skill": skill.name,
                "mode": mode.value,
                "hint": intent.skill_hint,
                "has_instructions": skill.instructions_doc() is not None,
            },
        )
        return {"skill_name": skill.name}

    def route_after_skill(state: AgentState) -> str:
        if state.get("status") is RunStatus.FAILED or not state.get("skill_name"):
            return "finalize"
        return "execute_skill"

    async def execute_skill(state: AgentState) -> dict[str, Any]:
        trace = runtime.trace(state["trace_id"])
        bound_gateway: Any = None
        try:
            skill = runtime.registry.get(state["skill_name"])
            # The selected skill's own SKILL.md is made available to it here;
            # no other skill's instructions are exposed.
            instructions_doc = skill.instructions_doc()
            # Policy is bound to the skill's static spec + mode, never to
            # model output; the skill only ever sees this bound view.
            if runtime.tool_gateway is not None:
                bound_gateway = runtime.tool_gateway.bind(
                    skill=skill.spec,
                    mode=state["mode"],
                    trace=trace,
                    secrets=[_secret(runtime)],
                )
            context = SkillContext(
                mode=state["mode"],
                intent=state["intent"],
                user_input=state.get("user_input"),
                payload=state.get("payload") or {},
                llm=runtime.optional_llm(),
                trace=trace,
                tool_gateway=bound_gateway,
                settings=runtime.settings,
                skill_instructions=instructions_doc,
                registry_describe=lambda: runtime.registry.describe(state["mode"]),
            )
            trace.event("skill_started", {"skill": skill.name})
            result = await skill.execute(context)
        except LedgerFlowError as exc:
            return _with_records(
                _fail(state, exc, "skill_execution_failed"), bound_gateway
            )
        except Exception as exc:
            return _with_records(
                _fail(
                    state,
                    SkillExecutionError(
                        f"skill '{state.get('skill_name')}' raised: {exc}",
                        details={"skill": state.get("skill_name")},
                    ),
                    "skill_execution_failed",
                ),
                bound_gateway,
            )

        trace.event(
            "skill_executed",
            {
                "skill": skill.name,
                "status": result.status.value,
                "tool_calls": len(result.tool_calls),
                "issues": len(result.issues),
            },
        )
        # The gateway is the source of truth for MCP tool records; a skill
        # that re-surfaced them in its own result is de-duplicated here.
        records = list(result.tool_calls)
        if bound_gateway is not None:
            records.extend(
                r for r in bound_gateway.records if r not in records
            )
        return {
            "skill_result": result,
            "tool_calls": records,
            "status": result.status,
        }

    async def finalize(state: AgentState) -> dict[str, Any]:
        trace = runtime.trace(state["trace_id"])
        result: SkillResult | None = state.get("skill_result")
        status = state.get("status", RunStatus.FAILED)

        if result is not None:
            status = result.status
            summary = result.summary
        else:
            summary = state.get("summary") or "execution failed"

        updates: dict[str, Any] = {"status": status, "summary": summary}

        # Only a successful execution produces an assistant turn; failures
        # surface through the API response, not a fabricated reply.
        if result is not None:
            answer = result.answer or summary
            updates["messages"] = [AIMessage(content=answer)]

        trace.event(
            "execution_completed",
            {
                "status": status.value,
                "summary": summary,
                "skill": state.get("skill_name"),
                "errors": [e.code for e in state.get("errors", [])],
            },
            secrets=[_secret(runtime)],
        )
        return updates

    builder = StateGraph(AgentState)
    builder.add_node("load_context", load_context)
    builder.add_node("identify_intent", identify_intent)
    builder.add_node("select_skill", select_skill)
    builder.add_node("execute_skill", execute_skill)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "identify_intent")
    builder.add_conditional_edges(
        "identify_intent",
        route_after_intent,
        {"select_skill": "select_skill", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "select_skill",
        route_after_skill,
        {"execute_skill": "execute_skill", "finalize": "finalize"},
    )
    builder.add_edge("execute_skill", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def _secret(runtime: AgentRuntime) -> str:
    """The API key to scrub from any trace payload (provider-agnostic)."""
    return getattr(runtime.settings, "resolved_llm_api_key", None) or ""


def _with_records(
    update: dict[str, Any], bound_gateway: Any
) -> dict[str, Any]:
    """Surface gateway tool records even when the skill failed mid-call."""
    if bound_gateway is not None and bound_gateway.records:
        update["tool_calls"] = list(bound_gateway.records)
    return update


def default_runtime(registry: SkillRegistry | None = None) -> AgentRuntime:
    """Runtime wired to process settings and filesystem storage.

    The tool gateway connects to the MCP server mounted on this same
    FastAPI process (loopback), with per-call sessions and a cached
    tool catalogue. Skills reach tools only through this gateway, which
    enforces their static ``permitted_tools`` and ``ToolPolicy``.
    """
    from app.agents.llm import create_chat_model
    from app.mcp.client import MCPClient
    from app.mcp.gateway import ToolGateway
    from app.storage.filesystem import storage_paths

    storage_paths.ensure_layout()
    return AgentRuntime(
        registry=registry or SkillRegistry(),
        llm_provider=lambda: create_chat_model(app_settings),
        thread_service=ThreadService(storage_paths.threads_dir),
        traces_dir=storage_paths.traces_dir,
        settings=app_settings,
        tool_gateway=ToolGateway(
            MCPClient(app_settings.mcp_server_url), settings=app_settings
        ),
    )
