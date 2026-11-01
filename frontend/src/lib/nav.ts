import {
  AlertTriangle,
  BarChart3,
  Bot,
  Building2,
  FileText,
  FolderOpen,
  LayoutDashboard,
  PlayCircle,
} from "lucide-react";

import type { NavItem, NavSection } from "@/types/nav";

/** Dashboard navigation model, shared by sidebar and header. */
export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Overview",
    items: [
      {
        label: "Dashboard",
        href: "/dashboard",
        icon: LayoutDashboard,
        description: "Platform overview and module entry points",
      },
    ],
  },
  {
    label: "Master data",
    items: [
      {
        label: "Entities",
        href: "/dashboard/entities",
        icon: Building2,
        description: "Legal entities, ownership and currencies",
      },
      {
        label: "Source data",
        href: "/dashboard/source-data",
        icon: FolderOpen,
        description: "Immutable financial input files per entity",
      },
    ],
  },
  {
    label: "Consolidation",
    items: [
      {
        label: "Consolidation",
        href: "/dashboard/consolidation",
        icon: BarChart3,
        description: "Run and review the FY2024 group consolidation",
      },
      {
        label: "Runs",
        href: "/dashboard/runs",
        icon: PlayCircle,
        description: "Consolidation run history and status",
      },
      {
        label: "Issues",
        href: "/dashboard/issues",
        icon: AlertTriangle,
        description: "Detected accounting issues and reviews",
      },
    ],
  },
  {
    label: "Output",
    items: [
      {
        label: "Reports",
        href: "/dashboard/reports",
        icon: FileText,
        description: "Consolidated financial statements",
      },
    ],
  },
  {
    label: "Intelligence",
    items: [
      {
        label: "Agent activity",
        href: "/dashboard/agent-activity",
        icon: Bot,
        description: "AI workflow runs, tool calls and decisions",
      },
    ],
  },
];

/** All navigation items flattened, in display order. */
export const NAV_ITEMS: NavItem[] = NAV_SECTIONS.flatMap(
  (section) => section.items,
);

/** Resolve the navigation item matching the current pathname. */
export function findNavItem(pathname: string): NavItem | undefined {
  return (
    NAV_ITEMS.find((item) => item.href === pathname) ??
    NAV_ITEMS.find(
      (item) => item.href !== "/dashboard" && pathname.startsWith(item.href),
    ) ??
    NAV_ITEMS[0]
  );
}
