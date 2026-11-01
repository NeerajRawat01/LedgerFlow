import { Building2 } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { PlaceholderPanel } from "@/components/layout/placeholder-panel";

export const metadata = { title: "Entities" };

export default function EntitiesPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        icon={Building2}
        title="Entity Management"
        description="Legal entities in the group, their functional currencies, ownership percentages, acquisition dates and reporting structures."
        badge="Placeholder"
      />
      <PlaceholderPanel
        icon={Building2}
        title="Entity registry"
        summary="The consolidation engine already uses the group structure (US parent, UK subsidiary, 80% India subsidiary acquired 1 July 2024) and shows each entity's functional currency and status on the Consolidation page. This dedicated registry is not built yet — the placeholder states that honestly."
        bullets={[
          "Entity list with country, functional currency and fiscal year end",
          "Ownership percentages and non-controlling interest tracking",
          "Acquisition dates and consolidation scope per period",
          "Per-entity chart of accounts and local-to-group mapping status",
        ]}
      />
    </div>
  );
}
