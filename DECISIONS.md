# Decisions

Engineering decisions made while building LedgerFlow, from the foundation
(project setup and Docker infrastructure) through the completed FY2024
consolidation engine.

---

## D1. Monorepo with three top-level service directories

**Decision:** Single repository containing `frontend/`, `backend/` and
infrastructure files at the root.

**Why:** Phase 1 ships one Docker Compose stack; a monorepo keeps the
compose file, docs and shared `.env` next to both services. Clear
subdirectory boundaries keep each service independently runnable.

## D2. Source data copied into `data/northwind/` byte-for-byte

**Decision:** The original Northwind assignment bundle (README, PDF and all
15 data files) was copied into `data/northwind/` with original names,
timestamps and content. SHA-256 checksums of the copies match the originals.
The original bundle in `~/Downloads` was left untouched.

**Why:** The product must treat source inputs as immutable evidence for
auditability. The backend mounts `data/` **read-only** in Docker to enforce
this at the infrastructure level.

## D3. Filesystem persistence with a single storage boundary

**Decision:** Phase 1 persists to `storage/` and `output/` via
`backend/app/storage/filesystem.py` (`FileStoragePaths`), configured through
`DATA_DIR` / `STORAGE_DIR` / `OUTPUT_DIR` environment variables.

**Why:** Requirements forbid database integration now, but runs, issues and
agent logs need somewhere to live in later phases. Centralising path
resolution means PostgreSQL can later replace/augment this layer without
touching the accounting engine.

## D4. PostgreSQL included but disconnected

**Decision:** `db` (postgres:16-alpine) runs with a named volume, published
port and health check — but the backend has **no connection string, no driver
dependency and no `depends_on`** on it.

**Why:** The brief requires the database as future infrastructure while the
application must work with the database stopped. Keeping it fully decoupled
now avoids accidental coupling (`docker compose stop db` must change nothing).

## D5. Backend health check uses Python stdlib, not curl

**Decision:** The backend container health check runs
`python -c "urllib.request.urlopen(...)"`.

**Why:** `python:3.11-slim` ships no HTTP client binary; adding `curl` would
bloat the image. The frontend (Node) and db (`pg_isready`) use tools already
present in their images.

## D6. Frontend built as a Next.js `standalone` multi-stage image

**Decision:** `output: "standalone"` with a three-stage Dockerfile
(deps → build → minimal runner on `node:22-alpine`, non-root user).

**Why:** Produces a small production image without `node_modules`. `public/`
and `.next/static/` are copied into the standalone output as required by
Next.js. `NEXT_PUBLIC_API_URL` is passed as a **build argument** because
`NEXT_PUBLIC_*` values are inlined at build time.

## D7. shadcn/ui "base-nova" style on Base UI primitives

**Decision:** Initialized shadcn with its default preset (`base-nova`,
Base UI components, lucide icon library, CSS variables).

**Why:** Current shadcn default for new Next.js projects; avoids legacy
Radix-only patterns. One consequence: composition uses the `render` prop
instead of Radix's `asChild` (recorded here because it surfaced as a type
error during the first build).

## D8. Root path redirects to `/dashboard`

**Decision:** `/` performs a server-side redirect to `/dashboard`.

**Why:** Authentication and marketing pages are out of scope, and inventing
fake landing content would misrepresent the product. The dashboard is the
product surface.

## D9. Placeholder pages state plainly what is not built

**Decision:** Each route renders its real navigation shell plus a panel that
describes the planned module and badges it "Not implemented yet". No
invented financial metrics anywhere.

**Why:** The brief forbids fake functionality; honest placeholders communicate
the roadmap without misleading reviewers.

## D10. Header shows live backend API status

**Decision:** The dashboard header polls `GET /api/health` client-side
(`src/hooks/use-api-health.ts`) and shows API online/offline.

**Why:** It exercises the frontend→backend HTTP path that the architecture
depends on, with zero build-time network calls (client component only). It
fails gracefully to "API offline" when the backend is unreachable.

## D11. Tailwind font token fix during shadcn init

**Decision:** shadcn's generated `globals.css` contained a self-referential
`--font-sans: var(--font-sans)`. Replaced with
`var(--font-geist-sans), ui-sans-serif, …` stacks so the Geist font variables
set by the root layout resolve.

**Why:** Without a definition, `font-family` would resolve to an undefined
variable and silently fall back to the browser default font.

## D12. No `__init__.py` files — namespace packages

**Decision:** At the maintainer's request, all ten backend `__init__.py`
package markers were removed. `[tool.setuptools.packages.find]` sets
`namespaces = true` so `pip install .` still discovers and installs the
`app` tree (verified by rebuilding the backend image and hitting
`/api/health`). Reserved-but-empty directories (`app/services`,
`app/accounting`, `app/agents`, `app/mcp`) are plain empty directories,
recreated on demand when work on them starts — no `.gitkeep` padding (the
maintainer removed those as unnecessary, since git simply doesn't track the
empty directories and nothing in the build or runtime needs them).
A new `backend/conftest.py` points `DATA_DIR`/`STORAGE_DIR`/
`OUTPUT_DIR` at a temp directory before the app imports, so test runs no
longer create stray `storage/`/`output/` directories in the backend working
directory. `backend/.dockerignore` was tightened to `**/__pycache__`-style
patterns so host bytecode never enters the image build context.

**Why:** The maintainer preferred a leaner tree. Namespace packages (PEP 420)
are fully supported on Python 3.11+ and keep behavior identical without the
marker files.

## D13. One shared Core LangGraph Agent for both entry points

**Decision:** Phase 2 builds a single `StateGraph`
(`backend/app/agents/graph.py`) that powers both the Consolidation Agent
and the AI Copilot. The execution `mode` is part of `AgentState` and
branches the nodes: consolidation derives intent **deterministically** from
the structured payload (period/scope/entities) and never needs an LLM;
copilot identifies intent **via Gemini**, with a keyword heuristic fallback
when the model returns unparseable JSON. The flow is
`load_context → identify_intent → select_skill → execute_skill → finalize`,
and a failure at any node sets a typed error in state and routes straight to
`finalize`, so every execution still produces a completed trace.

**Why:** The approved plan (Milestone 1) specified one shared agent with two
modes rather than separate per-skill agents. Deterministic consolidation
means runs are reproducible and testable without network access, while the
copilot keeps LLM-driven intent.

## D14. LLM does orchestration only; typed failures when unavailable

**Decision:** `app/agents/llm.py` exposes `create_chat_model()`, which raises
`LLM_NOT_CONFIGURED` when `GEMINI_API_KEY` is absent and otherwise builds a
`ChatGoogleGenerativeAI` (model from `LLM_MODEL`, default
`gemini-3.8-flash`). The key is stored with `repr=False` in `Settings` and as
a `SecretStr` in the model, so it can never appear in logs or `repr()`. A
separate `LLM_INVOCATION_FAILED` error distinguishes "configured but the call
broke" (network/provider) from "not configured". All financial arithmetic
stays in Python services — the model only picks skills and words answers.

**Why:** Per the maintainer's instruction, full Gemini support is built now
and the key will be added to the environment later ("assume we have a key in
env and build it"). Typed error codes let the API return a precise
`llm_not_configured` instead of a stack trace.

## D15. Filesystem persistence primitives for threads, traces and runs

**Decision:** `app/storage/filesystem.py` gained atomic JSON writes
(`temp file + os.replace + fsync`), JSONL append/iterate, and directories
`storage/agent_logs/threads/` and `storage/agent_logs/traces/`. Conversation
threads persist one JSON file per thread (`app/agents/thread_service.py`),
messages serialised to plain dicts so they survive a restart. Execution
traces append JSONL events (`app/agents/tracing.py`) with dense `seq`
numbers that resume across per-node recorder instances. The thread id is the
only isolation key — two conversations can never bleed into one another.

**Why:** The phase requirement is filesystem persistence with no
Postgres/Redis/Celery. Atomic writes mean a crash never leaves a
half-written file, and a backend restart loses nothing already written.

## D16. Skill contract carries its own safety policy; registry enforces modes

**Decision:** Each skill declares a `SkillSpec`: name, purpose,
instructions, execution procedure, **permitted tools**, validation
requirements, output expectations, and an `allowed_modes` allowlist, plus a
`ToolPolicy` (read-only / named tools / no tools). The `SkillRegistry` is
the single source of truth shared by both agents: registration validates
the spec is complete, duplicates are rejected, and `resolve()` refuses a
skill whose `allowed_modes` excludes the current mode
(`SKILL_NOT_ALLOWED_FOR_MODE`). An explicit skill hint (from a request or
the LLM) is validated rather than trusted — an unknown hint fails loudly
with `SKILL_NOT_FOUND`. Tests register stub skills; Milestone 4 swaps in the
real Consolidation and Financial Analysis skills.

**Why:** The plan requires skills to expose instructions, purpose,
procedure, permitted tools, validations and output expectations, and the
safety requirement is that the copilot can never reach consolidation-only
capabilities (or call undeclared tools).

## D17. Traces are redacted before they touch disk

**Decision:** Every trace payload passes through `redact()`, which masks
values under sensitive keys (`api_key`, `token`, `authorization`, …), scrubs
Google-style `AIza…` keys and `sk-…` secrets by pattern, and replaces any
explicitly supplied secret string. The current Gemini key is passed to every
`event()` call as an extra secret. `Settings.__repr__` excludes the key, and
a test asserts the model `repr()` never contains it.

**Why:** Traces are persisted evidence that the API will expose; a leaked
key in a JSONL file would be a security defect.

## D18. Stub skills in tests; real skills arrive in Milestone 4

**Decision:** Milestone 2 ships the skill **infrastructure** (base class,
spec, registry) plus test-only stub skills
(`tests/conftest.py`). The real Consolidation and Financial Analysis skills,
the MCP gateway and the accounting validation services are Milestone 3–4
work. Per the approved boundary, calculation services (FX translation,
eliminations, NCI, statements) remain **absent**: a consolidation run ends
`blocked` naming the missing dependency, exactly as the stub demonstrates.

**Why:** Keeps milestones small and reviewable, and honours the decision not
to fabricate missing accounting services.

---

# Decisions — FY2024 consolidation engine

Engineering decisions made while completing the deterministic accounting
engine and its integration with the agent and dashboard.

## D19. `Decimal` everywhere; rounding only at the reporting boundary

**Decision:** Every accounting service carries amounts as `Decimal` at full
source precision. Values are quantised to 2 dp only when a statement line or a
control figure is rendered.

**Why:** Binary floats accumulate representation error across translation,
elimination and aggregation; `Decimal` makes the trial balance balance exactly
and the independent recomputation reproducible. Rounding early would hide
genuine residuals (e.g. the intercompany difference) behind presentational
drift.

## D20. Missing FX rate stays `null` — never cross-derived

**Decision:** There is no GBP/SGD closing rate in the source bundle. The
control figure `sgd_bank_gbp` is reported as `null` with a reason string; the
engine does **not** synthesise it from the USD/SGD or USD/GBP rates.

**Why:** The assignment forbids inventing rates. A silent cross would produce a
plausible-looking but unsupported number; an explicit `null` + reason is the
honest answer and is surfaced as an issue and on the dashboard.

## D21. Intercompany difference is reported, not forced to zero

**Decision:** The reconciler matches reciprocal pairs by reference and amount
and reports the unmatched residual (20,066.42 GBP, decomposed into named
causes: rounding, in-transit and unmatched items) rather than posting a
plug.

**Why:** Forcing the difference to zero would fabricate a reconciliation the
data does not support. The residual and its causes are a required control
figure.

## D22. Duplicate journals are held, not silently tie-broken

**Decision:** Two year-end journals with the same effect (JE-014 / JE-019) are
both held for review — the engine does not pick one arbitrarily. JE-006 is
rejected (unbalanced, and its memo tries to waive the balance check); JE-017 is
excluded (dated 2025); and JE-016 is rejected **in full** because one of its
lines posts to an account outside the group chart of accounts. Posted 15,
rejected 2, held 2.

**Why:** Choosing between duplicates without evidence would be a fabricated
accounting decision. Holding both and surfacing the duplicate keeps the choice
with a human while the rest of the consolidation proceeds. Rejecting a
partly-mapped journal in full (rather than posting its mapped line) avoids
emitting a one-sided entry that would unbalance the entity's books.

## D23. Consolidated TB uses the open-ledger basis

**Decision:** The consolidated trial balance carries equity as closing share
capital + **opening** retained earnings + closing translation reserve + NCI at
acquisition, and leaves the year's P&L accounts open. The **statements** use
closing retained earnings and closing NCI as normal.

**Why:** On an open-ledger basis the consolidated TB balances exactly
(debits = credits) with the year's result still in the P&L accounts. Closing
the P&L into retained earnings inside the TB would double-count once the
statements are built from the same accounts. The choice is documented so the
two presentations are not mistaken for a discrepancy.

## D24. Eliminations are balanced double-entry journals

**Decision:** Every elimination is emitted as a balanced journal and the
`elimination_journals_balance` check must pass.

**Why:** An elimination that does not balance cannot be posted; emitting
balanced journals (rather than single-sided adjustments) keeps the TB in
balance and makes each elimination auditable line by line.

## D27. The translation reserve is a derived exchange difference, never a plug

**Decision:** The translation reserve movement (3300) is computed from the
translation arithmetic itself — currently the India acquisition-to-closing
difference (net assets at acquisition at the acquisition-date/spot rate plus
H2 profit at the H2 average, versus closing net assets at the closing rate),
giving −£108,138.43. The investment-elimination residual is routed to retained
earnings, and any part of the group's opening position that cannot be derived
from the supplied data (there is no US opening position) is **disclosed** as an
issue (`opening_position_unreconciled`), not absorbed in the reserve.

**Why:** A reserve that moves to make the balance sheet balance is not an
exchange difference — it hides whatever is actually wrong. Deriving the reserve
and disclosing the unreconcilable opening position separately keeps 3300
faithful to policy §2 and makes the residual visible to a reviewer.

## D28. In-transit intercompany items are reciprocal, not exchange differences

**Decision:** An intercompany item recorded by one entity but not yet by the
other (INV-US-2291, £8,108.54) is recognised by the receiving entity at the
closing rate so it appears on both sides and eliminates to zero. Only the
document-vs-booked-rate difference on the matched invoices (£11,957.88) is
posted to the FX account (7300).

**Why:** An in-transit item is a timing difference, not a currency difference.
Routing it to FX would overstate the group's exchange loss. Recognising both
sides is what consolidation policy §5 requires.

## D29. Reported statement lines foot to their totals

**Decision:** Because each line is rounded to 2 dp independently (policy §7),
every summary section (assets, liabilities, equity, and the P&L components) is
footed to its reported total, absorbing any one-penny presentation residual in
the largest line. The total itself is never changed.

**Why:** A reader who adds up the reported lines must reach the reported total.
The adjustment is purely presentational (the unrounded figures are unchanged
and still reconcile); leaving a visible 1p gap would look like an error.

## D25. Independent recomputation as the correctness gate

**Decision:** `scripts/verify_independent.py` recomputes all twelve control
figures from the raw source files with its own `Decimal` arithmetic, sharing no
code with the engine, and asserts equality.

**Why:** A test that reuses the engine's functions cannot catch a wrong formula.
A second, independent implementation that agrees to the penny is real evidence
the figures are correct.

## D26. Prompt-injection text in memos is data, never instructions

**Decision:** Memo/journal text that contains instruction-like content (e.g.
"waive the balance check") is quoted into AI prompts as untrusted data and
recorded as an issue; the LLM is never told to act on it, and the
deterministic engine never does.

**Why:** Source documents are attacker-controllable evidence. Treating their
contents as instructions would let a memo change a financial outcome.
