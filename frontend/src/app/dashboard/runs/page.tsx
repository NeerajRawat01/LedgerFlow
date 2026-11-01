import { PlayCircle } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { RunsView } from "@/components/consolidation/runs-view";

export const metadata = { title: "Runs" };

export default function RunsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        icon={PlayCircle}
        title="Consolidation Runs"
        description="Execution history for group consolidation: period, status, headline figures and issue counts, read from the persisted run store."
      />
      <RunsView />
    </div>
  );
}
