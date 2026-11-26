"use client";

import { useState } from "react";
import Link from "next/link";
import { Button, buttonVariants } from "@/components/ui/button";
import { useDecisions } from "@/components/decisions/use-decisions";
import { ActionButton, AltForm, Announcer, ChangeControls, ReadingForm, useAction, type Handle } from "@/components/decisions/controls";
import {
  bodies, decidedWords, decisionOfFact, ERRORS_NOT_DISMISSED, hasAltDecision, hasError, isSettled, openFindings, orphanId, pendingFor,
} from "@/lib/decisions/client";
import type { DecisionView } from "@/lib/decisions/status";
import type { InspectFact } from "@/lib/inspect";
import { suggestionText, type OcrSuggestion, type ReviewItem } from "@/lib/review";

// What a reviewer can do about one review item, under the "Review issue" box. It renders only
// when the builds are on and the page knows the build it shows (`rev`): the server binds every
// choice to that build, so a tab that is out of date gets a 409 and writes nothing.

export type Redecide = { label: string; href: string };

export function DecisionPanel({ slug, rev, item, nodeId, labels, supersedes, redecide }: {
  slug: string;
  /** The manifest `v` of the build this page shows. */
  rev: string | null;
  item: ReviewItem;
  /** The block selected on the page (null when the item has none). */
  nodeId: string | null;
  /** The review label of each finding code, from the node's facts. */
  labels: Record<string, string>;
  /** An orphaned decision this choice replaces (`?supersedes=` in the link Re-decide makes). */
  supersedes: string | null;
  /** The other items on the orphan's page that a reviewer can decide instead. */
  redecide: Redecide[];
}) {
  const d = useDecisions(slug);
  const { busy, outcome, run } = useAction(d.post);
  if (!d.enabled || !rev) return null;

  const waiting = pendingFor(item, d.decisions);
  const orphan = orphanId(item);
  const sugs = (item.suggestions ?? []).filter((s) => s.id && s.anchor);
  const figure = item.role === "figure" && !!item.anchor;
  const open = openFindings(item, waiting);
  const byCode = new Map<string, string[]>();
  // A grouped item's id and anchor are its first block's. Another member's alt text is not written here.
  const member = !!item.group && nodeId !== null && nodeId !== item.id;
  for (const f of open) byCode.set(f.code, [...(byCode.get(f.code) ?? []), f.id]);
  const previous = supersedes ? d.decisions.find((x) => x.id === supersedes) : undefined;

  return (
    <section aria-label="Decide" className="flex flex-col gap-3 rounded-lg border p-3 type-small" data-decision-panel>
      <h3 className="type-label font-semibold">Decide</h3>

      {waiting.map((w) => (
        <div key={w.id} className="flex flex-col gap-1.5" data-decided>
          <p className="font-medium">{decidedWords(w)}. Takes effect when you rebuild.</p>
          <div><ActionButton busy={busy} onClick={() => void run(bodies.undo(w.id))}>Undo</ActionButton></div>
        </div>
      ))}

      {item.warn === "decisions-other-pdf" ? (
        <div className="flex flex-col gap-1.5">
          <p>These decisions were made on another PDF. They are not applied.</p>
          <div><ActionButton busy={busy} onClick={() => void run(bodies.reattach(rev, item.id))}>Try them on this PDF</ActionButton></div>
        </div>
      ) : null}

      {orphan ? (
        <Orphan orphan={orphan} busy={busy} redecide={redecide} onRemove={() => void run(bodies.undo(orphan))}
          previousText={orphanAlt(d.decisions.find((x) => x.id === orphan))} />
      ) : null}

      {sugs.map((s) => (
        <Suggestion key={s.id} slug={slug} rev={rev} item={item} s={s} busy={busy} run={run} supersedes={supersedes}
          settled={isSettled(waiting, s)} />
      ))}

      {figure && member ? (
        <p>This item groups {item.group!.nodeIds.length} figures and names only the first. To write alt text, open the first one; it is not offered here so it cannot land on another figure.</p>
      ) : figure && !hasAltDecision(waiting) ? (
        <AltForm initial="" busy={busy} onSave={(alt) => void run(bodies.alt(rev, item.id, alt, supersedes ?? undefined, nodeId ?? undefined))}
          extra={(set) => previous?.kind === "alt" && !previous.decorative ? (
            <div className="flex flex-col gap-1">
              <p className="type-caption text-muted-foreground">The previous alt text was: “{previous.text}”</p>
              <div><Button type="button" variant="outline" size="sm" onClick={() => set(previous.text)}>Use the previous alt text</Button></div>
            </div>
          ) : null} />
      ) : null}

      {[...byCode].map(([code, ids]) => (
        <div key={code}>
          <ActionButton busy={busy} onClick={() => void run(bodies.dismiss(rev, item.id, ids))}>
            Dismiss: {labels[code] ?? code}{ids.length > 1 ? ` (${ids.length})` : ""}
          </ActionButton>
        </div>
      ))}
      {hasError(item) ? <p className="text-muted-foreground">{ERRORS_NOT_DISMISSED}</p> : null}

      <Announcer outcome={outcome} />
    </section>
  );
}

function Suggestion({ rev, item, s, busy, run, supersedes, settled }: {
  slug: string; rev: string; item: ReviewItem; s: OcrSuggestion; busy: boolean;
  run: (body: Record<string, unknown>) => Promise<boolean>; supersedes: string | null; settled: boolean;
}) {
  const [other, setOther] = useState(false);
  const id = s.id!;
  const sup = supersedes ?? undefined;
  return (
    <div className="flex flex-col gap-2" role="group" aria-label={`OCR read ${s.was}`}>
      <p>{suggestionText(s)}</p>
      {settled ? null : other ? (
        <ReadingForm was={s.was} busy={busy} onCancel={() => setOther(false)}
          onSave={(now) => void run(bodies.other(rev, item.id, id, now, sup)).then((ok) => { if (ok) setOther(false); })} />
      ) : (
        <div className="flex flex-wrap gap-2">
          <ActionButton busy={busy} variant="default" onClick={() => void run(bodies.accept(rev, item.id, id, sup))}>Accept ‘{s.now}’</ActionButton>
          <ActionButton busy={busy} onClick={() => void run(bodies.keep(rev, item.id, id, sup))}>Keep ‘{s.was}’</ActionButton>
          <Button type="button" variant="outline" size="sm" onClick={() => setOther(true)}>Another reading…</Button>
        </div>
      )}
    </div>
  );
}

const orphanAlt = (x: DecisionView | undefined): string | null => (x?.kind === "alt" && !x.decorative ? x.text : null);

function Orphan({ orphan, busy, redecide, onRemove, previousText }: {
  orphan: string; busy: boolean; redecide: Redecide[]; onRemove: () => void; previousText: string | null;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-2">
      {previousText ? <p>The alt text was: “{previousText}”</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen((v) => !v)}>Re-decide</Button>
        <ActionButton busy={busy} onClick={onRemove}>Remove</ActionButton>
      </div>
      {open ? (
        redecide.length ? (
          <div>
            <p>Decide again on one of this page’s issues. It replaces this decision.</p>
            <ul className="mt-1 list-disc pl-5">
              {redecide.map((r) => (
                <li key={r.href}><Link href={`${r.href}&supersedes=${encodeURIComponent(orphan)}`} className={buttonVariants({ variant: "link", size: "sm" })}>{r.label}</Link></li>
              ))}
            </ul>
          </div>
        ) : <p>This page has no issue to decide on. Use Remove to take the decision out.</p>
      ) : null}
    </div>
  );
}

/** For a node a reviewer's decision changed: what was done, with Change and Undo. */
export function DecidedNode({ slug, facts }: { slug: string; facts: InspectFact[] }) {
  const d = useDecisions(slug);
  if (!d.enabled) return null;
  const decided = facts.map((f) => ({ f, id: decisionOfFact(f) })).filter((x): x is { f: InspectFact; id: string } => x.id !== null);
  if (decided.length === 0) return null;
  return (
    <section aria-label="Reviewer’s decision" className="flex flex-col gap-3 rounded-lg border p-3 type-small" data-decided-node>
      {decided.map(({ f, id }) => {
        const view = d.decisions.find((x) => x.id === id);
        const undone = view?.status === "undone";
        const data = f.data ?? {};
        const was = String(data.was ?? "");
        const handle: Handle = f.code === "reviewed-alt"
          ? { id, kind: "alt", text: view?.kind === "alt" ? view.text : "", decorative: view?.kind === "alt" ? view.decorative : false }
          : { id, kind: f.code === "reviewed-kept" ? "keep" : "correct", was };
        return (
          <div key={id} className="flex flex-col gap-1.5">
            <p className="font-medium">
              {f.code === "reviewed-correction" ? `Corrected by a reviewer. OCR read ‘${was}’.`
                : f.code === "reviewed-kept" ? `Kept by a reviewer. OCR read ‘${was}’.`
                : data.decorative === true || (view?.kind === "alt" && view.decorative) ? "Marked decorative by a reviewer."
                : "Alt text written by a reviewer."}
            </p>
            {undone ? <p>Undone. Takes effect when you rebuild.</p>
              : <ChangeControls handle={handle} canChange post={d.post} />}
          </div>
        );
      })}
    </section>
  );
}
