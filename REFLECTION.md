# Reflection

Twelve hours is enough to build a financial consolidation engine that produces
traceable, correct statements for one fixed group — and not enough to build one
that survives a different group, a different period, or a second pair of eyes.
The part that went well was keeping arithmetic out of the model: a deterministic
`Decimal` engine plus an independent recomputation meant the numbers could be
defended line by line, and the AI's role stayed where it belongs — reading
messy inputs, flagging anomalies and proposing treatments that Python then
verified.

## What I would build differently with three months instead of twelve hours

A month would go on **generalisation**. The current engine reads a fixed set of
Northwind files with entity-specific assumptions baked into the pipeline; a
production system needs a declarative entity/period configuration so a new
subsidiary or a new month is data, not a code change. The next month would go on
**human-in-the-loop workflow**: today the held and rejected journals are
surfaced as issues, but there is no review console where an accountant resolves
them and the run re-executes with the decision recorded — that audit trail is
the actual product. The third month would go on **reconciliation depth**:
multi-period opening balances, prior-year comparatives, and a proper
intercompany matching UI, because the `opening_position_unreconciled` issue
shows how much the current build leans on the data being complete.

## Where the prototype breaks at scale

It breaks first on **periods, then on entities**. The India partial-period fix
(`28f3b43`) rebuilds one entity's P&L from a monthly file because India was
acquired mid-year; there is no general period-cut engine, so a group with
several mid-year acquisitions or disposals would need that logic rewritten per
entity. Second, the intercompany reconciliation matches reciprocal pairs by
document reference and amount — fine for five documents, quadratic and
error-prone for thousands, and it has no tolerance model for partial
settlements. Third, everything is single-period and filesystem-based: reruns are
cheap, but there is no incremental consolidation, no database, and the trace of
"what changed and why" lives in JSON files rather than queryable records. At
dozens of entities with thousands of accounts, the missing pieces are a
proper matching index, a period-cut model, and durable storage — not more
accounting rules.

## One thing I think is being underestimated

The hardest part is not the arithmetic or the FX; it is the **refusal to
guess**. The bundle contains memo text engineered to instruct automated tools
to waive a balance check, a mapping that contradicts the group chart of
accounts, a duplicate journal, and a genuinely missing exchange rate. Each one
tempts a system to produce a plausible number. The control that actually keeps
the output honest is an unexplained residual that *fails the run* rather than
being absorbed into equity — which is exactly the check that would have caught
the India error immediately, and which this codebase only recently grew. Framed
that way, the evaluation is not "can you consolidate a group", it is "can you
consolidate a group while leaving the gaps visible" — and that instinct is much
rarer, and much more valuable, than knowing how to post an elimination.
