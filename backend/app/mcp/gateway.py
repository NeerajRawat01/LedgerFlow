"""Typed tool gateway: policy enforcement between skills and MCP tools.

The gateway is the only path from a skill to an MCP tool. It exists so
that an LLM can never widen what a skill is allowed to do: policy comes
from the skill's static :class:`SkillSpec` (registered and validated at
startup) and the execution mode — never from model output.

Enforced, in order, on every call:

1. **budget** — per-execution call budget (``AGENT_MAX_TOOL_CALLS``);
   once exhausted, further calls raise without recording.
2. **policy** — ``ToolPolicy.NO_TOOLS`` rejects everything;
   otherwise the tool must be listed in the skill's ``permitted_tools``
   (an empty list permits nothing — fail closed).
3. **read-only** — required when the skill's policy is
   ``READ_ONLY`` **or** the execution mode is ``copilot`` (defence in
   depth: even a mis-declared copilot skill cannot reach a mutating
   tool). Read-only status comes from the server's own
   ``ToolAnnotations.read_only_hint``; anything not explicitly marked
   read-only is treated as mutating.
4. **dispatch** — only now does the call reach MCP.

Every attempt is recorded as a :class:`ToolCallRecord` (arguments
redacted) and emitted as a ``tool_call`` trace event, so policy
rejections and failures are as auditable as successes. Skills must not
re-add gateway records to their own ``SkillResult.tool_calls`` — the
graph merges them (duplicates are de-duplicated defensively).
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.skills.base import SkillSpec, ToolPolicy
from app.agents.state import AgentMode, ToolCallRecord
from app.agents.tracing import TraceRecorder, redact
from app.core.config import Settings, settings as app_settings
from app.core.errors import MCPError, ToolPolicyError
from app.mcp.client import MCPClient, MCPToolInfo


class ToolGateway:
    """Process-level gateway: one MCP client plus a cached tool catalogue."""

    def __init__(
        self, client: MCPClient, *, settings: Settings = app_settings
    ) -> None:
        self.client = client
        self.settings = settings
        self._catalog: dict[str, MCPToolInfo] | None = None

    async def catalog(self) -> dict[str, MCPToolInfo]:
        """Server tool catalogue by name, fetched once and cached."""
        if self._catalog is None:
            tools = await self.client.list_tools()
            self._catalog = {tool.name: tool for tool in tools}
        return self._catalog

    def bind(
        self,
        *,
        skill: SkillSpec,
        mode: AgentMode,
        trace: TraceRecorder | None = None,
        secrets: tuple[str, ...] | list[str] = (),
    ) -> BoundToolGateway:
        """Create the per-execution view one skill executes through."""
        return BoundToolGateway(
            self, skill=skill, mode=mode, trace=trace, secrets=tuple(secrets)
        )


class BoundToolGateway:
    """Per-execution gateway bound to one skill, mode and trace."""

    def __init__(
        self,
        gateway: ToolGateway,
        *,
        skill: SkillSpec,
        mode: AgentMode,
        trace: TraceRecorder | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self._gateway = gateway
        self._skill = skill
        self._mode = mode
        self._trace = trace
        self._secrets = secrets
        self.records: list[ToolCallRecord] = []

    # -- public API (what skills see on SkillContext.tool_gateway) -------

    async def call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Invoke one MCP tool under policy, recording the attempt.

        Budget-exhausted calls are the one exception: they raise
        immediately without recording, so a retry loop can never grow
        ``records`` (or the trace file) past the configured budget.
        """
        # Budget first: once exhausted, fail fast WITHOUT recording.
        max_calls = self._gateway.settings.agent_max_tool_calls
        if len(self.records) >= max_calls:
            raise ToolPolicyError(
                f"tool call budget exhausted ({max_calls} per execution)",
                details={
                    "tool": name,
                    "reason": "budget_exhausted",
                    "max_tool_calls": max_calls,
                },
            )

        args = dict(arguments or {})
        record = ToolCallRecord(
            name=name, arguments=redact(args, self._secrets)
        )
        started = time.perf_counter()
        try:
            payload = await self._dispatch(name, args)
        except ToolPolicyError as exc:
            self._finish(record, started, error=f"{exc.code}: {exc.message}")
            raise
        except MCPError as exc:
            self._finish(record, started, error=f"{exc.code}: {exc.message}")
            raise
        except Exception as exc:
            self._finish(record, started, error=f"unexpected_error: {exc}")
            raise
        self._finish(record, started, summary=_summarize(payload))
        return payload

    # -- internals --------------------------------------------------------

    async def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        spec = self._skill
        if spec.tool_policy is ToolPolicy.NO_TOOLS:
            raise ToolPolicyError(
                f"skill '{spec.name}' is not allowed to call tools",
                details={
                    "skill": spec.name,
                    "tool": name,
                    "reason": "no_tools",
                    "tool_policy": spec.tool_policy.value,
                },
            )
        if name not in spec.permitted_tools:
            raise ToolPolicyError(
                f"tool '{name}' is not permitted for skill '{spec.name}'",
                details={
                    "skill": spec.name,
                    "tool": name,
                    "reason": "not_permitted",
                    "permitted_tools": list(spec.permitted_tools),
                },
            )

        info = (await self._gateway.catalog()).get(name)
        if info is None:
            raise MCPError(
                f"MCP server does not expose tool '{name}'",
                details={
                    "tool": name,
                    "reason": "unknown_tool",
                    "available": sorted(await self._gateway.catalog()),
                },
            )

        needs_read_only = (
            spec.tool_policy is ToolPolicy.READ_ONLY
            or self._mode is AgentMode.COPILOT
        )
        if needs_read_only and not info.read_only_hint:
            raise ToolPolicyError(
                f"tool '{name}' is not read-only and cannot be used in "
                f"mode '{self._mode.value}'",
                details={
                    "skill": spec.name,
                    "tool": name,
                    "reason": "not_read_only",
                    "mode": self._mode.value,
                    "tool_policy": spec.tool_policy.value,
                },
            )

        return await self._gateway.client.call_tool(name, args)

    def _finish(
        self,
        record: ToolCallRecord,
        started: float,
        *,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        record.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        record.ok = error is None
        record.result_summary = summary
        record.error = error
        self.records.append(record)
        if self._trace is not None:
            self._trace.event(
                "tool_call",
                {
                    "tool": record.name,
                    "skill": self._skill.name,
                    "mode": self._mode.value,
                    "arguments": record.arguments,
                    "ok": record.ok,
                    "duration_ms": record.duration_ms,
                    "result_summary": record.result_summary,
                    "error": record.error,
                },
                secrets=list(self._secrets),
            )


def _summarize(payload: Any) -> str:
    """A short, redaction-safe description of a tool result for records."""
    if isinstance(payload, list):
        return f"list[{len(payload)}]"
    if isinstance(payload, dict):
        return f"dict[{len(payload)}]"
    if isinstance(payload, str):
        return payload[:160]
    return type(payload).__name__
