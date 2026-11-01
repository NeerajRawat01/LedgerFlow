"""Typed application errors for the agentic backend.

Every error carries a stable machine-readable ``code`` so API responses and
agent traces can classify failures without string matching.
"""

from __future__ import annotations

from typing import Any


class LedgerFlowError(Exception):
    """Base error for all LedgerFlow backend failures."""

    code: str = "ledgerflow_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigurationError(LedgerFlowError):
    """Missing or invalid runtime configuration."""

    code = "configuration_error"


class LLMNotConfiguredError(ConfigurationError):
    """The LLM provider cannot be created (no API key or bad settings)."""

    code = "llm_not_configured"


class LLMInvocationError(LedgerFlowError):
    """The LLM was configured but the call failed (network, provider, ...)."""

    code = "llm_invocation_failed"


class LLMStructuredOutputError(LedgerFlowError):
    """The LLM returned output that could not be validated into the target model."""

    code = "llm_structured_output_invalid"


class LLMBudgetExceededError(LedgerFlowError):
    """The per-execution LLM reasoning budget was exhausted."""

    code = "llm_budget_exceeded"


class LLMDisabledError(LedgerFlowError):
    """An AI-dependent operation was requested while no LLM is configured.

    Distinct from ``llm_not_configured`` (a configuration error): this is the
    explicit, expected status an AI stage reports when the deterministic path
    must proceed without reasoning. It is never silently swallowed.
    """

    code = "llm_disabled"


class IntentIdentificationError(LedgerFlowError):
    """Intent could not be determined from the request."""

    code = "intent_identification_failed"


class SkillError(LedgerFlowError):
    """Base class for skill registry failures."""

    code = "skill_error"


class SkillNotFoundError(SkillError):
    """Requested skill does not exist in the registry."""

    code = "skill_not_found"


class SkillNotAllowedForModeError(SkillError):
    """Skill exists but is not permitted for the current execution mode."""

    code = "skill_not_allowed_for_mode"


class SkillExecutionError(SkillError):
    """A skill raised an unrecoverable error while executing."""

    code = "skill_execution_failed"


class ToolPolicyError(LedgerFlowError):
    """A tool invocation violated the skill/tool gateway policy."""

    code = "tool_policy_violation"


class MCPError(LedgerFlowError):
    """MCP client/server communication failure."""

    code = "mcp_error"


class ServiceError(LedgerFlowError):
    """A backend service failed while processing a request."""

    code = "service_error"


class NotFoundError(LedgerFlowError):
    """A requested resource does not exist."""

    code = "not_found"


class StorageError(LedgerFlowError):
    """Filesystem persistence failure."""

    code = "storage_error"


def error_for_code(code: str) -> type[LedgerFlowError] | None:
    """Resolve a stable error ``code`` back to its class.

    Used when typed errors cross a process/transport boundary (MCP): the
    server formats ``"<code>: <message>"`` into the tool error text and
    the client restores the original class from the code, so callers
    still get ``NotFoundError`` / ``ServiceError`` / ... after the round
    trip instead of an opaque transport error.
    """
    if not code:
        return None
    for obj in list(globals().values()):
        if (
            isinstance(obj, type)
            and issubclass(obj, LedgerFlowError)
            and obj is not LedgerFlowError
            and obj.code == code
        ):
            return obj
    return None
