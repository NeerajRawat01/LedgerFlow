"""Provider-agnostic LLM factory for the core agent.

``create_chat_model`` builds a LangChain ``BaseChatModel`` for the configured
provider (``gemini`` or ``groq``) when a key is available, otherwise it raises
the typed ``LLM_NOT_CONFIGURED`` error. Provider selection lives entirely in
:class:`~app.core.config.Settings` (``LLM_PROVIDER`` / ``LLM_API_KEY``, with
the legacy ``GEMINI_API_KEY`` / ``GROQ_API_KEY`` still honoured).

Tests inject a ``FakeChatModel`` so no network access is ever required.

The LLM is used only for orchestration (intent identification, reasoning,
answer composition). It never performs financial arithmetic — that stays in
the Python services.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import Settings
from app.core.config import settings as app_settings
from app.core.errors import LLMNotConfiguredError


def _build_gemini(
    model: str, api_key: str, temperature: float, max_retries: int, timeout: float
) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_retries=max_retries,
        timeout=timeout,
    )


def _build_groq(
    model: str, api_key: str, temperature: float, max_retries: int, timeout: float
) -> BaseChatModel:
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_retries=max_retries,
        timeout=timeout,
    )


#: Neutral provider name -> builder. This is the single place that knows how to
#: construct each concrete provider; adding a provider is one entry here.
_PROVIDER_BUILDERS: dict[str, Callable[..., BaseChatModel]] = {
    "gemini": _build_gemini,
    "google": _build_gemini,
    "google_genai": _build_gemini,
    "groq": _build_groq,
}


def _resolve_provider(cfg: Settings) -> str:
    """Normalise the configured provider name to a known registry key."""
    requested = (cfg.llm_provider or "").strip().lower()
    if requested not in _PROVIDER_BUILDERS:
        raise LLMNotConfiguredError(
            f"Unsupported LLM_PROVIDER {requested!r}; expected one of "
            f"{sorted(set(_PROVIDER_BUILDERS))}.",
            details={"setting": "LLM_PROVIDER"},
        )
    return requested


def create_chat_model(settings: Settings | None = None) -> BaseChatModel:
    """Create the configured chat model, or raise ``LLM_NOT_CONFIGURED``.

    Args:
        settings: Config object; defaults to the process-wide settings.

    Returns:
        A configured LangChain chat model for the selected provider.

    Raises:
        LLMNotConfiguredError: When no usable key is present, or the provider
            is not supported.
    """
    cfg = settings or app_settings
    api_key = cfg.resolved_llm_api_key
    if not api_key:
        raise LLMNotConfiguredError(
            "No LLM API key is set; cannot run the AI Copilot. "
            "Set LLM_API_KEY (or the provider-specific key).",
            details={"setting": "LLM_API_KEY"},
        )
    provider = _resolve_provider(cfg)
    builder: Callable[..., Any] = _PROVIDER_BUILDERS[provider]
    return builder(
        cfg.llm_model,
        api_key,
        cfg.llm_temperature,
        cfg.llm_max_retries,
        cfg.llm_timeout_seconds,
    )


def is_llm_configured(settings: Settings | None = None) -> bool:
    """True when a key for the selected provider is present in the environment."""
    cfg = settings or app_settings
    return bool(cfg.resolved_llm_api_key)
