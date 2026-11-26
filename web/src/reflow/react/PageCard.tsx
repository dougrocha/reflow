"use client";

import { memo, useLayoutEffect, useRef } from "react";
import { ArrowLeft, ChevronLeft, ChevronRight, RotateCw } from "lucide-react";
import { pageImageSet, type ManifestPage } from "../core";
import { type ChunkState, type ViewMode } from "./useReflowDocument";
import { Button } from "@/components/ui/native-button";
import { pageText, parsePage } from "@/lib/pages";
import { boxPercent, type SourceRegion } from "@/lib/source-region";
import { CARD_CLASS as CARD, TRANSCRIPT_CLASS as TRANSCRIPT } from "./transcript-style";
import { restore, type TypedValues } from "./form-values";

// A page of the viewer: its transcript or its original image, the box it reserves before it
// loads, and its error state.

/** A place shown on its page image: the heading it came from, or an answer's passage, and its region if known. */
export type SourceItem = { page: number; nodeId: string; text: string; region: SourceRegion | null };
/** What is outlined on the page images. A heading's "Show on page" is origin "heading" with one item. */
export type ShownSources = { origin: "heading" | "ask"; items: SourceItem[]; active: number };
// The original-page card is at most 64rem wide (see PageCard) and never wider than the window;
// with the widths in srcset this picks the 1200 px image on a 2x screen.
const PAGE_IMAGE_SIZES = "min(64rem, 100vw)";
// Page cards reserve their height before the page loads, so the scrollbar is honest. The
// estimate is the card's whole height, page label included, as its contain-intrinsic-size.
// The browser keeps the real height once a card has rendered (the `auto` keyword). Cards are
// shrink-0: a skipped card would otherwise shrink to 0 in the flex column.
const ORIGINAL_MAX_PX = 1024; // 64rem: the width of an original page
const TRANSCRIPT_MAX_PX = 768; // 48rem: the width of the transcript column
// The page label: a 20px line, and its margin below (8px over an image, 24px in the transcript).
const labelPx = (mode: ViewMode) => (mode === "original" ? 28 : 44);
// Transcript height, fitted on 40 rendered pages (20 each from p17 and pima-directory): a
// body line is 16px at line-height 1.75; text takes wrapPx × characters ÷ the column's inner
// width; each block adds blockPx; restPx is the fitted intercept with the padding the pages had
// as cards taken out (negative, because the line count already covers part of what the wrap
// term measures).
const TRANSCRIPT_MODEL = { linePx: 28, wrapPx: 213, blockPx: 11, restPx: -310, minPx: 120 };
// The skeleton's lines: a few, spread over the estimated height.
const SKELETON_WIDTHS = [100, 96, 91, 100, 58, 100, 94, 88, 100, 72];
const SKELETON_MAX_LINES = 10;

/** Estimated height of a page card in px, page label included. */
function estimateCardHeight(page: ManifestPage, mode: ViewMode, contentWidth: number): number {
  if (mode === "original") {
    // The image has the page's own aspect ratio, so this is exact once the card is laid out.
    const width = Math.min(ORIGINAL_MAX_PX, contentWidth || ORIGINAL_MAX_PX);
    return Math.round(labelPx(mode) + width * (page.height / page.width));
  }
  const m = TRANSCRIPT_MODEL;
  const column = Math.min(TRANSCRIPT_MAX_PX, contentWidth || TRANSCRIPT_MAX_PX);
  const { chars, blocks, lines } = page.text;
  const px = m.linePx * lines + (m.wrapPx * chars) / column + m.blockPx * blocks + m.restPx;
  return Math.max(m.minPx, Math.round(px));
}
/**
 * A few grey lines, in the box a page's transcript will take. The pulse is motion, so it
 * runs only when the user has not asked for reduced motion.
 */
function PageSkeleton({ height, label }: { height: number; label: string }) {
  const lines = Math.max(3, Math.min(SKELETON_MAX_LINES, Math.floor((height - 32) / 28)));
  return (
    <div aria-busy="true" className="overflow-hidden" style={{ height }}>
      <span className="sr-only">Loading page {label}…</span>
      <div aria-hidden="true" className="flex flex-col gap-3.5 motion-safe:animate-pulse">
        {Array.from({ length: lines }, (_, i) => (
          <span key={i} className="block h-3 rounded bg-muted"
                style={{ width: `${SKELETON_WIDTHS[i % SKELETON_WIDTHS.length]}%` }} />
        ))}
      </div>
    </div>
  );
}

// "Show on page": Lucide's scan-search, drawn into the transcript HTML (so not a component).
const SHOW_SOURCE_ICON = '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="3"/><path d="m16 16-1.9-1.9"/></svg>';

/**
 * A transcript section that did not load: the card keeps its place, says so, and offers a retry.
 * Only the section's first page is an alert, so a screen reader announces the failure once.
 */
function PageError({ height, label, first, onRetry }: { height: number; label: string; first: boolean; onRetry: () => void }) {
  return (
    <div role={first ? "alert" : undefined}
         className="flex flex-col items-start justify-center gap-3 overflow-hidden"
         style={{ height }}>
      <p className="m-0 type-small">This section of the transcript did not load.</p>
      <Button type="button" variant="outline" onClick={onRetry}>
        <RotateCw aria-hidden="true" />
        Try again<span className="sr-only"> for the section with page {label}</span>
      </Button>
    </div>
  );
}

export const PageCard = memo(function PageCard({ page, base, version, imageWidths, mode, lang, dir, html, count, contentWidth, chunkState, chunkHref, chunkFirst, onRetry, canShowSource, onShowSource, onGoToPage, shown, typed, onBack, onBackToAnswer, onStep }: {
  page: ManifestPage; base: string;
  /** The manifest's digest of the output: page image URLs carry it (see core.resolve). */
  version: string | undefined;
  /** The widths of the page image the converter wrote (the manifest's imageWidths), for srcset. */
  imageWidths: number[] | undefined;
  mode: ViewMode;
  /** The document's language and direction: the transcript is in it, the app chrome is not. */
  lang: string; dir: "ltr" | "rtl";
  html: string | undefined; count: number;
  contentWidth: number;
  /** The load state of the chunk holding this page, and its href (for a retry). */
  chunkState: ChunkState | undefined;
  chunkHref: string | undefined;
  /** True on the first page of its chunk: only that page's error box is an alert. */
  chunkFirst: boolean;
  onRetry: (href: string) => void;
  canShowSource: boolean;
  onShowSource: (page: number, nodeId: string, text: string) => void;
  /** A "#pN" link in the transcript jumps the viewer to page N. */
  onGoToPage: (page: number) => void;
  /** What the reader has typed (see form-values.ts), put back when the card is mounted again. */
  typed: TypedValues;
  /** What is outlined on the page images, when this page holds some of it. */
  shown: ShownSources | null;
  onBack: () => void;
  /** "Back to answer", with the passage being shown. */
  onBackToAnswer: (evidence: number) => void;
  /** Previous (-1) and next (+1) passage of an answer. */
  onStep: (delta: number) => void;
}) {
  const ratio = `${page.width} / ${page.height}`;
  const estimate = estimateCardHeight(page, mode, contentWidth);
  const label = pageText(page.number, page.label);
  const body = useRef<HTMLDivElement>(null);
  const showButtons = canShowSource && !!page.image;
  const activeItem = shown && shown.items[shown.active]?.page === page.number ? shown.items[shown.active] : null;

  // A "Show on page" icon button beside each heading with no higher-level heading above it on
  // the page, added to the transcript HTML once it is mounted. A button by every subheading is
  // noise, and the outline of the section above a subheading already covers it; a subheading
  // that opens the page (its section began on an earlier page) has no such section, so it keeps
  // its button. The heading and its button share a row ([data-heading-row], styled in
  // transcript.css); the button is a sibling of the heading, not inside it, so the heading's
  // accessible name and the text find searches stay as they were. The HTML is React's
  // (dangerouslySetInnerHTML) and is replaced whenever the card re-mounts, so this runs again then.
  useLayoutEffect(() => {
    if (body.current) restore(body.current, typed);
  }, [html, mode, typed]);

  useLayoutEffect(() => {
    const root = body.current;
    if (!root) return;
    const tables = root.querySelectorAll<HTMLElement>(".table-wrap");
    const updateTableFocus = () => {
      for (const wrap of tables) {
        if (wrap.scrollWidth > wrap.clientWidth) wrap.setAttribute("tabindex", "0");
        else wrap.removeAttribute("tabindex");
      }
    };
    const tableResize = new ResizeObserver(updateTableFocus);
    for (const wrap of tables) {
      tableResize.observe(wrap);
      const table = wrap.querySelector("table");
      if (table) tableResize.observe(table);
    }
    updateTableFocus();
    if (!showButtons) return () => tableResize.disconnect();
    let top = 7;
    for (const h of root.querySelectorAll<HTMLElement>("h2[data-node], h3[data-node], h4[data-node], h5[data-node], h6[data-node]")) {
      const level = Number(h.tagName[1]);
      if (level > top) continue;
      top = level;
      if (h.parentElement?.hasAttribute("data-heading-row")) continue;
      const row = document.createElement("div");
      row.dataset.headingRow = "";
      const button = document.createElement("button");
      button.type = "button";
      button.lang = "en"; // the label is English whatever the document's language
      button.dataset.showSource = h.dataset.node;
      button.innerHTML = SHOW_SOURCE_ICON;
      button.setAttribute("aria-label", `Show ${(h.textContent ?? "").trim().slice(0, 80)} on page`);
      button.title = "Show on page";
      h.replaceWith(row);
      row.append(h, button);
    }
    return () => tableResize.disconnect();
  }, [html, mode, showButtons]);

  const onTranscriptClick = (e: React.MouseEvent) => {
    // The default must not run: a "#pN" hash would replace the #reflow= that keeps this viewer open.
    const link = (e.target as Element).closest<HTMLAnchorElement>("a[href^='#']");
    const href = link?.getAttribute("href") ?? "";
    const target = /^#p\d+$/.test(href) ? parsePage(href.slice(2)) : null;
    if (target) {
      e.preventDefault();
      onGoToPage(target);
      return;
    }
    // A note reference ("#n-<id>") and its backlink ("#r-<id>"): the note is on the same page.
    // Move focus there too, so a screen reader reads the note and Tab continues from it.
    const note = /^#[nr]-/.test(href) ? document.getElementById(decodeURIComponent(href.slice(1))) : null;
    if (note) {
      e.preventDefault();
      if (!note.matches("a[href]")) note.tabIndex = -1;
      note.focus({ preventScroll: true });
      note.scrollIntoView({ block: "center" });
      return;
    }
    const button = (e.target as Element).closest<HTMLElement>("[data-show-source]");
    const id = button?.dataset.showSource;
    if (!button || !id) return;
    onShowSource(page.number, id, (button.parentElement?.querySelector("[data-node]")?.textContent ?? "").trim());
  };

  return (
    <div className={`mx-auto w-full shrink-0 [content-visibility:auto] [overflow-clip-margin:12px] ${mode === "original" ? "max-w-[64rem]" : "max-w-[48rem]"}`}
         data-page={page.number}
         style={{ containIntrinsicSize: `auto ${estimate}px` }}>
      {/* Over an image, a caption; in the transcript, a thin rule that marks where the page begins. */}
      <p className={`flex scroll-mt-4 items-center gap-3 type-small text-muted-foreground ${mode === "original" ? "mb-2 justify-center" : "mb-6 before:h-px before:flex-1 before:bg-border after:h-px after:flex-1 after:bg-border"}`}
         data-page-label>
        <span>Page {label}<span className="sr-only"> of {count}</span></span>
      </p>
      {mode === "original" ? (
        <>
          {shown && activeItem ? (
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-card px-3 py-2">
              <p className="min-w-0 type-small">
                {shown.items.length > 1 ? <span className="me-1 text-muted-foreground">Passage {shown.active + 1} of {shown.items.length}.</span> : null}
                {activeItem.region ? "Outlined on the page: " : "Outline not available for "}
                <span className="font-medium">{shown.origin === "ask" ? <q lang={lang} dir={dir}>{activeItem.text}</q> : activeItem.text}</span>
              </p>
              {shown.origin === "heading" ? (
                <Button type="button" variant="outline" data-source-back onClick={onBack}>
                  <ArrowLeft aria-hidden="true" />
                  Back to transcript
                </Button>
              ) : (
                <div className="flex flex-wrap items-center gap-2">
                  {shown.items.length > 1 ? (
                    <>
                      <Button type="button" variant="outline" size="icon" data-ask-step="prev" disabled={shown.active === 0} onClick={() => onStep(-1)}>
                        <ChevronLeft aria-hidden="true" /><span className="sr-only">Previous passage</span>
                      </Button>
                      <Button type="button" variant="outline" size="icon" data-ask-step="next" disabled={shown.active === shown.items.length - 1} onClick={() => onStep(1)}>
                        <ChevronRight aria-hidden="true" /><span className="sr-only">Next passage</span>
                      </Button>
                    </>
                  ) : null}
                  <Button type="button" variant="outline" data-ask-back onClick={() => onBackToAnswer(shown.active)}>
                    <ArrowLeft aria-hidden="true" />
                    Back to answer
                  </Button>
                </div>
              )}
            </div>
          ) : null}
          {page.image ? (
            <div className="relative">
              {/* eslint-disable-next-line @next/next/no-img-element -- static page images, sized by the manifest */}
              <img className={`block h-auto w-full bg-white ${CARD}`} {...pageImageSet(base, page.image, imageWidths, version)}
                   sizes={PAGE_IMAGE_SIZES}
                   style={{ aspectRatio: ratio }}
                   width={page.width} height={page.height} loading="lazy" decoding="async"
                   alt={`Image of original page ${label}. Switch to the accessible transcript view to read its text.`} />
              {shown?.items.map((it, i) => it.page === page.number && it.region ? (
                // Decoration: the section's text is in the transcript, so nothing here is read out.
                // A dark line with a white halo reads on any page, and the tint adds a second cue.
                <div key={i} aria-hidden="true" data-source-overlay data-active={i === shown.active ? "" : undefined}
                     className="pointer-events-none absolute rounded-[3px] border-[3px] border-solid border-[#111827] bg-[#facc15]/20 outline-2 outline-white forced-colors:border-[Highlight]"
                     style={boxPercent(it.region.bbox, it.region.width, it.region.height)} />
              ) : null)}
            </div>
          ) : <p className={`m-0 p-6 text-muted-foreground italic ${CARD}`}>No image of this page.</p>}
        </>
      ) : html === undefined ? (
        chunkState === "failed" && chunkHref ? (
          <PageError height={estimate - labelPx(mode)} label={label} first={chunkFirst}
                     onRetry={() => onRetry(chunkHref)} />
        ) : (
          <PageSkeleton height={estimate - labelPx(mode)} label={label} />
        )
      ) : html.trim() ? (
        // Click delegation only: the injected buttons are the focusable controls.
        <div ref={body} data-transcript lang={lang} dir={dir} onClick={onTranscriptClick}
             className={TRANSCRIPT} dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <p className="m-0 text-muted-foreground italic">This page has no text.</p>
      )}
    </div>
  );
});
