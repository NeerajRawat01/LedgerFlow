import Link from "next/link";
import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  Bot,
  Building2,
  FileText,
  FolderOpen,
  Landmark,
  PlayCircle,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { NAV_SECTIONS } from "@/lib/nav";

interface ModuleCard {
  title: string;
  href: string;
  icon: LucideIcon;
  description: string;
}

const MODULE_CARDS: ModuleCard[] = [
  {
    title: "Consolidation",
    href: "/dashboard/consolidation",
    icon: BarChart3,
    description: "Run the FY2024 group consolidation and review stage results",
  },
  {
    title: "Entities",
    href: "/dashboard/entities",
    icon: Building2,
    description: "Legal entities, ownership percentages and currencies",
  },
  {
    title: "Source data",
    href: "/dashboard/source-data",
    icon: FolderOpen,
    description: "Immutable financial input files imported per entity",
  },
  {
    title: "Runs",
    href: "/dashboard/runs",
    icon: PlayCircle,
    description: "Consolidation run history, inputs and outcomes",
  },
  {
    title: "Issues",
    href: "/dashboard/issues",
    icon: AlertTriangle,
    description: "Accounting inconsistencies flagged for review",
  },
  {
    title: "Reports",
    href: "/dashboard/reports",
    icon: FileText,
    description: "Consolidated balance sheet, P&L and equity statements",
  },
  {
    title: "Agent activity",
    href: "/dashboard/agent-activity",
    icon: Bot,
    description: "AI workflow runs, tool invocations and decisions",
  },
];

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      {/* Platform introduction */}
      <Card className="overflow-hidden border-slate-200">
        <CardHeader className="gap-4 bg-[#0a1428] text-slate-200">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-4">
              <span className="flex size-11 items-center justify-center rounded-xl bg-blue-500/15 text-blue-400 ring-1 ring-blue-400/30">
                <Landmark className="size-6" aria-hidden />
              </span>
              <div>
                <div className="flex flex-wrap items-center gap-2.5">
                  <CardTitle className="text-lg font-semibold text-white">
                    LedgerFlow
                  </CardTitle>
                  <Badge className="border border-blue-400/40 bg-blue-500/10 text-blue-300">
                    FY2024 · Consolidation ready
                  </Badge>
                </div>
                <CardDescription className="mt-1.5 max-w-2xl text-sm leading-relaxed text-slate-400">
                  AI-powered financial consolidation and reporting for groups
                  that operate through multiple legal entities, currencies and
                  charts of accounts — with full traceability from every
                  reported figure back to its source.
                </CardDescription>
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4 pt-5 text-sm text-slate-600 sm:grid-cols-3">
          <div>
            <p className="font-medium text-slate-800">Current state</p>
            <p className="mt-1 leading-relaxed">
              The FY2024 group consolidation runs end-to-end: ingestion,
              validation, mapping, intercompany reconciliation, FX translation,
              eliminations, acquisition accounting and consolidated statements.
            </p>
          </div>
          <div>
            <p className="font-medium text-slate-800">Design principle</p>
            <p className="mt-1 leading-relaxed">
              A deterministic Python engine performs all financial calculations.
              AI agents assist, explain and recommend — they never compute.
            </p>
          </div>
          <div>
            <p className="font-medium text-slate-800">Auditability</p>
            <p className="mt-1 leading-relaxed">
              Every figure traces back to source records, FX conversions,
              adjustments and elimination entries.
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Module entry points */}
      <div>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-900">Modules</h2>
          <Badge variant="secondary">Consolidation &amp; reporting</Badge>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {MODULE_CARDS.map((module) => {
            const Icon = module.icon;
            return (
              <Link
                key={module.href}
                href={module.href}
                className="group block rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-blue-500/60"
              >
                <Card className="h-full border-slate-200 transition-colors group-hover:border-slate-300 group-hover:bg-slate-50/70">
                  <CardHeader className="pb-2">
                    <div className="flex items-center justify-between">
                      <span className="flex size-9 items-center justify-center rounded-lg bg-slate-100 text-slate-700 ring-1 ring-slate-200 group-hover:bg-[#0a1428] group-hover:text-blue-400 group-hover:ring-[#0a1428]">
                        <Icon className="size-4.5" aria-hidden />
                      </span>
                      <ArrowRight
                        className="size-4 text-slate-300 transition-all group-hover:translate-x-0.5 group-hover:text-slate-600"
                        aria-hidden
                      />
                    </div>
                    <CardTitle className="mt-2 text-sm font-semibold text-slate-900">
                      {module.title}
                    </CardTitle>
                    <CardDescription className="text-xs leading-relaxed">
                      {module.description}
                    </CardDescription>
                  </CardHeader>
                </Card>
              </Link>
            );
          })}
        </div>
      </div>

      {/* Navigation model reference */}
      <Card className="border-slate-200">
        <CardHeader>
          <CardTitle className="text-sm font-semibold text-slate-900">
            Dashboard navigation
          </CardTitle>
          <CardDescription className="text-xs">
            Product areas exposed as routes.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {NAV_SECTIONS.flatMap((section) => section.items).map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-600 transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900"
            >
              {item.label}
            </Link>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
