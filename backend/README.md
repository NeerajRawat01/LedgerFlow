# LedgerFlow Backend

FastAPI service for the LedgerFlow consolidation platform.

## Scope

- `GET /` — application identification
- `GET /api/health` — health check
- `POST /api/consolidations/runs` — run the FY2024 group consolidation
- `GET /api/consolidations/runs` — persisted run history
- `GET /api/consolidations/runs/{run_id}` — one persisted run
- `GET /api/consolidations/runs/{run_id}/trace` — execution trace + tool calls
- `GET /api/consolidations/output/{filename}` — download an `/output` deliverable
- CORS configured for the frontend
- Deterministic accounting engine (`app/accounting/`), shared LangGraph agent
  (`app/agents/`) and MCP tool layer (`app/mcp/`) — no database dependency;
  state is persisted on the filesystem under `storage/` and `output/`.

## Run the consolidation

```bash
.venv/bin/python ../scripts/run_consolidation.py
```

## Run locally

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

## Run tests

```bash
pytest
```
