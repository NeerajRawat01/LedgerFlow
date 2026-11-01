import type { LucideIcon } from "lucide-react";
import { Clock } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface PlaceholderPanelProps {
  icon: LucideIcon;
  title: string;
  summary: string;
  bullets: string[];
}

/**
 * Honest placeholder for modules that belong to later phases.
 * States what will be built without pretending functionality exists.
 */
export function PlaceholderPanel({
  icon: Icon,
  title,
  summary,
  bullets,
}: PlaceholderPanelProps) {
  return (
    <Card className="border-dashed">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <Icon className="size-4 text-slate-500" aria-hidden />
            <CardTitle className="text-sm font-semibold text-slate-700">
              {title}
            </CardTitle>
          </div>
          <Badge
            variant="outline"
            className="gap-1 text-slate-500"
          >
            <Clock className="size-3" aria-hidden />
            Not implemented yet
          </Badge>
        </div>
        <CardDescription className="text-sm leading-relaxed">
          {summary}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {bullets.map((bullet) => (
            <li
              key={bullet}
              className="flex items-start gap-2 text-sm text-slate-600"
            >
              <span
                className="mt-1.5 size-1.5 shrink-0 rounded-full bg-slate-300"
                aria-hidden
              />
              {bullet}
            </li>
          ))}
        </ul>
        <p className="mt-4 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-500">
          This module is planned for a later implementation phase. No financial
          data or calculations are available yet.
        </p>
      </CardContent>
    </Card>
  );
}
