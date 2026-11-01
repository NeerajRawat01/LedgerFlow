"""LLM reasoning foundation: structured, traced, budgeted LLM calls.

This is the single seam through which an AI-assisted workflow talks to the
LLM. It deliberately does **not** replace the existing graph or the
``AgentRuntime``: the graph already owns orchestration and tool access, and
:class:`~app.agents.llm.create_chat_model` already owns provider creation.
This module adds the missing capabilities the milestone needs:

* **structured output** — an LLM response is parsed into a Pydantic model,
  with a bounded retry when the model returns unusable JSON;
* **typed errors** — malformed output raises ``LLMStructuredOutputError``;
  a missing provider raises ``LLMDisabledError``;
* **explicit budgets** — one reasoner may make at most
  ``settings.llm_max_reasoning_calls`` invocations per execution;
* **observability** — every invocation emits ``llm_invocation_started`` /
  ``llm_invocation_completed`` / ``llm_invocation_failed`` trace events with
  the template id, version, model name, latency and token usage (when the
  provider supplies it). Prompt *content* is never stored — only a length and
  the template identifier — so sensitive source text cannot leak into traces;
* **secret redaction** — payloads are written through ``TraceRecorder``'s
  redaction, and the API key is passed as an extra secret.

The reasoner never performs arithmetic and never touches a tool. Tool access
remains exclusively the skill's policy-bound ``ToolGateway``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from app.agents.tracing import TraceRecorder
from app.core.config import Settings, settings as app_settings
from app.core.errors import (
    LLMBudgetExceededError,
    LLMDisabledError,
    LLMInvocationError,
    LLMStructuredOutputError,
)

#: The Pydantic model type a structured call parses into.
ModelT = TypeVar("ModelT", bound=BaseModel)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class LLMUsage:
    """Token usage and latency captured for one invocation (best-effort)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float | None = None
    model: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
        }


@dataclass
class LLMCallRecord:
    """One recorded LLM invocation, for the frontend/agent-activity view."""

    template_id: str
    prompt_version: str
    model: str | None
    ok: bool
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    error: str | None = None
    structured_output: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "error": self.error,
            "structured_output": self.structured_output,
        }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of an LLM response (fences tolerated)."""
    cleaned = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    match = _JSON_OBJECT_RE.search(cleaned)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _usage_from_response(
    response: Any, latency_ms: float, model: str | None
) -> LLMUsage:
    """Best-effort token usage from a LangChain response's metadata."""
    usage = LLMUsage(latency_ms=latency_ms, model=model)
    meta = getattr(response, "usage_metadata", None) or {}
    if isinstance(meta, dict):
        usage.input_tokens = meta.get("input_tokens")
        usage.output_tokens = meta.get("output_tokens")
        usage.total_tokens = meta.get("total_tokens")
    # Also look at response_metadata for providers that report it there.
    if usage.total_tokens is None:
        rmeta = getattr(response, "response_metadata", None) or {}
        tokens = rmeta.get("token_usage") if isinstance(rmeta, dict) else None
        if isinstance(tokens, dict):
            usage.input_tokens = tokens.get("prompt_tokens", usage.input_tokens)
            usage.output_tokens = tokens.get("completion_tokens", usage.output_tokens)
            usage.total_tokens = tokens.get("total_tokens", usage.total_tokens)
    return usage


class LLMReasoner:
    """A budgeted, traced wrapper around a chat model for structured reasoning.

    One reasoner is created per skill execution (bound to that execution's
    trace and the skill's LLM handle). It holds the reasoning budget so a
    single workflow cannot issue unbounded calls.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        *,
        trace: TraceRecorder | None = None,
        settings: Settings = app_settings,
        secrets: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._llm = llm
        self._trace = trace
        self._settings = settings
        self._secrets = tuple(secrets)
        self.records: list[LLMCallRecord] = []

    # -- budget -----------------------------------------------------------

    @property
    def max_calls(self) -> int:
        return max(0, int(self._settings.llm_max_reasoning_calls))

    @property
    def calls_made(self) -> int:
        return len(self.records)

    def _check_budget(self, template_id: str) -> None:
        if self.calls_made >= self.max_calls:
            raise LLMBudgetExceededError(
                f"LLM reasoning budget exhausted ({self.max_calls} calls)",
                details={
                    "template_id": template_id,
                    "max_reasoning_calls": self.max_calls,
                },
            )

    # -- tracing ----------------------------------------------------------

    def _event(self, event: str, payload: dict[str, Any]) -> None:
        if self._trace is not None:
            self._trace.event(event, payload, secrets=self._secrets)

    @property
    def model_name(self) -> str | None:
        return getattr(self._llm, "model", None) or getattr(
            self._llm, "model_name", None
        )

    # -- structured call --------------------------------------------------

    async def reason(
        self,
        *,
        template_id: str,
        system_prompt: str,
        user_prompt: str,
        schema: type[ModelT],
        max_attempts: int = 2,
    ) -> ModelT:
        """Run one structured reasoning call and validate the output.

        Args:
            template_id: Stable identifier of the prompt template (traced).
            system_prompt: The system instruction (never traced verbatim).
            user_prompt: The user content (never traced verbatim).
            schema: Pydantic model the response must validate into.
            max_attempts: Bounded retries when the model returns unusable
                output. Never exceeds the remaining reasoning budget.

        Raises:
            LLMBudgetExceededError: The reasoning budget is exhausted.
            LLMDisabledError: No chat model is bound.
            LLMStructuredOutputError: Output never validated into ``schema``.
            LLMInvocationError: The provider call itself failed.
        """
        if self._llm is None:
            raise LLMDisabledError(
                "AI reasoning requested but no LLM is configured",
                details={"template_id": template_id},
            )
        self._check_budget(template_id)

        version = self._settings.llm_prompt_version
        model = self.model_name
        last_error: str | None = None

        attempts = max(1, min(max_attempts, self.max_calls - self.calls_made))
        for attempt in range(1, attempts + 1):
            self._check_budget(template_id)
            self._event(
                "llm_invocation_started",
                {
                    "template_id": template_id,
                    "prompt_version": version,
                    "model": model,
                    "attempt": attempt,
                    "prompt_chars": len(system_prompt) + len(user_prompt),
                },
            )
            started = time.perf_counter()
            try:
                response = await self._llm.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                )
            except Exception as exc:  # provider/network failure
                latency = round((time.perf_counter() - started) * 1000, 3)
                record = LLMCallRecord(
                    template_id=template_id,
                    prompt_version=version,
                    model=model,
                    ok=False,
                    latency_ms=latency,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.records.append(record)
                self._event("llm_invocation_failed", record.to_payload())
                raise LLMInvocationError(
                    f"LLM call '{template_id}' failed: {exc}",
                    details={"template_id": template_id, "attempt": attempt},
                ) from exc

            latency = round((time.perf_counter() - started) * 1000, 3)
            usage = _usage_from_response(response, latency, model)
            content = (
                response.content
                if isinstance(response.content, str)
                else str(response.content)
            )
            data = _extract_json_object(content)
            if data is None:
                last_error = "response contained no JSON object"
                record = LLMCallRecord(
                    template_id=template_id,
                    prompt_version=version,
                    model=model,
                    ok=False,
                    latency_ms=latency,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    error=last_error,
                )
                self.records.append(record)
                self._event("llm_invocation_failed", record.to_payload())
                continue

            try:
                validated = schema.model_validate(data)
            except ValidationError as exc:
                last_error = f"schema validation failed: {exc.error_count()} error(s)"
                record = LLMCallRecord(
                    template_id=template_id,
                    prompt_version=version,
                    model=model,
                    ok=False,
                    latency_ms=latency,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    error=last_error,
                )
                self.records.append(record)
                self._event(
                    "llm_invocation_failed",
                    {**record.to_payload(), "validation_errors": exc.errors()},
                )
                continue

            record = LLMCallRecord(
                template_id=template_id,
                prompt_version=version,
                model=model,
                ok=True,
                latency_ms=latency,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                structured_output=validated.model_dump(),
            )
            self.records.append(record)
            self._event("llm_invocation_completed", record.to_payload())
            return validated

        raise LLMStructuredOutputError(
            f"LLM output for '{template_id}' did not validate after "
            f"{attempts} attempt(s): {last_error}",
            details={"template_id": template_id, "last_error": last_error},
        )

    # -- convenience ------------------------------------------------------

    def aggregate_usage(self) -> dict[str, Any]:
        """Totals across every recorded invocation for the activity view."""
        input_tokens = sum(r.input_tokens or 0 for r in self.records)
        output_tokens = sum(r.output_tokens or 0 for r in self.records)
        total = sum(r.total_tokens or 0 for r in self.records)
        return {
            "calls": len(self.records),
            "failed_calls": sum(1 for r in self.records if not r.ok),
            "input_tokens": input_tokens or None,
            "output_tokens": output_tokens or None,
            "total_tokens": total or None,
        }


def build_reasoner(
    llm: BaseChatModel | None,
    *,
    trace: TraceRecorder | None = None,
    settings: Settings = app_settings,
    secrets: tuple[str, ...] | list[str] = (),
) -> LLMReasoner | None:
    """Return a reasoner bound to ``llm``, or ``None`` when no LLM is present.

    A ``None`` return is the explicit "AI not configured" signal: callers must
    report the ``not_configured`` status rather than pretend reasoning ran.
    """
    if llm is None:
        return None
    return LLMReasoner(
        llm, trace=trace, settings=settings, secrets=secrets
    )
