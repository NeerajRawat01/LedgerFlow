"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  Bot,
  Brain,
  Building2,
  CheckCircle2,
  Clock,
  Loader2,
  PlayCircle,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ControlFiguresPanel,
  EliminationsPanel,
  IntegrityChecksPanel,
  StatementsPanel,
  TrialBalancePanel,
} from "@/components/consolidation/statement-view";
import {
  ConsolidationApiError,
  getConsolidationRun,
  getConsolidationTrace,
  startConsolidationRun,
} from "@/lib/consolidation";
import type {
  AIMappingProposal,
  AIMatchProposal,
  AIStage,
  ConsolidationRun,
  ConsolidationTrace,
  EntityStatus,
  Issue,
  StageOutcome,
  StageStatus,
  ToolCall,
} from "@/types/consolidation";
import { cn } from "@/lib/utils";

/** Human labels for internal stage identifiers. */
const STAGE_LABELS: Record<string, string> = {
  trial_balance_ingestion: "Trial balance ingestion",
  trial_balance_validation: "Trial balance validation",
  account_mapping: "Account mapping",
  intercompany_reconciliation: "Intercompany reconciliation",
  fx_translation: "FX translation",
  intercompany_elimination: "Eliminations",
  goodwill: "Goodwill",
  nci: "Non-controlling interest",
  translation_reserve: "Translation reserve",
  financial_statement_assembly: "Financial statement assembly",
};

/** Per-status presentation metadata for the stage ledger. */
const STAGE_META: Record<
  StageStatus,
  { label: string; icon: typeof CheckCircle2; badge: string; dot: string }
> = {
  completed: {
    label: "Completed",
    icon: CheckCircle2,
    badge:
      "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/20",
    dot: "bg-emerald-500",
  },
  blocked: {
    label: "Blocked",
    icon: ShieldAlert,
    badge: "bg-rose-50 text-rose-700 ring-1 ring-rose-600/20",
    dot: "bg-rose-500",
  },
  pending: {
    label: "Pending",
    icon: Clock,
    badge: "bg-amber-50 text-amber-700 ring-1 ring-amber-600/20",
    dot: "bg-amber-500",
  },
  failed: {
    label: "Failed",
    icon: AlertTriangle,
    badge: "bg-rose-50 text-rose-700 ring-1 ring-rose-600/20",
    dot: "bg-rose-500",
  },
};

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.replace(/_/g, " ");
}

function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Format a minor-unit decimal string with thousands separators. */
function formatAmount(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  return numeric.toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/** The overall run-status badge (blocked / failed / completed). */
function RunStatusBadge({ status }: { status: string }) {
  const meta =
    status === "completed"
      ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/20"
      : status === "failed"
        ? "bg-rose-50 text-rose-700 ring-1 ring-rose-600/20"
        : "bg-slate-100 text-slate-700 ring-1 ring-slate-600/20";
  return <Badge className={cn("font-medium", meta)}>{titleCase(status)}</Badge>;
}

function StageRow({ stage }: { stage: StageOutcome }) {
  const meta = STAGE_META[stage.status] ?? STAGE_META.pending;
  const Icon = meta.icon;
  return (
    <div className="flex items-start justify-between gap-4 py-2.5">
      <div className="flex min-w-0 items-start gap-3">
        <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", meta.dot)} />
        <div className="min-w-0">
          <p className="text-sm font-medium text-slate-800">
            {stageLabel(stage.stage)}
          </p>
          {stage.detail ? (
            <p className="mt-0.5 text-xs leading-relaxed text-slate-500">
              {stage.detail}
            </p>
          ) : null}
        </div>
      </div>
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1 rounded-4xl px-2 py-0.5 text-xs font-medium",
          meta.badge,
        )}
      >
        <Icon className="size-3" aria-hidden />
        {meta.label}
      </span>
    </div>
  );
}

function EntityCard({ entity }: { entity: EntityStatus }) {
  const healthy = entity.balanced && entity.unmapped_row_count === 0;
  return (
    <div className="rounded-lg border border-slate-200 p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Building2 className="size-4 text-slate-400" aria-hidden />
          <span className="text-sm font-medium text-slate-900">
            {entity.entity}
          </span>
        </div>
        <Badge variant="outline" className="font-mono text-xs">
          {entity.functional_currency}
        </Badge>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <div className="flex justify-between">
          <dt className="text-slate-500">Rows</dt>
          <dd className="font-medium text-slate-700">{entity.row_count}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-slate-500">Mapped</dt>
          <dd className="font-medium text-slate-700">
            {entity.mapped_row_count}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-slate-500">Debit</dt>
          <dd className="font-medium text-slate-700">
            {formatAmount(entity.debit_total)}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-slate-500">Credit</dt>
          <dd className="font-medium text-slate-700">
            {formatAmount(entity.credit_total)}
          </dd>
        </div>
      </dl>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-4xl px-2 py-0.5 font-medium",
            entity.balanced
              ? "bg-emerald-50 text-emerald-700"
              : "bg-rose-50 text-rose-700",
          )}
        >
          {entity.balanced ? "Balanced" : "Out of balance"}
        </span>
        <span className="text-slate-500">
          {entity.unmapped_row_count} unmapped
        </span>
        <span className="text-slate-500">
          {entity.error_count} errors · {entity.warning_count} warnings
        </span>
        {entity.source_file ? (
          <span className="font-mono text-[11px] text-slate-400">
            {entity.source_file}
          </span>
        ) : null}
      </div>
      {entity.difference && entity.difference !== "0" ? (
        <p className="mt-2 text-xs text-rose-600">
          Difference: {formatAmount(entity.difference)}
        </p>
      ) : null}
      {healthy && entity.error_count === 0 ? null : null}
    </div>
  );
}

function IssueRow({ issue }: { issue: Issue }) {
  const severity = issue.severity.toLowerCase();
  const tone =
    severity === "error"
      ? "bg-rose-50 text-rose-700 ring-1 ring-rose-600/20"
      : severity === "warning"
        ? "bg-amber-50 text-amber-700 ring-1 ring-amber-600/20"
        : "bg-slate-100 text-slate-700 ring-1 ring-slate-600/20";
  return (
    <div className="border-b border-slate-100 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "inline-flex rounded-4xl px-2 py-0.5 text-xs font-medium",
            tone,
          )}
        >
          {titleCase(issue.severity)}
        </span>
        <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
          {issue.code}
        </code>
        {issue.entity ? (
          <span className="text-xs text-slate-500">Entity {issue.entity}</span>
        ) : null}
        {issue.source_file ? (
          <span className="font-mono text-[11px] text-slate-400">
            {issue.source_file}
            {issue.source_row != null ? `:${issue.source_row}` : ""}
          </span>
        ) : null}
        {issue.account_code ? (
          <span className="font-mono text-[11px] text-slate-400">
            acct {issue.account_code}
          </span>
        ) : null}
      </div>
      <p className="mt-1.5 text-sm text-slate-700">{issue.message}</p>
      {(issue.details?.affected_amount as string | undefined) ? (
        <p className="mt-1 text-xs text-slate-500">
          Affected amount:{" "}
          {formatAmount(issue.details?.affected_amount as string)}
        </p>
      ) : null}
      {(issue.details?.treatment as string | undefined) ? (
        <p className="mt-0.5 text-xs text-slate-500">
          Treatment: {issue.details?.treatment as string}
        </p>
      ) : null}
    </div>
  );
}

function ToolCallRow({ call }: { call: ToolCall }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-slate-100 py-2 last:border-0">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "size-2 shrink-0 rounded-full",
              call.ok ? "bg-emerald-500" : "bg-rose-500",
            )}
          />
          <code className="font-mono text-xs text-slate-800">{call.name}</code>
        </div>
        {call.error ? (
          <p className="mt-1 pl-4 text-xs text-rose-600">{call.error}</p>
        ) : call.result_summary ? (
          <p className="mt-1 pl-4 text-xs text-slate-500">
            {call.result_summary}
          </p>
        ) : null}
      </div>
      <span className="shrink-0 font-mono text-[11px] text-slate-400">
        {formatDuration(call.duration_ms)}
      </span>
    </div>
  );
}

/** Human labels for the AI-assisted stages. */
const AI_STAGE_LABELS: Record<string, string> = {
  ai_execution_plan: "AI execution planning",
  ai_account_mapping: "AI account-mapping investigation",
  ai_anomaly_investigation: "AI anomaly investigation",
  ai_intercompany_matching: "AI intercompany candidate matching",
};

/** Presentation metadata for an AI stage's explicit availability status. */
const AI_STAGE_META: Record<
  string,
  { label: string; badge: string; dot: string }
> = {
  completed: {
    label: "Ran",
    badge: "bg-violet-50 text-violet-700 ring-1 ring-violet-600/20",
    dot: "bg-violet-500",
  },
  not_configured: {
    label: "AI not configured",
    badge: "bg-slate-100 text-slate-600 ring-1 ring-slate-500/20",
    dot: "bg-slate-400",
  },
  failed: {
    label: "Failed",
    badge: "bg-rose-50 text-rose-700 ring-1 ring-rose-600/20",
    dot: "bg-rose-500",
  },
  skipped: {
    label: "Skipped",
    badge: "bg-slate-100 text-slate-600 ring-1 ring-slate-500/20",
    dot: "bg-slate-300",
  },
};

function aiStageLabel(stage: string): string {
  return AI_STAGE_LABELS[stage] ?? titleCase(stage);
}

/** Total latency and token usage across a stage's recorded LLM calls. */
function summarizeCalls(stage: AIStage): {
  calls: number;
  failed: number;
  latencyMs: number;
  tokens: number | null;
} {
  const calls = stage.call_records ?? [];
  const latencyMs = calls.reduce((sum, c) => sum + (c.latency_ms ?? 0), 0);
  const tokens = calls.reduce(
    (sum, c) => sum + (c.total_tokens ?? 0),
    calls.length > 0 ? 0 : 0,
  );
  return {
    calls: calls.length,
    failed: calls.filter((c) => !c.ok).length,
    latencyMs,
    tokens: tokens > 0 ? tokens : null,
  };
}

function AIStageRow({ stage }: { stage: AIStage }) {
  const meta = AI_STAGE_META[stage.status] ?? AI_STAGE_META.skipped;
  const summary = summarizeCalls(stage);
  return (
    <div className="flex items-start justify-between gap-4 py-2.5">
      <div className="flex min-w-0 items-start gap-3">
        <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", meta.dot)} />
        <div className="min-w-0">
          <p className="text-sm font-medium text-slate-800">
            {aiStageLabel(stage.stage)}
          </p>
          {stage.detail ? (
            <p className="mt-0.5 text-xs leading-relaxed text-slate-500">
              {stage.detail}
            </p>
          ) : null}
          {summary.calls > 0 ? (
            <p className="mt-0.5 font-mono text-[11px] text-slate-400">
              {summary.calls} LLM call{summary.calls === 1 ? "" : "s"}
              {summary.failed > 0 ? ` · ${summary.failed} failed` : ""}
              {summary.latencyMs > 0
                ? ` · ${formatDuration(summary.latencyMs)}`
                : ""}
              {summary.tokens ? ` · ${summary.tokens} tokens` : ""}
            </p>
          ) : null}
        </div>
      </div>
      <span
        className={cn(
          "inline-flex shrink-0 rounded-4xl px-2 py-0.5 text-xs font-medium",
          meta.badge,
        )}
      >
        {meta.label}
      </span>
    </div>
  );
}

function MappingProposalRow({ proposal }: { proposal: AIMappingProposal }) {
  return (
    <div className="border-b border-slate-100 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-slate-700">
          Entity {proposal.entity} · account {proposal.local_code}
        </span>
        {proposal.verified ? null : (
          <span className="inline-flex rounded-4xl bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-700 ring-1 ring-rose-600/20">
            Unverified
          </span>
        )}
        <span className="inline-flex rounded-4xl bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
          confidence {proposal.confidence}
        </span>
        {proposal.needs_human_review ? (
          <span className="inline-flex rounded-4xl bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-amber-600/20">
            Human review required
          </span>
        ) : null}
      </div>
      <p className="mt-1.5 text-sm text-slate-700">
        Proposed: group account{" "}
        <code className="font-mono text-xs text-slate-800">
          {proposal.current_group_code ?? "—"}
        </code>
        {proposal.proposed_group_code ? (
          <>
            {" → "}
            <code className="font-mono text-xs text-slate-800">
              {proposal.proposed_group_code}
            </code>
            {proposal.proposed_group_name
              ? ` (${proposal.proposed_group_name})`
              : ""}
          </>
        ) : (
          " (no change proposed)"
        )}
      </p>
      {proposal.reasoning ? (
        <p className="mt-1 text-xs text-slate-500">{proposal.reasoning}</p>
      ) : null}
      {proposal.verification_notes ? (
        <p className="mt-0.5 text-[11px] text-slate-400">
          Verification: {proposal.verification_notes}
        </p>
      ) : null}
      <p className="mt-0.5 text-[11px] text-slate-400">
        Advisory only — the deterministic mapping is never changed automatically.
      </p>
    </div>
  );
}

function MatchProposalRow({ proposal }: { proposal: AIMatchProposal }) {
  return (
    <div className="border-b border-slate-100 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-slate-700">
          rows {proposal.left_row} · {proposal.right_row}
        </span>
        <span
          className={cn(
            "inline-flex rounded-4xl px-2 py-0.5 text-xs font-medium",
            proposal.accepted
              ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/20"
              : "bg-amber-50 text-amber-700 ring-1 ring-amber-600/20",
          )}
        >
          {proposal.accepted ? "Verified" : "Not accepted"}
        </span>
        <span className="inline-flex rounded-4xl bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
          {proposal.match_type.replace(/_/g, " ")}
        </span>
        {proposal.confidence ? (
          <span className="text-xs text-slate-500">
            confidence {proposal.confidence}
          </span>
        ) : null}
      </div>
      {proposal.residual ? (
        <p className="mt-1 text-xs text-slate-600">
          Residual: {formatAmount(proposal.residual)}
          {proposal.reporting_currency
            ? ` ${proposal.reporting_currency}`
            : ""}
        </p>
      ) : null}
      {proposal.reason ? (
        <p className="mt-0.5 text-xs text-slate-500">{proposal.reason}</p>
      ) : null}
    </div>
  );
}

export function ConsolidationDashboard() {
  const [run, setRun] = useState<ConsolidationRun | null>(null);
  const [trace, setTrace] = useState<ConsolidationTrace | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadTrace = useCallback(async (runId: string) => {
    try {
      setTrace(await getConsolidationTrace(runId));
    } catch {
      setTrace(null);
    }
  }, []);

  const runConsolidation = useCallback(async () => {
    setRunning(true);
    setError(null);
    setTrace(null);
    try {
      const started = await startConsolidationRun();
      setRun(started);
      await loadTrace(started.run_id);
      // Reflect the run id in the URL so a refresh reloads the same run.
      const url = new URL(window.location.href);
      url.searchParams.set("run", started.run_id);
      window.history.replaceState(null, "", url.toString());
    } catch (err) {
      if (err instanceof ConsolidationApiError) {
        setError(err.message);
        if (err.body) {
          setRun(err.body);
          await loadTrace(err.body.run_id);
        }
      } else {
        setError(err instanceof Error ? err.message : "Consolidation failed");
      }
    } finally {
      setRunning(false);
    }
  }, [loadTrace]);

  // Refresh path: if the URL carries a run id, load the persisted run.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const runId = params.get("run");
    if (!runId) return;
    let cancelled = false;
    (async () => {
      try {
        const loaded = await getConsolidationRun(runId);
        if (cancelled) return;
        setRun(loaded);
        await loadTrace(runId);
      } catch {
        if (!cancelled) setError(`Run ${runId} could not be loaded`);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadTrace]);

  const readiness = run?.readiness ?? null;
  const entityOrder = ["US", "UK", "IN"];
  const entityKeys = readiness
    ? [
        ...entityOrder.filter((k) => k in readiness.entities_detail),
        ...Object.keys(readiness.entities_detail).filter(
          (k) => !entityOrder.includes(k),
        ),
      ]
    : [];

  return (
    <div className="space-y-6">
      {/* A. Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3.5">
          <span className="mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-lg bg-[#0a1428] text-blue-400 ring-1 ring-slate-900/10">
            <BarChart3 className="size-5" aria-hidden />
          </span>
          <div>
            <div className="flex flex-wrap items-center gap-2.5">
              <h1 className="text-xl font-semibold tracking-tight text-slate-900">
                Group Consolidation
              </h1>
              {run ? <RunStatusBadge status={run.status} /> : null}
            </div>
            <p className="mt-1 text-sm text-slate-500">
              Reporting period{" "}
              <span className="font-medium text-slate-700">
                {readiness ? `FY${readiness.period}` : "FY2024"}
              </span>{" "}
              · Presentation currency{" "}
              <span className="font-medium text-slate-700">
                {readiness?.functional_currency ?? "GBP"}
              </span>
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={runConsolidation}
            disabled={running}
            aria-busy={running}
          >
            {running ? (
              <Loader2 className="size-4 animate-spin" aria-hidden />
            ) : (
              <PlayCircle className="size-4" aria-hidden />
            )}
            {running ? "Running consolidation…" : "Run Consolidation"}
          </Button>
        </div>
      </div>

      {error ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>{error}</span>
        </div>
      ) : null}

      {run ? (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-slate-500">
          <span>
            Run ID <code className="font-mono text-slate-700">{run.run_id}</code>
          </span>
          <span>
            Trace{" "}
            <code className="font-mono text-slate-700">{run.trace_id}</code>
          </span>
          <span>
            Started{" "}
            {run.created_at
              ? new Date(run.created_at).toLocaleString("en-GB")
              : "—"}
          </span>
        </div>
      ) : null}

      {/* B. Consolidated financial results (the actual figures) */}
      {run && run.consolidated_summary ? (
        <Card className="border-slate-300">
          <CardHeader>
            <CardTitle>Consolidated results (GBP)</CardTitle>
            <CardDescription>
              Headline figures for the group, produced by the deterministic
              accounting engine.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {(
              [
                ["Group revenue", run.consolidated_summary.group_revenue],
                ["Group profit", run.consolidated_summary.group_profit],
                ["Total assets", run.consolidated_summary.total_assets],
                ["Total equity", run.consolidated_summary.total_equity],
              ] as const
            ).map(([label, value]) => (
              <div
                key={label}
                className="rounded-lg bg-[#0a1428] px-3 py-3 text-white"
              >
                <p className="text-xs font-medium tracking-wide text-slate-300 uppercase">
                  {label}
                </p>
                <p className="mt-1 font-mono text-lg font-semibold">
                  {formatAmount(value)}
                </p>
                <p className="text-xs text-slate-400">
                  {run.consolidated_summary?.currency ?? "GBP"}
                </p>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      {run?.control_figures ? (
        <ControlFiguresPanel figures={run.control_figures} />
      ) : null}

      {run?.integrity_checks && run.integrity_checks.length > 0 ? (
        <IntegrityChecksPanel checks={run.integrity_checks} />
      ) : null}

      {run?.statements ? <StatementsPanel statements={run.statements} /> : null}

      {run?.trial_balance && run.trial_balance.length > 0 ? (
        <TrialBalancePanel rows={run.trial_balance} />
      ) : null}

      {run?.eliminations && run.eliminations.length > 0 ? (
        <EliminationsPanel entries={run.eliminations} />
      ) : null}

      {/* C. Entity overview */}
      <Card>
        <CardHeader>
          <CardTitle>Entities</CardTitle>
          <CardDescription>
            Group entities with their functional currency and actual
            validation / mapping status from the backend run.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {readiness && entityKeys.length > 0 ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {entityKeys.map((key) => (
                <EntityCard
                  key={key}
                  entity={readiness.entities_detail[key]}
                />
              ))}
            </div>
          ) : (
            <p className="text-sm text-slate-500">
              Run a consolidation to load entity statuses.
            </p>
          )}
        </CardContent>
      </Card>

      {/* C + D. Execution status and stage results */}
      <Card>
        <CardHeader>
          <CardTitle>Consolidation stages</CardTitle>
          <CardDescription>
            Each stage reflects what the backend actually executed. Blocked
            stages require accounting services that are not yet implemented.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {running ? (
            <div className="flex items-center gap-2 py-6 text-sm text-slate-500">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              Executing the shared agent…
            </div>
          ) : readiness ? (
            <>
              <div className="divide-y divide-slate-100">
                {readiness.stages.map((stage) => (
                  <StageRow key={stage.stage} stage={stage} />
                ))}
              </div>
              {!readiness.complete ? (
                <p className="mt-4 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
                  Consolidation is not complete: {readiness.blocked_stages.length}{" "}
                  required stage
                  {readiness.blocked_stages.length === 1 ? "" : "s"} remain
                  blocked and {readiness.pending_stages.length} pending.
                </p>
              ) : null}
            </>
          ) : (
            <p className="py-2 text-sm text-slate-500">
              No run yet. Use{" "}
              <span className="font-medium text-slate-700">
                Run Consolidation
              </span>{" "}
              to execute the FY2024 group consolidation.
            </p>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* E. Issues */}
        <Card>
          <CardHeader>
            <CardTitle>Issues</CardTitle>
            <CardDescription>
              Accounting inconsistencies flagged by deterministic checks.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {run && run.issues.length > 0 ? (
              <div>
                {run.issues.map((issue, index) => (
                  <IssueRow key={`${issue.code}-${index}`} issue={issue} />
                ))}
              </div>
            ) : run ? (
              <p className="text-sm text-slate-500">No issues reported.</p>
            ) : (
              <p className="text-sm text-slate-500">No run yet.</p>
            )}
          </CardContent>
        </Card>

        {/* F. Agent activity */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Bot className="size-4 text-slate-400" aria-hidden />
              Agent activity
            </CardTitle>
            <CardDescription>
              MCP tool calls recorded during the run, with status and duration.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {run && run.tool_calls.length > 0 ? (
              <div>
                {run.tool_calls.map((call, index) => (
                  <ToolCallRow key={`${call.name}-${index}`} call={call} />
                ))}
              </div>
            ) : run ? (
              <p className="text-sm text-slate-500">No tool calls recorded.</p>
            ) : (
              <p className="text-sm text-slate-500">No run yet.</p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* G. AI-assisted activity */}
      {run ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Brain className="size-4 text-violet-500" aria-hidden />
              AI-assisted reasoning
            </CardTitle>
            <CardDescription>
              LLM-assisted stages run alongside the deterministic accounting.
              The LLM plans, investigates and proposes; Python performs every
              calculation and verifies every proposal. Nothing here replaces the
              deterministic result.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {!run.ai_configured ? (
              <div className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
                <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
                <span>
                  AI is not available because no LLM credentials are configured.
                  Deterministic consolidation still ran; no AI activity is
                  reported.
                </span>
              </div>
            ) : null}

            {run.ai_stages.length > 0 ? (
              <div className="divide-y divide-slate-100">
                {run.ai_stages.map((stage) => (
                  <AIStageRow key={stage.stage} stage={stage} />
                ))}
              </div>
            ) : (
              <p className="text-sm text-slate-500">
                No AI stages recorded for this run.
              </p>
            )}

            {run.ai_reasoning.execution_plan?.summary ? (
              <div className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-700">
                <span className="font-medium text-slate-800">Plan: </span>
                {run.ai_reasoning.execution_plan.summary}
              </div>
            ) : null}

            {run.ai_reasoning.mapping_proposals &&
            run.ai_reasoning.mapping_proposals.length > 0 ? (
              <div>
                <p className="mb-1 text-xs font-semibold tracking-wide text-slate-500 uppercase">
                  Proposed mapping decisions
                </p>
                {run.ai_reasoning.mapping_proposals.map((proposal, index) => (
                  <MappingProposalRow
                    key={`${proposal.entity}-${proposal.local_code}-${index}`}
                    proposal={proposal}
                  />
                ))}
              </div>
            ) : null}

            {run.ai_reasoning.intercompany_matching?.proposals &&
            run.ai_reasoning.intercompany_matching.proposals.length > 0 ? (
              <div>
                <p className="mb-1 text-xs font-semibold tracking-wide text-slate-500 uppercase">
                  Intercompany match proposals (deterministically verified)
                </p>
                {run.ai_reasoning.intercompany_matching.proposals.map(
                  (proposal, index) => (
                    <MatchProposalRow
                      key={`${proposal.left_row}-${proposal.right_row}-${index}`}
                      proposal={proposal}
                    />
                  ),
                )}
                {run.ai_reasoning.intercompany_matching.unmatched_notes &&
                run.ai_reasoning.intercompany_matching.unmatched_notes.length >
                  0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-slate-500">
                    {run.ai_reasoning.intercompany_matching.unmatched_notes.map(
                      (note, index) => (
                        <li key={index}>{note}</li>
                      ),
                    )}
                  </ul>
                ) : null}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {/* Trace events */}
      {trace && trace.events.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <RefreshCw className="size-4 text-slate-400" aria-hidden />
              Execution trace
            </CardTitle>
            <CardDescription>
              Persisted trace events for this run ({trace.trace_id}).
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ol className="space-y-1.5">
              {trace.events.map((event) => (
                <li
                  key={event.seq}
                  className="flex items-start gap-3 text-xs text-slate-600"
                >
                  <span className="w-6 shrink-0 text-right font-mono text-slate-400">
                    {event.seq}
                  </span>
                  <span className="mt-1 size-1.5 shrink-0 rounded-full bg-slate-300" />
                  <span className="font-mono text-slate-700">
                    {event.event}
                  </span>
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
