# LedgerFlow

**LedgerFlow** is an AI-powered financial consolidation and reporting platform
for companies that operate through multiple legal entities across different
countries.

> **Status: FY2024 consolidation works end-to-end.**
> One command ingests the three entities, validates and maps their trial
> balances, reconciles intercompany balances, translates to GBP, applies
> acquisition accounting, performs eliminations and writes the consolidated
> statements and all seven deliverables to `/output`. The AI agents assist
> (plan, investigate, propose); every financial figure is computed by the
> deterministic Python engine. Database integration and authentication remain
> out of scope.

---

## The problem it solves

Consider a group with a parent company in the United States (USD), a
subsidiary in the United Kingdom (GBP) and another subsidiary in India (INR).
Each entity keeps its own financial records, chart of accounts and reporting
structure. Combining them into accurate group-level financial statements is
hard because of:

- Different currencies and exchange rates
- Different charts of accounts
- Intercompany transactions that must be eliminated
- Ownership percentages and non-controlling interests (NCI)
- Acquisition dates and business combination accounting
- Accounting adjustments and reporting policies
- The need to trace every reported figure back to its source

LedgerFlow automates this process while maintaining complete financial
traceability.

## Core capabilities

1. **Source data management** — read and organize financial input files from
   different entities.
2. **Financial consolidation** — convert entity-level records into
   consolidated group accounts.
3. **AI-assisted analysis** — agents interpret accounting policies, suggest
   account mappings, investigate anomalies and explain issues.
4. **Deterministic accounting engine** — all financial calculations are
   performed by Python logic, never by LLM-generated arithmetic.
5. **Issue management** — flag missing FX rates, unbalanced journals,
   duplicate adjustments and other inconsistencies.
6. **Financial reporting** — consolidated trial balance, balance sheet,
   profit and loss, and changes in equity.
7. **Auditability** — trace every reported figure to source records,
   adjustments, FX conversions and elimination entries.
8. **Agent activity** — show what the AI workflow did, which tools it invoked
   and what it recommended.

## Run the FY2024 consolidation

One command produces every deliverable in `/output`:

```bash
# from the repository root, with the backend venv installed
backend/.venv/bin/python scripts/run_consolidation.py
```

This writes the seven assignment files (`consolidated_tb.csv`,
`balance_sheet.json`, `profit_and_loss.json`, `changes_in_equity.json`,
`eliminations.csv`, `issues.json`, `control_figures_ns.json`) and prints the
twelve control figures. Fully deterministic — no network access and no LLM
call is required.

Independent verification of every control figure (a second, self-contained
`Decimal` recomputation that shares no code with the engine):

```bash
backend/.venv/bin/python scripts/verify_independent.py
```

> **Current gap:** after commit `28f3b43` rebuilt India's P&L from the monthly
> rows, `verify_independent.py` still recomputes India from the full-year trial
> balance, so it no longer agrees with the engine. The balance-sheet identity it
> checks still holds. See `VALIDATION.md` §3.

## Technology stack

| Layer     | Technology |
|-----------|------------|
| Frontend  | Next.js (App Router), TypeScript, Tailwind CSS, shadcn/ui, Lucide React |
| Backend   | Python 3.11+, FastAPI, Pydantic, Uvicorn |
| Accounting engine | Python `Decimal` (no floats) — deterministic, no LLM on the calculation path |
| AI agents | LangGraph (`StateGraph`) + LangChain Core; one shared agent with two modes (Consolidation Agent, AI Copilot) |
| MCP       | Model Context Protocol — `mcp` SDK server + client behind a permissioned tool gateway |
| LLM providers | LangChain Google Generative AI (Gemini) and Groq, selectable via `LLM_PROVIDER` |
| Database  | PostgreSQL (official Docker image) — infrastructure only, currently unused |
| Storage   | Filesystem (`data/`, `storage/`, `output/`) |
| Infra     | Docker, Docker Compose |

## High-level architecture

```
Browser ──▶ frontend (Next.js :3000) ──HTTP──▶ backend (FastAPI :8000)
                                                     │
                       ┌─────────────────────────────┼──────────────────┐
                       ▼                             ▼                  ▼
                data/ (read-only)             storage/ (rw)       output/ (rw)
                immutable inputs              runs · issues       generated
                                               agent logs          reports

db (PostgreSQL :5432) ── intentionally NOT connected to the application
```

- The **frontend** container runs the Next.js app and talks to the backend
  over HTTP.
- The **backend** container runs FastAPI and uses **filesystem-based
  storage**.
- The **db** container runs official PostgreSQL with a persistent volume,
  present for future use. The backend has **no database connection string and
  no dependency on the database** — the application works with the database
  stopped.

See [docs/architecture.md](docs/architecture.md) for details.

## Repository structure

```text
ledgerflow/
├── frontend/           Next.js app (src/app, components, lib, hooks, types)
├── backend/            FastAPI app (app/api, core, schemas, services,
│                       accounting, agents, mcp, storage) + tests
├── data/
│   └── northwind/      Immutable original source inputs
├── storage/            Future run/issue/agent-log state (filesystem)
├── output/             Generated financial reports
├── docs/               architecture.md
├── transcripts/        Working transcripts
├── DECISIONS.md        Key engineering decisions
├── VALIDATION.md       Validation results and independent recomputation
├── REFLECTION.md       Author reflection
├── .env.example        Environment template (no secrets)
├── .gitignore
├── .dockerignore
└── docker-compose.yml  frontend + backend + db
```

## Local development (without Docker)

### Backend

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

- Frontend: http://localhost:3000 (redirects to `/dashboard`)
- Backend: http://localhost:8000

### Tests and lint

```bash
cd backend && pytest            # backend tests
cd frontend && npm run lint     # eslint
cd frontend && npm run build    # production build
```

## Docker setup

```bash
cp .env.example .env    # optional; defaults work out of the box
docker compose up --build
```

| Service   | Port  | Notes |
|-----------|-------|-------|
| frontend  | 3000  | Next.js dashboard |
| backend   | 8000  | FastAPI API |
| db        | 5432  | PostgreSQL (unused by the app) |

Stop only the database — the application keeps working:

```bash
docker compose stop db
```

Other useful commands:

```bash
docker compose ps            # service status + health
docker compose logs -f       # follow logs
docker compose down          # stop and remove containers (volume persists)
docker compose down -v       # also remove the PostgreSQL volume
```

## API health endpoint

```bash
curl http://localhost:8000/api/health
```

```json
{
  "status": "ok",
  "service": "ledgerflow-backend"
}
```

`GET /` returns basic application identification, and interactive API docs are
available at http://localhost:8000/docs.

## Environment variables

Copy `.env.example` to `.env` and adjust values. Real secrets (such as
`GEMINI_API_KEY`) must never be committed — `.env` is git-ignored.

```env
APP_ENV=development
BACKEND_PORT=8000
FRONTEND_PORT=3000

POSTGRES_DB=ledgerflow
POSTGRES_USER=ledgerflow
POSTGRES_PASSWORD=change_me

GEMINI_API_KEY=
LLM_MODEL=
```

## Implemented scope

Implemented: monorepo scaffold, dashboard shell, FastAPI endpoints, Docker
Compose infrastructure, filesystem storage layout, and the **complete
deterministic FY2024 consolidation engine** — ingestion, validation, mapping,
journal processing, intercompany reconciliation, FX translation, acquisition
accounting (goodwill and NCI), eliminations, translation reserve, statement
assembly and output generation — plus the shared LangGraph agent and
AI-assisted (advisory-only) reasoning stages.

**Out of scope:** PostgreSQL integration (present but unused), authentication,
and multi-period operation. The AI agents never compute a figure; they plan,
investigate and propose, and the deterministic engine verifies.
