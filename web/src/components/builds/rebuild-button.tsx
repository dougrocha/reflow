"use client";

import { useState } from "react";
import { Hammer } from "lucide-react";
import { Button } from "@/components/ui/button";
import { BuildStatus } from "./build-status";
import { useBuilds } from "./context";
import { latestJob } from "@/lib/builds/view";
import { variantOf } from "@/pipelines";

/**
 * Rebuild one document. While it is queued, building or being asked for, the button stays
 * focusable but does nothing (aria-disabled, not disabled), so focus is not thrown away and the
 * reader hears why. Renders nothing when builds are off.
 */
export function RebuildButton({ slug, title, size, built = true }: {
  slug: string; title: string; size?: "lg"; built?: boolean;
}) {
  const builds = useBuilds();
  if (!builds) return null;
  const job = builds.data ? latestJob(builds.data, slug) : null;
  const queued = job?.state === "queued";
  const running = job?.state === "running";
  const busy = queued || running || builds.pending.has(slug);
  const verb = built ? "Rebuild" : "Build";
  const label = running ? (job.kind === "rerender" ? "Applying…" : "Building…") : queued ? "Queued" : builds.pending.has(slug) ? "Starting…" : verb;
  return (
    <Button
      type="button"
      variant="outline"
      size={size}
      aria-disabled={busy || undefined}
      onClick={() => {
        if (!busy) void builds.rebuild(slug);
      }}
      className={busy ? "cursor-default opacity-70" : undefined}
    >
      <Hammer aria-hidden="true" />
      {label}
      <span className="sr-only">, {title}</span>
    </Button>
  );
}

/** The not-converted notice's Build button and status. Nothing when builds are off. */
export function ConvertNow({ slug, title }: { slug: string; title: string }) {
  const builds = useBuilds();
  const [asked, setAsked] = useState(false);
  if (!builds) return null;
  // A variant built through the Anthropic API costs money: it asks first, as the variants menu does.
  const api = variantOf(slug)?.pipeline;
  if (api?.where === "api" && !asked) {
    return (
      <div className="flex flex-col items-start gap-2">
        <Button type="button" variant="outline" onClick={() => setAsked(true)}>
          <Hammer aria-hidden="true" />
          Build…
          <span className="sr-only">, {title}</span>
        </Button>
        <BuildStatus slug={slug} />
      </div>
    );
  }
  return (
    <div className="flex flex-col items-start gap-2">
      {api?.where === "api" ? (
        <div role="group" aria-label="Confirm the build" className="flex flex-col items-start gap-2 rounded-md border p-3">
          <p className="type-small">
            Build with {api.label} through the Anthropic API? This run sends parts of the document to Anthropic,
            billed to the ANTHROPIC_API_KEY in the server&apos;s .env.
          </p>
          <div className="flex gap-2">
            <RebuildButton slug={slug} title={title} built={false} />
            <Button type="button" variant="ghost" onClick={() => setAsked(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <RebuildButton slug={slug} title={title} built={false} />
      )}
      <BuildStatus slug={slug} />
    </div>
  );
}
