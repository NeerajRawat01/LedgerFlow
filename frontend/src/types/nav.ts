import type { LucideIcon } from "lucide-react";

/** A single dashboard navigation destination. */
export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
  description: string;
}

/** A labelled group of navigation items in the sidebar. */
export interface NavSection {
  label: string;
  items: NavItem[];
}
