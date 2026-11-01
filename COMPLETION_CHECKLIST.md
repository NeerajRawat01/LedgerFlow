# LedgerFlow — Assignment Completion Checklist

Bundle: **NWG-D0BC** · Reporting period: **FY2024** · Presentation currency: **GBP**

Tracks the AI Agentic Engineer take-home requirements against the actual
implementation. Status values: **Done** (implemented + validated),
**Partial** (implemented, needs evidence), **Pending** (not started),
**Blocked** (cannot be completed from the given inputs — recorded as an issue).

---

## A. Required outputs (assignment §4)

The prototype writes these to `/output` by running one command
(`backend/.venv/bin/python scripts/run_consolidation.py`).

| # | File | Purpose | Status | Implemented in |
|---|------|---------|--------|----------------|
| 1 | `consolidated_tb.csv` | Consolidated trial balance in GBP w/ source rows | Done | `app/accounting/outputs.py` |
| 2 | `balance_sheet.json` | Group balance sheet (GBP) | Done | `app/accounting/outputs.py` |
| 3 | `profit_and_loss.json` | Group P&L (GBP) | Done | `app/accounting/outputs.py` |
| 4 | `changes_in_equity.json` | Changes in equity (GBP) | Done | `app/accounting/outputs.py` |
| 5 | `eliminations.csv` | Every elimination with source rows | Done | `app/accounting/outputs.py` |
| 6 | `issues.json` | Every problem found: file, line, amount, treatment | Done | `app/accounting/outputs.py` |
| 7 | `control_figures_ns.json` | The 12 control figures (+ split causes) | Done | `app/accounting/outputs.py` |

## B. Deterministic accounting engine

| Area | Requirement | Status | Files |
|------|-------------|--------|-------|
| Ingestion | 3 entities ingested, source rows retained | Done | `app/accounting/ingestion.py` |
| Validation | Debit/credit totals reconcile per entity | Done | `app/accounting/validation.py` |
| Mapping | Every account mapped or flagged | Done | `app/accounting/mapping.py` |
| Mapping anomaly | UK 2310 → 2300 correction w/ evidence | Done | `statements.py` |
| Intercompany | Document + aggregate reconciliation, in-transit, FX residual | Done | `app/accounting/intercompany.py` |
| FX translation | Closing / average / H2 average / historical equity | Done | `app/accounting/translation.py` |
| FX gap | GBP/SGD closing rate missing → null + reason | Done | `statements.py` |
| Journals | Post/reject/exclude/flag + injection-safe | Done | `app/accounting/adjustments.py` |
| Eliminations | IC balance + P&L elimination, balanced | Done | `app/accounting/eliminations.py` |
| Acquisition | Goodwill, NCI at acquisition, post-acq profit | Done | `app/accounting/acquisition.py` |
| NCI | At acquisition, share of profit, closing | Done | `app/accounting/acquisition.py` |
| Translation reserve | Movement + reconciliation | Done | `statements.py` |

## C. Statement assembly (§2/§3 of this task)

| Item | Status | Files |
|------|--------|-------|
| Orchestrator wiring existing services | Done | `app/accounting/statements.py` |
| Consolidated trial balance | Done | `app/accounting/statements.py` |
| Balance sheet (A = L + E) | Done | `app/accounting/statements.py` |
| P&L (owners + NCI reconcile) | Done | `app/accounting/statements.py` |
| Changes in equity | Done | `app/accounting/statements.py` |
| Control figures | Done | `app/accounting/statements.py` |

## D. Pipeline integration (§5)

| Item | Status |
|------|--------|
| ConsolidationSkill runs full pipeline | Done |
| Persist run/ trace/ stages/ issues/ outputs/ AI proposals | Done |
| Historical runs preserved | Done |

## E. Frontend (§7)

| Item | Status |
|------|--------|
| Branding + API status | Done |
| Run Consolidation button | Done |
| Status / completion summary / pending issues | Done |
| Financial summary (revenue, profit, assets, equity) | Done |
| Previous runs history | Done |
| Run detail: statements, TB, issues, 7 files w/ downloads | Done |

## F. Documentation (§8)

| Item | Status |
|------|--------|
| README (setup, architecture, reproducible run) | Done |
| Architecture doc | Done |
| DECISIONS.md (5–7 decisions) | Done |
| VALIDATION.md (checks, recomputations, ≥3 AI mistakes) | Done |
| AI transcripts (secrets redacted) | Done (redacted run traces under `storage/agent_logs/traces/`) |
| One-page reflection | Done |

## G. Integrity rules (never violate)

- [x] No invented FX rate (missing rate → `null` + reason).
- [x] No forced intercompany reconciliation.
- [x] Journals classified posted / rejected / excluded / flagged.
- [x] Prompt-injection text in memos treated as data, never instructions.
- [x] Source data under `data/northwind/` never modified.
- [x] No LLM computes, alters or approves a financial figure.
- [x] No real LLM API calls during this implementation.

---

## Key accounting determinations (recorded as we build)

1. **UK 2310 corrected to group 2300 (Deferred income)** — the source mapping
   file maps `2310 Deferred income` to group `4100 Revenue`, contradicting the
   group COA which keeps *Deferred income* at `2300` (a liability). Corrected
   deterministically, flagged as an issue, source file left unmodified.
2. **GBP/SGD closing rate missing** → `sgd_bank_gbp = null` with reason. No
   substitution; no USD/SGD rate exists to derive a cross.
3. **JE-006 rejected** (unbalanced by USD 1,200; memo tries to waive the check),
   **JE-017 excluded** (dated 2025-01-06), **JE-014/JE-019 flagged** duplicate,
   **JE-016 rejected in full** (one line posts to an account outside the group
   chart of accounts — posting a partial journal would unbalance the entity).
4. **Goodwill** = consideration − 80% × net assets at acquisition (INR),
   retranslated at closing (GBP 1,375,815.07). **NCI** at proportionate share
   of net assets (at acquisition GBP 1,263,960.19; closing GBP 1,341,888.19).
5. **Consolidated TB uses the open-ledger basis** (equity = closing share
   capital + opening retained earnings + closing translation reserve + NCI at
   acquisition; year's P&L accounts stay open) so it balances exactly.
6. **Intercompany difference 20,066.42 GBP** is reported with its causes,
   never forced to zero. Only the £11,957.88 document-vs-booking difference is
   an exchange difference (posted to FX 7300); the £8,108.54 in-transit item is
   recognised on both sides at the closing rate and eliminates to zero.
7. **Translation reserve movement (−108,138.43 GBP)** is the genuine, derived
   exchange difference (India acquisition-to-closing), never a balancing plug.
   The unreconcilable part of the opening position is disclosed as an issue.
8. **Reported statement lines foot to their totals** — each line is rounded to
   2 dp independently (policy §7), so every summary section absorbs any 1p
   presentation residual in its largest line; totals are never changed.
