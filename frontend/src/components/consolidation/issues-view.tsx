"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ConsolidationApiError,
  getConsolidationRun,
  listConsolidationRuns,
} from "@/lib/consolidation";
import type { Issue } from "@/types/consolidation";
import { cn } from "@/lib/utils";

function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatAmount(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return numeric.toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

const SEVERITY_ORDER: Record<string, number> = {
  error: 0,
  warning: 1,
  info: 2,
};

function severityTone(severity: string): string {
  const s = severity.toLowerCase();
  if (s === "error") return "bg-rose-50 text-rose-700 ring-1 ring-rose-600/20";
  if (s === "warning")
    return "bg-amber-50 text-amber-700 ring-1 ring-amber-600/20";
  return "bg-slate-100 text-slate-700 ring-1 ring-slate-600/20";
}

/** Load the issues from the most recent persisted run, most severe first. */
async function fetchLatestIssues(): Promise<{
  runId: string | null;
  issues: Issue[];
}> {
  const runs = await listConsolidationRuns();
  if (runs.length === 0) return { runId: null, issues: [] };
  const run = await getConsolidationRun(runs[0].run_id);
  const issues = [...run.issues].sort(
    (a, b) =>
      (SEVERITY_ORDER[a.severity.toLowerCase()] ?? 9) -
      (SEVERITY_ORDER[b.severity.toLowerCase()] ?? 9),
  );
  return { runId: run.run_id, issues };
}

/** Client view of the issues raised by the latest consolidation run. */
export function IssuesView() {
  const [issues, setIssues] = useState<Issue[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const result = await fetchLatestIssues();
      setRunId(result.runId);
      setIssues(result.issues);
    } catch (err) {
      setError(
        err instanceof ConsolidationApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Issues could not be loaded",
      );
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const result = await fetchLatestIssues();
        if (cancelled) return;
        setRunId(result.runId);
        setIssues(result.issues);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof Error ? err.message : "Issues could not be loaded",
        );
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const counts = issues.reduce<Record<string, number>>((acc, issue) => {
    const key = issue.severity.toLowerCase();
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle>Issues</CardTitle>
            <CardDescription>
              {runId
                ? `Accounting findings from run ${runId} (latest).`
                : "Accounting inconsistencies flagged by the deterministic checks."}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {Object.entries(counts).map(([severity, count]) => (
              <span
                key={severity}
                className={cn(
                  "inline-flex items-center gap-1 rounded-4xl px-2 py-0.5 text-xs font-medium",
                  severityTone(severity),
                )}
              >
                {count} {titleCase(severity)}
              </span>
            ))}
            <Button variant="outline" size="sm" onClick={() => void refresh()}>
              <RefreshCw className="size-4" aria-hidden />
              Refresh
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {!loaded ? (
          <div className="flex items-center gap-2 py-6 text-sm text-slate-500">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            Loading issues…
          </div>
        ) : error ? (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
          >
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
        ) : issues.length === 0 ? (
          <p className="py-2 text-sm text-slate-500">
            {runId
              ? "No issues reported for the latest run."
              : "No run yet — start a consolidation to populate the issue queue."}
          </p>
        ) : (
          <div className="divide-y divide-slate-100">
            {issues.map((issue, index) => (
              <div key={`${issue.code}-${index}`} className="py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={cn(
                      "inline-flex rounded-4xl px-2 py-0.5 text-xs font-medium",
                      severityTone(issue.severity),
                    )}
                  >
                    {titleCase(issue.severity)}
                  </span>
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
                    {issue.code}
                  </code>
                  {issue.entity ? (
                    <span className="text-xs text-slate-500">
                      Entity {issue.entity}
                    </span>
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
                {typeof issue.details?.affected_amount === "string" ||
                typeof issue.details?.affected_amount === "number" ? (
                  <p className="mt-1 text-xs text-slate-500">
                    Affected amount:{" "}
                    {formatAmount(
                      issue.details.affected_amount as string | number,
                    )}
                  </p>
                ) : null}
                {typeof issue.details?.treatment === "string" ? (
                  <p className="mt-0.5 text-xs text-slate-500">
                    Treatment: {issue.details.treatment}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
