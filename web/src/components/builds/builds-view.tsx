"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { Ban, RotateCcw } from "lucide-react";
import { EmptyState } from "@/components/app/empty-state";
import { Section } from "@/components/app/section";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { BuildProgress } from "./build-status";
import { useBuilds } from "./context";
import { doing, formatAgo, progressOf, queuePosition, type JobRecord } from "@/lib/builds/view";
import { formatDuration } from "@/lib/format";
import { variantTitle } from "@/pipelines";
import { overviewUrl } from "@/lib/urls";

const WHO = { ui: "Started on this page", cli: "Started from the command line" } as const;
const END: Record<string, string> = { done: "Built", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted" };

function JobRow({ job, title, position }: { job: JobRecord; title: string; position: number | null }) {
  const builds = useBuilds()!;
  const row = useRef<HTMLLIElement>(null);
  const { focusJob, clearFocusJob } = builds;
  const active = job.state === "queued" || job.state === "running";
  // After Cancel or Retry the button that was pressed is gone; this row takes focus instead: the
  // retried job's new row, or the cancelled job's row once it has moved to Recent.
  useEffect(() => {
    if ((focusJob === job.id || (focusJob === `end:${job.id}` && !active)) && row.current) {
      row.current.focus();
      clearFocusJob();
    }
  }, [focusJob, job.id, active, clearFocusJob]);

  const busy = builds.pending.has(job.id);
  const progress = progressOf(job);
  const when = job.finishedAt ?? job.startedAt ?? job.enqueuedAt;
  const took = job.finishedAt !== null && job.startedAt !== null ? formatDuration(job.finishedAt - job.startedAt) : null;
  return (
    <li
      ref={row}
      tabIndex={-1}
      data-job={job.id}
      data-state={job.state}
      className="flex flex-col gap-2 rounded-xl p-4 ring-1 ring-foreground/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:flex-row sm:items-start sm:justify-between"
    >
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <Link href={overviewUrl(job.slug)} className="type-body font-medium text-link underline decoration-link/40 underline-offset-4 hover:decoration-link">
            {title}
          </Link>
          <Badge variant="outline" className="type-caption">{WHO[job.who]}</Badge>
        </div>
        <p className="type-small text-muted-foreground">
          {job.state === "queued" ? (position && position > 1 ? `Queued, number ${position}` : "Queued, next")
            : job.state === "running" ? `${doing(job)}${job.stage ? `: ${job.stage}` : ""}`
            : `${END[job.state]} ${formatAgo(when, builds.now)}${took ? `, took ${took}` : ""}`}
        </p>
        {active && job.who === "cli" ? <p className="type-small text-muted-foreground">Cancel stops all documents in this command.</p> : null}
        {progress ? <BuildProgress label={`${title}: build progress`} {...progress} /> : null}
        {job.error ? <p className="type-small break-words text-destructive">{job.error}</p> : null}
      </div>
      <div className="flex shrink-0 gap-2">
        {active ? (
          <Button
            type="button"
            variant="outline"
            aria-disabled={busy || undefined}
            onClick={() => {
              if (!busy) void builds.cancel(job.id);
            }}
          >
            <Ban aria-hidden="true" />
            {busy ? "Cancelling…" : "Cancel"}
            <span className="sr-only"> build of {title}</span>
          </Button>
        ) : job.state !== "done" ? (
          <Button
            type="button"
            variant="outline"
            aria-disabled={busy || undefined}
            onClick={() => {
              if (!busy) void builds.retry(job.id);
            }}
          >
            <RotateCcw aria-hidden="true" />
            Retry<span className="sr-only"> build of {title}</span>
          </Button>
        ) : null}
      </div>
    </li>
  );
}

/** The queue and the recent builds, live. */
export function BuildsView() {
  const builds = useBuilds();
  if (!builds) return <p className="type-small text-muted-foreground">Checking builds…</p>;
  const { data, error } = builds;
  if (!data) return <p className="type-small text-muted-foreground">{error ? `${error} Retrying automatically…` : "Checking builds…"}</p>;
  const title = (slug: string) => variantTitle(slug) ?? slug;
  return (
    <div className="flex flex-col gap-10">
      {error ? <p className="type-small text-destructive">{error}</p> : null}
      <Section
        title="Queue"
        description={`${data.concurrency === 1 ? "One build runs" : `Up to ${data.concurrency} builds run`} at a time, the rest wait their turn.`}
        headingLevel={2}
      >
        {data.active.length === 0 ? (
          <EmptyState title="Nothing is building" description="Press Rebuild on a document in the list to start a build, or run bun run docs:build in a terminal: it shows up here." headingLevel={3} />
        ) : (
          <ul role="list" aria-label="Queued and running builds" className="grid gap-3">
            {data.active.map((job) => (
              <JobRow key={job.id} job={job} title={title(job.slug)} position={queuePosition(data, job)} />
            ))}
          </ul>
        )}
      </Section>
      <Section title="Recent" description="The latest finished builds, newest first." headingLevel={2}>
        {data.recent.length === 0 ? (
          <p className="type-small text-muted-foreground">No finished builds yet.</p>
        ) : (
          <ul role="list" aria-label="Recent builds" className="grid gap-3">
            {data.recent.map((job) => (
              <JobRow key={job.id} job={job} title={title(job.slug)} position={null} />
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
