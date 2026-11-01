"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw } from "lucide-react";

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
  listConsolidationRuns,
} from "@/lib/consolidation";
import type { ConsolidationRunSummary } from "@/types/consolidation";
import { cn } from "@/lib/utils";

function formatAmount(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  return numeric.toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function statusTone(status: string): string {
  if (status === "completed")
    return "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/20";
  if (status === "failed")
    return "bg-rose-50 text-rose-700 ring-1 ring-rose-600/20";
  return "bg-slate-100 text-slate-700 ring-1 ring-slate-600/20";
}

/** Client view of the persisted consolidation run history. */
export function RunsView() {
  const [runs, setRuns] = useState<ConsolidationRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRuns(await listConsolidationRuns());
    } catch (err) {
      setError(
        err instanceof ConsolidationApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Run history could not be loaded",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await listConsolidationRuns();
        if (!cancelled) setRuns(loaded);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof Error
            ? err.message
            : "Run history could not be loaded",
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle>Run history</CardTitle>
            <CardDescription>
              Every persisted FY2024 consolidation run, newest first.
            </CardDescription>
          </div>
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw className="size-4" aria-hidden />
            Refresh
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="flex items-center gap-2 py-6 text-sm text-slate-500">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            Loading runs…
          </div>
        ) : error ? (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
          >
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
        ) : runs.length === 0 ? (
          <p className="py-2 text-sm text-slate-500">
            No runs recorded yet. Start one from the{" "}
            <Link
              href="/dashboard/consolidation"
              className="font-medium text-slate-700 underline-offset-2 hover:underline"
            >
              Consolidation
            </Link>{" "}
            page.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs tracking-wide text-slate-500 uppercase">
                  <th className="py-2 pr-3 font-medium">Run</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 pr-3 font-medium">Period</th>
                  <th className="py-2 pr-3 text-right font-medium">Revenue</th>
                  <th className="py-2 pr-3 text-right font-medium">Profit</th>
                  <th className="py-2 pr-3 text-right font-medium">Issues</th>
                  <th className="py-2 pr-3 font-medium">When</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.run_id}
                    className="border-b border-slate-100 last:border-0"
                  >
                    <td className="py-2 pr-3">
                      <Link
                        href={`/dashboard/reports?run=${encodeURIComponent(run.run_id)}`}
                        className="font-mono text-xs text-slate-700 underline-offset-2 hover:underline"
                      >
                        {run.run_id}
                      </Link>
                    </td>
                    <td className="py-2 pr-3">
                      <span
                        className={cn(
                          "inline-flex items-center gap-1 rounded-4xl px-2 py-0.5 text-xs font-medium",
                          statusTone(run.status),
                        )}
                      >
                        {run.complete ? (
                          <CheckCircle2 className="size-3" aria-hidden />
                        ) : null}
                        {run.status}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-slate-700">
                      {run.period ?? "—"}
                    </td>
                    <td className="py-2 pr-3 text-right font-mono text-slate-900">
                      {formatAmount(run.group_revenue)}
                    </td>
                    <td className="py-2 pr-3 text-right font-mono text-slate-900">
                      {formatAmount(run.group_profit)}
                    </td>
                    <td className="py-2 pr-3 text-right text-slate-700">
                      {run.issue_count}
                    </td>
                    <td className="py-2 pr-3 text-xs text-slate-500">
                      {run.created_at
                        ? new Date(run.created_at).toLocaleString("en-GB")
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
