/** Types mirroring the consolidation-run API responses. */

export type StageStatus = "completed" | "blocked" | "pending" | "failed";

export interface StageOutcome {
  stage: string;
  status: StageStatus;
  detail: string;
  required_for_complete: boolean;
}

export interface EntityStatus {
  entity: string;
  functional_currency: string;
  source_file: string | null;
  row_count: number;
  balanced: boolean;
  debit_total: string;
  credit_total: string;
  difference: string;
  mapped_row_count: number;
  unmapped_row_count: number;
  error_count: number;
  warning_count: number;
}

export interface Readiness {
  period: string;
  scope: string;
  entities: string[];
  functional_currency: string;
  complete: boolean;
  stages: StageOutcome[];
  completed_stages: string[];
  blocked_stages: string[];
  pending_stages: string[];
  missing_required_stages: string[];
  entities_detail: Record<string, EntityStatus>;
}

export interface Issue {
  code: string;
  severity: string;
  message: string;
  entity?: string | null;
  source_file?: string | null;
  source_row?: number | null;
  account_code?: string | null;
  details?: Record<string, unknown>;
}

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  result_summary?: string | null;
  error?: string | null;
  duration_ms?: number | null;
}

export interface AgentError {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

/** Availability status for one AI-assisted stage. */
export type AIStageStatus =
  | "completed"
  | "not_configured"
  | "failed"
  | "skipped";

export interface AICallRecord {
  template_id: string;
  prompt_version: string;
  model: string | null;
  ok: boolean;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  error: string | null;
  structured_output: Record<string, unknown> | null;
}

export interface AIStage {
  stage: string;
  status: AIStageStatus | string;
  detail: string;
  call_records: AICallRecord[];
}

/** One LLM-proposed intercompany match, with the deterministic verdict. */
export interface AIMatchProposal {
  left_row: number;
  right_row: number;
  accepted: boolean;
  match_type: string;
  residual: string | null;
  reporting_currency?: string | null;
  reason: string;
  confidence: string | null;
  reasoning?: string;
}

/** A mapping proposal for the UK 2310 anomaly (never auto-applied). */
export interface AIMappingProposal {
  entity: string;
  local_code: string;
  local_name: string | null;
  current_group_code: string | null;
  proposed_group_code: string | null;
  proposed_group_name: string | null;
  confidence: string;
  reasoning: string;
  needs_human_review: boolean;
  verified: boolean;
  verification_notes: string;
}

export interface AIReasoning {
  execution_plan?: {
    period?: string | null;
    entities?: string[];
    presentation_currency?: string | null;
    workflow?: string;
    summary?: string;
    steps?: {
      stage: string;
      rationale: string;
      executable: boolean;
      requires_approval: boolean;
    }[];
    approval_stages?: string[];
  };
  mapping_proposals?: AIMappingProposal[];
  anomaly_investigation?: {
    summary?: string;
    findings?: unknown[];
    note?: string;
  };
  intercompany_matching?: {
    summary?: string;
    unmatched_notes?: string[];
    proposals?: AIMatchProposal[];
    accepted_count?: number;
    proposed_count?: number;
  };
}

export interface ConsolidationRun {
  run_id: string;
  trace_id: string;
  mode: string;
  status: string;
  summary: string | null;
  skill_name: string | null;
  created_at: string;
  readiness: Readiness | null;
  issues: Issue[];
  tool_calls: ToolCall[];
  errors: AgentError[];
  warnings: string[];
  ai_configured: boolean;
  ai_stages: AIStage[];
  ai_reasoning: AIReasoning;
  consolidated_summary: ConsolidatedSummary | null;
  control_figures: ControlFigures | null;
  statements: Statements | null;
  output_files: OutputFile[];
  trial_balance: TrialBalanceRow[];
  eliminations: EliminationEntry[];
  integrity_checks: IntegrityCheck[];
}

/** One consolidated trial-balance row (GBP) with source traceability. */
export interface TrialBalanceSourceRow {
  entity: string | null;
  source_file: string | null;
  source_row: number | null;
}

export interface TrialBalanceRow {
  group_account_code: string;
  group_account_name: string;
  account_type: string;
  statement: string;
  gbp_debit: string;
  gbp_credit: string;
  gbp_net: string;
  source_entities: string[];
  source_rows: TrialBalanceSourceRow[];
}

/** One elimination journal line (GBP) with its source reference. */
export interface EliminationEntry {
  journal_id: string;
  kind: string;
  reference: string;
  group_account_code: string;
  group_account_name: string;
  entity: string;
  gbp_debit: string;
  gbp_credit: string;
  reason: string;
}

/** One coded integrity check (must pass before outputs are relied on). */
export interface IntegrityCheck {
  check: string;
  passed: boolean;
  detail: string;
}

/** The four headline figures shown on the dashboard (GBP, 2 dp strings). */
export interface ConsolidatedSummary {
  group_revenue: string | null;
  group_profit: string | null;
  total_assets: string | null;
  total_equity: string | null;
  currency: string;
}

/** The twelve control figures (assignment §4). */
export interface ControlFigures {
  group_revenue: number | null;
  group_profit: number | null;
  profit_owners: number | null;
  profit_nci: number | null;
  nci_closing: number | null;
  goodwill_closing: number | null;
  translation_reserve_movement: number | null;
  total_assets: number | null;
  total_equity: number | null;
  intercompany_difference:
    | {
        value: number | null;
        causes: Record<string, number>;
        matched_pairs?: number;
        unmatched_items?: number;
      }
    | null;
  sgd_bank_gbp: { value: number | null; reason: string } | null;
  journals: {
    posted: number;
    rejected: number;
    held_for_review: number;
  } | null;
}

/** One line of a group statement. */
export interface StatementLine {
  group_account_code: string | null;
  label: string;
  amount: number | null;
  reason?: string | null;
  note?: string | null;
}

/** One statement section (assets / liabilities / equity / P&L). */
export interface StatementSection {
  title: string;
  lines: StatementLine[];
  total: number | null;
  total_reason?: string | null;
}

/** The three group statements. */
export interface Statements {
  presentation_currency: string;
  period: string;
  basis: string;
  balance_sheet: StatementSection[];
  profit_and_loss: StatementSection[];
  changes_in_equity: StatementSection[];
  checks: { check: string; passed: boolean; detail: string }[];
}

/** One written deliverable under /output. */
export interface OutputFile {
  file: string;
  path: string;
  bytes: number;
}

/** One compact row of the persisted run history. */
export interface ConsolidationRunSummary {
  run_id: string;
  trace_id: string;
  status: string;
  summary: string | null;
  period: string | null;
  created_at: string;
  updated_at: string;
  issue_count: number;
  complete: boolean;
  group_revenue: string | null;
  group_profit: string | null;
  total_assets: string | null;
  total_equity: string | null;
}

export interface TraceEvent {
  ts: string;
  seq: number;
  trace_id: string;
  event: string;
  payload: Record<string, unknown>;
}

export interface ConsolidationTrace {
  run_id: string;
  trace_id: string;
  events: TraceEvent[];
  tool_calls: ToolCall[];
}
