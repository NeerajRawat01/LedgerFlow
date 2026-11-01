"""Filesystem storage helpers.

Phase 1 persists everything on the filesystem. This module centralises
path resolution so later phases (runs, issues, agent logs, reports) share
one storage boundary — and so PostgreSQL can be introduced later behind
the same interface without touching the accounting engine.

Phase 2 adds the persistence primitives used by the agentic layer:
atomic JSON writes, JSONL append (traces) and safe reads. All writes go
through :func:`atomic_write_json` so a crash never leaves a half-written
file, and persisted state survives backend restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

from app.core.config import settings
from app.core.errors import StorageError


class FileStoragePaths:
    """Resolve and expose the LedgerFlow filesystem layout."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        storage_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir or settings.data_dir)
        self.storage_dir = Path(storage_dir or settings.storage_dir)
        self.output_dir = Path(output_dir or settings.output_dir)

    @property
    def runs_dir(self) -> Path:
        """Future consolidation run metadata."""
        return self.storage_dir / "runs"

    @property
    def issues_dir(self) -> Path:
        """Future issue and review state."""
        return self.storage_dir / "issues"

    @property
    def agent_logs_dir(self) -> Path:
        """Future agent execution logs."""
        return self.storage_dir / "agent_logs"

    @property
    def threads_dir(self) -> Path:
        """Persisted conversation threads (copilot)."""
        return self.agent_logs_dir / "threads"

    @property
    def traces_dir(self) -> Path:
        """Agent execution traces (JSONL)."""
        return self.agent_logs_dir / "traces"

    @property
    def source_data_dir(self) -> Path:
        """Immutable source inputs (e.g. data/northwind)."""
        return self.data_dir

    def ensure_layout(self) -> None:
        """Create writable directories if they do not exist.

        The source data directory is never created or modified here; it is
        treated as immutable input.
        """
        for directory in (
            self.storage_dir,
            self.runs_dir,
            self.issues_dir,
            self.agent_logs_dir,
            self.threads_dir,
            self.traces_dir,
            self.output_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


storage_paths = FileStoragePaths()


# --- persistence primitives -------------------------------------------------


def atomic_write_json(path: str | Path, data: Any) -> None:
    """Write JSON atomically (temp file in the same dir + ``os.replace``)."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    except OSError as exc:  # pragma: no cover - defensive
        raise StorageError(f"failed to write {target}", details={"path": str(target)}) from exc


def read_json(path: str | Path) -> Any:
    """Read a JSON file. Raises ``StorageError`` when it does not exist."""
    target = Path(path)
    if not target.is_file():
        raise StorageError(f"missing json file: {target}", details={"path": str(target)})
    try:
        with target.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(
            f"unreadable json file: {target}", details={"path": str(target)}
        ) from exc


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    """Append one JSON record as a line (used for execution traces)."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        raise StorageError(
            f"failed to append to {target}", details={"path": str(target)}
        ) from exc


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield records from a JSONL file, skipping malformed lines."""
    target = Path(path)
    if not target.is_file():
        raise StorageError(f"missing jsonl file: {target}", details={"path": str(target)})
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
