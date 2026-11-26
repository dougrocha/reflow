"use client";

import { CircleAlert, CircleCheck, CircleDashed, CircleSlash, Hourglass, LoaderCircle } from "lucide-react";
import { useBuilds } from "./context";
import { statusView } from "@/lib/builds/view";
import { cn } from "@/lib/utils";

const ICON = {
  unbuilt: CircleDashed, built: CircleCheck, queued: Hourglass, running: LoaderCircle, failed: CircleAlert, stopped: CircleSlash,
} as const;

/** A bar with its value in words next to it. Native <progress>: it has the progressbar role and
 *  the browser draws it with the system colours in forced-colors mode. Not a live region. */
export function BuildProgress({ label, value, max, text, className }: {
  label: string; value: number | null; max: number | null; text: string; className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <progress
        aria-label={label}
        aria-valuetext={text}
        value={value ?? undefined}
        max={max ?? undefined}
        className="h-2 w-24 shrink-0 accent-primary"
      />
      <span className="type-caption tabular-nums text-muted-foreground">{text}</span>
    </div>
  );
}

/** One document's build status: built when, queued, building with stage and progress, or failed with the error. */
export function BuildStatus({ slug, className }: { slug: string; className?: string }) {
  const builds = useBuilds();
  if (!builds) return null;
  if (!builds.data) return <span className={cn("type-small text-muted-foreground", className)}>{builds.error ? `${builds.error} Retrying automatically…` : "Checking builds…"}</span>;
  const view = statusView(builds.data, slug, builds.now);
  const Icon = ICON[view.tone];
  return (
    <div className={cn("flex flex-col items-start gap-1 type-small", className)} data-build-status={view.tone}>
      <span className={cn("inline-flex items-start gap-1.5", view.tone === "failed" ? "text-destructive" : view.tone === "built" ? "text-success" : "text-muted-foreground")}>
        <Icon aria-hidden="true" className={cn("mt-0.5 size-3.5 shrink-0", view.tone === "running" && "motion-safe:animate-spin")} />
        <span className="line-clamp-3 break-words" title={view.tone === "failed" ? view.text : undefined}>
          {view.builtAt !== null && view.tone === "built" ? (
            <time dateTime={new Date(view.builtAt).toISOString()} title={new Date(view.builtAt).toLocaleString("en-US")}>{view.text}</time>
          ) : view.text}
        </span>
      </span>
      {view.progress ? (
        <BuildProgress label={`${slug} build progress`} {...view.progress} />
      ) : null}
    </div>
  );
}
