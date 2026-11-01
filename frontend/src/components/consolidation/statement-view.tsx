"use client";

import {
  Calculator,
  CheckCircle2,
  FileSpreadsheet,
  Layers,
  Scale,
  ShieldAlert,
  Shuffle,
} from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type {
  ControlFigures,
  EliminationEntry,
  IntegrityCheck,
  StatementSection,
  Statements,
  TrialBalanceRow,
} from "@/types/consolidation";
import { cn } from "@/lib/utils";

/** Format a numeric string or number as a 2-dp GBP amount. */
export function gbp(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return numeric.toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function ControlFigure({
  label,
  value,
  reason,
}: {
  label: string;
  value: number | null | undefined;
  reason?: string | null;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5">
      <p className="text-xs font-medium tracking-wide text-slate-500 uppercase">
        {label}
      </p>
      {value === null || value === undefined ? (
        <p className="mt-1 text-sm font-semibold text-amber-700">
          null
          {reason ? (
            <span className="mt-0.5 block text-xs font-normal text-slate-500">
              {reason}
            </span>
          ) : null}
        </p>
      ) : (
        <p className="mt-1 font-mono text-base font-semibold text-slate-900">
          {gbp(value)}
        </p>
      )}
    </div>
  );
}

/** The twelve control figures (assignment §4), with honest nulls. */
export function ControlFiguresPanel({ figures }: { figures: ControlFigures }) {
  const sgd = figures.sgd_bank_gbp;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Calculator className="size-4 text-slate-400" aria-hidden />
          Control figures
        </CardTitle>
        <CardDescription>
          The twelve figures from the assignment appendix, computed by the
          deterministic engine (GBP, 2 dp).
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <ControlFigure label="Group revenue" value={figures.group_revenue} />
        <ControlFigure label="Group profit" value={figures.group_profit} />
        <ControlFigure
          label="Profit — owners"
          value={figures.profit_owners}
        />
        <ControlFigure
          label="Profit — NCI"
          value={figures.profit_nci}
        />
        <ControlFigure label="NCI at 31 Dec" value={figures.nci_closing} />
        <ControlFigure
          label="Goodwill at 31 Dec"
          value={figures.goodwill_closing}
        />
        <ControlFigure
          label="Translation reserve movement"
          value={figures.translation_reserve_movement}
        />
        <ControlFigure label="Total assets" value={figures.total_assets} />
        <ControlFigure label="Total equity" value={figures.total_equity} />
        {figures.intercompany_difference ? (
          <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5">
            <p className="text-xs font-medium tracking-wide text-slate-500 uppercase">
              Intercompany difference
            </p>
            <p className="mt-1 font-mono text-base font-semibold text-slate-900">
              {gbp(figures.intercompany_difference.value)}
            </p>
            <ul className="mt-1 space-y-0.5 text-xs text-slate-500">
              {Object.entries(
                figures.intercompany_difference.causes ?? {},
              ).map(([cause, amount]) => (
                <li key={cause}>
                  {cause.replace(/_/g, " ")}: {gbp(amount)}
                </li>
              ))}
              <li>
                matched pairs: {figures.intercompany_difference.matched_pairs ?? 0}
                {" · "}unmatched:{" "}
                {figures.intercompany_difference.unmatched_items ?? 0}
              </li>
            </ul>
          </div>
        ) : null}
        <ControlFigure
          label="UK SGD bank (GBP)"
          value={sgd?.value ?? null}
          reason={sgd?.reason}
        />
        <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5">
          <p className="text-xs font-medium tracking-wide text-slate-500 uppercase">
            Journals
          </p>
          {figures.journals ? (
            <p className="mt-1 text-sm text-slate-800">
              <span className="font-semibold text-emerald-700">
                {figures.journals.posted} posted
              </span>
              {" · "}
              <span className="font-semibold text-rose-700">
                {figures.journals.rejected} rejected
              </span>
              {" · "}
              <span className="font-semibold text-amber-700">
                {figures.journals.held_for_review} held
              </span>
            </p>
          ) : (
            <p className="mt-1 text-sm text-slate-500">—</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function StatementSectionTable({ section }: { section: StatementSection }) {
  return (
    <div>
      <p className="mb-1.5 text-xs font-semibold tracking-wide text-slate-500 uppercase">
        {section.title}
      </p>
      <table className="w-full text-sm">
        <tbody>
          {section.lines.map((line, index) => (
            <tr
              key={`${line.label}-${index}`}
              className="border-b border-slate-100 last:border-0"
            >
              <td className="py-1.5 pr-3 text-slate-700">
                {line.group_account_code ? (
                  <span className="mr-2 font-mono text-xs text-slate-400">
                    {line.group_account_code}
                  </span>
                ) : null}
                {line.label}
              </td>
              <td className="py-1.5 text-right font-mono text-slate-900">
                {gbp(line.amount)}
              </td>
            </tr>
          ))}
          {section.total !== null && section.total !== undefined ? (
            <tr className="border-t-2 border-slate-200 font-semibold">
              <td className="py-1.5 pr-3 text-slate-900">Total</td>
              <td className="py-1.5 text-right font-mono text-slate-900">
                {gbp(section.total)}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

/** The three group statements. */
export function StatementsPanel({ statements }: { statements: Statements }) {
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Scale className="size-4 text-slate-400" aria-hidden />
            Balance sheet
          </CardTitle>
          <CardDescription>
            {statements.presentation_currency} · {statements.period}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {statements.balance_sheet.map((section) => (
            <StatementSectionTable key={section.title} section={section} />
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Layers className="size-4 text-slate-400" aria-hidden />
            Profit and loss
          </CardTitle>
          <CardDescription>
            {statements.presentation_currency} · {statements.period}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {statements.profit_and_loss.map((section) => (
            <StatementSectionTable key={section.title} section={section} />
          ))}
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileSpreadsheet className="size-4 text-slate-400" aria-hidden />
            Changes in equity
          </CardTitle>
          <CardDescription>
            Including non-controlling interest and the translation reserve.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {statements.changes_in_equity.map((section) => (
            <StatementSectionTable key={section.title} section={section} />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

/** The consolidated trial balance with source traceability. */
export function TrialBalancePanel({ rows }: { rows: TrialBalanceRow[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileSpreadsheet className="size-4 text-slate-400" aria-hidden />
          Consolidated trial balance (GBP)
        </CardTitle>
        <CardDescription>
          Every balance with the source rows that produced it. Debits equal
          credits.
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-left text-xs tracking-wide text-slate-500 uppercase">
              <th className="py-2 pr-3 font-medium">Code</th>
              <th className="py-2 pr-3 font-medium">Account</th>
              <th className="py-2 pr-3 text-right font-medium">Debit</th>
              <th className="py-2 pr-3 text-right font-medium">Credit</th>
              <th className="py-2 pr-3 font-medium">Sources</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.group_account_code}
                className="border-b border-slate-100 last:border-0"
              >
                <td className="py-1.5 pr-3 font-mono text-xs text-slate-500">
                  {row.group_account_code}
                </td>
                <td className="py-1.5 pr-3 text-slate-800">
                  {row.group_account_name}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono text-slate-900">
                  {Number(row.gbp_debit) ? gbp(row.gbp_debit) : "—"}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono text-slate-900">
                  {Number(row.gbp_credit) ? gbp(row.gbp_credit) : "—"}
                </td>
                <td className="py-1.5 pr-3 text-xs text-slate-500">
                  {row.source_rows.length > 0
                    ? row.source_rows
                        .map((s) =>
                          s.source_row
                            ? `${s.entity}:${s.source_file}#${s.source_row}`
                            : `${s.entity}:${s.source_file}`,
                        )
                        .join("; ")
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

/** Every elimination entry, grouped by journal. */
export function EliminationsPanel({
  entries,
}: {
  entries: EliminationEntry[];
}) {
  const byJournal = entries.reduce<Record<string, EliminationEntry[]>>(
    (acc, entry) => {
      (acc[entry.journal_id] ??= []).push(entry);
      return acc;
    },
    {},
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Shuffle className="size-4 text-slate-400" aria-hidden />
          Eliminations
        </CardTitle>
        <CardDescription>
          Every elimination journal, with its account lines and reason. Each
          journal balances (debits = credits).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 overflow-x-auto">
        {Object.entries(byJournal).map(([journalId, lines]) => {
          const debit = lines.reduce((s, l) => s + Number(l.gbp_debit), 0);
          const credit = lines.reduce((s, l) => s + Number(l.gbp_credit), 0);
          const balanced = Math.abs(debit - credit) < 0.005;
          return (
            <div key={journalId}>
              <div className="mb-1 flex items-center gap-2">
                <span className="font-mono text-xs font-semibold text-slate-700">
                  {journalId}
                </span>
                <span
                  className={cn(
                    "rounded-full px-2 py-0.5 text-xs font-medium",
                    balanced
                      ? "bg-emerald-50 text-emerald-700"
                      : "bg-rose-50 text-rose-700",
                  )}
                >
                  {balanced ? "balanced" : "unbalanced"}
                </span>
              </div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-left text-xs tracking-wide text-slate-500 uppercase">
                    <th className="py-1.5 pr-3 font-medium">Code</th>
                    <th className="py-1.5 pr-3 font-medium">Account</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Debit</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Credit</th>
                    <th className="py-1.5 pr-3 font-medium">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((line, index) => (
                    <tr
                      key={`${line.group_account_code}-${index}`}
                      className="border-b border-slate-100 last:border-0"
                    >
                      <td className="py-1.5 pr-3 font-mono text-xs text-slate-500">
                        {line.group_account_code}
                      </td>
                      <td className="py-1.5 pr-3 text-slate-800">
                        {line.group_account_name}
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono text-slate-900">
                        {Number(line.gbp_debit) ? gbp(line.gbp_debit) : "—"}
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono text-slate-900">
                        {Number(line.gbp_credit) ? gbp(line.gbp_credit) : "—"}
                      </td>
                      <td className="py-1.5 pr-3 text-xs text-slate-500">
                        {line.reason}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

/** The coded integrity checks that gate the outputs. */
export function IntegrityChecksPanel({
  checks,
}: {
  checks: IntegrityCheck[];
}) {
  const allPassed = checks.every((c) => c.passed);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {allPassed ? (
            <CheckCircle2 className="size-4 text-emerald-500" aria-hidden />
          ) : (
            <ShieldAlert className="size-4 text-rose-500" aria-hidden />
          )}
          Integrity checks
        </CardTitle>
        <CardDescription>
          Coded checks that must pass before the figures are relied on.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {checks.map((check) => (
            <li key={check.check} className="flex items-start gap-2 text-sm">
              {check.passed ? (
                <CheckCircle2
                  className="mt-0.5 size-4 shrink-0 text-emerald-500"
                  aria-hidden
                />
              ) : (
                <ShieldAlert
                  className="mt-0.5 size-4 shrink-0 text-rose-500"
                  aria-hidden
                />
              )}
              <span>
                <span className="font-mono text-xs text-slate-700">
                  {check.check}
                </span>
                <span className="ml-2 text-slate-500">{check.detail}</span>
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
