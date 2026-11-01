# Reflection

## What went smoothly

- **Determinism paid off.** Keeping every calculation in `Decimal` and
  delegating all arithmetic to the engine made the independent recomputation
  possible: a second implementation that shares no code reproduces all twelve
  control figures to the penny. That is far stronger evidence than any test
  that reuses the engine's own functions.
- **Traceability by construction.** Because every TB row and elimination line
  carries its source reference (entity, file, row), the consolidated trial
  balance and `eliminations.csv` document themselves — there was no separate
  "add audit info" pass.
- **The honest-null rule held.** The missing GBP/SGD closing rate is reported
  as `null` with a reason everywhere it appears (control figure, issue,
  dashboard), rather than being cross-derived into a plausible number.
- **The agent layer stayed advisory.** AI stages plan, investigate and propose;
  Python verifies each proposal and never adopts one on confidence alone. The
  deterministic result is identical whether or not a model is configured.

## What I had to correct

- **Unbalanced elimination journals.** Two elimination journals had sign
  errors and did not balance. Fixed into balanced double-entry journals (the
  investment journal now carries the goodwill and translation-reserve
  residual), guarded by a new `elimination_journals_balance` check.
- **The consolidated TB was short by the group profit.** Closing the P&L into
  retained earnings while leaving the P&L accounts open double-counted the
  year's result. Fixed by adopting the open-ledger basis for the TB (statements
  still use closing retained earnings). Guarded by `consolidated_tb_balances`.
- **Stale self-description.** The skill docstring and `SkillSpec` still claimed
  translation, elimination and statement assembly were "not implemented" and
  that `complete` could never be true. A wrong self-description would mislead
  a reviewer, so it was corrected to match the shipped engine.
- **Next.js 16 lint semantics.** The new client views initially set state
  synchronously inside `useEffect`, which the current `react-hooks` rules
  reject. Reworked the bootstrap fetch to resolve after an await tick,
  matching the pattern the existing dashboard already used.

## Trade-offs accepted

- **Open-ledger TB vs closing statements.** The two presentations of equity
  differ by design (the TB keeps the P&L accounts open so it balances exactly;
  the statements close them). This is documented so it is not mistaken for a
  discrepancy.
- **Held duplicates instead of a tie-break.** JE-014 / JE-019 are both held for
  review rather than picking one arbitrarily — a deliberate refusal to fabricate
  an accounting decision.
- **Frontend reads the latest persisted run.** The Reports, Runs and Issues
  pages load the newest run (or `?run=`) from the API rather than caching state
  locally; the backend remains the single source of truth.
- **Dedicated entities/source-data browsers remain placeholders.** Their data
  is already visible on the Consolidation page; the placeholders now say so
  rather than claiming the features are absent.

## Notes for next steps

1. The stale-doc risk is real: whenever a capability ships, the skill spec,
   docstrings, README and checklist must move with it. Treat descriptions as
   part of the deliverable.
2. `scripts/verify_independent.py` is the regression gate that matters most —
   extend it before trusting any new figure.
3. The accounting rules are deliberately conservative (hold, report, never
   force); a human review workflow is the natural next layer on top of the
   held/flagged items.
4. Keep the rule absolute: the engine stays deterministic, and every AI
   suggestion remains a suggestion a human approves.
