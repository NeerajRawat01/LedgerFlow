"""Execution tracing for the core agent.

Each execution gets one JSONL trace file (``storage/agent_logs/traces/<trace_id>.jsonl``).
Events are appended as they happen so a run can be reconstructed after the
fact. All payloads pass through :func:`redact` before they are written, so
secrets (API keys, tokens) can never leak into a stored trace.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.storage.filesystem import append_jsonl, iter_jsonl

# Keys whose values are always masked, regardless of content.
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|password|secret|token|credential)",
    re.IGNORECASE,
)

# Values that look like common API key formats.
_SENSITIVE_VALUE_RES = [
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"sk-[0-9A-Za-z_\-]{16,}"),  # generic secret key
]

_MASK = "***REDACTED***"


def redact(value: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """Recursively mask secrets in strings, dicts and lists."""
    secrets = [s for s in extra_secrets if s and len(s) >= 8]

    if isinstance(value, str):
        out = value
        for secret in secrets:
            if secret in out:
                out = out.replace(secret, _MASK)
        for pattern in _SENSITIVE_VALUE_RES:
            out = pattern.sub(_MASK, out)
        return out
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                redacted[key] = _MASK
            else:
                redacted[key] = redact(item, secrets)
        return redacted
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secrets) for item in value)
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceRecorder:
    """Append-only JSONL recorder bound to a single execution trace."""

    def __init__(self, traces_dir: str | Path, trace_id: str) -> None:
        self.trace_id = trace_id
        self.path = Path(traces_dir) / f"{trace_id}.jsonl"
        # Resume numbering so a recorder rebuilt for the same trace (one is
        # created per graph node) continues the file instead of restarting.
        self._seq = 0
        if self.path.is_file():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    self._seq = sum(1 for line in handle if line.strip())
            except OSError:  # pragma: no cover - defensive
                self._seq = 0

    def event(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        secrets: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Append one event; returns the record that was written."""
        self._seq += 1
        record = {
            "ts": _now(),
            "seq": self._seq,
            "trace_id": self.trace_id,
            "event": event,
            "payload": redact(payload or {}, secrets),
        }
        append_jsonl(self.path, record)
        return record

    def read_events(self) -> list[dict[str, Any]]:
        """Read back every event recorded for this trace."""
        if not self.path.is_file():
            return []
        return list(iter_jsonl(self.path))
