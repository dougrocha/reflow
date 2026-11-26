import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export function EmptyState({
  icon: Icon, title, description, action, headingLevel = 2, className,
}: {
  icon?: LucideIcon;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  headingLevel?: 1 | 2 | 3;
  className?: string;
}) {
  const H = (`h${headingLevel}`) as "h1" | "h2" | "h3";
  return (
    <div className={cn("flex flex-col items-center gap-3 rounded-xl border border-dashed p-8 text-center sm:p-10", className)}>
      {Icon ? <Icon aria-hidden="true" className="size-8 text-muted-foreground" /> : null}
      <H className={headingLevel === 1 ? "type-h1" : "type-h3"}>{title}</H>
      {description ? <p className="max-w-prose type-small text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-2 flex flex-wrap justify-center gap-2">{action}</div> : null}
    </div>
  );
}
