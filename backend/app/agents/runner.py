"""Agent runner: executes one request through the core graph.

Responsibilities:

* mint ``run_id`` / ``trace_id`` (and a thread id for copilot conversations),
* load persisted thread history into state before the graph runs,
* invoke the compiled graph under the configured step limit,
* persist the updated thread after a successful turn,
* return an API-ready :class:`ExecutionResult`.

Persistence is filesystem-only: threads as JSON, traces as JSONL. A backend
restart loses nothing that has been written.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from app.agents.graph import AgentRuntime, build_graph
from app.agents.state import (
    AgentError,
    AgentMode,
    AgentState,
    ExecutionResult,
    RunStatus,
)
from app.core.errors import LedgerFlowError, ServiceError


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class AgentRunner:
    """Runs requests through the shared core agent graph."""

    def __init__(
        self,
        runtime: AgentRuntime,
        graph: CompiledStateGraph | None = None,
    ) -> None:
        self.runtime = runtime
        self.graph = graph or build_graph(runtime)

    # -- helpers ------------------------------------------------------------

    @property
    def _recursion_limit(self) -> int:
        return max(10, self.runtime.settings.agent_max_graph_steps)

    def _history(self, messages: list[Any]) -> list[dict[str, Any]]:
        from app.agents.thread_service import serialize_message

        return [serialize_message(m) for m in messages]

    def _persist_thread(
        self, thread_id: str | None, mode: AgentMode, messages: Iterable[Any]
    ) -> None:
        if not thread_id:
            return
        self.runtime.thread_service.save(thread_id, messages, mode=mode.value)

    # -- execution ----------------------------------------------------------

    async def run(
        self,
        *,
        mode: AgentMode | str,
        user_input: str | None = None,
        payload: dict[str, Any] | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> ExecutionResult:
        """Execute one request and return its typed result.

        Args:
            mode: ``consolidation`` or ``copilot``.
            user_input: The natural-language question (copilot).
            payload: Structured consolidation request (period/scope/...).
            thread_id: Continue an existing conversation; omitted for a new
                copilot thread or for stateless consolidation runs.
            run_id: Optional pre-generated id (used by API layer).

        Returns:
            The execution result, including status, skill output, errors
            and the trace id for inspection.
        """
        mode = AgentMode(mode)
        run_id = run_id or _new_id("run")
        trace_id = _new_id("trace")

        if mode is AgentMode.COPILOT and not (user_input or "").strip():
            raise ServiceError(
                "copilot requests require user_input",
                details={"mode": mode.value},
            )

        # Conversations need a thread; consolidation runs are stateless
        # unless the caller supplies one.
        effective_thread = thread_id
        if mode is AgentMode.COPILOT and not effective_thread:
            effective_thread = _new_id("thread")

        history_msgs = self._load_messages(effective_thread)
        history = self._history(history_msgs)

        messages: list[Any] = list(history_msgs)
        if user_input:
            messages.append(HumanMessage(content=user_input))

        initial: AgentState = {
            "mode": mode,
            "run_id": run_id,
            "thread_id": effective_thread,
            "trace_id": trace_id,
            "user_input": user_input,
            "payload": payload or {},
            "history": history,
            "messages": messages,
            "errors": [],
            "warnings": [],
            "tool_calls": [],
            "status": RunStatus.RUNNING,
        }

        try:
            final_state = await self.graph.ainvoke(
                initial,
                config={"recursion_limit": self._recursion_limit},
            )
        except LedgerFlowError as exc:
            # A defect that escaped the node boundary still deserves a
            # completed trace rather than a dangling one.
            self.runtime.trace(trace_id).event(
                "execution_crashed", exc.to_dict()
            )
            return ExecutionResult(
                run_id=run_id,
                trace_id=trace_id,
                thread_id=effective_thread,
                mode=mode,
                status=RunStatus.FAILED,
                summary=exc.message,
                intent=None,
                skill_name=None,
                skill_result=None,
                errors=[AgentError(**exc.to_dict())],
            )
        except Exception as exc:  # pragma: no cover - unexpected defects
            self.runtime.trace(trace_id).event(
                "execution_crashed",
                {"code": "unexpected_error", "message": str(exc)},
            )
            return ExecutionResult(
                run_id=run_id,
                trace_id=trace_id,
                thread_id=effective_thread,
                mode=mode,
                status=RunStatus.FAILED,
                summary=f"unexpected failure: {exc}",
                intent=None,
                skill_name=None,
                skill_result=None,
                errors=[
                    AgentError(
                        code="unexpected_error",
                        message=str(exc),
                    )
                ],
            )

        # Persist the conversation as it stands after this turn (a failed
        # turn still records the question, without a fabricated reply).
        final_messages = final_state.get("messages") or []
        if effective_thread and final_messages:
            self._persist_thread(effective_thread, mode, final_messages)

        return self._to_result(run_id, trace_id, mode, effective_thread, final_state)

    def _load_messages(self, thread_id: str | None) -> list[Any]:
        if not thread_id:
            return []
        return self.runtime.thread_service.load(thread_id)

    @staticmethod
    def _to_result(
        run_id: str,
        trace_id: str,
        mode: AgentMode,
        thread_id: str | None,
        state: AgentState,
    ) -> ExecutionResult:
        return ExecutionResult(
            run_id=run_id,
            trace_id=trace_id,
            thread_id=thread_id,
            mode=mode,
            status=state.get("status", RunStatus.FAILED),
            summary=state.get("summary"),
            intent=state.get("intent"),
            skill_name=state.get("skill_name"),
            skill_result=state.get("skill_result"),
            tool_calls=list(state.get("tool_calls") or []),
            errors=list(state.get("errors") or []),
            warnings=list(state.get("warnings") or []),
        )
