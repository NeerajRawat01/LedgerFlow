import type { LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";

interface PageHeaderProps {
  icon: LucideIcon;
  title: string;
  description: string;
  badge?: string;
}

/** Consistent heading block for dashboard pages. */
export function PageHeader({ icon: Icon, title, description, badge }: PageHeaderProps) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex items-start gap-3.5">
        <span className="mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-lg bg-[#0a1428] text-blue-400 ring-1 ring-slate-900/10">
          <Icon className="size-5" aria-hidden />
        </span>
        <div>
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">
              {title}
            </h1>
            {badge ? (
              <Badge variant="secondary" className="font-medium">
                {badge}
              </Badge>
            ) : null}
          </div>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-500">
            {description}
          </p>
        </div>
      </div>
    </div>
  );
}
