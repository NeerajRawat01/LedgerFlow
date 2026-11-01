import { FileText } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { ReportsView } from "@/components/consolidation/reports-view";

export const metadata = { title: "Reports" };

export default function ReportsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        icon={FileText}
        title="Financial Reports"
        description="Consolidated group statements — trial balance, balance sheet, profit and loss, changes in equity — produced by the deterministic accounting engine, with full traceability to source records."
      />
      <ReportsView />
    </div>
  );
}
