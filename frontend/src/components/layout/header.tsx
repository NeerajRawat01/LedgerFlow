"use client";

import { usePathname } from "next/navigation";
import { Menu, Radio } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Separator } from "@/components/ui/separator";
import { findNavItem } from "@/lib/nav";
import { useApiHealth, type ApiHealthStatus } from "@/hooks/use-api-health";
import { cn } from "@/lib/utils";
import { useState } from "react";
import { Sidebar } from "@/components/layout/sidebar";

const STATUS_META: Record<
  ApiHealthStatus,
  { label: string; dot: string; text: string }
> = {
  checking: {
    label: "API checking",
    dot: "bg-slate-400",
    text: "text-slate-500",
  },
  online: {
    label: "API online",
    dot: "bg-emerald-500",
    text: "text-emerald-600",
  },
  offline: {
    label: "API offline",
    dot: "bg-rose-500",
    text: "text-rose-600",
  },
};

export function Header() {
  const pathname = usePathname();
  const health = useApiHealth();
  const [open, setOpen] = useState(false);
  const activeItem = findNavItem(pathname);
  const status = STATUS_META[health];

  return (
    <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-slate-200 bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/80 lg:px-6">
      {/* Mobile navigation */}
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetTrigger
          render={
            <Button
              variant="outline"
              size="icon-sm"
              className="lg:hidden"
              aria-label="Open navigation menu"
            />
          }
        >
          <Menu className="size-4" aria-hidden />
        </SheetTrigger>
        <SheetContent side="left" className="w-64 border-white/10 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>LedgerFlow navigation</SheetTitle>
          </SheetHeader>
          <Sidebar />
        </SheetContent>
      </Sheet>

      {/* Page title */}
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-semibold text-slate-900">
          {activeItem?.label ?? "Dashboard"}
        </p>
        <p className="hidden truncate text-xs text-slate-500 sm:block">
          {activeItem?.description ?? "LedgerFlow overview"}
        </p>
      </div>

      {/* Backend API status */}
      <span
        className={cn(
          "hidden items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium sm:inline-flex",
          status.text,
        )}
        title="Backend health endpoint status"
      >
        <Radio className="size-3" aria-hidden />
        <span className={cn("size-1.5 rounded-full", status.dot)} aria-hidden />
        {status.label}
      </span>

      <Separator orientation="vertical" className="h-6 bg-slate-200" />

      <span className="flex items-center gap-2 text-xs font-medium text-slate-500">
        <span className="flex size-7 items-center justify-center rounded-full bg-[#0a1428] text-[11px] font-semibold text-white">
          LF
        </span>
        <span className="hidden md:inline">LedgerFlow</span>
      </span>
    </header>
  );
}
