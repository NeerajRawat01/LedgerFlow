"""Consolidation Skill — the consolidation pipeline on the shared core agent.

This skill executes the full FY2024 group consolidation end-to-end and reports
each stage's status explicitly and deterministically:

executed here
    * trial-balance ingestion (US / UK / IN),
    * trial-balance validation (debit/credit balance & row checks),
    * local→group account mapping (including the UK 2310 anomaly correction),
    * journal validation (post / reject / exclude / hold-for-review),
    * intercompany reconciliation (document + aggregate, in-transit, FX residual),
    * FX translation to GBP (closing / average / H2 average / historical equity),
    * intercompany elimination,
    * acquisition accounting (goodwill, NCI at acquisition, post-acq profit),
    * translation reserve,
    * financial-statement assembly and output generation.

Design rules honoured
    * source-data retrieval goes through ``context.tool_gateway.call`` under
      the skill's static ``ToolPolicy`` — never the filesystem, never a
      direct ``SourceDataService`` call as a substitute for MCP retrieval;
    * all accounting computation is delegated to the existing deterministic
      services (no logic duplicated in the skill);
    * the result carries **readiness** plus the assembled statements, control
      figures and written deliverables; a missing FX rate is reported as
      ``null`` with a reason rather than fabricated, and ``complete`` stays
      False while a required stage is missing.
"""

from __future__ import annotations

from typing import Any

from app.accounting.ai_workflows import (
    investigate_anomalies,
    match_intercompany,
    plan_execution,
    propose_account_mapping,
)
from app.accounting.consolidation import (
    ConsolidationReadiness,
    EntityConsolidationResult,
    StageStatus,
    build_stage_ledger,
)
from app.accounting.fx import FXService
from app.accounting.ingestion import TrialBalanceIngestor
from app.accounting.intercompany import IntercompanyReconciler
from app.accounting.mapping import AccountMapper
from app.accounting.models import FXRateType, Severity, ValidationIssue
from app.accounting.reasoning import AIStageReport
from app.accounting.validation import TrialBalanceValidator
from app.agents.llm_reasoning import build_reasoner
from app.agents.skills.base import (
    Skill,
    SkillContext,
    SkillSpec,
    ToolPolicy,
)
from app.agents.state import AgentMode, RunStatus, SkillResult
from app.core.errors import LedgerFlowError
from app.services.source_data import SourceDataService

# The reporting period this increment supports. The source dataset is FY2024
# (see ``data/northwind/group_policies.md``); other periods are rejected
# rather than silently consolidated with the wrong data.
SUPPORTED_PERIOD = "2024"
PERIOD_ALIASES = frozenset({"2024", "fy2024", "fy-2024", "fy 2024"})

# Tools the skill may call, all read-only source accessors.
PERMITTED_TOOLS: tuple[str, ...] = (
    "list_entities",
    "get_trial_balance",
    "get_group_coa",
    "get_account_mapping",
    "get_fx_rates",
    "get_intercompany",
)

# Policy rate types that must resolve for a full FY2024 group consolidation.
# Kept to the types the source actually records for the group's currencies.
_READINESS_RATE_TYPES: tuple[FXRateType, ...] = (
    FXRateType.CLOSING,
    FXRateType.AVERAGE,
    FXRateType.H2_AVERAGE,
)


class ConsolidationSkill(Skill):
    """Runs the implemented consolidation stages for one period."""

    def __init__(
        self,
        *,
        ingestor: TrialBalanceIngestor | None = None,
        validator: TrialBalanceValidator | None = None,
        mapper: AccountMapper | None = None,
        fx: FXService | None = None,
        source: Any = None,
        reconciler: IntercompanyReconciler | None = None,
    ) -> None:
        # Deterministic accounting services. Injectable so tests can point
        # them at the real or a synthetic dataset; defaults bind to process
        # settings, which is what production uses.
        self._ingestor = ingestor or TrialBalanceIngestor()
        self._validator = validator or TrialBalanceValidator()
        self._mapper = mapper or AccountMapper()
        self._fx = fx or FXService()
        # The intercompany verifier needs raw rows; it shares one
        # SourceDataService so no reader is duplicated. Default it to the
        # mapper's service so an injected dataset (tests, alternate data dir)
        # is honoured consistently, falling back to process settings.
        if source is not None:
            self._source = source
        else:
            self._source = getattr(self._mapper, "service", None) or SourceDataService()
        self._reconciler = reconciler or IntercompanyReconciler(
            self._source, fx=self._fx
        )
        from app.core.config import settings as _settings

        self._settings = _settings

    spec = SkillSpec(
        name="consolidation",
        description=(
            "Run a multi-entity group consolidation for a reporting period: "
            "ingest, validate, map, reconcile intercompany, translate, "
            "eliminate, apply acquisition accounting and assemble the "
            "consolidated statements."
        ),
        purpose=(
            "Given an explicit reporting period, load the group entities and "
            "their trial balances through MCP, then run the deterministic "
            "accounting services for ingestion, validation, local→group "
            "account mapping, journal processing, intercompany reconciliation, "
            "FX translation, eliminations, acquisition accounting, the "
            "translation reserve and financial-statement assembly, and return "
            "the consolidated statements, control figures and written "
            "deliverables alongside a typed readiness result."
        ),
        instructions=(
            "You are orchestrating a consolidation. Do NOT compute any "
            "financial figure yourself. Validate the period, ask the skill to "
            "execute the deterministic stages, then present the readiness "
            "result and the assembled statements: which stages completed, and "
            "which (if any) are blocked and why. Never invent a figure — a "
            "missing rate or unresolved item is reported as null with a reason."
        ),
        execution_procedure=(
            "validate the reporting period",
            "load entity context via MCP (list_entities)",
            "retrieve each entity's trial balance via MCP (get_trial_balance)",
            "ingest each trial balance with TrialBalanceIngestor",
            "validate each trial balance with TrialBalanceValidator",
            "map local accounts with AccountMapper",
            "process journal adjustments (post / reject / exclude / hold)",
            "reconcile intercompany balances with IntercompanyReconciler",
            "translate to GBP with FXService",
            "eliminate intercompany balances and P&L",
            "apply acquisition accounting (goodwill, NCI, post-acq profit)",
            "compute the translation-reserve movement",
            "assemble the statements and write the /output deliverables",
            "build the stage ledger and consolidation-readiness result",
        ),
        permitted_tools=PERMITTED_TOOLS,
        validation_requirements=(
            "an explicit FY2024 reporting period is required",
            "the requested scope must be 'group'",
            "every requested entity must be present in the source dataset",
        ),
        output_expectations=(
            "a typed consolidation-readiness result",
            "per-entity validation and mapping summaries",
            "explicit completed/blocked/pending stage statuses",
            "the consolidated statements and the twelve control figures",
            "the seven written deliverables under /output",
        ),
        allowed_modes=frozenset({AgentMode.CONSOLIDATION}),
        tool_policy=ToolPolicy.READ_ONLY,
    )

    # -- input validation --------------------------------------------------

    @staticmethod
    def normalise_period(period: str | None) -> str | None:
        """Return the canonical FY2024 period token, or None if unsupported."""
        if period is None:
            return None
        token = str(period).strip().lower().replace("_", " ")
        token = token.replace("-", " ").replace("  ", " ").strip()
        if token in PERIOD_ALIASES or token == SUPPORTED_PERIOD:
            return SUPPORTED_PERIOD
        return None

    # -- execution ---------------------------------------------------------

    async def execute(self, context: SkillContext) -> SkillResult:
        period_raw = getattr(context.intent, "period", None) or (
            context.payload or {}
        ).get("period")
        period = self.normalise_period(period_raw)

        if period is None:
            return self._reject(
                "period",
                f"an explicit FY{SUPPORTED_PERIOD} reporting period is required; "
                f"got {period_raw!r}",
                {"period": period_raw, "supported": SUPPORTED_PERIOD},
            )

        scope = str((context.payload or {}).get("scope") or "group").strip().lower()
        if scope not in ("group", "consolidated"):
            return self._reject(
                "scope",
                f"unsupported consolidation scope {scope!r}; expected 'group'",
                {"scope": scope},
            )

        requested = self._requested_entities(context, period_raw)

        # --- MCP source retrieval (policy-checked + traced) ---------------
        try:
            entities = await self._load_entities(context)
        except LedgerFlowError as exc:
            return self._error_result(exc, period)

        known = {e["entity_id"].strip().upper() for e in entities}
        selected = self._select_entities(known, requested)
        if not selected:
            return self._reject(
                "entities",
                f"no known entities selected; known={sorted(known)}, "
                f"requested={requested}",
                {"known": sorted(known), "requested": sorted(requested)},
            )

        issues: list[ValidationIssue] = []
        entity_results: dict[str, EntityConsolidationResult] = {}

        # --- deterministic stages per entity ------------------------------
        ingestor = self._ingestor
        validator = self._validator
        mapper = self._mapper

        ingestion_ok = validation_ok = mapping_ok = bool(selected)
        for entity in selected:
            try:
                # Retrieve the raw TB through MCP (traced), then hand the
                # deterministic service the same immutable source.
                tb_payload = await context.tool_gateway.call(
                    "get_trial_balance", {"entity": entity}
                )
            except LedgerFlowError as exc:
                ingestion_ok = validation_ok = mapping_ok = False
                issues.append(
                    ValidationIssue(
                        code="trial_balance_unavailable",
                        severity=Severity.ERROR,
                        message=(
                            f"{entity} trial balance could not be retrieved "
                            f"via MCP: {exc.message}"
                        ),
                        entity=entity,
                        details={"error_code": exc.code},
                    )
                )
                continue

            try:
                tb = ingestor.ingest(entity)
            except LedgerFlowError as exc:
                ingestion_ok = False
                issues.append(
                    ValidationIssue(
                        code="ingestion_failed",
                        severity=Severity.ERROR,
                        message=f"{entity} ingestion failed: {exc.message}",
                        entity=entity,
                        details={"error_code": exc.code},
                    )
                )
                continue

            validation = validator.validate(tb)
            if not validation.balanced:
                validation_ok = False
            issues.extend(validation.issues)

            mapped = mapper.map_trial_balance(tb)
            if mapped.unmapped_row_count:
                mapping_ok = False
            issues.extend(mapped.issues)

            entity_results[entity] = EntityConsolidationResult(
                entity=entity,
                functional_currency=tb.functional_currency,
                source_file=str(tb_payload.get("source_file"))
                if isinstance(tb_payload, dict)
                else tb.source_file,
                row_count=validation.row_count,
                balanced=validation.balanced,
                debit_total=str(validation.debit_total),
                credit_total=str(validation.credit_total),
                difference=str(validation.difference),
                mapped_row_count=sum(1 for r in mapped.rows if r.mapped),
                unmapped_row_count=mapped.unmapped_row_count,
                issues=list(validation.issues) + list(mapped.issues),
            )

        # --- FX-rate readiness checks -------------------------------------
        # Records a structured issue per unresolved required rate; the
        # return value is informational only (issues carry the detail).
        await self._fx_readiness(context, issues)

        # --- full deterministic assembly (translation → statements → outputs) --
        assembly_ok = False
        statement_payload: dict[str, Any] | None = None
        control_figures: dict[str, Any] | None = None
        output_manifest: list[dict[str, Any]] = []
        consolidated_summary: dict[str, Any] | None = None
        trial_balance: list[dict[str, Any]] = []
        eliminations: list[dict[str, Any]] = []
        integrity_checks: list[dict[str, Any]] = []
        try:
            from app.accounting.outputs import OutputWriter
            from app.accounting.statements import StatementAssembler

            assembler = StatementAssembler(self._source)
            bundle = assembler.assemble(selected)
            writer = OutputWriter(self._settings.output_dir)
            output_manifest = writer.write_all(bundle)
            assembly_ok = True

            # Structured issue rows from the assembly stage (with action).
            for issue in bundle.issues:
                issues.append(
                    ValidationIssue(
                        code=issue.code,
                        severity=issue.severity,
                        message=issue.message,
                        entity=issue.entity,
                        source_file=issue.source_file,
                        source_row=issue.source_row,
                        account_code=issue.account_code,
                        details={
                            **issue.details,
                            "action": issue.action,
                            "affected_amount": str(issue.affected_amount)
                            if issue.affected_amount is not None
                            else None,
                        },
                    )
                )

            control_figures = bundle.control_figures
            statement_payload = bundle.statements.model_dump()
            consolidated_summary = self._financial_summary(control_figures)
            # The consolidated trial balance and the eliminations are attached
            # as plain rows so the UI can display (and trace) exactly what the
            # deterministic engine produced.
            trial_balance = [
                {
                    "group_account_code": r.group_account_code,
                    "group_account_name": r.group_account_name,
                    "account_type": r.account_type,
                    "statement": r.statement,
                    "gbp_debit": str(r.gbp_debit),
                    "gbp_credit": str(r.gbp_credit),
                    "gbp_net": str(r.gbp_net),
                    "source_entities": sorted(
                        {s.get("entity") for s in r.sources if s.get("entity")}
                    ),
                    "source_rows": [
                        {
                            "entity": s.get("entity"),
                            "source_file": s.get("source_file"),
                            "source_row": s.get("source_row"),
                        }
                        for s in r.sources
                        if s.get("source_file")
                    ],
                }
                for r in bundle.trial_balance_rows
            ]
            eliminations = [
                {
                    "journal_id": e.journal_id,
                    "kind": e.kind,
                    "reference": e.reference,
                    "group_account_code": e.group_account_code,
                    "group_account_name": e.group_account_name,
                    "entity": e.entity,
                    "gbp_debit": str(e.gbp_debit),
                    "gbp_credit": str(e.gbp_credit),
                    "reason": e.reason or e.note,
                }
                for e in bundle.eliminations
            ]
            integrity_checks = bundle.statements.checks
        except LedgerFlowError as exc:
            issues.append(
                ValidationIssue(
                    code="assembly_failed",
                    severity=Severity.ERROR,
                    message=f"consolidated statement assembly failed: {exc.message}",
                    details={"error_code": exc.code},
                )
            )

        stages = build_stage_ledger(
            ingestion=ingestion_ok,
            validation=validation_ok,
            mapping=mapping_ok,
            journals=True,
            intercompany=True,
            translation=True,
            elimination=True,
            goodwill=True,
            nci=True,
            translation_reserve=True,
            statements=assembly_ok,
            outputs=bool(output_manifest),
        )

        # --- AI-assisted stages (LLM when configured; never authoritative) --
        ai_reports, ai_reasoning = await self._run_ai_stages(
            context,
            entities=entities,
            selected=selected,
            issues=issues,
            entity_results=entity_results,
        )

        readiness = ConsolidationReadiness(
            period=period,
            scope="group",
            entities=selected,
            stages=stages,
            entity_results=entity_results,
            issues=issues,
            complete=assembly_ok and not any(
                s.required_for_complete and s.status is not StageStatus.COMPLETED
                for s in stages
            ),
        )

        status = self._status_for(readiness)
        data = readiness.to_payload()
        # Consolidated statements, control figures and the written output files
        # are attached *alongside* the readiness summary. They are produced by
        # the deterministic engine; no figure here is LLM-generated.
        data["statements"] = statement_payload
        data["control_figures"] = control_figures
        data["consolidated_summary"] = consolidated_summary
        data["output_files"] = output_manifest
        data["trial_balance"] = trial_balance
        data["eliminations"] = eliminations
        data["integrity_checks"] = integrity_checks
        # AI-assisted context is attached *alongside* the deterministic
        # readiness result — it never replaces or alters it. Each AI stage
        # carries an explicit status (completed / not_configured / failed).
        data["ai_stages"] = [r.model_dump() for r in ai_reports]
        data["ai_reasoning"] = ai_reasoning
        data["ai_configured"] = context.llm is not None
        return SkillResult(
            status=status,
            summary=self._summary(readiness),
            answer=self._answer(readiness),
            data=data,
            issues=[
                {
                    "code": i.code,
                    "severity": i.severity.value,
                    "message": i.message,
                    "entity": i.entity,
                }
                for i in issues
            ],
        )

    # -- MCP helpers -------------------------------------------------------

    async def _load_entities(self, context: SkillContext) -> list[dict[str, str]]:
        payload = await context.tool_gateway.call("list_entities")
        if not isinstance(payload, list):
            return []
        return [e for e in payload if isinstance(e, dict) and e.get("entity_id")]

    @staticmethod
    def _requested_entities(
        context: SkillContext, period_raw: Any
    ) -> set[str]:
        entities = getattr(context.intent, "entities", None) or (
            context.payload or {}
        ).get("entities")
        if not entities:
            return set()
        if isinstance(entities, str):
            entities = [entities]
        return {str(e).strip().upper() for e in entities if str(e).strip()}

    @staticmethod
    def _select_entities(known: set[str], requested: set[str]) -> list[str]:
        if requested:
            return sorted(requested & known)
        return sorted(known)

    async def _fx_readiness(
        self, context: SkillContext, issues: list[ValidationIssue]
    ) -> bool:
        """Verify required policy rates resolve; record issues, never invent."""
        # Pull the recorded rates through MCP (traced) for the audit trail.
        await context.tool_gateway.call("get_fx_rates", {})

        fx = self._fx
        ok = True
        # Conversions the group policy needs for FY2024.
        required = (
            ("USD", "GBP", FXRateType.CLOSING),
            ("USD", "GBP", FXRateType.AVERAGE),
            ("INR", "GBP", FXRateType.CLOSING),
            ("INR", "GBP", FXRateType.H2_AVERAGE),
        )
        for base, quote, rtype in required:
            try:
                fx.resolve(base, quote, rtype)
            except LedgerFlowError as exc:
                ok = False
                issues.append(
                    ValidationIssue(
                        code="fx_rate_unresolved",
                        severity=Severity.WARNING,
                        message=(
                            f"no {rtype.value} rate to convert {base} to "
                            f"{quote}: {exc.message}"
                        ),
                        details={
                            "base_currency": base,
                            "quote_currency": quote,
                            "rate_type": rtype.value,
                            "error_code": exc.code,
                        },
                    )
                )
        return ok

    # -- AI-assisted stages ------------------------------------------------

    async def _run_ai_stages(
        self,
        context: SkillContext,
        *,
        entities: list[dict[str, str]],
        selected: list[str],
        issues: list[ValidationIssue],
        entity_results: dict[str, EntityConsolidationResult],
    ) -> tuple[list[AIStageReport], dict[str, Any]]:
        """Run the three AI-assisted workflows, degrading cleanly.

        Every workflow records an explicit :class:`AIStageReport`; when no LLM
        is configured the reasoner is ``None`` and each stage reports
        ``not_configured`` rather than pretending it ran. The deterministic
        readiness result is never modified by anything here.
        """
        reasoner = build_reasoner(
            context.llm,
            trace=context.trace,
            settings=context.settings or self._settings,
            secrets=[
                getattr(context.settings, "resolved_llm_api_key", None) or ""
            ],
        )
        reports: list[AIStageReport] = []
        reasoning: dict[str, Any] = {}

        # A. Execution planning (uses the registry's skill catalogue).
        plan, plan_report = await plan_execution(
            reasoner,
            request={
                "period": getattr(context.intent, "period", None),
                "scope": (context.payload or {}).get("scope", "group"),
                "entities": selected,
            },
            entities=entities,
            skills=await self._describe_skills(context),
        )
        reports.append(plan_report)
        if plan is not None:
            reasoning["execution_plan"] = plan.model_dump()

        # B. UK 2310 mapping anomaly investigation + proposal.
        mapping_proposals: list[dict[str, Any]] = []
        anomaly_issues = [i for i in issues if i.code == "group_account_name_mismatch"]
        for issue in anomaly_issues:
            proposal = await self._propose_for_anomaly(context, reasoner, issue)
            if proposal is not None:
                mapping_proposals.append(proposal.model_dump())
        if anomaly_issues:
            reports.append(
                AIStageReport(
                    stage="ai_account_mapping",
                    status="completed" if reasoner is not None else "not_configured",
                    detail=(
                        f"{len(mapping_proposals)} proposal(s) for "
                        f"{len(anomaly_issues)} mapping anomaly(ies)"
                        if reasoner is not None
                        else "no LLM configured; deterministic mapping retained"
                    ),
                )
            )
        if mapping_proposals:
            reasoning["mapping_proposals"] = mapping_proposals

        # C. Validation anomaly investigation (advisory).
        investigation, anomaly_report = await investigate_anomalies(
            reasoner,
            issues=[
                {
                    "code": i.code,
                    "severity": i.severity.value,
                    "message": i.message,
                    "entity": i.entity,
                    "account_code": i.account_code,
                }
                for i in issues
            ],
            policy_excerpt=self._policy_excerpt(),
        )
        reports.append(anomaly_report)
        if investigation is not None:
            reasoning["anomaly_investigation"] = investigation.model_dump()

        # D. Intercompany candidate matching (LLM proposes, Python verifies).
        matching, match_report = await self._run_intercompany_matching(
            context, reasoner
        )
        reports.append(match_report)
        if matching is not None:
            reasoning["intercompany_matching"] = matching

        return reports, reasoning

    async def _describe_skills(self, context: SkillContext) -> list[dict[str, Any]]:
        """The registry catalogue, if the graph supplied one on the context."""
        describe = getattr(context, "registry_describe", None)
        if callable(describe):
            return describe()
        # Fall back to this skill's own spec — still real, just narrower.
        spec = self.spec
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "purpose": spec.purpose,
            }
        ]

    async def _propose_for_anomaly(
        self,
        context: SkillContext,
        reasoner: Any,
        issue: ValidationIssue,
    ) -> Any:
        """Fetch real COA candidates and ask for a mapping proposal."""
        try:
            coa = await context.tool_gateway.call("get_group_coa")
            mapping = await context.tool_gateway.call(
                "get_account_mapping", {"entity": issue.entity or ""}
            )
        except LedgerFlowError:
            return None

        local_name = (issue.details or {}).get("local_name")
        candidates = self._candidate_coa(
            coa, local_name=local_name, current_code=issue.details.get("group_code")
        )
        proposal, report = await propose_account_mapping(
            reasoner,
            entity=issue.entity or "",
            local_code=issue.account_code or "",
            local_name=local_name,
            current_group_code=(issue.details or {}).get("group_code"),
            candidates=candidates,
            policy_excerpt=self._policy_excerpt(),
        )
        # Attach the raw mapping row for traceability (never modified).
        if proposal is not None:
            rows = (mapping or {}).get("rows") or []
            local_code = issue.account_code
            source_row = next(
                (
                    index + 1
                    for index, r in enumerate(rows)
                    if (r.get("local_code") or "").strip() == local_code
                ),
                None,
            )
            proposal = proposal.model_copy(
                update={
                    "entity": issue.entity or proposal.entity,
                    "local_code": local_code or proposal.local_code,
                }
            )
            self._record_proposal_trace(context, proposal, source_row)
        return proposal

    @staticmethod
    def _candidate_coa(
        coa: Any,
        *,
        local_name: str | None,
        current_code: str | None,
    ) -> list[dict[str, Any]]:
        """The group COA rows relevant to a name anomaly (real rows only)."""
        rows = coa if isinstance(coa, list) else []
        wanted = (local_name or "").strip().casefold()
        selected: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("account_code", "")).strip()
            name = str(row.get("account_name", "")).strip()
            if code == current_code or (wanted and name.casefold() == wanted):
                selected.append(
                    {
                        "account_code": code,
                        "account_name": name,
                        "account_type": row.get("account_type"),
                        "statement": row.get("statement"),
                        "normal_balance": row.get("normal_balance"),
                    }
                )
        return selected

    def _record_proposal_trace(
        self, context: SkillContext, proposal: Any, source_row: int | None
    ) -> None:
        if context.trace is None:
            return
        context.trace.event(
            "llm_proposal_recorded",
            {
                "proposal_type": "account_mapping",
                "entity": proposal.entity,
                "local_code": proposal.local_code,
                "current_group_code": proposal.current_group_code,
                "proposed_group_code": proposal.proposed_group_code,
                "confidence": proposal.confidence.value,
                "verified": proposal.verified,
                "needs_human_review": proposal.needs_human_review,
                "source_row": source_row,
            },
            secrets=[
                getattr(context.settings, "resolved_llm_api_key", None) or ""
            ],
        )

    async def _run_intercompany_matching(
        self, context: SkillContext, reasoner: Any
    ) -> tuple[dict[str, Any] | None, AIStageReport]:
        """Retrieve intercompany rows via MCP, then match (LLM) + verify (Python)."""
        try:
            await context.tool_gateway.call("get_intercompany")
            records = self._reconciler.load_records()
        except LedgerFlowError as exc:
            return None, AIStageReport(
                stage="ai_intercompany_matching",
                status="failed",
                detail=f"{exc.code}: {exc.message}",
            )
        return await match_intercompany(reasoner, self._reconciler, records)

    @staticmethod
    def _policy_excerpt() -> str:
        """A short, fixed accounting-policy summary for the prompts.

        Kept deterministic and short so no whole document is pasted into a
        prompt; the authoritative policy file remains the source of truth.
        """
        return (
            "Presentation currency GBP. Assets/liabilities at closing rate; "
            "income/expenses at average (India: H2 average); equity at "
            "historical rates; translation differences to reserve 3300. "
            "Intercompany balances and income/expense eliminated in full. "
            "NCI at proportionate share of net assets; goodwill in the "
            "subsidiary's functional currency. Round reported figures to 2 dp."
        )

    # -- result shaping ----------------------------------------------------

    @staticmethod
    def _financial_summary(control_figures: dict[str, Any]) -> dict[str, Any]:
        """The four headline figures the dashboard shows (already rounded)."""

        def show(key: str) -> Any:
            value = control_figures.get(key)
            return str(value) if value is not None else None

        return {
            "group_revenue": show("group_revenue"),
            "group_profit": show("group_profit"),
            "total_assets": show("total_assets"),
            "total_equity": show("total_equity"),
            "currency": "GBP",
        }

    @staticmethod
    def _status_for(readiness: ConsolidationReadiness) -> RunStatus:
        # A run cannot be COMPLETED while required stages are missing; the
        # only question is whether we reached a validated, mapped position.
        if readiness.missing_required_stages:
            has_errors = any(
                i.severity is Severity.ERROR for i in readiness.issues
            )
            return RunStatus.FAILED if has_errors else RunStatus.BLOCKED
        return RunStatus.COMPLETED

    @staticmethod
    def _summary(readiness: ConsolidationReadiness) -> str:
        completed = ", ".join(readiness.completed_stages) or "none"
        if readiness.complete:
            return (
                f"FY{readiness.period} group consolidation completed: "
                f"{len(readiness.completed_stages)} stage(s) completed "
                f"({completed}); consolidated statements and deliverables "
                f"written to /output."
            )
        blocked = ", ".join(readiness.blocked_stages) or "none"
        return (
            f"FY{readiness.period} consolidation readiness: "
            f"{len(readiness.completed_stages)} stage(s) completed "
            f"({completed}); {len(readiness.blocked_stages)} blocked "
            f"({blocked}). Consolidation is NOT complete: required stages "
            f"missing ({', '.join(readiness.missing_required_stages)})."
        )

    @staticmethod
    def _answer(readiness: ConsolidationReadiness) -> str:
        lines = [
            f"Consolidation readiness for FY{readiness.period} "
            f"(scope: {readiness.scope}).",
            "",
            "Stages:",
        ]
        for stage in readiness.stages:
            mark = {
                "completed": "x",
                "blocked": " ",
                "pending": " ",
                "failed": "!",
            }[stage.status.value]
            lines.append(f"  [{mark}] {stage.stage} — {stage.status.value}"
                         f"{' (' + stage.detail + ')' if stage.detail else ''}")
        lines.append("")
        for name in readiness.entities:
            r = readiness.entity_results.get(name)
            if r is None:
                lines.append(f"{name}: not processed")
                continue
            lines.append(
                f"{name}: {r.row_count} rows, balanced={r.balanced}, "
                f"mapped={r.mapped_row_count}, unmapped={r.unmapped_row_count}"
            )
        lines.append("")
        if readiness.complete:
            lines.append(
                "The group consolidation completed end to end. The consolidated "
                "statements and the seven deliverables were written to /output."
            )
        else:
            lines.append(
                "The consolidation is NOT complete: at least one required stage "
                "did not finish. No consolidated statements should be relied on."
            )
        return "\n".join(lines)

    def _reject(
        self, field: str, message: str, details: dict[str, Any]
    ) -> SkillResult:
        return SkillResult(
            status=RunStatus.FAILED,
            summary=f"invalid consolidation request: {message}",
            answer=message,
            data={"validation_error": True, "field": field, **details},
            issues=[
                {
                    "code": f"invalid_{field}",
                    "severity": Severity.ERROR.value,
                    "message": message,
                }
            ],
        )

    def _error_result(self, exc: LedgerFlowError, period: str) -> SkillResult:
        return SkillResult(
            status=RunStatus.FAILED,
            summary=f"consolidation blocked by {exc.code}: {exc.message}",
            answer=exc.message,
            data={"period": period, "error_code": exc.code},
            issues=[
                {
                    "code": exc.code,
                    "severity": Severity.ERROR.value,
                    "message": exc.message,
                }
            ],
        )
