"""AI-assisted consolidation workflows (planning, mapping, matching).

Each function is a thin orchestration over the shared
:class:`~app.agents.llm_reasoning.LLMReasoner`, grounded on real source data
retrieved through the skill's MCP ``ToolGateway``. They:

* retrieve the *actual* source records through the gateway (never the model's
  memory, never the filesystem);
* ask the LLM for a structured proposal;
* hand that proposal to a deterministic verifier before anything is treated as
  decided;
* record an explicit ``AIStageReport`` (``completed`` / ``not_configured`` /
  ``failed``) so an unavailable or failed AI stage is never silently skipped.

No function here computes a financial figure. Planning proposes; mapping
proposes; matching proposes. The deterministic services (and, for matching,
:class:`~app.accounting.intercompany.IntercompanyReconciler`) decide.
"""

from __future__ import annotations

import json
from typing import Any

from app.accounting.intercompany import (
    IntercompanyRecord,
    IntercompanyReconciler,
    MatchVerdict,
)
from app.accounting.prompts import (
    ANOMALY_SYSTEM,
    ANOMALY_TEMPLATE_ID,
    ANOMALY_USER,
    INTERCOMPANY_SYSTEM,
    INTERCOMPANY_TEMPLATE_ID,
    INTERCOMPANY_USER,
    MAPPING_SYSTEM,
    MAPPING_TEMPLATE_ID,
    MAPPING_USER,
    PLAN_SYSTEM,
    PLAN_TEMPLATE_ID,
    PLAN_USER,
)
from app.accounting.reasoning import (
    AIStageReport,
    AnomalyInvestigation,
    ExecutionPlan,
    IntercompanyMatchingOutput,
    MappingProposal,
)
from app.agents.llm_reasoning import LLMReasoner
from app.core.errors import LedgerFlowError

# The stage identifiers the plan is allowed to use, and which of them the
# deterministic services can actually execute today. Kept here (not in the
# prompt) so the plan's ``executable`` flags can be cross-checked in Python.
_EXECUTABLE_STAGES = frozenset(
    {
        "trial_balance_ingestion",
        "trial_balance_validation",
        "account_mapping",
        "intercompany_reconciliation",
    }
)
_ALL_STAGES = (
    "trial_balance_ingestion",
    "trial_balance_validation",
    "account_mapping",
    "intercompany_reconciliation",
    "fx_translation",
    "intercompany_elimination",
    "goodwill",
    "nci",
    "translation_reserve",
    "financial_statement_assembly",
)


def _not_configured(stage: str, detail: str) -> AIStageReport:
    return AIStageReport(stage=stage, status="not_configured", detail=detail)


def _failed(stage: str, detail: str, records: list[dict[str, Any]]) -> AIStageReport:
    return AIStageReport(
        stage=stage, status="failed", detail=detail, call_records=records
    )


# --- A. execution planning --------------------------------------------------


async def plan_execution(
    reasoner: LLMReasoner | None,
    *,
    request: dict[str, Any],
    entities: list[dict[str, Any]],
    skills: list[dict[str, Any]],
) -> tuple[ExecutionPlan | None, AIStageReport]:
    """Produce a structured execution plan (LLM) or report ``not_configured``."""
    if reasoner is None:
        return None, _not_configured(
            "ai_execution_plan", "no LLM configured; deterministic plan used"
        )

    user = PLAN_USER.format(
        request=json.dumps(request, indent=2),
        entities=json.dumps(
            [
                {
                    "entity_id": e.get("entity_id"),
                    "functional_currency": e.get("functional_currency"),
                }
                for e in entities
            ],
            indent=2,
        ),
        skills=json.dumps(
            [{"name": s.get("name"), "description": s.get("description")} for s in skills],
            indent=2,
        ),
    )
    try:
        plan = await reasoner.reason(
            template_id=PLAN_TEMPLATE_ID,
            system_prompt=PLAN_SYSTEM,
            user_prompt=user,
            schema=ExecutionPlan,
        )
    except LedgerFlowError as exc:
        return None, _failed(
            "ai_execution_plan",
            f"{exc.code}: {exc.message}",
            [r.to_payload() for r in reasoner.records],
        )

    # Cross-check the plan's executable flags in Python: the model cannot
    # declare a stage runnable that has no deterministic service.
    checked_steps = [
        step.model_copy(
            update={"executable": step.stage in _EXECUTABLE_STAGES}
        )
        for step in plan.steps
    ]
    plan = plan.model_copy(update={"steps": checked_steps})
    return plan, AIStageReport(
        stage="ai_execution_plan",
        status="completed",
        detail=f"planned {len(plan.steps)} stage(s)",
        call_records=[r.to_payload() for r in reasoner.records],
    )


# --- B. account-mapping anomaly investigation -------------------------------


async def propose_account_mapping(
    reasoner: LLMReasoner | None,
    *,
    entity: str,
    local_code: str,
    local_name: str | None,
    current_group_code: str | None,
    candidates: list[dict[str, Any]],
    policy_excerpt: str,
) -> tuple[MappingProposal | None, AIStageReport]:
    """Ask the LLM to investigate one mapping and propose a candidate.

    The proposal is verified in Python before it is reported: the proposed
    group code must exist in the group chart, and the current mapping is never
    changed — only recommended.
    """
    stage = f"ai_account_mapping.{entity}.{local_code}"
    if reasoner is None:
        return None, _not_configured(
            stage, "no LLM configured; deterministic mapping retained"
        )

    record = {
        "entity": entity,
        "local_code": local_code,
        "local_name": local_name,
        "current_group_code": current_group_code,
    }
    user = MAPPING_USER.format(
        record=json.dumps(record, indent=2),
        candidates=json.dumps(candidates, indent=2),
        policy=policy_excerpt,
    )
    try:
        proposal = await reasoner.reason(
            template_id=MAPPING_TEMPLATE_ID,
            system_prompt=MAPPING_SYSTEM,
            user_prompt=user,
            schema=MappingProposal,
        )
    except LedgerFlowError as exc:
        return None, _failed(
            stage,
            f"{exc.code}: {exc.message}",
            [r.to_payload() for r in reasoner.records],
        )

    # Deterministic verification: a proposal naming a group code that does not
    # exist in the chart is rejected and marked unverified.
    known_codes = {str(c.get("account_code")) for c in candidates}
    notes: list[str] = []
    verified = True
    if proposal.proposed_group_code is not None:
        if proposal.proposed_group_code not in known_codes:
            verified = False
            notes.append(
                f"proposed group code {proposal.proposed_group_code!r} is not in "
                f"the supplied group chart candidates"
            )
    else:
        notes.append("no change proposed")
    if proposal.proposed_group_code == current_group_code:
        notes.append("proposal matches the current mapping")

    proposal = proposal.model_copy(
        update={"verified": verified, "verification_notes": "; ".join(notes)}
    )
    return proposal, AIStageReport(
        stage=stage,
        status="completed",
        detail=(
            f"proposed {proposal.proposed_group_code or 'no change'} "
            f"(confidence={proposal.confidence.value}, verified={verified})"
        ),
        call_records=[r.to_payload() for r in reasoner.records],
    )


# --- C. validation anomaly investigation ------------------------------------


async def investigate_anomalies(
    reasoner: LLMReasoner | None,
    *,
    issues: list[dict[str, Any]],
    policy_excerpt: str,
) -> tuple[AnomalyInvestigation | None, AIStageReport]:
    """Ask the LLM to explain deterministic validation issues (advisory only)."""
    if reasoner is None:
        return None, _not_configured(
            "ai_anomaly_investigation", "no LLM configured; issues left as reported"
        )
    if not issues:
        return None, AIStageReport(
            stage="ai_anomaly_investigation",
            status="skipped",
            detail="no validation issues to investigate",
        )

    user = ANOMALY_USER.format(
        issues=json.dumps(issues, indent=2), policy=policy_excerpt
    )
    try:
        investigation = await reasoner.reason(
            template_id=ANOMALY_TEMPLATE_ID,
            system_prompt=ANOMALY_SYSTEM,
            user_prompt=user,
            schema=AnomalyInvestigation,
        )
    except LedgerFlowError as exc:
        return None, _failed(
            "ai_anomaly_investigation",
            f"{exc.code}: {exc.message}",
            [r.to_payload() for r in reasoner.records],
        )
    return investigation, AIStageReport(
        stage="ai_anomaly_investigation",
        status="completed",
        detail=f"investigated {len(investigation.findings)} finding(s)",
        call_records=[r.to_payload() for r in reasoner.records],
    )


# --- D. intercompany candidate matching -------------------------------------


def _row_brief(record: IntercompanyRecord) -> str:
    return (
        f"row {record.source_row}: {record.reporting_entity} -> "
        f"{record.counterparty} | ref={record.doc_ref} | date={record.doc_date} | "
        f"{record.doc_currency} {record.doc_amount} | booked {record.booked_currency} "
        f"{record.booked_amount} | {record.description}"
    )


def verify_matches(
    reconciler: IntercompanyReconciler,
    proposals: list[dict[str, Any]],
    records: list[IntercompanyRecord],
) -> list[dict[str, Any]]:
    """Deterministically verify LLM-proposed matches; never trust confidence.

    Returns one dict per proposal with the verdict (accepted, match type,
    residual, reason). A proposal citing a non-existent row is rejected.
    """
    results: list[dict[str, Any]] = []
    for proposal in proposals:
        left = proposal.get("left_row")
        right = proposal.get("right_row")
        if not isinstance(left, int) or not isinstance(right, int):
            results.append(
                {
                    "left_row": left,
                    "right_row": right,
                    "accepted": False,
                    "match_type": "invalid_proposal",
                    "residual": None,
                    "reason": "proposal did not reference two row numbers",
                    "confidence": proposal.get("confidence"),
                }
            )
            continue
        try:
            verdict: MatchVerdict = reconciler.verify_pair(left, right, records)
        except LedgerFlowError as exc:
            results.append(
                {
                    "left_row": left,
                    "right_row": right,
                    "accepted": False,
                    "match_type": "invalid_proposal",
                    "residual": None,
                    "reason": f"{exc.code}: {exc.message}",
                    "confidence": proposal.get("confidence"),
                }
            )
            continue
        results.append(
            {
                "left_row": verdict.left_row,
                "right_row": verdict.right_row,
                "accepted": verdict.accepted,
                "match_type": verdict.match_type.value,
                "residual": str(verdict.residual)
                if verdict.residual is not None
                else None,
                "reporting_currency": verdict.reporting_currency,
                "reason": verdict.reason,
                "confidence": proposal.get("confidence"),
                "reasoning": proposal.get("reasoning", ""),
            }
        )
    return results


async def match_intercompany(
    reasoner: LLMReasoner | None,
    reconciler: IntercompanyReconciler,
    records: list[IntercompanyRecord],
) -> tuple[dict[str, Any] | None, AIStageReport]:
    """Propose intercompany matches (LLM) and verify them (Python)."""
    if reasoner is None:
        return None, _not_configured(
            "ai_intercompany_matching", "no LLM configured; no proposals generated"
        )
    if not records:
        return None, AIStageReport(
            stage="ai_intercompany_matching",
            status="skipped",
            detail="no intercompany rows to match",
        )

    rows_text = "\n".join(_row_brief(r) for r in records)
    user = INTERCOMPANY_USER.format(
        rows=rows_text, reporting_currency=reconciler.reporting_currency
    )
    try:
        output = await reasoner.reason(
            template_id=INTERCOMPANY_TEMPLATE_ID,
            system_prompt=INTERCOMPANY_SYSTEM,
            user_prompt=user,
            schema=IntercompanyMatchingOutput,
        )
    except LedgerFlowError as exc:
        return None, _failed(
            "ai_intercompany_matching",
            f"{exc.code}: {exc.message}",
            [r.to_payload() for r in reasoner.records],
        )

    verified = verify_matches(
        reconciler,
        [p.model_dump() for p in output.proposals],
        records,
    )
    payload = {
        "summary": output.summary,
        "unmatched_notes": list(output.unmatched_notes),
        "proposals": verified,
        "accepted_count": sum(1 for v in verified if v["accepted"]),
        "proposed_count": len(verified),
    }
    return payload, AIStageReport(
        stage="ai_intercompany_matching",
        status="completed",
        detail=(
            f"{payload['accepted_count']}/{payload['proposed_count']} proposal(s) "
            f"verified as reconciling"
        ),
        call_records=[r.to_payload() for r in reasoner.records],
    )
