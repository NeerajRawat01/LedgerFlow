"""Consolidation run persistence (filesystem, no database).

Each run is stored as one JSON file under ``storage/runs/<run_id>.json`` using
the existing atomic-write primitive, so a run survives a backend restart and
can be re-fetched by id. This is the minimum needed to support the
``GET /api/consolidations/runs/{run_id}`` refresh path — no new persistence
system is introduced, and no PostgreSQL is required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.errors import NotFoundError, StorageError
from app.storage.filesystem import atomic_write_json, read_json


class ConsolidationRunStore:
    """Read/write consolidation run records on the filesystem."""

    def __init__(self, runs_dir: str | Path) -> None:
        self.runs_dir = Path(runs_dir)

    def _path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
            raise StorageError("invalid run id", details={"run_id": run_id})
        return self.runs_dir / f"{run_id}.json"

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).is_file()

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        """Persist a run record (``created_at`` set once, preserved on rewrite)."""
        run_id = str(record.get("run_id") or "")
        path = self._path(run_id)
        now = datetime.now(timezone.utc).isoformat()
        created_at = now
        if path.is_file():
            try:
                created_at = read_json(path).get("created_at", now)
            except StorageError:
                pass
        stored = {**record, "created_at": created_at, "updated_at": now}
        atomic_write_json(path, stored)
        return stored

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return stored runs, newest first (by ``created_at``).

        Corrupt/unreadable records are skipped rather than failing the whole
        listing, so one bad file cannot hide the run history.
        """
        if not self.runs_dir.is_dir():
            return []
        runs: list[dict[str, Any]] = []
        for path in self.runs_dir.glob("*.json"):
            try:
                data = read_json(path)
            except StorageError:
                continue
            if isinstance(data, dict) and data.get("run_id"):
                runs.append(data)
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return runs[:limit]

    def load(self, run_id: str) -> dict[str, Any]:
        """Return a stored run, or raise ``NotFoundError``."""
        path = self._path(run_id)
        if not path.is_file():
            raise NotFoundError(
                "consolidation run not found", details={"run_id": run_id}
            )
        data = read_json(path)
        if not isinstance(data, dict):
            raise StorageError(
                "corrupt consolidation run record", details={"run_id": run_id}
            )
        return data
