"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Section } from "@/components/app/section";
import { BuildStatus } from "@/components/builds/build-status";
import { ApplyDecisionsButton } from "@/components/builds/apply-decisions-button";
import { RebuildButton } from "@/components/builds/rebuild-button";
import { ChangeControls, type Handle } from "@/components/decisions/controls";
import { useDecisions } from "@/components/decisions/use-decisions";
import { actionsOf, changesNextBuild, decidedWords, describeDecision, pendingFor, pendingLine, statusWords } from "@/lib/decisions/client";
import type { DecisionView } from "@/lib/decisions/status";
import { type ReviewItem } from "@/lib/review";
import { inspectUrl } from "@/lib/urls";

// The decisions of one document, on its page beside the issue list: what each one says, whether the
// published build applied it, and Undo or Change. An applied decision has no queue item any more,
// so this list is where it can be seen and undone. The parent renders it only when builds are on.

const handleOf = (d: DecisionView): Handle | null =>
  d.kind === "alt" ? { id: d.id, kind: "alt", text: d.text, decorative: d.decorative }
  : d.kind === "correct" ? { id: d.id, kind: "correct", was: d.was }
  : d.kind === "keep" ? { id: d.id, kind: "keep", was: d.was }
  : d.kind === "dismiss" ? { id: d.id, kind: "dismiss" }
  : null;

export function DecisionsSection({ slug, title }: { slug: string; title: string }) {
  const d = useDecisions(slug);
  if (!d.enabled) return null;
  const line = pendingLine(changesNextBuild(d.decisions));
  return (
    <div className="mt-10">
      <Section
        id="decisions"
        title="Decisions"
        description="What reviewers decided about this document. A decision takes effect when you apply decisions or rebuild."
        actions={
          <div className="flex flex-wrap items-center gap-3">
            {line ? <p data-pending-line className="type-small font-medium">{line}</p> : null}
            <ApplyDecisionsButton slug={slug} title={title} pending={changesNextBuild(d.decisions)} />
            <RebuildButton slug={slug} title={title} />
            <BuildStatus slug={slug} />
          </div>
        }
      >
        {d.state.status === "error" ? <p role="alert" className="type-small">{d.state.error}</p> : null}
        {d.decisions.length === 0 ? (
          <p className="type-small text-muted-foreground">
            {d.state.status === "ready" ? "No decisions yet. Decide on an issue in the inspector." : "Reading the decisions…"}
          </p>
        ) : (
          <ul role="list" className="divide-y overflow-hidden rounded-xl ring-1 ring-foreground/10" data-decisions>
            {d.decisions.map((x) => {
              const handle = handleOf(x);
              const acts = actionsOf(x);
              return (
                <li key={x.id} className="flex flex-col gap-2 p-4" data-decision={x.id} data-status={x.status}>
                  <p className="font-medium">{describeDecision(x)}</p>
                  <p className="type-small text-muted-foreground">
                    {statusWords(x)}
                    {x.page ? <> · <Link href={inspectUrl({ slug, page: x.page })} className="underline underline-offset-2">Page {x.page} in the inspector</Link></> : null}
                  </p>
                  {handle && acts.undo ? (
                    <ChangeControls handle={handle} canChange={acts.change} post={d.post} label={describeDecision(x)} />
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </Section>
    </div>
  );
}

/** "Decided: …" on an issue whose decision waits for the next build. Words and an icon-free badge, never colour alone. */
export function DecidedBadge({ slug, item }: { slug: string; item: ReviewItem }) {
  const d = useDecisions(slug);
  if (!d.enabled) return null;
  const waiting = pendingFor(item, d.decisions);
  if (waiting.length === 0) return null;
  return <Badge variant="secondary" className="h-6 px-2 type-caption" data-decided-badge>{decidedWords(waiting[0]!)}<span className="sr-only">. Takes effect at the next build.</span></Badge>;
}
