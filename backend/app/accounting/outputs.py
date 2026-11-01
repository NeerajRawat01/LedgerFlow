"""Write the seven assignment deliverables from a :class:`ConsolidationOutput`.

This module is the only place that turns the in-memory consolidation result
into the files under ``/output``. It performs **no accounting arithmetic** — it
serialises figures that were already produced and validated by the deterministic
engine, applying :func:`round_reported` only where a figure has not already been
rounded at the reporting boundary.

Files written (assignment §4):

* ``consolidated_tb.csv``   — consolidated trial balance in GBP w/ source rows
* ``balance_sheet.json``    — group balance sheet (GBP)
* ``profit_and_loss.json``  — group P&L (GBP), incl. NCI
* ``changes_in_equity.json``— changes in equity (GBP)
* ``eliminations.csv``      — every elimination entry w/ source rows
* ``issues.json``           — every issue found, w/ file/line/amount/action
* ``control_figures_ns.json``— the 12 control figures (null + reason if unknown)

The output directory is resolved from settings unless overridden, and is created
if absent. Writing is atomic per file so a crash cannot leave a half-written
deliverable.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.accounting.models import round_reported
from app.accounting.statements import ConsolidationOutput

# The control figures file name. The assignment asks for
# ``control_figures_<initials>.json``; the submitting author's initials are
# "ns" (see DECISIONS.md).
CONTROL_FIGURES_FILE = "control_figures_ns.json"

CONTROL_FIGURE_KEYS: tuple[str, ...] = (
    "group_revenue",
    "group_profit",
    "profit_owners",
    "profit_nci",
    "nci_closing",
    "goodwill_closing",
    "translation_reserve_movement",
    "total_assets",
    "total_equity",
    "intercompany_difference",
    "sgd_bank_gbp",
    "journals",
)


def _num(value: Any) -> Any:
    """Render a Decimal as a JSON number string-free (2 dp), else pass through."""
    if isinstance(value, Decimal):
        return float(round_reported(value))
    if isinstance(value, dict):
        return {k: _num(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_num(v) for v in value]
    return value


class OutputWriter:
    """Serialise a :class:`ConsolidationOutput` to the seven deliverables."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    def write_all(self, output: ConsolidationOutput) -> list[dict[str, Any]]:
        """Write every deliverable and return a manifest of what was written."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written: list[dict[str, Any]] = []

        written.append(self._write_consolidated_tb(output))
        written.append(self._write_statement(output, "balance_sheet.json", output.statements.balance_sheet))
        written.append(self._write_statement(output, "profit_and_loss.json", output.statements.profit_and_loss))
        written.append(self._write_statement(output, "changes_in_equity.json", output.statements.changes_in_equity))
        written.append(self._write_eliminations(output))
        written.append(self._write_issues(output))
        written.append(self._write_control_figures(output))
        return written

    # -- individual writers ------------------------------------------------

    def _write(self, name: str, text: str) -> dict[str, Any]:
        path = self.output_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return {"file": name, "path": str(path), "bytes": len(text.encode("utf-8"))}

    def _write_consolidated_tb(self, output: ConsolidationOutput) -> dict[str, Any]:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(
            [
                "group_account_code",
                "group_account_name",
                "account_type",
                "statement",
                "gbp_debit",
                "gbp_credit",
                "gbp_net",
                "source_entities",
                "source_rows",
            ]
        )
        for row in output.trial_balance_rows:
            # Compact, traceable source reference list: entity/file#row per
            # contributing record, so each balance walks back to its source.
            source_rows = ";".join(
                (
                    f"{s.get('entity')}:{s.get('source_file')}"
                    f"#{s.get('source_row')}"
                    if s.get("source_row")
                    else f"{s.get('entity')}:{s.get('source_file')}"
                )
                for s in row.sources
                if s.get("source_file")
            )
            writer.writerow(
                [
                    row.group_account_code,
                    row.group_account_name,
                    row.account_type,
                    row.statement,
                    str(round_reported(row.gbp_debit)),
                    str(round_reported(row.gbp_credit)),
                    str(round_reported(row.gbp_net)),
                    ";".join(sorted({s["entity"] for s in row.sources})),
                    source_rows,
                ]
            )
        return self._write("consolidated_tb.csv", buffer.getvalue())

    def _write_statement(self, output: ConsolidationOutput, name: str, sections: list[Any]) -> dict[str, Any]:
        import json

        payload = {
            "presentation_currency": output.statements.presentation_currency,
            "period": output.statements.period,
            "basis": "GBP, rounded to 2 dp at the reporting boundary",
            "sections": [
                {
                    "title": section.title,
                    "lines": [
                        {
                            "group_account_code": line.group_account_code,
                            "label": line.label,
                            "amount": _num(line.amount),
                            "reason": line.reason,
                            "note": line.note,
                        }
                        for line in section.lines
                    ],
                    "total": _num(section.total),
                    "total_reason": section.total_reason,
                }
                for section in sections
            ],
        }
        return self._write(name, json.dumps(payload, indent=2) + "\n")

    def _write_eliminations(self, output: ConsolidationOutput) -> dict[str, Any]:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(
            [
                "journal_id",
                "kind",
                "reference",
                "group_account_code",
                "group_account_name",
                "entity",
                "gbp_debit",
                "gbp_credit",
                "reason",
            ]
        )
        for entry in output.eliminations:
            writer.writerow(
                [
                    entry.journal_id,
                    entry.kind,
                    entry.reference,
                    entry.group_account_code,
                    entry.group_account_name,
                    entry.entity,
                    str(round_reported(entry.gbp_debit)),
                    str(round_reported(entry.gbp_credit)),
                    entry.reason or entry.note,
                ]
            )
        return self._write("eliminations.csv", buffer.getvalue())

    def _write_issues(self, output: ConsolidationOutput) -> dict[str, Any]:
        import json

        issues = []
        for issue in output.issues:
            issues.append(
                {
                    "code": issue.code,
                    "severity": issue.severity.value,
                    "message": issue.message,
                    "action": issue.action,
                    "entity": issue.entity,
                    "source_file": issue.source_file,
                    "source_row": issue.source_row,
                    "account_code": issue.account_code,
                    "affected_amount": _num(issue.affected_amount),
                    "treatment": issue.treatment,
                    "details": _num(issue.details),
                }
            )
        payload = {
            "count": len(issues),
            "issues": issues,
        }
        return self._write("issues.json", json.dumps(payload, indent=2, default=str) + "\n")

    def _write_control_figures(self, output: ConsolidationOutput) -> dict[str, Any]:
        import json

        figures = output.control_figures
        payload: dict[str, Any] = {}
        for key in CONTROL_FIGURE_KEYS:
            payload[key] = _num(figures.get(key))

        # Attach an explicit reason alongside a null sgd_bank_gbp.
        reason = figures.get("sgd_bank_gbp_reason")
        if payload.get("sgd_bank_gbp") is None and reason:
            payload["sgd_bank_gbp"] = {"value": None, "reason": reason}

        # The intercompany difference keeps its split causes.
        ic = figures.get("intercompany_difference")
        if isinstance(ic, dict):
            payload["intercompany_difference"] = {
                "value": _num(ic.get("total")),
                "causes": _num(ic.get("causes", {})),
                "matched_pairs": ic.get("matched_pairs"),
                "unmatched_items": ic.get("unmatched_items"),
            }
        return self._write(CONTROL_FIGURES_FILE, json.dumps(payload, indent=2, default=str) + "\n")
