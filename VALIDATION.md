# Validation

How we know the outputs are correct: the checks that are coded, the figures
recomputed independently, and the AI-tool implementation mistakes that were
found and fixed — each with the commit that fixed it.

Environment: macOS, Node 22, Python 3.11, Next.js 16.4.3.

> **Read this first — two honest gaps.**
> The figures below were re-run on 2026-11-03 from the current commit
> (`28f3b43`). They differ from the earlier run recorded previously in this
> file, because commit `28f3b43` changed India's P&L. The independent
> recomputation (`scripts/verify_independent.py`) still uses the pre-fix India
> basis and therefore **no longer agrees with the engine**. This is disclosed
> rather than papered over. See §3.

---

## 1. One command produces all seven deliverables

`backend/.venv/bin/python scripts/run_consolidation.py` (run 2026-11-03) wrote:

```
consolidated_tb.csv (3358 bytes)   balance_sheet.json (3434 bytes)
profit_and_loss.json (2495 bytes)  changes_in_equity.json (2455 bytes)
eliminations.csv (3391 bytes)      issues.json (9842 bytes)
control_figures_ns.json (712 bytes)
```

and printed the twelve control figures. Fully deterministic — no network
access and no LLM call is required. Re-running produces the same files.

## 2. Twelve control figures (engine output, 2026-11-03)

| Figure | Value (GBP) |
|---|---|
| Group revenue | 28,301,607.81 |
| Group profit | 2,987,713.67 |
| Profit — owners | 2,909,785.66 |
| Profit — NCI | 77,928.01 |
| NCI at 31 Dec | 1,341,888.19 |
| Goodwill at 31 Dec | 1,375,815.07 |
| Translation reserve movement | −108,138.43 |
| Total assets | 28,596,245.74 |
| Total equity | 18,531,898.56 |
| Intercompany difference | 20,066.42 |
| UK SGD bank (GBP) | `null` + reason (no GBP/SGD closing rate) |
| Journals | 15 posted · 2 rejected · 2 held for review |

## 3. Independent recomputation — and its current divergence

`scripts/verify_independent.py` recomputes the figures from the raw source
files with its **own** `Decimal` arithmetic, sharing no code with the engine.

**Status: NOT clean as of 2026-11-03.** The script still builds India from the
full Apr–Dec trial balance (`tb_in.csv`), i.e. the pre-`28f3b43` basis. The
engine now builds India from the Jul–Dec monthly rows. The script prints:

```
group_revenue 30203422.23     group_profit 3069352.53
profit_owners 2991424.52      profit_nci 77928.01
```

which do **not** equal the engine's `28,301,607.81 / 2,987,713.67 /
2,909,785.66`. The residual is exactly the Apr–Jun India profit
(`28f3b43`'s subject). What the script *does* still prove, and still matches
the engine on, is the balance-sheet identity and the balance-sheet totals:

```
check A = L + E: 0.00
```

**Gap to close before submission:** update `verify_independent.py` to the
Jul–Dec monthly basis so the gate agrees again. Until then, this file does not
claim the independent recomputation passes — it does not.

## 4. Integrity checks (engine, all pass on the current commit)

- `balance_sheet_balances` — assets 28,596,245.74 = liabilities 10,064,347.18 +
  equity 18,531,898.56.
- `profit_split_reconciles` — owners 2,909,785.66 + NCI 77,928.01 = group profit
  2,987,713.67.
- `consolidated_tb_balances` — total debits 58,077,868.84 = total credits
  58,077,868.84 (difference 0.00).
- `elimination_journals_balance` — all 4 elimination journals balance.

## 5. Backend tests

**NOT passing as of 2026-11-03.** `pytest -q` → **31 failed, 272 passed**. The
failures are concentrated in tests that assert the pre-`28f3b43` India basis
(e.g. `test_statements.py::test_group_profit_recomputed_from_p_and_l`) and in
MCP-server tests. Earlier documentation claimed 303 passing; that is no longer
true and is corrected here. **Gap to close:** reconcile the tests with the
current engine.

## 6. API, frontend and source data (not re-tested this session)

- `POST /api/consolidations/runs` and the download endpoints were validated
  earlier (run history, full run record, `text/csv` for `consolidated_tb.csv`);
  they were **not re-run** in this documentation pass.
- `npm run lint` / `npm run build` were validated earlier; **not re-run** here.
- The 17 files under `data/northwind/` are untouched: the engine only reads
  them (the backend mounts `data/` read-only).

---

## AI mistakes found and corrected (with the fixing commit)

The AI-assisted stages are advisory and are verified deterministically, but
several **implementation** mistakes surfaced. Each is listed with the commit
that fixed it, verified against the real Git history.

### M1. India's P&L included the pre-acquisition quarter — fixed in `28f3b43`

India was acquired on 1 July 2024, but `tb_in.csv` covers the full Apr–Dec
period, so every India P&L line was overstated by its Apr–Jun amount
(INR 18,000,000 of profit belonging to the previous owners). The AI's own
self-review flagged this as "the biggest error"; it was first fixed in the
working tree over several sessions, then committed as
**`28f3b43b7b3b03f31c442dd2787e78d3e3fb0b4e`** ("FIX 1: build a mid-year
acquisition's P&L from the monthly rows only"), which rebuilds India's P&L from
`in_pl_monthly.csv` rows with `month >= acquisition month`, translated at the
H2-average rate, and blocks the run if the monthly totals do not tie to the TB.

### M2. The SGD bank balance summed every foreign-currency row — fixed in `28f3b43`

The first implementation (`_sgd_bank`) totalled every TB row whose transaction
currency was not GBP, which wrongly included the UK's USD intercompany payable.
The AI proposed filtering to the foreign-currency **bank** account only
(group 1100); the replacement `_foreign_currency_bank` also moved the missing-
rate reason inside the `sgd_bank_gbp` value so the file keeps exactly the 12
required keys. Both changes landed in
**`28f3b43b7b3b03f31c442dd2787e78d3e3fb0b4e`** (confirmed with
`git log -S"_foreign_currency_bank"`).

### M3. In-transit intercompany item had a missing leg / FX routed whole — fixed in `aeab17c`

The AI credited the UK payable for the in-transit invoice (£8,108.54) with no
matching debit, leaving group profit overstated and the missing amount absorbed
into a residual; it also posted the full £20,066.42 intercompany difference to
FX. The audit prompted the split treatment (in-transit recognised on both sides
at closing and eliminated to zero; only the £11,957.88 booking-rate difference
posted to 7300). The corrected eliminations/intercompany logic is contained in
**`aeab17c17b940189fde78a7d740bdacd6003e7f5`** ("build deterministic accounting
+ advisory AI"). **Caveat:** this is *not a dedicated fix commit* — the fix was
made in the working tree and is bundled into the single engine commit, so no
commit isolates just this change.

### M4. Reported statements did not foot — fixed in `aeab17c`

Each line is rounded to 2 dp independently (policy §7), so in several places
the rounded lines summed to a penny different from the reported total. Fixed by
footing every summary section to its total, absorbing the presentation residual
in the largest line while never changing the total (`_foot_section`). Introduced
in **`aeab17c17b940189fde78a7d740bdacd6003e7f5`**.

### M5. Stale skill/spec text claimed capabilities did not exist — fixed in `aeab17c`

The `ConsolidationSkill` docstring and `SkillSpec` still said translation,
elimination and statement assembly were "not implemented" and that `complete`
could never be true. Corrected in
**`aeab17c17b940189fde78a7d740bdacd6003e7f5`**.

### Commit-coverage gap (explicit)

Of the five mistakes above, only **M1 and M2 have a distinct, attributable fix
commit (`28f3b43`)**. **M3, M4 and M5 cannot be tied to a dedicated fix
commit**: the repository keeps a single engine commit (`aeab17c`) plus one
further fix (`28f3b43`), and the intermediate fixes were made in the working
tree without being committed individually. Two `git stash` entries
(`stash@{0}`, `stash@{1}`) and their base commits remain in the object store as
evidence that intermediate states existed, but they are not commits on the main
history. No commit hash is invented here to fill the gap.

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
