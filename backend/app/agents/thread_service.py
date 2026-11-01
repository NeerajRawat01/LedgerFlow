"""Conversation thread persistence.

Threads (copilot conversations) are stored as one JSON file per thread under
``storage/agent_logs/threads/<thread_id>.json``. Messages are serialised to
plain dicts so they survive a backend restart with no database.

The thread id is the only isolation key: two threads never share a file, so
separate conversations cannot bleed into one another.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.core.errors import NotFoundError, StorageError
from app.storage.filesystem import atomic_write_json, read_json

_MESSAGE_TYPES: dict[str, type] = {
    "human": HumanMessage,
    "ai": AIMessage,
    "system": SystemMessage,
    "tool": ToolMessage,
}


def serialize_message(message: AnyMessage) -> dict[str, Any]:
    """Convert a LangChain message into a JSON-safe dict."""
    record: dict[str, Any] = {"type": message.type, "content": message.content}
    if message.additional_kwargs:
        record["additional_kwargs"] = message.additional_kwargs
    if isinstance(message, ToolMessage):
        record["name"] = message.name
        record["tool_call_id"] = message.tool_call_id
    return record


def deserialize_message(record: dict[str, Any]) -> AnyMessage:
    """Rebuild a LangChain message from its stored dict."""
    msg_type = record.get("type", "human")
    cls = _MESSAGE_TYPES.get(msg_type, HumanMessage)
    kwargs: dict[str, Any] = {"content": record.get("content", "")}
    if record.get("additional_kwargs"):
        kwargs["additional_kwargs"] = record["additional_kwargs"]
    if cls is ToolMessage:
        kwargs["name"] = record.get("name", "")
        kwargs["tool_call_id"] = record.get("tool_call_id", "")
    return cls(**kwargs)


class ThreadService:
    """Read/write conversation threads on the filesystem."""

    def __init__(self, threads_dir: str | Path) -> None:
        self.threads_dir = Path(threads_dir)

    def _path(self, thread_id: str) -> Path:
        if not thread_id or "/" in thread_id or "\\" in thread_id or ".." in thread_id:
            raise StorageError(
                "invalid thread id", details={"thread_id": thread_id}
            )
        return self.threads_dir / f"{thread_id}.json"

    def exists(self, thread_id: str) -> bool:
        return self._path(thread_id).is_file()

    def load(self, thread_id: str) -> list[AnyMessage]:
        """Return the stored messages for a thread ([] when it does not exist)."""
        path = self._path(thread_id)
        if not path.is_file():
            return []
        data = read_json(path)
        if not isinstance(data, dict):
            raise StorageError(
                "corrupt thread record", details={"thread_id": thread_id}
            )
        return [deserialize_message(m) for m in data.get("messages", [])]

    def meta(self, thread_id: str) -> dict[str, Any]:
        """Thread metadata (mode, timestamps) without the messages."""
        path = self._path(thread_id)
        if not path.is_file():
            raise NotFoundError(
                "thread not found", details={"thread_id": thread_id}
            )
        data = read_json(path)
        return {k: v for k, v in data.items() if k != "messages"}

    def save(
        self,
        thread_id: str,
        messages: Iterable[AnyMessage],
        *,
        mode: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically persist a thread's messages (created_at is preserved)."""
        path = self._path(thread_id)
        now = datetime.now(timezone.utc).isoformat()
        created_at = now
        if path.is_file():
            try:
                existing = read_json(path)
                created_at = existing.get("created_at", now)
            except StorageError:
                pass

        record: dict[str, Any] = {
            "thread_id": thread_id,
            "mode": mode,
            "created_at": created_at,
            "updated_at": now,
            "messages": [serialize_message(m) for m in messages],
        }
        if metadata:
            record["metadata"] = metadata
        atomic_write_json(path, record)
        return record

    def list(self) -> list[dict[str, Any]]:
        """List every persisted thread (metadata only)."""
        if not self.threads_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(self.threads_dir.glob("*.json")):
            data = read_json(path)
            if isinstance(data, dict):
                out.append({k: v for k, v in data.items() if k != "messages"})
        return out

    def delete(self, thread_id: str) -> None:
        path = self._path(thread_id)
        if not path.is_file():
            raise NotFoundError(
                "thread not found", details={"thread_id": thread_id}
            )
        path.unlink()
