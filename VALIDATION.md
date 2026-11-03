# Validation

How we know the outputs are correct: the checks that are coded, the figures
recomputed independently, and the accounting bugs that were found and fixed —
each with the commit that fixed it.

Environment: macOS, Node 22, Python 3.11, Next.js 16.4.3.

> **Status: clean as of 2026-11-03.**
> The engine and the independent recomputation
> (`scripts/verify_independent.py`) now agree on **every** control figure, and
> the full test suite passes (`303 passed`). The balance sheet reconciles with
> **zero** unexplained residual. The fixes below were applied on top of commit
> `d13a630` (the tip of `main` after `28f3b43`).

---

## 1. One command produces all seven deliverables

`backend/.venv/bin/python scripts/run_consolidation.py` (run 2026-11-03) wrote:

```
consolidated_tb.csv (3358 bytes)   balance_sheet.json (3434 bytes)
profit_and_loss.json (2495 bytes)  changes_in_equity.json (2455 bytes)
eliminations.csv (4773 bytes)      issues.json (10005 bytes)
control_figures_ns.json (747 bytes)
```

and printed the twelve control figures plus six integrity checks. Fully
deterministic — no network access and no LLM call is required. Re-running
produces the same files.

## 2. Twelve control figures (engine output, 2026-11-03)

| Figure | Value (GBP) |
|---|---|
| Group revenue | 28,301,607.81 |
| Group profit | 2,987,713.67 |
| Profit — owners | 2,909,785.66 |
| Profit — NCI | 77,928.01 |
| NCI at 31 Dec | 1,342,258.61 |
| Goodwill at 31 Dec | 1,375,815.07 |
| Translation reserve movement | −75,355.69 |
| Total assets | 28,575,272.95 |
| Total equity | 18,446,143.15 |
| Intercompany difference | 20,066.42 |
| SGD bank (GBP) | `null` + reason (no GBP/SGD closing rate is supplied) |
| Journals | 15 posted · 2 rejected · 2 held · 1 excluded · 20 total |

The `journals` object keeps the required `posted` / `rejected` /
`held_for_review` keys and now **also** reports `excluded` and the `total`, so
every journal in `adjustments.json` is accounted for exactly once
(15 + 2 + 2 + 1 = 20). No required key was removed.

## 3. Independent recomputation — now in exact agreement

`scripts/verify_independent.py` recomputes the figures from the raw source
files with its **own** `Decimal` arithmetic, sharing no code with the engine.
Run on 2026-11-03 it prints:

```
=== INDEPENDENT CONTROL FIGURES ===
{
  "group_revenue": "28301607.81",
  "group_profit": "2987713.67",
  "profit_owners": "2909785.66",
  "profit_nci": "77928.01",
  "nci_closing": "1342258.61",
  "goodwill_closing": "1375815.07",
  "translation_reserve_movement": "-75355.69",
  "total_assets": "28575272.95",
  "total_equity": "18446143.15"
}
check A = L+E: 0.00
opening_position_unreconciled (disclosed) 0.00
```

Every one of these equals the engine's figure to the penny. The script was
updated this session to (a) build India from the Jul–Dec monthly rows, (b)
measure NCI at 20% of India's closing net assets at the closing rate, and (c)
derive the translation reserve as the genuine exchange difference rather than
routing a residual to retained earnings. It now shares **no** balancing plug
with the engine.

## 4. Integrity checks (engine, all pass on the current working tree)

- `balance_sheet_balances` — assets 28,575,272.95 = liabilities 10,129,129.80 +
  equity 18,446,143.15.
- `unexplained_residual_within_tolerance` — assets − liabilities − equity =
  **0.00** GBP (tolerance £0.005). No residual is routed to retained earnings or
  the translation reserve.
- `nci_reconciles_to_closing_net_assets` — NCI closing 1,342,258.61 =
  20.00% of India's closing net assets at the closing rate (difference 0.00).
- `profit_split_reconciles` — owners 2,909,785.66 + NCI 77,928.01 = group profit
  2,987,713.67.
- `consolidated_tb_balances` — total debits 58,077,868.84 = total credits
  58,077,868.84 (difference 0.00).
- `elimination_journals_balance` — all 5 elimination journals balance.

## 5. Backend tests

**Passing as of 2026-11-03.** `pytest -q` → **303 passed**. The seven tests
that encoded the previous (buggy) behaviour were updated to the corrected
expectations (India H2-only profit, NCI at closing net assets, the four-way
journal count, the ledger-basis TB equity, the genuine-FX translation reserve,
and the removal of the opening-position plug).

## 6. API, frontend and source data (not re-tested this session)

- `POST /api/consolidations/runs` and the download endpoints were validated
  earlier (run history, full run record, `text/csv` for `consolidated_tb.csv`);
  they were **not re-run** in this documentation pass.
- `npm run lint` / `npm run build` were validated earlier; **not re-run** here.
- The 17 files under `data/northwind/` are untouched: the engine only reads
  them (the backend mounts `data/` read-only).

---

## Accounting bugs found and corrected

The issues below were raised against the engine. Each is listed with its status
and the change that fixed it. Fixes are in the working tree on top of `d13a630`;
they are **not** committed (no commit is fabricated here).

### 1. India's P&L included the pre-acquisition quarter — FIXED

India was acquired on 1 July 2024, but `tb_in.csv` covers the full Apr–Dec
period, so every India P&L line was overstated by its Apr–Jun amount (INR
18,000,000 of profit belonging to the previous owners). The engine now rebuilds
India's P&L from `in_pl_monthly.csv` rows with `month >= 2024-07`, translated
at the H2-average rate, and blocks the run if the monthly totals do not tie to
the TB. This landed in commit `28f3b43`; this session also fixed a follow-on
gap — the partial-year rebuild started from the **raw** TB rows and so dropped
the balance-sheet effects of posted India journals (extra depreciation and
accruals). It now starts from the journal-adjusted balances, so India's balance
sheet ties exactly to the independent recomputation.

### 2. India equity translated at the wrong rate — FIXED

Subsidiary equity is now translated at the **acquisition-date (spot)** rate for
a mid-year acquisition (India), not the closing rate, so no translation
difference is imported into equity. The UK (owned before FY2024) remains at its
historical/opening basis.

### 3. NCI did not reconcile to India's closing net assets — FIXED

NCI at 31 December was previously "NCI at acquisition (spot) + share of H2
profit" = 1,341,888.19, which did not equal 20% of India's closing net assets.
It is now measured directly as `20% x (net assets at acquisition + H2 result)`
translated at the **closing** rate = **1,342,258.61**, and the
`nci_reconciles_to_closing_net_assets` check passes with a zero difference.

### 4. Retained-earnings / translation-reserve plugs — REMOVED

Two plugs previously absorbed unreconciled amounts into retained earnings
(`_opening_position_unreconciled`) and the investment-elimination residual.
Both are gone. Retained earnings is now exactly opening RE + the explainable UK
share-capital excess + profit attributable to owners, and the translation
reserve is the genuine exchange difference on translation (see §5 below). The
`unexplained_residual_within_tolerance` check now fails if any residual exceeds
£0.005; on the supplied data the residual is **0.00**.

### 5. Translation-reserve calculation — CORRECTED

The reserve movement is no longer India's component alone. It is derived from
the full translation arithmetic — India (acquisition-to-closing and H2 profit),
goodwill (retranslated from spot), the US parent (opening net assets and the
year's result), the parent investments, and the removal of the NCI's share —
and is then checked against the ledger identity. The reported figure,
**−75,355.69**, is what the ledger produces and it reconciles the balance sheet
exactly. The simplified closed-form estimate (44,780.67) and the difference are
disclosed as a warning (`translation_reserve_reconciliation`): the difference is
the exchange effect of the posted adjusting journals and the eliminations that
the closed form does not model. It is never routed to retained earnings.

### 6. Journal counts omitted the excluded bucket — FIXED

`journals` now reports `posted`, `rejected`, `held_for_review`, **and**
`excluded`, plus the `total` (15 + 2 + 2 + 1 = 20). All required output keys are
retained.

### 7. Silent hardcoded fallbacks — REMOVED

`_opening_equity` no longer carries hardcoded default balances
(1,250,000 / 12,604,747.93 / 412,350): it reads strictly from
`prior_year_group.csv` and fails if a required account is missing.
Acquisition dates and functional currencies come only from the entity master.

### 8. SGD bank summed every foreign-currency row — FIXED

The SGD figure now includes only rows that are **both** transacted in SGD
**and** map to a bank account in the group chart of accounts, so the UK's USD
intercompany payable is excluded. With no GBP/SGD closing rate in
`fx_rates.csv`, the figure is correctly `null` with an explicit reason.

### 9. Intercompany in-transit and elimination signs — VERIFIED

The in-transit invoice (`INV-US-2291`) is recognised on **both** sides at the
closing rate and eliminated to zero (`ELIM-TRANSIT-005`); only the genuine
booking-rate difference (£11,957.88 of the £20,066.42 total) is posted to FX
(7300). All elimination journals balance.

---

## Appendix — Phase 1 foundation (earlier work, retained for history)

Validated on 2026-10-31 at commit `765492c`:

- `docker compose config --quiet` exits 0; services `frontend`, `backend`,
  `db` all reach **healthy**.
- `GET /` → 307 redirect to `/dashboard`; dashboard routes → 200.
- `curl /api/health` → `{"status": "ok", "service": "ledgerflow-backend"}`.
- `docker compose stop db` leaves frontend and backend healthy (no DB
  coupling); no `DATABASE_URL` or driver anywhere in `backend/app`.
- `pytest` → 5 passed; `npm run lint` → 0 errors; `npm run build` → all routes
  prerendered.
- SHA-256 of all 17 files in `data/northwind/` matches the originals.
