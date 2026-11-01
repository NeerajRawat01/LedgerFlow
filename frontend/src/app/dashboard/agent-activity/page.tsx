import { Bot } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { PlaceholderPanel } from "@/components/layout/placeholder-panel";

export const metadata = { title: "Agent activity" };

export default function AgentActivityPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        icon={Bot}
        title="Agent Activity"
        description="What the AI workflow did: tools invoked, policies interpreted, mappings suggested and recommendations produced."
        badge="Placeholder"
      />
      <PlaceholderPanel
        icon={Bot}
        title="Agent execution log"
        summary="This module will expose LangGraph-based agent runs for transparency. Agents advise and explain; all financial calculations stay in the deterministic Python engine."
        bullets={[
          "Timeline of agent runs with status and outcomes",
          "Tool calls made by each agent, with inputs and outputs",
          "Suggested account mappings and policy interpretations for review",
          "Persistent agent logs under storage/agent_logs/",
        ]}
      />
    </div>
  );
}
