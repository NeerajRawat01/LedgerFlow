/** Callers for the consolidation-run backend endpoints. */

import { apiUrl } from "@/lib/api";
import type {
  ConsolidationRun,
  ConsolidationRunSummary,
  ConsolidationTrace,
} from "@/types/consolidation";

/** Error thrown when the backend returns a non-OK response. */
export class ConsolidationApiError extends Error {
  status: number;
  /** The parsed body when the backend returned a structured (non-2xx) result. */
  body: ConsolidationRun | null;

  constructor(message: string, status: number, body: ConsolidationRun | null) {
    super(message);
    this.name = "ConsolidationApiError";
    this.status = status;
    this.body = body;
  }
}

async function parse<T>(response: Response): Promise<T> {
  const text = await response.text();
  return (text ? JSON.parse(text) : null) as T;
}

/**
 * Start a FY2024 consolidation run. Resolves with the completed run.
 *
 * A 400/500 response still carries a structured run body, so the caller can
 * render the typed failure; it is surfaced as a ConsolidationApiError with
 * that body attached.
 */
export async function startConsolidationRun(): Promise<ConsolidationRun> {
  const response = await fetch(apiUrl("/api/consolidations/runs"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ period: "FY2024", scope: "group" }),
    cache: "no-store",
  });
  const body = await parse<ConsolidationRun>(response);
  if (!response.ok) {
    throw new ConsolidationApiError(
      body?.summary ?? `Consolidation run failed (HTTP ${response.status})`,
      response.status,
      body,
    );
  }
  return body;
}

/** Fetch the persisted run history, newest first (compact summaries). */
export async function listConsolidationRuns(): Promise<
  ConsolidationRunSummary[]
> {
  const response = await fetch(apiUrl("/api/consolidations/runs"), {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ConsolidationApiError(
      `Run history could not be loaded (HTTP ${response.status})`,
      response.status,
      null,
    );
  }
  const body = await parse<{ runs: ConsolidationRunSummary[] }>(response);
  return body?.runs ?? [];
}

/** Fetch a previously executed run by id (the refresh path). */export async function getConsolidationRun(
  runId: string,
): Promise<ConsolidationRun> {
  const response = await fetch(
    apiUrl(`/api/consolidations/runs/${encodeURIComponent(runId)}`),
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new ConsolidationApiError(
      `Run ${runId} not found (HTTP ${response.status})`,
      response.status,
      null,
    );
  }
  return parse<ConsolidationRun>(response);
}

/** Fetch the execution trace (events + tool calls) for a run. */
export async function getConsolidationTrace(
  runId: string,
): Promise<ConsolidationTrace> {
  const response = await fetch(
    apiUrl(`/api/consolidations/runs/${encodeURIComponent(runId)}/trace`),
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new ConsolidationApiError(
      `Trace for run ${runId} not found (HTTP ${response.status})`,
      response.status,
      null,
    );
  }
  return parse<ConsolidationTrace>(response);
}
