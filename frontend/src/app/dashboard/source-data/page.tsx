import { FolderOpen } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { PlaceholderPanel } from "@/components/layout/placeholder-panel";

export const metadata = { title: "Source data" };

export default function SourceDataPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        icon={FolderOpen}
        title="Source Data"
        description="Financial input files supplied by each entity: trial balances, mappings, FX rates, intercompany details, adjustments and policies."
        badge="Placeholder"
      />
      <PlaceholderPanel
        icon={FolderOpen}
        title="Source file browser"
        summary="The engine already ingests these immutable inputs from data/ and their per-entity status is shown on the Consolidation page. This dedicated browser is not built yet — the placeholder states that honestly rather than inventing content."
        bullets={[
          "Browse original input files without modifying them",
          "Per-entity trial balances, account mappings and subledger controls",
          "FX rate tables, intercompany documents and adjustment journals",
          "Validation status, currently surfaced through the consolidation run",
        ]}
      />
    </div>
  );
}
