"""Typed AI reasoning models: plans, proposals and investigations.

These are the structured outputs the LLM produces and Python validates. Every
model is designed so that **no field can carry an authoritative financial
figure the LLM invented**:

* a *proposal* references source rows and offers a candidate decision with a
  confidence score and explicit ``needs_human_review`` flag — it is never the
  posting itself;
* an *extracted fact* always carries its source file/section and a value that
  Python must cross-check against the source;
* an *investigation* explains and recommends, but cites the deterministic
  issue it is reasoning about and never overrides it.

Nothing here computes. The deterministic services remain the only source of
accounting numbers.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    """A coarse, model-reported confidence band (never a financial value)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceRef(BaseModel):
    """A pointer back to the exact source record a claim rests on."""

    model_config = {"frozen": True}

    source_file: str | None = None
    source_row: int | None = None
    section: str | None = None
    account_code: str | None = None
    note: str = ""


class ExtractedFact(BaseModel):
    """One accounting fact the LLM extracted from a source document.

    ``value`` is a *reference* the deterministic layer must verify against the
    source — the LLM never supplies an authoritative figure. ``needs_review``
    defaults to True so nothing extracted is trusted without confirmation.
    """

    model_config = {"frozen": True}

    fact: str
    value: str | None = None
    interpretation: str = ""
    evidence: EvidenceRef = Field(default_factory=EvidenceRef)
    confidence: Confidence = Confidence.LOW
    needs_review: bool = True


class PlanStep(BaseModel):
    """One step of an AI-proposed execution plan."""

    model_config = {"frozen": True}

    stage: str
    rationale: str = ""
    # Whether Python can execute this stage automatically today, or whether it
    # requires human approval / is blocked (mirrors the stage ledger).
    executable: bool = False
    requires_approval: bool = False


class ExecutionPlan(BaseModel):
    """The structured plan the LLM produces for a consolidation request.

    The plan names period, entities and presentation currency, lists the
    accounting stages, and separates auto-executable stages from those needing
    approval. It is advisory: the deterministic skill still decides what it
    can actually run.
    """

    model_config = {"frozen": True}

    period: str | None = None
    entities: list[str] = Field(default_factory=list)
    presentation_currency: str | None = None
    workflow: str = "group_consolidation"
    summary: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)
    approval_stages: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    source: str = "llm"


class MappingProposal(BaseModel):
    """An AI-assisted account-mapping recommendation (never auto-applied).

    Carries the recommended group account, the reasoning, the evidence (the
    real mapping row and COA entries it compared) and a confidence band. The
    deterministic mapper only adopts an approved proposal; an unapproved or
    low-confidence proposal is surfaced for human review.
    """

    model_config = {"frozen": True}

    entity: str
    local_code: str
    local_name: str | None = None
    current_group_code: str | None = None
    proposed_group_code: str | None = None
    proposed_group_name: str | None = None
    confidence: Confidence = Confidence.LOW
    reasoning: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)
    needs_human_review: bool = True
    # Filled in by the deterministic layer: whether the proposal passed its
    # own checks (real COA account, name consistency, no material override).
    verified: bool = False
    verification_notes: str = ""


class AnomalyFinding(BaseModel):
    """One possible explanation for a validation anomaly (advisory only)."""

    model_config = {"frozen": True}

    issue_code: str
    entity: str | None = None
    account_code: str | None = None
    hypothesis: str = ""
    recommended_investigation: str = ""
    confidence: Confidence = Confidence.LOW
    evidence: list[EvidenceRef] = Field(default_factory=list)


class AnomalyInvestigation(BaseModel):
    """The LLM's advisory investigation of deterministic validation issues."""

    model_config = {"frozen": True}

    summary: str = ""
    findings: list[AnomalyFinding] = Field(default_factory=list)
    note: str = "Advisory only: deterministic validation results are preserved."


class MatchProposal(BaseModel):
    """The LLM's proposed intercompany match (never accepted on confidence alone).

    References two source rows by their 1-based row numbers in
    ``intercompany.csv`` and offers a confidence band and reasoning. Python
    independently verifies the proposal before it is treated as a match.
    """

    model_config = {"frozen": True}

    left_row: int = Field(ge=1)
    right_row: int = Field(ge=1)
    match_type: str = "reciprocal"
    confidence: Confidence = Confidence.LOW
    reasoning: str = ""
    proposed_difference_reason: str = ""

    @property
    def pair(self) -> tuple[int, int]:
        return (min(self.left_row, self.right_row), max(self.left_row, self.right_row))


class IntercompanyMatchingOutput(BaseModel):
    """The LLM's full response for an intercompany candidate-matching call."""

    model_config = {"frozen": True}

    proposals: list[MatchProposal] = Field(default_factory=list)
    unmatched_notes: list[str] = Field(default_factory=list)
    summary: str = ""


class AIStageReport(BaseModel):
    """The recorded outcome of one AI-assisted stage.

    ``status`` makes the AI availability explicit (``completed`` when it ran,
    ``not_configured`` when no key is present, ``failed`` when the LLM output
    could not be used). This is what the frontend renders as the AI stripe.
    """

    model_config = {"frozen": True}

    stage: str
    status: str  # completed | not_configured | failed | skipped
    detail: str = ""
    call_records: list[dict[str, Any]] = Field(default_factory=list)
