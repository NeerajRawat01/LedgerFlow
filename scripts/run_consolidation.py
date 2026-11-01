#!/usr/bin/env python
"""Run the Northwind Group FY2024 consolidation and write the deliverables.

This is the "one command" from the README. It runs the deterministic accounting
engine directly (no LLM, no network) and writes the seven assignment files to
``OUTPUT_DIR`` (default ``output/``):

    python scripts/run_consolidation.py

Set ``DATA_DIR`` to point at a different dataset root (default ``data``) and
``OUTPUT_DIR`` to change the output directory. The script exits non-zero if the
self-checks fail, so it is safe to wire into CI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the backend package importable when run from the repository root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.accounting.outputs import OutputWriter  # noqa: E402
from app.accounting.statements import StatementAssembler  # noqa: E402
from app.services.source_data import SourceDataService  # noqa: E402


def main() -> int:
    data_dir = os.getenv("DATA_DIR", str(ROOT / "data"))
    output_dir = os.getenv("OUTPUT_DIR", str(ROOT / "output"))

    service = SourceDataService(data_dir)
    assembler = StatementAssembler(service)
    bundle = assembler.assemble(["US", "UK", "IN"])

    writer = OutputWriter(output_dir)
    manifest = writer.write_all(bundle)

    print("Northwind Group FY2024 consolidation")
    print("=" * 40)
    for entry in manifest:
        print(f"  wrote {entry['file']} ({entry['bytes']} bytes)")

    print("\nControl figures (GBP):")
    figures = bundle.control_figures
    for key in (
        "group_revenue",
        "group_profit",
        "profit_owners",
        "profit_nci",
        "nci_closing",
        "goodwill_closing",
        "translation_reserve_movement",
        "total_assets",
        "total_equity",
    ):
        print(f"  {key}: {figures[key]}")
    print(f"  intercompany_difference: {figures['intercompany_difference']['total']}")
    print(f"  journals: {figures['journals']}")
    print(f"  sgd_bank_gbp: {figures.get('sgd_bank_gbp')}")

    checks = bundle.statements.checks
    failed = [c for c in checks if not c.get("passed")]
    print("\nIntegrity checks:")
    for check in checks:
        mark = "OK" if check.get("passed") else "FAIL"
        print(f"  [{mark}] {check['check']}: {check['detail']}")

    if failed:
        print("\nOne or more integrity checks failed.", file=sys.stderr)
        return 1

    print(f"\nDone. Outputs written to {output_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
