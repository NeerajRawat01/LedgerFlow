"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Download,
  FileText,
  Loader2,
  RefreshCw,
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
  listConsolidationRuns,
} from "@/lib/consolidation";
import type { ConsolidationRun } from "@/types/consolidation";
import { apiUrl } from "@/lib/api";

/** Resolve the latest persisted run (or the one named by ``?run=``). */
async function fetchReportRun(): Promise<ConsolidationRun | null> {
  const params = new URLSearchParams(window.location.search);
  const runId = params.get("run");
  if (runId) {
    return getConsolidationRun(runId);
  }
  const runs = await listConsolidationRuns();
  if (runs.length === 0) return null;
  return getConsolidationRun(runs[0].run_id);
}

/**
 * Client view of the consolidated group statements.
 *
 * Loads the most recent persisted run (or the one named by ``?run=``) and
 * renders only figures the deterministic engine already produced — no
 * calculation happens here.
 */
export function ReportsView() {
  const [run, setRun] = useState<ConsolidationRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRun(await fetchReportRun());
    } catch (err) {
      if (err instanceof ConsolidationApiError) {
        setError(err.message);
        setRun(err.body ?? null);
      } else {
        setError(
          err instanceof Error ? err.message : "Reports could not be loaded",
        );
      }
    } finally {
      setLoading(false);
    }
  }, []);

  // Bootstrap: fetch after an await tick so no state is set synchronously.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await fetchReportRun();
        if (!cancelled) setRun(loaded);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof Error ? err.message : "Reports could not be loaded",
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-4 py-6 text-sm text-slate-500">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        Loading the latest consolidation run…
      </div>
    );
  }

  if (!run) {
    return (
      <Card className="border-dashed">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm font-semibold text-slate-700">
            <FileText className="size-4 text-slate-500" aria-hidden />
            No reports yet
          </CardTitle>
          <CardDescription>
            {error ??
              "Run a consolidation from the Consolidation page to produce the group statements."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw className="size-4" aria-hidden />
            Refresh
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-slate-500">
          <span>
            Run <code className="font-mono text-slate-700">{run.run_id}</code>
          </span>
          <span>
            {run.created_at
              ? new Date(run.created_at).toLocaleString("en-GB")
              : "—"}
          </span>
          <Badge variant="secondary" className="font-mono text-[11px] uppercase">
            {run.status}
          </Badge>
        </div>
        <Button variant="outline" size="sm" onClick={() => void refresh()}>
          <RefreshCw className="size-4" aria-hidden />
          Refresh
        </Button>
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

      {run.control_figures ? (
        <ControlFiguresPanel figures={run.control_figures} />
      ) : null}

      {run.integrity_checks.length > 0 ? (
        <IntegrityChecksPanel checks={run.integrity_checks} />
      ) : null}

      {run.statements ? <StatementsPanel statements={run.statements} /> : null}

      {run.trial_balance.length > 0 ? (
        <TrialBalancePanel rows={run.trial_balance} />
      ) : null}

      {run.eliminations.length > 0 ? (
        <EliminationsPanel entries={run.eliminations} />
      ) : null}

      {run.output_files.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Download className="size-4 text-slate-400" aria-hidden />
              Generated deliverables
            </CardTitle>
            <CardDescription>
              The seven files written to the output directory by the engine.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {run.output_files.map((file) => (
              <a
                key={file.file}
                href={apiUrl(
                  `/api/consolidations/output/${encodeURIComponent(file.file)}`,
                )}
                className="inline-flex items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900"
              >
                <Download className="size-3.5" aria-hidden />
                {file.file}
              </a>
            ))}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
