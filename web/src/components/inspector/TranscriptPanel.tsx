"use client";

import { useEffect, useId, useMemo, useRef, useState, type MouseEvent } from "react";
import { Button } from "@/components/ui/button";
import { chunkFor, loadChunkPages, type Manifest } from "@/reflow/core";
import { manifestFor } from "./manifest-cache";
import { CARD_CLASS, TRANSCRIPT_CLASS, transcriptAnchor } from "@/reflow/react/transcript-style";
import { cn } from "@/lib/utils";
import type { InspectNode } from "@/lib/inspect";

export const TEXT_SCALES = [0.875, 1, 1.125, 1.25, 1.5];

// The page's HTML, with the document's language and direction that the transcript is set in.
type Loaded =
  | { page: number; html: string; lang: string; dir: Manifest["dir"] }
  | { page: number; error: true };

/**
 * The transcript of one page, styled as in the viewer. Two way with the boxes: the block
 * for `selected` is outlined and scrolled to, and a click on a block selects its node. Blocks
 * carry data-node (new output); older output falls back to reflow's element ids.
 */
export function TranscriptPanel({
  title,
  base,
  version,
  page,
  pageText,
  nodes,
  selected,
  onSelect,
  textScale,
  onTextScale,
}: {
  title: string;
  base: string;
  /** The build's digest (the manifest's `v`), the key of the manifest kept for `base`. */
  version: string | null;
  page: number;
  pageText: string;
  nodes: InspectNode[];
  selected: string | null;
  onSelect: (id: string) => void;
  textScale: number;
  onTextScale: (scale: number) => void;
}) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    manifestFor(base, version)
      .then(async (manifest) => {
        const chunk = chunkFor(manifest, page);
        if (!chunk) throw new Error(`no chunk holds page ${page}`);
        const pages = await loadChunkPages(base, chunk.href, ctrl.signal);
        if (!ctrl.signal.aborted) {
          setLoaded({ page, html: pages.get(page) ?? "", lang: manifest.lang, dir: manifest.dir });
        }
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setLoaded({ page, error: true });
      });
    return () => ctrl.abort();
  }, [base, version, page]);

  const current = loaded?.page === page ? loaded : null;
  const html = current && "html" in current ? current.html : null;

  // Node ids by the element id reflow gives them (for output built before data-node).
  const byId = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes]);
  const byAnchor = useMemo(() => new Map(nodes.map((n) => [transcriptAnchor(n.id), n.id])), [nodes]);

  // A click on a block selects its node.
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const target = e.target as Element;
    const block = target.closest<HTMLElement>("[data-node]");
    if (block?.dataset.node) {
      if (byId.has(block.dataset.node)) onSelect(block.dataset.node);
      return;
    }
    const anchored = target.closest<HTMLElement>("[id]");
    const id = anchored && byAnchor.get(anchored.id);
    if (id) onSelect(id);
  };

  // The selected node's block is outlined and kept in view.
  useEffect(() => {
    const el = root.current;
    if (!el || html === null) return;
    el.querySelectorAll("[data-highlight]").forEach((el) => el.removeAttribute("data-highlight"));
    if (!selected) return;
    const target =
      el.querySelector<HTMLElement>(`[data-node="${selected}"]`) ??
      el.querySelector<HTMLElement>(`#${CSS.escape(transcriptAnchor(selected))}`);
    if (!target) return;
    target.setAttribute("data-highlight", "");
    target.scrollIntoView({ block: "nearest" });
  }, [selected, html]);

  const smaller = TEXT_SCALES.filter((s) => s < textScale).at(-1);
  const larger = TEXT_SCALES.find((s) => s > textScale);
  const headingId = useId();

  return (
    <section aria-labelledby={headingId} className="flex h-full min-h-0 flex-col bg-card">
      <header className="flex h-11 shrink-0 items-center justify-between gap-2 border-b px-3">
        <h2 id={headingId} className="type-label font-semibold">Transcript</h2>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="icon" aria-label="Smaller text" disabled={smaller === undefined}
                  onClick={() => smaller !== undefined && onTextScale(smaller)}>
            <span aria-hidden="true" className="text-xs">A−</span>
          </Button>
          <Button variant="outline" size="icon" aria-label="Larger text" disabled={larger === undefined}
                  onClick={() => larger !== undefined && onTextScale(larger)}>
            <span aria-hidden="true" className="text-sm">A+</span>
          </Button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto" onClick={onClick}>
        {!current ? (
          <p aria-busy="true" className="p-6 text-muted-foreground">Loading transcript…</p>
        ) : "error" in current ? (
          <p role="alert" className="p-6 text-destructive">The transcript for page {pageText} could not be loaded.</p>
        ) : (
          <div className="mx-auto max-w-[68ch] px-5 py-4">
            <h3 className="sticky top-0 z-10 -mx-5 mb-4 border-b bg-card/95 px-5 py-2 text-sm font-medium backdrop-blur">
              <span className="sr-only">{title}, </span>
              Page {pageText}
            </h3>
            {current.html.trim() ? (
              <div
                ref={root}
                lang={current.lang}
                dir={current.dir}
                style={{ fontSize: `${textScale}rem` }}
                className={cn(
                  TRANSCRIPT_CLASS,
                  "[&_[data-highlight]]:outline [&_[data-highlight]]:outline-[3px] [&_[data-highlight]]:outline-offset-2 [&_[data-highlight]]:outline-ring [&_[data-highlight]]:rounded-sm",
                )}
                dangerouslySetInnerHTML={{ __html: current.html }}
              />
            ) : (
              <p className={cn("m-0 p-6 text-muted-foreground italic", CARD_CLASS)}>This page has no text.</p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
