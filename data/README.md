# Data directory

This directory contains **immutable source inputs**.

## `northwind/`

The original Northwind Group consolidation assignment bundle, preserved
byte-for-byte exactly as it was received:

- `README.md` — original assignment readme
- `AI_Agentic_Engineer_TakeHome_v2.pdf` — original assignment brief
- CSV/JSON/Markdown input files — trial balances, account mappings, FX rates,
  intercompany documents, adjustments, policies and related inputs

Rules for this directory:

- **Do not modify, rename, reformat or clean** any file here.
- **Do not create replacement sample data** here.
- **Do not seed** any of this data into PostgreSQL.
- Application code mounts this directory **read-only** (`data:/app/data:ro`).

All processing, if/when implemented in later phases, must read from these
files without writing back to them. Derived output belongs in `storage/`
and `output/` at the repository root.
