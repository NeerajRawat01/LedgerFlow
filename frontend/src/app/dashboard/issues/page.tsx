import { AlertTriangle } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { IssuesView } from "@/components/consolidation/issues-view";

export const metadata = { title: "Issues" };

export default function IssuesPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        icon={AlertTriangle}
        title="Issue Management"
        description="Accounting inconsistencies detected during consolidation: missing FX rates, unbalanced journals, duplicate adjustments and similar findings — each traced to its source records."
      />
      <IssuesView />
    </div>
  );
}
