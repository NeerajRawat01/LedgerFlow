"""Read-only access to the immutable source dataset (``data/northwind``).

This service **parses** source files (CSV/JSON) and returns them exactly as
recorded — column names, string values, no type coercion, no interpretation.
Interpretation and financial validation belong to the validation services
(Milestone 4); FX translation, eliminations, NCI and statement assembly
remain missing dependencies on purpose (decision D? — validation-only
accounting boundary) and must never be fabricated here.

Every method is read-only: the service never creates, moves or modifies
anything under the data directory, which is mounted read-only in Docker.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import NotFoundError, ServiceError


class SourceDataService:
    """Typed readers over ``<data_dir>/<dataset>`` (default: ``data/northwind``)."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        dataset: str = "northwind",
    ) -> None:
        self.data_dir = Path(data_dir if data_dir is not None else settings.data_dir)
        self.dataset = dataset
        self.root = self.data_dir / dataset

    # -- internal helpers -------------------------------------------------

    def _require_root(self) -> None:
        if not self.root.is_dir():
            raise NotFoundError(
                f"source dataset '{self.dataset}' not found",
                details={"root": str(self.root)},
            )

    def _read_csv(self, name: str) -> tuple[list[str], list[dict[str, str]]]:
        """Return ``(columns, rows)`` for one CSV, values kept as strings."""
        self._require_root()
        path = self.root / name
        if not path.is_file():
            raise NotFoundError(
                f"source file not found: {name}",
                details={"root": str(self.root), "file": name},
            )
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                columns = list(reader.fieldnames or [])
                rows: list[dict[str, str]] = []
                for row in reader:
                    # csv.DictReader puts leftovers under the ``None`` key
                    # for malformed rows; drop them so output stays typed.
                    rows.append({k: v for k, v in row.items() if k is not None})
                return columns, rows
        except OSError as exc:
            raise ServiceError(
                f"failed to read {name}: {exc}", details={"file": name}
            ) from exc
        except csv.Error as exc:
            raise ServiceError(
                f"malformed CSV {name}: {exc}", details={"file": name}
            ) from exc

    def _read_json(self, name: str) -> Any:
        self._require_root()
        path = self.root / name
        if not path.is_file():
            raise NotFoundError(
                f"source file not found: {name}",
                details={"root": str(self.root), "file": name},
            )
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except OSError as exc:
            raise ServiceError(
                f"failed to read {name}: {exc}", details={"file": name}
            ) from exc
        except json.JSONDecodeError as exc:
            raise ServiceError(
                f"malformed JSON {name}: {exc}", details={"file": name}
            ) from exc

    def _entity_rows(self) -> list[dict[str, str]]:
        _, rows = self._read_csv("entities.csv")
        return rows

    def _entity(self, entity: str) -> dict[str, str]:
        """Normalise and resolve one entity id, or raise ``not_found``."""
        wanted = (entity or "").strip().upper()
        for row in self._entity_rows():
            if row.get("entity_id", "").strip().upper() == wanted:
                return row
        available = sorted(r.get("entity_id", "") for r in self._entity_rows())
        raise NotFoundError(
            f"unknown entity: {entity!r}",
            details={"requested": entity, "available": available},
        )

    # -- API used by MCP tools -------------------------------------------

    def list_source_files(self) -> list[dict[str, Any]]:
        """Inventory of every file in the dataset (name, size in bytes)."""
        self._require_root()
        return [
            {"name": path.name, "bytes": path.stat().st_size}
            for path in sorted(self.root.iterdir())
            if path.is_file()
        ]

    def list_entities(self) -> list[dict[str, str]]:
        """All group entities as recorded in ``entities.csv``."""
        return self._entity_rows()

    def get_trial_balance(self, entity: str) -> dict[str, Any]:
        """Raw trial balance rows for one entity, exactly as recorded."""
        row = self._entity(entity)
        source = (row.get("tb_file") or "").strip()
        if not source:
            raise NotFoundError(
                f"entity {entity!r} has no trial balance file",
                details={"entity": row.get("entity_id")},
            )
        columns, rows = self._read_csv(source)
        return {
            "entity": row.get("entity_id"),
            "source_file": source,
            "columns": columns,
            "rows": rows,
        }

    def get_fx_rates(
        self,
        base_currency: str | None = None,
        rate_type: str | None = None,
    ) -> list[dict[str, str]]:
        """FX rate rows, optionally filtered by base currency and/or rate type."""
        _, rows = self._read_csv("fx_rates.csv")
        if base_currency:
            want = base_currency.strip().upper()
            rows = [r for r in rows if r.get("base_currency", "").strip().upper() == want]
        if rate_type:
            want = rate_type.strip().lower()
            rows = [r for r in rows if r.get("rate_type", "").strip().lower() == want]
        return rows

    def get_group_coa(self) -> list[dict[str, str]]:
        """Group chart of accounts (``group_coa.csv``)."""
        _, rows = self._read_csv("group_coa.csv")
        return rows

    def get_account_mapping(self, entity: str) -> dict[str, Any]:
        """Local→group account mapping rows for one entity.

        The US posts directly to the group chart, so no mapping file exists
        for it; that is reported as ``source_file: null`` with empty rows.
        """
        row = self._entity(entity)
        entity_id = row.get("entity_id", "")
        name = f"map_{entity_id.lower()}.csv"
        path = self.root / name
        if not path.is_file():
            return {"entity": entity_id, "source_file": None, "rows": []}
        columns, rows = self._read_csv(name)
        return {"entity": entity_id, "source_file": name, "columns": columns, "rows": rows}

    def get_intercompany(self) -> list[dict[str, str]]:
        """Intercompany invoice positions (``intercompany.csv``)."""
        _, rows = self._read_csv("intercompany.csv")
        return rows

    def get_subledger_controls(
        self, entity: str | None = None
    ) -> list[dict[str, str]]:
        """Subledger control totals, optionally filtered by entity id."""
        _, rows = self._read_csv("subledger_controls.csv")
        if entity:
            want = entity.strip().upper()
            rows = [r for r in rows if r.get("entity_id", "").strip().upper() == want]
        return rows

    def get_prior_year_group(self) -> list[dict[str, str]]:
        """Prior-year group balances (``prior_year_group.csv``)."""
        _, rows = self._read_csv("prior_year_group.csv")
        return rows

    def get_monthly_pnl(self, entity: str | None = None) -> list[dict[str, str]]:
        """Monthly P&L movements, optionally filtered by entity id.

        The dataset only carries monthly movement data for IN ledgers;
        filtering for other entities returns an empty list.
        """
        _, rows = self._read_csv("in_pl_monthly.csv")
        if entity:
            want = entity.strip().upper() + "-"
            rows = [r for r in rows if r.get("ledger_code", "").upper().startswith(want)]
        return rows

    def get_adjustments(self) -> dict[str, Any]:
        """Year-end adjustment entries (``adjustments.json``), verbatim."""
        data = self._read_json("adjustments.json")
        return data if isinstance(data, dict) else {"entries": data}
