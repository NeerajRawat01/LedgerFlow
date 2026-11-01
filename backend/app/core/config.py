"""Application configuration.

Environment-driven settings only. Phase 1 intentionally keeps this
minimal: no database connection settings exist here (and must not be
added while PostgreSQL remains an unused infrastructure service).

Phase 2 adds LLM, MCP and agent-execution settings. The API key is
excluded from ``repr`` so it can never be printed or logged accidentally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


#: Providers the agent can talk to, mapped to their default model. A key is
#: only consulted for the provider that is actually selected.
_DEFAULT_LLM_MODELS: dict[str, str] = {
    "gemini": "gemini-3.8-flash",
    "groq": "openai/gpt-oss-120b",
}


def _default_llm_provider() -> str:
    """Pick a provider when ``LLM_PROVIDER`` is unset.

    Groq wins if only a Groq key is present; otherwise Gemini. This keeps the
    explicit ``LLM_PROVIDER`` authoritative while still doing the obvious thing
    when exactly one key is configured.
    """
    if (os.getenv("GROQ_API_KEY") or "").strip() and not (
        os.getenv("GEMINI_API_KEY") or ""
    ).strip():
        return "groq"
    return "gemini"


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from environment variables."""

    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    service_name: str = field(
        default_factory=lambda: os.getenv("SERVICE_NAME", "ledgerflow-backend")
    )
    version: str = field(default_factory=lambda: os.getenv("APP_VERSION", "0.1.0"))
    cors_origins: list[str] = field(
        default_factory=lambda: _split_origins(
            os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
        )
    )

    # Filesystem storage roots (Phase 1 persistence is filesystem-based).
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", "data"))
    storage_dir: str = field(default_factory=lambda: os.getenv("STORAGE_DIR", "storage"))
    output_dir: str = field(default_factory=lambda: os.getenv("OUTPUT_DIR", "output"))

    # --- LLM (provider-agnostic) ------------------------------------------
    # The agent works with any supported chat provider. Selection is explicit
    # via ``LLM_PROVIDER`` (``gemini`` | ``groq``) and ``LLM_API_KEY``. The
    # legacy ``GEMINI_API_KEY`` is still honoured as a fallback so existing
    # Gemini setups keep working without change. Neither key ever appears in
    # repr/log output.
    llm_provider: str = field(
        default_factory=lambda: (
            os.getenv("LLM_PROVIDER") or _default_llm_provider()
        ).strip().lower()
        or "gemini"
    )
    llm_api_key: str | None = field(
        default_factory=lambda: (os.getenv("LLM_API_KEY") or "").strip() or None,
        repr=False,
    )
    # Backward-compatible Gemini alias. ``llm_api_key`` wins when both are set;
    # otherwise this is used for the ``gemini`` provider.
    gemini_api_key: str | None = field(
        default_factory=lambda: (os.getenv("GEMINI_API_KEY") or "").strip() or None,
        repr=False,
    )
    llm_model: str = field(
        default_factory=lambda: (
            os.getenv("LLM_MODEL") or _DEFAULT_LLM_MODELS.get(
                (os.getenv("LLM_PROVIDER") or _default_llm_provider()).strip().lower(),
                _DEFAULT_LLM_MODELS["gemini"],
            )
        ).strip()
        or _DEFAULT_LLM_MODELS["gemini"]
    )
    llm_temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE") or "0")
    )
    llm_max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 2))
    # Hard ceiling on the wall-clock time of a single LLM call.
    llm_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("LLM_TIMEOUT_SECONDS") or "60")
    )
    # Explicit reasoning budget: the maximum number of LLM invocations one
    # agent execution may make. Mirrors ``agent_max_tool_calls`` so reasoning
    # can never run away silently.
    llm_max_reasoning_calls: int = field(
        default_factory=lambda: _env_int("LLM_MAX_REASONING_CALLS", 8)
    )
    # Version tag stamped onto every prompt template, so a stored trace can be
    # tied back to the exact prompt that produced it.
    llm_prompt_version: str = field(
        default_factory=lambda: (os.getenv("LLM_PROMPT_VERSION") or "v1").strip() or "v1"
    )

    # --- MCP --------------------------------------------------------------
    mcp_server_url: str = field(
        default_factory=lambda: (
            os.getenv("MCP_SERVER_URL") or ""
        ).strip()
        or "http://127.0.0.1:8000/mcp"
    )
    # Per-request timeout for one MCP session (connect + initialize + call).
    mcp_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("MCP_TIMEOUT_SECONDS") or "30")
    )

    # --- Agent execution limits -------------------------------------------
    agent_max_tool_calls: int = field(
        default_factory=lambda: _env_int("AGENT_MAX_TOOL_CALLS", 25)
    )
    agent_max_graph_steps: int = field(
        default_factory=lambda: _env_int("AGENT_MAX_GRAPH_STEPS", 50)
    )

    @property
    def resolved_llm_api_key(self) -> str | None:
        """The key for the selected provider, honouring legacy fallbacks.

        ``LLM_API_KEY`` is authoritative. Otherwise the provider-specific
        variable is used: ``GROQ_API_KEY`` for groq, ``GEMINI_API_KEY`` for
        gemini. Returns ``None`` when nothing usable is configured.
        """
        if self.llm_api_key:
            return self.llm_api_key
        if self.llm_provider == "groq":
            return (os.getenv("GROQ_API_KEY") or "").strip() or None
        return self.gemini_api_key


settings = Settings()
