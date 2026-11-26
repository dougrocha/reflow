"use client";

import { ListChecks } from "lucide-react";
import { Button } from "@/components/ui/button";
import { applyDecisionsView } from "@/lib/builds/view";
import { useBuilds } from "./context";

/**
 * Apply the document's recorded decisions to its built model, without converting it again. Only
 * this button (or `bun scripts/build-docs.ts --rerender`) starts such a job: recording a decision
 * starts nothing. It shows only when builds are on and a decision is waiting. Like RebuildButton it
 * stays focusable but does nothing (aria-disabled, not disabled) while a job of the document is
 * active or being asked for, and its text says why.
 */
export function ApplyDecisionsButton({ slug, title, pending }: { slug: string; title: string; pending: number }) {
  const builds = useBuilds();
  if (!builds || pending <= 0) return null;
  const { busy, label } = builds.data
    ? applyDecisionsView(builds.data, slug, builds.pending.has(slug))
    : { busy: builds.pending.has(slug), label: builds.pending.has(slug) ? "Starting…" : "Apply decisions" };
  return (
    <Button
      type="button"
      variant="outline"
      aria-disabled={busy || undefined}
      data-apply-decisions
      onClick={() => {
        if (!busy) void builds.applyDecisions(slug);
      }}
      className={busy ? "cursor-default opacity-70" : undefined}
    >
      <ListChecks aria-hidden="true" />
      {label}
      <span className="sr-only">, {title}</span>
    </Button>
  );
}
