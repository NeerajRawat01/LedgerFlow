# Decisions

The 6 design decisions that mattered most while building LedgerFlow. Each one
records the options considered, what was chosen, the trade-off accepted, and
what the AI tool suggested and whether that suggestion was taken.

Full AI session records (including the rejected suggestions) are in
`transcripts/`.

---

## D1. The LLM advises; Python computes every figure

**Options considered**

1. Let the LLM compute or approve consolidation figures (send the trial balances
   to a frontier model and ask for the statements).
2. A **deterministic `Decimal` accounting engine** produces every reported
   figure; the LangGraph agent and LLM only plan, investigate, classify and
   explain, and every AI proposal is re-verified by Python before it is used.

**Chosen:** Option 2. All arithmetic lives in `backend/app/accounting/*` using
Python `Decimal`. The AI stages (execution planning, mapping investigation,
anomaly investigation, intercompany matching) return *proposals*; nothing is
auto-applied. The 12 control figures are identical with the LLM on or off.

**Trade-off:** Real accounting coverage is limited to what the engine encodes,
and every new rule is hand-written and tested rather than prompted. In exchange,
outputs are reproducible, auditable to a source row, and free of hallucinated
numbers — which is exactly what a figure a CFO can sign requires.

**AI suggestion:** When asked "is it a better idea to give the documents to the
AI and let the AI do those tasks so there is no manual work?", the assistant
argued against it: free-text-to-number is unverifiable, and the assignment's
own evaluation says an architecture that "sends the trial balance to a frontier
model" is a disqualifier (`transcripts/09-app-overview-and-fix-1-commit.md`).
**Accepted** — deterministic engine kept, LLM stayed advisory.

---

## D2. A missing input is `null` + reason — never cross-derived

**Options considered**

1. Derive the missing GBP/SGD closing balance by crossing available rates
   (e.g. via USD), producing a plausible number.
2. Return `null` with an explicit reason, and log an issue.

**Chosen:** Option 2. There is no GBP/SGD closing rate in the bundle, so
`sgd_bank_gbp` is reported as `{"value": null, "reason": "…"}` and the same
reason is surfaced in `issues.json` and on the dashboard. The reason is nested
**inside** the value so `control_figures_ns.json` contains exactly the 12
required keys.

**Trade-off:** One of the 12 control figures is `null` rather than a number.
The assignment states an honest `null` scores better than a confident guess, so
this is the intended outcome; the cost is a visibly incomplete figure.

**AI suggestion:** The first implementation summed *every* foreign-currency row
to get the SGD bank balance (which would have wrongly included the UK's USD
intercompany payable, and would have needed a cross rate). The AI flagged this
in a self-review as "sums every foreign-currency row … once an SGD rate exists,
`sgd_bank_gbp` would be wrong" and proposed filtering to the bank accounts only
(`transcripts/10-consolidation-engine-fixes.md`). **Accepted** — fixed in
commit `28f3b43`.

---

## D3. Report the intercompany difference; never force it to zero

**Options considered**

1. Post a single balancing plug to make the intercompany balance disappear.
2. Match reciprocal balances by document reference and amount, post only the
   genuinely supported differences, and **report** the residual (with named
   causes) as a control figure.

**Chosen:** Option 2. The £20,066.42 difference is decomposed into in-transit
items (£8,108.54, recognised on both sides at the closing rate so they
eliminate to zero) and a document-versus-booked-rate FX difference
(£11,957.88, posted to account 7300). It is never plugged.

**Trade-off:** A reviewer sees a non-zero intercompany difference on the face
of the reconciliation. That is deliberate: an unexplained residual is
information, and forcing it away would fabricate a reconciliation the data does
not support.

**AI suggestion:** The AI initially routed the **entire** £20,066.42 to the FX
account, overstating the group FX loss, and (separately) treated the in-transit
item as an exchange difference. Its own audit then found that the in-transit
leg "looks incomplete — you credit the UK payable but record no matching debit"
(`transcripts/10-consolidation-engine-fixes.md`). **Accepted** — the split
treatment above was implemented.

---

## D4. Hold ambiguous journals for a human instead of guessing

**Options considered**

1. Tie-break duplicates and partial journals deterministically so the counts
   look clean (e.g. post the first of two identical journals and skip the
   other, post the mapped lines of a partly-unmapped journal).
2. Apply a stated, testable rule set and **hold or reject** anything that
   cannot be verified, surfacing each decision as an issue.

**Chosen:** Option 2. `JE-006` is rejected (unbalanced; its memo tries to waive
the check — treated as data, never an instruction). `JE-016` is rejected **in
full** because one line posts to an account outside the group chart of
accounts; posting only the mapped line would emit a one-sided entry and
unbalance India. Duplicate year-end journals are held for review rather than
tie-broken. Exact duplicate rule: post the first occurrence, reject the later
one — but a journal that reverses a balance-sheet account beyond its whole
balance is held because its sign cannot be verified.

**Trade-off:** The clean "15 posted / 2 rejected / 2 held" totals come with two
items a human must still resolve. Fewer figures are final than a
tie-breaking engine would produce; more decisions are defensible.

**AI suggestion:** The AI first classified out-of-period and duplicate journals
under an `excluded` status that was never counted, so the journal totals did not
add up. It also proposed rejecting *partial* journals line-by-line. The
assistant's later audit corrected both: map statuses to the required
`blocked | assumed | flagged` wording and reject partially-unmapped journals in
full (`transcripts/08-northwind-app-fixes-1-6.md`). **Accepted** — all-or-nothing
rejection adopted; exact-duplicate tie-break adopted.

---

## D5. Independent recomputation as the correctness gate

**Options considered**

1. Trust the engine's own unit and integration tests.
2. Write a second, self-contained `Decimal` recomputation
   (`scripts/verify_independent.py`) that shares **no code** with the engine and
   must reproduce every control figure.

**Chosen:** Option 2, plus four runtime integrity checks that must pass
(`balance_sheet_balances`, `profit_split_reconciles`, `consolidated_tb_balances`,
`elimination_journals_balance`). A test that reuses the engine's functions
cannot catch a wrong formula; a second implementation that agrees to the penny
can.

**Trade-off:** Two implementations must be kept in step, and the verifier is
itself code that can drift. **Known current gap:** after commit `28f3b43`
rebuilt India's P&L from the monthly rows, `verify_independent.py` still
recomputes India from the full-year trial balance, so the two no longer agree
(see `VALIDATION.md`). This is disclosed, not hidden.

**AI suggestion:** The AI proposed the independent script and the closed-form
re-derivation of the translation reserve and NCI. It also suggested *hardcoding
the expected control figures* as a regression oracle. That last part was
**rejected**: expected values may be used only as a test oracle and must be
independently recomputed, never hardcoded into the engine
(`transcripts/10-consolidation-engine-fixes.md`).

---

## D6. One shared LangGraph agent with two modes; the LLM never blocks a run

**Options considered**

1. One agent per task (a "12-agent design"), or an agentic pipeline that fails
   when the LLM is unavailable.
2. A **single** `StateGraph` shared by the Consolidation Agent and the AI
   Copilot. Execution `mode` is part of `AgentState` and branches the nodes;
   consolidation intent is derived deterministically from the structured
   payload and never needs an LLM, while the copilot uses the LLM with a
   keyword fallback. Any node failure routes straight to `finalize`, so every
   execution still produces a completed trace. Tools are reached only through
   the MCP gateway, whose policy the registry enforces.

**Chosen:** Option 2. Without credentials every AI stage reports
`not_configured` and the deterministic consolidation is unaffected; the model
is selectable (Gemini or Groq) via configuration.

**Trade-off:** More branching inside one graph than a set of small agents would
have, and the deterministic/copilot split has to be tested explicitly. The
benefit is one place to reason about state, tracing and tool permissions, and a
run that cannot be broken by an LLM outage.

**AI suggestion:** The AI supported the single shared agent and the
deterministic-intent branch, and built the Gemini/Groq provider abstraction. It
also, at one point, produced a "readiness report" that described stages without
producing financial statements; the author pushed back that a readiness report
is not the deliverable, and the agent was corrected to execute the full
pipeline (`transcripts/05-financial-consolidation-completion.md`). **Accepted**
(shared agent) and **partially rejected** (readiness-only output rejected).
