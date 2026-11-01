"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Landmark } from "lucide-react";

import { cn } from "@/lib/utils";
import { NAV_SECTIONS } from "@/lib/nav";

interface SidebarProps {
  className?: string;
}

/**
 * Dark navy navigation sidebar used by the dashboard shell.
 * Rendered statically on desktop and inside a Sheet on mobile.
 */
export function Sidebar({ className }: SidebarProps) {
  const pathname = usePathname();

  return (
    <div
      className={cn(
        "flex h-full w-64 shrink-0 flex-col border-r border-white/10 bg-[#0a1428] text-slate-200",
        className,
      )}
    >
      {/* Brand */}
      <div className="flex items-center gap-3 px-5 py-5">
        <span className="flex size-9 items-center justify-center rounded-lg bg-blue-500/15 text-blue-400 ring-1 ring-blue-400/30">
          <Landmark className="size-5" aria-hidden />
        </span>
        <span className="flex flex-col">
          <span className="text-base font-semibold tracking-tight text-white">
            LedgerFlow
          </span>
          <span className="text-[11px] font-medium tracking-wide text-slate-400">
            Group consolidation
          </span>
        </span>
      </div>

      {/* Navigation */}
      <nav
        aria-label="Dashboard navigation"
        className="flex-1 overflow-y-auto px-3 pb-4 pt-2"
      >
        {NAV_SECTIONS.map((section) => (
          <div key={section.label} className="mb-5 last:mb-0">
            <p className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
              {section.label}
            </p>
            <ul className="space-y-0.5">
              {section.items.map((item) => {
                const active =
                  pathname === item.href ||
                  (item.href !== "/dashboard" &&
                    pathname.startsWith(item.href));
                const Icon = item.icon;

                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors outline-none focus-visible:ring-2 focus-visible:ring-blue-400/60",
                        active
                          ? "bg-white/10 font-medium text-white shadow-[inset_2px_0_0_0_#3b82f6]"
                          : "text-slate-300 hover:bg-white/5 hover:text-white",
                      )}
                    >
                      <Icon
                        className={cn(
                          "size-4 shrink-0",
                          active ? "text-blue-400" : "text-slate-400",
                        )}
                        aria-hidden
                      />
                      {item.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      {/* Footer */}
      <div className="border-t border-white/10 px-4 py-3.5">
        <p className="text-[11px] leading-relaxed text-slate-500">
          Phase 1 · Project foundation
          <br />
          v0.1.0
        </p>
      </div>
    </div>
  );
}
