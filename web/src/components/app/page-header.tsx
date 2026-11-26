import type { ReactNode } from "react";
import { Crumbs, type Crumb } from "./crumbs";
import { cn } from "@/lib/utils";

export function PageHeader({
  title, lead, crumbs, meta, actions, display = false,
}: {
  title: ReactNode;
  lead?: ReactNode;
  crumbs?: Crumb[];
  meta?: ReactNode;
  actions?: ReactNode;
  /** The larger display size; the homepage only. */
  display?: boolean;
}) {
  return (
    <div className="mb-8 flex flex-col gap-3 sm:mb-10">
      {crumbs ? <Crumbs items={crumbs} className="mb-1" /> : null}
      <h1 className={cn(display ? "type-display" : "type-h1")}>{title}</h1>
      {lead ? <p className="type-lead max-w-3xl">{lead}</p> : null}
      {meta ? <div className="flex flex-wrap items-center gap-x-3 gap-y-2 type-small text-muted-foreground">{meta}</div> : null}
      {actions ? <div className="mt-2 grid gap-2 sm:flex sm:flex-wrap sm:items-center">{actions}</div> : null}
    </div>
  );
}
