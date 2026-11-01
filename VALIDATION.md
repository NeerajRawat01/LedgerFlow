# Validation

Two validation cycles are recorded here:

1. **FY2024 consolidation engine** — the deterministic accounting engine,
   independent recomputation, API and frontend (below).
2. **Phase 1 foundation** — project setup and Docker infrastructure
   (retained further down for history).

Environment: macOS, Node 22, Python 3.11, Next.js 16.4.3.

---

# Part 1 — FY2024 consolidation engine

Validated 2026-11-01.

## 1. One-command run produces all seven deliverables

✅ `backend/.venv/bin/python scripts/run_consolidation.py` writes:

```
consolidated_tb.csv (3360 bytes)   balance_sheet.json (3434 bytes)
profit_and_loss.json (2496 bytes)  changes_in_equity.json (2456 bytes)
eliminations.csv (3391 bytes)      issues.json (9947 bytes)
control_figures_ns.json (728 bytes)
```

and prints the twelve control figures. Deterministic — no network, no LLM.

## 2. Twelve control figures

| Figure | Value (GBP) |
|---|---|
| Group revenue | 30,203,422.23 |
| Group profit | 3,069,352.53 |
| Profit — owners | 2,991,424.52 |
| Profit — NCI | 77,928.01 |
| NCI at 31 Dec | 1,341,888.19 |
| Goodwill at 31 Dec | 1,375,815.07 |
| Translation reserve movement | −108,138.43 |
| Total assets | 28,575,272.95 |
| Total equity | 18,446,143.15 |
| Intercompany difference | 20,066.42 |
| UK SGD bank (GBP) | `null` + reason (no GBP/SGD closing rate) |
| Journals | 15 posted · 2 rejected · 2 held for review |

## 3. Independent recomputation (the correctness gate)

✅ `scripts/verify_independent.py` recomputes every figure from the raw source
files with its **own** `Decimal` arithmetic and shares **no code** with the
engine. It reproduces every control figure exactly:

```
group_revenue 30203422.23        group_profit 3069352.53
profit_owners 2991424.52         profit_nci 77928.01
nci_closing 1341888.19           goodwill_closing 1375815.07
translation_reserve_movement -108138.43
total_assets 28575272.95         total_equity 18446143.15
check A = L + E: 0.00
```

The script also re-derives, independently: the journal decisions
(15 posted / 2 rejected / 2 held), the intercompany difference and its
causes, goodwill (INR 147,600,000 → GBP 1,375,815.07), NCI at acquisition
(INR 135,600,000 → GBP 1,263,960.19), the India H2 profit split, and the
balance-sheet identity A = L + E (difference 0.00).

## 4. Integrity checks (all pass)

- `balance_sheet_balances` — assets 28,575,272.95 = liabilities 10,129,129.80
  + equity 18,446,143.15.
- `profit_split_reconciles` — owners 2,991,424.52 + NCI 77,928.01 = group
  profit 3,069,352.53.
- `consolidated_tb_balances` — total debits 59,898,044.40 = total credits
  59,898,044.40 (difference 0.00).
- `elimination_journals_balance` — all 4 elimination journals balance.

## 5. Backend tests

✅ `pytest -q` on Python 3.11 — **303 passed**. Includes regression tests for
JE-016 being rejected in full (unmapped line), the in-transit intercompany
item being reciprocal (not routed to FX), the translation reserve being the
genuine FX movement (never a plug), statement footing, the consolidated TB
balancing, balanced elimination journals, and TB source traceability.

## 6. API

✅ `POST /api/consolidations/runs` → HTTP 200, `status: completed`, 12 stages
completed, 31 consolidated-TB rows, 13 elimination lines, 4 integrity checks
all pass, 7 output files listed. `GET /api/consolidations/runs` returns the
persisted history; `GET .../runs/{id}` returns the full record;
`GET .../output/{file}` returns the deliverables (verified `text/csv` for
`consolidated_tb.csv`, 200 for the JSON files).

## 7. Frontend

✅ `npm run lint` — 0 errors, 0 warnings.
✅ `npm run build` — compiles, type-checks, prerenders all 12 routes.
✅ Live smoke test against the API: `/dashboard/reports`, `/dashboard/runs`
and `/dashboard/issues` each return HTTP 200 and render the engine's figures
(statements, control figures, trial balance, eliminations, integrity checks
and the seven downloadable deliverables).

## 8. Source data unchanged

✅ The 17 files under `data/northwind/` are untouched: the engine only reads
them (the backend mounts `data/` read-only). Checksums match the originals.

---

## AI mistakes found and corrected

The AI-assisted stages are advisory and are verified deterministically, but
several **implementation** mistakes surfaced and were fixed — recorded here as
required evidence:

1. **Unbalanced elimination journals (sign errors).** `ELIM-PL-001` and
   `ELIM-INV-001` were emitted with mismatched debit/credit signs, so the
   elimination journals did not balance. Fixed so each is a balanced
   double-entry journal; the investment journal now also carries the goodwill
   and translation-reserve residual. The new `elimination_journals_balance`
   check guards against regression.
2. **Consolidated trial balance out of balance by the group profit.** The TB
   closed the P&L into retained earnings while the P&L accounts were still
   present, double-counting the year's result and leaving the TB short by
   exactly the group profit. Fixed by using the open-ledger basis (P&L
   accounts stay open). The new `consolidated_tb_balances` check guards it.
3. **Elimination sign bug in the group-account net presentation.** The
   aggregate elimination netted the wrong side for intercompany payables;
   corrected against the independently recomputed intercompany residual.
4. **Stale skill/spec text asserted capabilities did not exist.** The
   `ConsolidationSkill` docstring and `SkillSpec` still claimed translation,
   elimination and statement assembly were "not implemented" and that
   `complete` could never be true. Corrected to match the shipped engine.
   (A wrong self-description is itself a defect — reviewers would be misled.)
5. **Translation reserve was a balancing plug, not an exchange difference.**
   The investment-elimination journal (`ELIM-INV-001`) credited £1,288,166.08
   to the translation reserve (3300) with no FX basis, leaving the reported
   reserve movement (−£147,343.09) as a plug that absorbed every other
   imbalance. Corrected so 3300 carries only the genuine, derivable exchange
   difference (the India acquisition-to-closing movement, −£108,138.43); the
   investment-elimination residual is routed to retained earnings, and the
   part of the opening position that cannot be derived from source (no US
   opening position is supplied) is **disclosed** as an issue
   (`opening_position_unreconciled`) instead of being hidden in the reserve.
6. **Partially-posted journal with an unmapped line.** `JE-016` (India) had
   one line on an unmapped account (`IN-GST-ITC`); the engine posted the
   mapped line and skipped the unmapped one, leaving India unbalanced by
   INR 540,000. Corrected: a journal whose lines are not *all* mapped to the
   group chart of accounts is rejected **in full** (policy §6) — no
   one-sided entries.
7. **In-transit intercompany item treated as an exchange difference.** The
   £20,066.42 intercompany balance difference was posted entirely to the FX
   account (7300), overstating the group FX loss. Only £11,957.88 (the
   document-vs-booked-rate difference on the four matched invoices) is a
   genuine exchange difference; the £8,108.54 in-transit item is now
   recognised on both sides at the closing rate (`ELIM-TRANSIT-*`) and
   eliminates to zero (policy §5).
8. **Reported statements did not foot.** Each line is rounded to 2 dp
   independently (policy §7), so in three places the rounded lines summed to
   a penny different from the reported total. Fixed by footing every summary
   section (assets, liabilities, equity, P&L components) to its total,
   absorbing the presentation residual in the largest line — never changing
   the total. Regression tests assert every section foots.

---

# Part 2 — Phase 1 foundation (history)

---

## 1. `docker compose config`

✅ `docker compose config --quiet` exits 0. Services resolved:
`frontend`, `backend`, `db`.

## 2. Build all images

✅ `docker compose build` — `frontend` built (multi-stage: deps → build →
standalone runner), `backend` built (python:3.11-slim + pip install),
`db` uses the official `postgres:16-alpine` image (pull only, no build).

## 3. Start all services

✅ `docker compose up -d` — all three containers start and reach
**healthy**:

| Service | Status | Port |
|---|---|---|
| frontend | Up (healthy) | 0.0.0.0:3000 → 3000 |
| backend | Up (healthy) | 0.0.0.0:8000 → 8000 |
| db | Up (healthy) | 0.0.0.0:5432 → 5432 |

## 4. Frontend loads

✅ From the host:

- `GET /` → **307** redirect to `/dashboard`
- `GET /dashboard` → **200**; page contains LedgerFlow branding and nav
- `GET /dashboard/entities`, `/source-data`, `/runs`, `/issues`,
  `/reports`, `/agent-activity` → **200** each

## 5. Backend health endpoint

✅ `curl http://localhost:8000/api/health` →

```json
{"status": "ok", "service": "ledgerflow-backend"}
```

✅ `GET /` returns application identification
(`name`, `service`, `version`, `phase`, `description`).

## 6. PostgreSQL health

✅ Container reports healthy; `docker exec ledgerflow-db pg_isready -U
ledgerflow -d ledgerflow` → `/var/run/postgresql:5432 - accepting
connections`.

**Environment note:** host port 5432 was initially occupied by an unrelated
`websage-db` container from another project; that project was stopped by the
user before this validation. LedgerFlow's compose file keeps the standard
`5432:5432` mapping. No LedgerFlow code depends on this port.

## 7. Application works without PostgreSQL

✅ `docker compose stop db` → database container stops; frontend and backend
both remain **healthy** and responsive:

- `GET http://localhost:3000/dashboard` → 200
- `GET http://localhost:3000/dashboard/reports` → 200
- `GET http://localhost:8000/api/health` → `{"status": "ok", ...}`

✅ Decoupling audit:

- `grep` over `backend/app/**` and `backend/pyproject.toml` finds **no**
  database drivers, connection strings or `DATABASE_URL` (only docstrings
  mentioning PostgreSQL as future infrastructure).
- The `backend` service in `docker-compose.yml` has **no `depends_on`** and
  **no `POSTGRES_*` variables**.

The db service was restarted afterwards; final state: all three healthy.

## 8. Source data unchanged

✅ SHA-256 for all 17 files in `data/northwind/` matches:

1. the checksums recorded at import time (unchanged since copy), and
2. the pristine original bundle outside the repository.

Original filenames and timestamps preserved (`entities.csv` still
`Oct 21 11:55`, 345 bytes). The backend mounts `data/` **read-only**.

## 9. Frontend lint and production build

✅ `npm run lint` (ESLint 9, flat config) — 0 errors, 0 warnings.
✅ `npm run build` (Next.js 16 Turbopack) — compiles, type-checks,
prerenders all 9 routes as static output (`standalone`).
✅ Standalone smoke test: `node .next/standalone/server.js` serves
`/` (307) and dashboard routes (200).

Note: Next.js 16 removed `next lint`; `next build` no longer runs lint, so
lint and build are validated as separate steps.

## 10. Backend tests

✅ `pytest` on Python 3.11 — **5 passed** (health endpoint, root endpoint,
CORS for the frontend origin, storage layout creation, immutable
source-data path). One dependency deprecation warning
(`fastapi.testclient` suggests `httpx2`) — non-blocking.

---

## Issues found and fixed during validation

1. **`asChild` type error** (first frontend build) — shadcn's base-nova
   preset uses Base UI, whose composition prop is `render`. Switched the
   mobile menu trigger to `render={<Button … />}`.
2. **Unused `LucideIcon` import** in `src/lib/nav.ts` — removed (ESLint).
3. **Self-referential `--font-sans: var(--font-sans)`** written by shadcn
   init — repointed at the Geist font variables the root layout sets.
4. **Port 5432 conflict** with the unrelated `websage-db` container —
   resolved by the user stopping that project (environmental, not a repo
   defect).

## Post-review update (same day)

At the maintainer's request, all backend `__init__.py` files were removed:

- Re-ran validation after the change: `pip install -e ".[dev]"` (fresh),
  **pytest 5/5 passed**, backend image rebuilt, container **healthy**,
  `GET /api/health` → `{"status": "ok", ...}` — namespace-package discovery
  confirmed working in the real image.
- Added `backend/conftest.py` so tests write storage to a temp dir instead
  of the working directory (removed stray `backend/storage`,
  `backend/output` created by earlier test runs).
- Tightened `backend/.dockerignore` (`**/__pycache__`) — rebuilt image no
  longer contains host bytecode; `/app/app` verified clean in the container.

## Unresolved issues

None affecting Phase 1 requirements. The `httpx2` deprecation warning is
worth revisiting at the next dependency upgrade.
