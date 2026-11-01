# LedgerFlow — Architecture

> **FY2024 consolidation works end-to-end.** This document describes the
> implemented system: the deterministic accounting engine, the shared
> LangGraph agent, the MCP tool layer and the API/dashboard on top.

## 1. Goals

LedgerFlow consolidates the financial records of a multi-entity,
multi-currency group into accurate group-level statements. Two principles
shape the architecture:

1. **Deterministic accounting.** All financial calculations are performed by
   plain Python logic using `Decimal` for precision. LLMs never do arithmetic
   — AI agents interpret, suggest, investigate and explain, and always cite
   what they based a conclusion on.
2. **Traceability.** Every reported figure is traceable back to source
   records, account mappings, FX conversions, adjustments and elimination
   entries. Storage is designed around that audit trail.

## 2. Frontend / backend separation

| | Frontend | Backend |
|---|---|---|
| Framework | Next.js (App Router) + TypeScript | Python 3.11+ + FastAPI |
| Port | 3000 | 8000 |
| Runs | Dashboard UI, routing, presentation | APIs, accounting, AI workflows, file processing |
| Talks to backend | HTTP (`NEXT_PUBLIC_API_URL`) | — |

- The frontend never contains accounting logic; it renders what the API
  returns.
- The backend is framework-modular: `app/api` (HTTP layer),
  `app/core` (configuration), `app/schemas` (Pydantic models),
  `app/services`, `app/accounting`, `app/agents`, `app/mcp`,
  `app/storage`.
- CORS on the backend allows the frontend origin.

API endpoints:

- `GET /` — application identification
- `GET /api/health` — health check
- `POST /api/consolidations/runs` — run the FY2024 group consolidation
- `GET /api/consolidations/runs` — persisted run history (compact summaries)
- `GET /api/consolidations/runs/{run_id}` — one persisted run (full result)
- `GET /api/consolidations/runs/{run_id}/trace` — execution trace + tool calls
- `GET /api/consolidations/output/{filename}` — download one `/output` deliverable

## 3. Docker service architecture

`docker-compose.yml` defines three independent services on a dedicated
`ledgerflow` bridge network:

```text
        browser
           │
           ▼
┌─────────────────────┐    HTTP     ┌─────────────────────┐
│ frontend            │ ──────────▶ │ backend             │
│ Next.js · :3000     │             │ FastAPI · :8000     │
│ healthcheck: fetch  │             │ healthcheck: urllib │
└─────────────────────┘             └──────────┬──────────┘
                                               │ bind mounts
                     ┌─────────────────────────┼──────────────────────┐
                     ▼                         ▼                      ▼
              data/  (read-only)        storage/ (rw)           output/ (rw)

┌─────────────────────┐
│ db                  │
│ postgres:16-alpine  │   ← NOT connected to frontend or backend
│ :5432 · pg_isready  │      no depends_on, no connection string
│ volume: postgres_data│
└─────────────────────┘
```

Configuration details:

- Ports come from `.env` (`FRONTEND_PORT`, `BACKEND_PORT`) with defaults.
- `data/` is mounted **read-only** into the backend; `storage/` and `output/`
  are mounted read-write.
- All three services have health checks (Next `fetch`, Python `urllib`,
  `pg_isready`).
- The frontend receives `NEXT_PUBLIC_API_URL` as a **build argument** (Next.js
  inlines public env vars at build time).

### PostgreSQL is present but intentionally unused

PostgreSQL ships with the stack because later phases may want durable
relational storage. During Phase 1:

- The backend has **no database connection string** anywhere in code or
  configuration.
- There is **no `depends_on: db`** for any service — the backend does not
  wait for database health.
- There are **no database models, migrations, tables or seed data**.
- Stopping the database (`docker compose stop db`) does not affect the
  frontend or backend.

## 4. Filesystem-based persistence

Phase 1 persists everything on the filesystem:

```text
data/
  northwind/       # Original source files — immutable, mounted read-only

storage/
  runs/            # Future consolidation run metadata
  issues/          # Future issue and review state
  agent_logs/      # Future agent execution logs

output/            # Generated financial reports
```

- The backend centralises path resolution in
  `backend/app/storage/filesystem.py` (`FileStoragePaths`), which reads
  `DATA_DIR`, `STORAGE_DIR` and `OUTPUT_DIR` from the environment and creates
  the writable layout on startup.
- The accounting engine and API layers will depend on this storage boundary,
  **not** on the underlying medium. When PostgreSQL is introduced later, it
  slots in behind the same interface without rewriting accounting logic.
- `data/` is treated as immutable input: the application never writes to it.

## 5. The deterministic accounting engine

The engine lives in `backend/app/accounting/` and runs the full FY2024 group
consolidation. Every stage is plain Python `Decimal` arithmetic; no LLM is on
the calculation path.

| Module | Responsibility |
|---|---|
| `ingestion.py` | Normalise the US/UK/IN trial-balance schemas into typed rows |
| `validation.py` | Debit/credit balance and row/schema checks per entity |
| `mapping.py` | Map local accounts to the group chart (with the UK 2310 anomaly correction) |
| `adjustments.py` | Validate and classify year-end journals (post / reject / exclude / hold) |
| `fx.py`, `translation.py` | Closing / average / H2-average / historical-equity translation to GBP |
| `intercompany.py` | Reconcile reciprocal balances and P&L (in-transit items, FX residual) |
| `eliminations.py` | Balanced elimination journals for intercompany balances, investments and P&L |
| `acquisition.py` | Goodwill, NCI at acquisition, NCI share of post-acquisition profit |
| `statements.py` | Translation reserve, consolidated TB, balance sheet, P&L, changes in equity, control figures |
| `outputs.py` | Write the seven deliverables under `/output` (no arithmetic) |

Integrity checks gate the outputs: `balance_sheet_balances`,
`profit_split_reconciles`, `consolidated_tb_balances` and
`elimination_journals_balance` must all pass. An independent, self-contained
`Decimal` recomputation (`scripts/verify_independent.py`, sharing no code with
the engine) reproduces every control figure exactly.

A genuinely missing rate (there is no GBP/SGD closing rate) is reported as
`sgd_bank_gbp = null` with a reason — never invented or cross-derived.

## 6. The AI layer (advisory only)

`backend/app/agents/` holds one shared `StateGraph` (`graph.py`) that powers
both the Consolidation Agent and the AI Copilot. The execution mode branches
the nodes; consolidation intent is derived deterministically from the
structured payload and never needs an LLM.

- `skills/consolidation.py` runs the deterministic pipeline and attaches the
  statements, trial balance, eliminations and integrity checks to the result.
- `accounting/ai_workflows.py` + `prompts.py` run four LLM-assisted stages —
  execution planning, account-mapping investigation (the real UK 2310 anomaly),
  anomaly investigation and intercompany candidate matching. Each output is
  returned as a proposal that Python independently verifies; nothing is
  auto-applied, and prompt-injection text in memos is treated as data.
- Without credentials every AI stage reports `not_configured`; the
  deterministic consolidation is unaffected. Traces are redacted before they
  touch disk (`agents/tracing.py`).

## 7. Repository layout

```text
frontend/   Next.js application (src/app routes, components, lib, hooks, types)
backend/    FastAPI application (app/* packages, tests, pyproject.toml)
data/       Immutable source inputs (northwind bundle)
storage/    Runtime state: runs, issues, agent_logs
output/     Generated reports
docs/       Architecture documentation
```

## 7. Design decisions

See [DECISIONS.md](../DECISIONS.md) for the decision log covering this phase.
