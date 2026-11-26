"use client";

import { lazy, Suspense, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createPortal, flushSync } from "react-dom";
import { BookOpenText, FileText, Keyboard, ListTree, X } from "lucide-react";
import { chunkFor, resolve, type Manifest } from "../core";
import { useReflowDocument, type ViewMode } from "./useReflowDocument";
import { Button } from "@/components/ui/native-button";
import { clampPage, pageText } from "@/lib/pages";
import type { AnswerResult } from "@/lib/ask-contract";
import { sectionRegion, type PageSource, type SourceRegion } from "@/lib/source-region";
import { matchesMedia, useMediaQuery } from "@/lib/media";
import { plural } from "@/lib/format";
import { ViewModeToggle } from "./ViewModeToggle";
import { useFind } from "./useFind";
import { clearMarks, markMatch } from "./find";
import { clearAskMarks, markQuote } from "./ask-highlight";
import { highlightedInTranscript, notInTranscript, shownOnPage } from "./ask-copy";
import type { AskFocusRequest } from "./AskSection";
import { useSidebarSections } from "./sidebar-sections";
import { findSpot, focusWhenPossible, spotOf, type FocusSpot } from "./sidebar-focus";
import { shortcutFor, typingIn } from "./shortcuts";
import { trackReadingPage } from "./page-at-line";
import { record, type TypedValues } from "./form-values";
import { Sidebar, sidebarLabel } from "./ViewerSidebar";
import { PageJump } from "./PageJump";
import { PageCard, type ShownSources, type SourceItem } from "./PageCard";

// React.lazy, not next/dynamic: Next's loadable wrapper would be part of the first load.
const ErrorState = lazy(() => import("@/components/app/error-state").then((m) => ({ default: m.ErrorState })));

// Loaded on demand, outside the viewer's first load (SPEC §6: shell + first chunk under
// 200 KB). The two dialogs bring Base UI's popup machinery (focus trap, scroll lock,
// dismiss), which a reader who never opens them does not need; they are mounted the first
// time they open and stay mounted after, so closing still animates and returns focus. The
// find bar is small but lives in its own file, so it joins once the viewer has hydrated.
const ContentsSheet = lazy(() => import("./ContentsSheet").then((m) => ({ default: m.ContentsSheet })));
const ShortcutsDialog = lazy(() => import("./ShortcutsDialog").then((m) => ({ default: m.ShortcutsDialog })));
const FindBar = lazy(() => import("./FindBar").then((m) => ({ default: m.FindBar })));
// "Ask this document": the question state, the form and the answer panel, in one lazily loaded
// piece. It loads when the Ask section is first opened (or was open when the page was reloaded),
// so a viewer that never asks carries none of it.
const AskController = lazy(() => import("./AskSection").then((m) => ({ default: m.AskController })));
// Holds the field's place while its code arrives, so the toolbar does not shift.
const FIND_PLACEHOLDER = <div aria-hidden="true" className="hidden h-8 sm:block sm:w-48" />;

type Props = {
  /** URL of a reflow output directory, e.g. "/docs/resnet/". */
  base: string;
  /** Dialog mode: shows a title row with the document title (titleId) and a Close button. */
  onClose?: () => void;
  /** id for the dialog's accessible name. Only used in dialog mode. */
  titleId?: string;
  /** The document's display title, for the pages region's name. The manifest's title is the file name when the PDF has none. */
  documentTitle?: string;
  /** Page to jump to once the document has loaded (the /view page's ?page=N). */
  initialPage?: number;
  /** Called when the page at the reading line changes. */
  onPageChange?: (page: number) => void;
  /**
   * Where one page's layout comes from (see /api/source). When given, each heading with no higher-level
   * heading above it on its page gets a "Show on page" button. Without it the buttons are left out.
   */
  pageSourceUrl?: (page: number) => string;
  /**
   * POST endpoint of "Ask this document" (/api/ask/<slug>). Given only for a document that can
   * answer (askReady); without it, or without a manifest version, the sidebar has no Ask section.
   */
  askUrl?: string;
  /** A page asked for from outside after the first load (the embed's parent, src/embed): each new
   *  `n` is one jump, as if the reader had typed the page. */
  pageRequest?: { page: number; n: number };
};

/** An answer's passage to mark in the transcript. `n` makes each request new. */
type TranscriptMark = { n: number; page: number; node: string; quote: string; section: string; result: AnswerResult; i: number };
/** What the next view switch should do besides keeping the page. */
type SwitchIntent = { say?: string; focusBack?: boolean; focusHeading?: boolean };

// Outline depth shown in the contents.

const noSubscription = () => () => {};

// The sidebar is a column from Tailwind's lg breakpoint up and a sheet below it.
const WIDE = "(min-width: 64rem)";
const isWide = () => matchesMedia(WIDE);

/** Move focus to `el` without scrolling, making it focusable first when it is not. */
function focusQuietly(el: HTMLElement): void {
  if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
  el.focus({ preventScroll: true });
}

/** Open every <details> that holds `el`, so it is not hidden. */
function openDetailsAround(el: Element): void {
  for (let d = el.closest("details"); d; d = d.parentElement?.closest("details") ?? null) d.open = true;
}

/** The innermost section of an answer's "A > B > C" section path. */
const innermostSection = (path: string): string => path.split(" > ").pop()?.trim() ?? "";

const OUTLINE_DEPTH = 3;
// How long a filled PDF opened for printing keeps its blob URL.
const PRINT_URL_MS = 60_000;
// How long a heading stays outlined after a jump to it.
const JUMP_MARK_MS = 1800;
// How long after the page has loaded the viewer starts fetching the chunks ahead of the reader.
const READ_AHEAD_DELAY_MS = 2000;


/** Whether page 1 already opens with the title: a title the converter found on the page
 *  (the manifest's titleOnPage), or a first heading that reads the same. The large title above
 *  it would only say it twice. */
export function opensWithTitle(manifest: Pick<Manifest, "toc" | "titleOnPage">, title: string): boolean {
  if (manifest.titleOnPage) return true;
  const same = (s: string) => s.replace(/\s+/g, " ").trim().toLocaleLowerCase();
  const first = manifest.toc[0];
  return !!first && first.page === 1 && same(first.text) === same(title);
}

export function DocumentViewer({ base, onClose, titleId, documentTitle, initialPage, onPageChange, pageSourceUrl, askUrl, pageRequest }: Props) {
  const doc = useReflowDocument(base);
  const { manifest, error, pages, chunkStates, ensurePage, retryChunk, readChunk, loadAll, mode, setMode } = doc;
  const fallbackId = useId();
  const headingId = titleId ?? `${fallbackId}-title`;
  const scroller = useRef<HTMLDivElement>(null);
  const [current, setCurrent] = useState(initialPage ?? 1);
  // The live region's message. Setting the same text twice would change nothing in the DOM and
  // so be silent; a trailing no-break space on every second message makes it a new announcement.
  const [announcement, setAnnouncement] = useState({ text: "", n: 0 });
  const say = useCallback((text: string) => setAnnouncement((a) => ({ text, n: a.n + 1 })), []);
  // The heading just jumped to, outlined for a moment.
  const jumpMark = useRef<{ el: HTMLElement; timer: number } | null>(null);
  const [loadingAll, setLoadingAll] = useState(false);
  const [contentsOpen, setContentsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  // The lazily loaded sheet and dialog mount the first time they open, then stay mounted.
  const [contentsMounted, setContentsMounted] = useState(false);
  const [helpMounted, setHelpMounted] = useState(false);
  // False on the server and in the first client render, so the lazy find bar joins after hydration.
  const hydrated = useSyncExternalStore(noSubscription, () => true, () => false);
  const contentsTrigger = useRef<HTMLButtonElement>(null);
  const [downloading, setDownloading] = useState(false);
  const [printing, setPrinting] = useState(false);
  // Blob URLs of filled PDFs opened for printing, revoked on a timer and when the viewer goes.
  const printUrls = useRef(new Set<string>());
  useEffect(() => {
    const urls = printUrls.current;
    return () => { for (const u of urls) URL.revokeObjectURL(u); urls.clear(); };
  }, []);
  const [fillMessage, setFillMessage] = useState("");
  const [fillingPdf, setFillingPdf] = useState<"download" | "print" | null>(null);
  // What the reader typed into the transcript's form controls, by control id. Kept here, not
  // read back from the DOM: a page card that scrolls away and returns is mounted again from the
  // converted HTML (PageCard restores from this), and the downloads need every page.
  const [typed] = useState<TypedValues>(() => new Map());
  const [findQuery, setFindQuery] = useState("");
  const [findOpen, setFindOpen] = useState(false);
  const [findFocus, setFindFocus] = useState(0);
  const findInputRef = useRef<HTMLInputElement>(null);
  const [shown, setShown] = useState<ShownSources | null>(null);
  const [transcriptMark, setTranscriptMark] = useState<TranscriptMark | null>(null);
  const [answer, setAnswer] = useState<AnswerResult | null>(null);
  // Ask this document lives in the sidebar's second section: the column's or the sheet's, whichever
  // shows. Its form is drawn into that section's host element; its header takes focus back on Escape.
  const [sections, setSection] = useSidebarSections();
  const [askSeen, setAskSeen] = useState(false);
  const [columnAskHost, setColumnAskHost] = useState<HTMLDivElement | null>(null);
  const [sheetAskHost, setSheetAskHost] = useState<HTMLDivElement | null>(null);
  const columnAskHeader = useRef<HTMLButtonElement>(null);
  const sheetAskHeader = useRef<HTMLButtonElement>(null);
  // Where focus is in the sidebar, for when the window crosses the breakpoint: by then the focused
  // control is gone, and a control that is hidden or removed under focus fires no focusin for what
  // replaces it. So the last focusin is kept (a focusout is no sign: the window can fire one while
  // it is resized), and cleared when focus goes somewhere else (a focusin outside the sidebar, or a
  // click outside it, which can leave focus on <body> with no focusin). A listener on the media
  // query reads the focus too, registered before the one that re-renders (the effects run in order).
  const focusSpot = useRef<FocusSpot | null>(null);
  useEffect(() => {
    const into = (e: Event) => { focusSpot.current = spotOf(e.target); };
    const query = window.matchMedia(WIDE);
    const crossing = () => { focusSpot.current = spotOf(document.activeElement) ?? focusSpot.current; };
    document.addEventListener("focusin", into);
    document.addEventListener("pointerdown", into, true);
    query.addEventListener("change", crossing);
    return () => {
      document.removeEventListener("focusin", into);
      document.removeEventListener("pointerdown", into, true);
      query.removeEventListener("change", crossing);
    };
  }, []);
  const wide = useMediaQuery(WIDE);
  const askHost = wide ? columnAskHost : sheetAskHost;
  // The sheet is for narrow windows: on a wide one it is not open, whatever it was a moment ago.
  if (wide && contentsOpen) setContentsOpen(false);
  const [boxRequest, setBoxRequest] = useState(0);
  const [answerRequest, setAnswerRequest] = useState<AskFocusRequest>({ n: 0, evidence: null });
  const markDone = useRef(0);
  const markSeq = useRef(0);
  const intent = useRef<SwitchIntent | null>(null);
  const sourceCache = useRef(new Map<number, Promise<PageSource | null>>());
  const asideRef = useRef<HTMLElement>(null);
  const pageInputRef = useRef<HTMLInputElement>(null);
  const pickedFromSheet = useRef(false);
  const initialDone = useRef(false);
  // The width of the scroller's content: page cards estimate their height from it.
  const [contentWidth, setContentWidth] = useState(0);
  // The page at the reading line, readable in a layout effect before the observers catch up.
  const currentRef = useRef(current);
  // A page the view was just moved to: it stays the current page until the reader scrolls, so
  // a short page above the reading line doesn't hand "current" to the page after it.
  const pinned = useRef<number | null>(null);
  const shownMode = useRef(mode);

  // Tell the page (the workspace header's view switch) which page is at the reading line.
  useEffect(() => { currentRef.current = current; onPageChange?.(current); }, [current, onPageChange]);

  // The reader's own scrolling (wheel, touch, keys, the scrollbar) releases a pinned page.
  useEffect(() => {
    const root = scroller.current;
    if (!root) return;
    const release = () => { pinned.current = null; };
    const events = ["wheel", "touchstart", "keydown", "pointerdown"] as const;
    events.forEach((e) => root.addEventListener(e, release, { passive: true }));
    return () => events.forEach((e) => root.removeEventListener(e, release));
  }, []);

  useEffect(() => {
    const root = scroller.current;
    if (!root) return;
    const sizer = new ResizeObserver(([entry]) => setContentWidth(Math.round(entry.contentRect.width)));
    sizer.observe(root);
    return () => sizer.disconnect();
  }, []);

  // Read ahead only once the page has finished loading and settled: until then a chunk is
  // fetched only for a page on screen, so the first load is the first chunk and nothing more.
  const [readAhead, setReadAhead] = useState(false);
  useEffect(() => {
    let timer = 0;
    const arm = () => { timer = window.setTimeout(() => setReadAhead(true), READ_AHEAD_DELAY_MS); };
    if (document.readyState === "complete") arm();
    else window.addEventListener("load", arm, { once: true });
    return () => { window.removeEventListener("load", arm); window.clearTimeout(timer); };
  }, []);
  // A link to a page (initialPage) loads the chunk of that page and no other: the loader waits
  // for the jump, instead of first loading the chunk of page 1 that the jump leaves at once.
  const holdLoader = useRef(initialPage !== undefined);

  // Load chunks as their pages come near, and track the page at the reading line.
  useEffect(() => {
    const root = scroller.current;
    if (!root || !manifest) return;
    const cards = [...root.querySelectorAll<HTMLElement>("[data-page]")];
    const loader = new IntersectionObserver((entries) => {
      if (holdLoader.current) return;
      for (const e of entries) {
        if (e.isIntersecting) ensurePage(Number((e.target as HTMLElement).dataset.page)).catch(() => {});
      }
    }, { root, rootMargin: readAhead ? "1500px 0px" : "0px" });
    const stopReading = trackReadingPage({
      root,
      cards: () => cards,
      isPinned: () => pinned.current !== null,
      setPage: setCurrent,
    });
    cards.forEach((c) => loader.observe(c));
    return () => { loader.disconnect(); stopReading(); };
  }, [manifest, ensurePage, readAhead]);

  // Outline the heading just jumped to for ~1.8 s. The mark is an attribute on the heading
  // (styled in transcript-style.ts): an outline plus a tint, still when motion is reduced.
  const markJump = useCallback((el: HTMLElement) => {
    if (jumpMark.current) {
      window.clearTimeout(jumpMark.current.timer);
      jumpMark.current.el.removeAttribute("data-jump");
    }
    el.setAttribute("data-jump", "");
    const timer = window.setTimeout(() => {
      el.removeAttribute("data-jump");
      jumpMark.current = null;
    }, JUMP_MARK_MS);
    jumpMark.current = { el, timer };
  }, []);
  useEffect(() => () => {
    if (jumpMark.current) window.clearTimeout(jumpMark.current.timer);
  }, []);

  // Switching between the transcript and the original pages keeps the page being read. The
  // two views' cards differ in height, so the old scroll offset would land on another page;
  // this runs before paint, so the wrong page is never shown. Focus stays on the switch,
  // unless the switch came from "Show on page" or "Back to transcript".
  useLayoutEffect(() => {
    if (shownMode.current === mode) return;
    shownMode.current = mode;
    const root = scroller.current;
    const intended = intent.current;
    intent.current = null;
    // Coming back from a section shown on its page: return to that page and its heading.
    const back = mode === "transcript" && shown?.origin === "heading" ? shown.items[0] : null;
    const page = back ? back.page : currentRef.current;
    const label = root?.querySelector<HTMLElement>(`[data-page="${page}"] [data-page-label]`);
    if (!root || !label) return;
    pinned.current = page;
    currentRef.current = page;
    setCurrent(page);
    label.scrollIntoView({ block: "start" });
    ensurePage(page).catch(() => {});
    const view = mode === "original" ? "Original pages" : "Accessible transcript";
    if (mode === "original" && shown) {
      root.querySelector<HTMLElement>("[data-source-overlay][data-active]")?.scrollIntoView({ block: "center" });
      if (intended?.focusBack) root.querySelector<HTMLElement>(shown.origin === "ask" ? "[data-ask-back]" : "[data-source-back]")?.focus({ preventScroll: true });
      say(intended?.say ?? `${view}, page ${page}.`);
      return;
    }
    // A switch to the transcript by hand leaves an answer's panel as it is; its outlines go.
    if (mode === "transcript" && shown?.origin === "ask") setShown(null);
    if (back) {
      const heading = root.querySelector<HTMLElement>(`[data-page="${back.page}"] [data-node="${CSS.escape(back.nodeId)}"]`);
      setShown(null);
      if (heading) {
        heading.scrollIntoView({ block: "start" });
        markJump(heading);
        if (intended?.focusHeading) {
          focusQuietly(heading);
        }
        say(`Back to ${back.text} in the accessible transcript, page ${page}.`);
        return;
      }
    }
    say(`${view}, page ${page}.`);
  }, [mode, shown, ensurePage, say, markJump]);

  const goTo = useCallback(async (page: number, anchor?: string, label?: string) => {
    // A TOC entry with a bad page number (NaN, 1.5) is not a jump, so it starts no status.
    if (!manifest || !Number.isInteger(page)) return;
    const target = clampPage(page, manifest.pageCount);
    say(`Going to page ${target}…`);
    try {
      await ensurePage(target);
    } catch {
      say(`Page ${target} could not be loaded.`);
      return;
    }
    // Wait a frame so the loaded HTML is in the DOM.
    requestAnimationFrame(() => {
      const root = scroller.current;
      const heading = root && anchor && mode === "transcript"
        ? root.querySelector<HTMLElement>(`#${CSS.escape(anchor)}`)
        : null;
      const el = heading || root?.querySelector<HTMLElement>(`[data-page="${target}"] [data-page-label]`);
      if (!root || !el) {
        say(`Page ${target} could not be shown.`);
        return;
      }
      if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
      pinned.current = target;
      setCurrent(target);
      el.scrollIntoView({ block: "start" });
      el.focus({ preventScroll: true });
      if (heading) markJump(heading);
      // After a contents pick the heading is announced; a plain page jump needs no message,
      // because focus has moved to the page label, which reads "Page N of M".
      if (label) say(heading ? `Jumped to ${label}` : `Jumped to ${label}, page ${target}`);
      else say("");
    });
  }, [manifest, ensurePage, mode, say, markJump]);

  // "Show on page": switch to the original pages at the heading's page, with the section's
  // region outlined on the page image. Layout comes from pageSourceUrl, one page at a time.
  const showSource = useCallback(async (page: number, nodeId: string, text: string) => {
    if (!pageSourceUrl) return;
    say(`Finding ${text} on page ${page}…`);
    let region: SourceRegion | null = null;
    try {
      let pending = sourceCache.current.get(page);
      if (!pending) {
        pending = fetch(pageSourceUrl(page)).then((r) => (r.ok ? (r.json() as Promise<PageSource>) : null));
        sourceCache.current.set(page, pending);
        pending.catch(() => sourceCache.current.delete(page));
      }
      const layout = await pending;
      region = layout ? sectionRegion(layout, nodeId) : null;
    } catch { /* the page shows without an outline */ }
    // Committed before the view switches, so the switch finds the overlay in place.
    flushSync(() => setShown({ origin: "heading", items: [{ page, nodeId, text, region }], active: 0 }));
    currentRef.current = page;
    intent.current = {
      focusBack: true,
      say: region
        ? `Showing ${text} on page ${page}. Press Back to transcript to return.`
        : `Showing page ${page}. The outline for ${text} is not available.`,
    };
    setMode("original");
  }, [pageSourceUrl, say, setMode]);

  const backToTranscript = useCallback(() => {
    intent.current = { focusHeading: true };
    setMode("transcript");
  }, [setMode]);

  // ---- Ask this document: the highlights. The controller (AskBar.tsx) owns the question and the panel.

  const labelOf = useCallback((page: number) => pageText(page, manifest?.pages[page - 1]?.label), [manifest]);

  // Scroll to the active outline and move focus to the bar's "Back to answer". When the original
  // pages already show, no view switch runs the layout effect, so it is done here.
  const revealShown = useCallback((page: number, spoken: string) => {
    requestAnimationFrame(() => {
      const root = scroller.current;
      if (!root) return;
      root.querySelector<HTMLElement>(`[data-page="${page}"] [data-page-label]`)?.scrollIntoView({ block: "start" });
      root.querySelector<HTMLElement>("[data-source-overlay][data-active]")?.scrollIntoView({ block: "center" });
      root.querySelector<HTMLElement>("[data-ask-back]")?.focus({ preventScroll: true });
      say(spoken);
    });
  }, [say]);

  const canShowOnPage = useCallback((page: number) => !!manifest?.pages[page - 1]?.image, [manifest]);

  // `said` replaces the announcement when "Show in transcript" fell back to this.
  const showEvidenceOnPage = useCallback((result: AnswerResult, i: number, said?: (shown: string) => string) => {
    const ev = result.evidence[i];
    const items: SourceItem[] = result.evidence.map((e) => ({ page: e.page, nodeId: e.node, text: e.quote, region: e.region }));
    const shownText = shownOnPage(labelOf(ev.page), innermostSection(ev.section));
    const spoken = said ? said(shownText) : shownText;
    flushSync(() => { setShown({ origin: "ask", items, active: i }); setTranscriptMark(null); });
    pinned.current = ev.page;
    currentRef.current = ev.page;
    if (mode === "original") {
      setCurrent(ev.page);
      revealShown(ev.page, spoken);
    } else {
      intent.current = { focusBack: true, say: spoken };
      setMode("original");
    }
  }, [mode, setMode, revealShown, labelOf]);

  // Previous and next passage in the bar. The bar moves to the page of the new active outline, so
  // focus goes to the same button there (or the other one at either end).
  const stepEvidence = useCallback((delta: number) => {
    if (!shown || shown.origin !== "ask") return;
    const next = shown.active + delta;
    if (next < 0 || next >= shown.items.length) return;
    const item = shown.items[next];
    flushSync(() => setShown({ ...shown, active: next }));
    pinned.current = item.page;
    currentRef.current = item.page;
    setCurrent(item.page);
    requestAnimationFrame(() => {
      const root = scroller.current;
      if (!root) return;
      root.querySelector<HTMLElement>(`[data-page="${item.page}"] [data-page-label]`)?.scrollIntoView({ block: "start" });
      root.querySelector<HTMLElement>("[data-source-overlay][data-active]")?.scrollIntoView({ block: "center" });
      const want = delta > 0 ? "next" : "prev";
      const other = delta > 0 ? "prev" : "next";
      const pick = root.querySelector<HTMLButtonElement>(`[data-ask-step="${want}"]:not(:disabled)`)
        ?? root.querySelector<HTMLButtonElement>(`[data-ask-step="${other}"]:not(:disabled)`);
      pick?.focus({ preventScroll: true });
    });
    const ev = answer?.evidence[next];
    say(shownOnPage(labelOf(item.page), ev ? innermostSection(ev.section) : ""));
  }, [shown, answer, say, labelOf]);

  // "Show in transcript" could not mark the passage: say so, and show it on the page image instead
  // when there is one. Once per request.
  const markFailed = useCallback((m: TranscriptMark) => {
    if (markDone.current === m.n) return;
    markDone.current = m.n;
    if (canShowOnPage(m.page)) showEvidenceOnPage(m.result, m.i, notInTranscript);
    else say(notInTranscript(null));
  }, [canShowOnPage, showEvidenceOnPage, say]);

  const showEvidenceInTranscript = useCallback((result: AnswerResult, i: number) => {
    const ev = result.evidence[i];
    setShown(null);
    const m: TranscriptMark = { n: ++markSeq.current, page: ev.page, node: ev.node, quote: ev.quote, section: ev.section, result, i };
    setTranscriptMark(m);
    ensurePage(ev.page).catch(() => { if (markSeq.current === m.n) markFailed(m); });
    if (mode !== "transcript") setMode("transcript");
  }, [mode, setMode, ensurePage, markFailed]);

  const openContents = useCallback(() => {
    pickedFromSheet.current = false;
    setContentsMounted(true);
    setContentsOpen(true);
  }, []);

  // Make the Ask section show: open it in the column, or, when only the sheet is on screen, open the
  // sheet with it. The controller loads on the first call (askSeen) and then moves focus.
  const revealAsk = useCallback(() => {
    setSection("ask", true);
    setAskSeen(true);
    if (!isWide()) openContents();
  }, [setSection, openContents]);

  // Escape in the empty question box: collapse the section, focus its header.
  const collapseAsk = useCallback(() => {
    setSection("ask", false);
    (isWide() ? columnAskHeader : sheetAskHeader).current?.focus();
  }, [setSection]);

  // The Ask buttons that move the view ("Show on page", "Show in transcript") act on the page, which
  // the open sheet covers: close it first, without sending focus back to its button.
  const leaveSheet = useCallback(() => {
    if (!contentsOpen) return;
    pickedFromSheet.current = true;
    setContentsOpen(false);
  }, [contentsOpen]);

  // Alt+Shift+R and "Back to answer": the Ask section opens and focus goes to the answer, or, from
  // "Back to answer", to the "Show on page" button of the passage that was shown.
  const returnToAnswer = useCallback((evidence: number | null = null) => {
    revealAsk();
    setAnswerRequest((r) => ({ n: r.n + 1, evidence }));
  }, [revealAsk]);

  // The controller reports each answer, and none when it is cleared or a new question starts: the
  // old outlines and marks go. An answer opens the section if the reader collapsed it meanwhile.
  const onAnswer = useCallback((next: AnswerResult | null) => {
    setAnswer(next);
    if (next) setSection("ask", true);
    setShown((s) => (s?.origin === "ask" ? null : s));
    setTranscriptMark(null);
    const root = scroller.current;
    if (root) clearAskMarks(root);
  }, [setSection]);

  // The passage in the transcript: mark the quote (or outline its block when the DOM splits it),
  // scroll to it and focus the block, so reading goes on from the cited place. Waits for the
  // page's HTML; acts once per request.
  const markPage = transcriptMark?.page;
  const markHtml = markPage ? pages[markPage] : undefined;
  useEffect(() => {
    const root = scroller.current;
    const m = transcriptMark;
    if (!root || !m || mode !== "transcript" || markDone.current === m.n) return;
    if (markHtml === undefined) { ensurePage(m.page).catch(() => {}); return; }
    const card = root.querySelector<HTMLElement>(`[data-page="${m.page}"] [data-transcript]`);
    const block = card?.querySelector<HTMLElement>(`[data-node="${CSS.escape(m.node)}"]`);
    if (!card || !block) {
      // Loaded, but the page has no such block. The microtask keeps flushSync out of this effect.
      queueMicrotask(() => markFailed(m));
      return;
    }
    markDone.current = m.n;
    clearAskMarks(root);
    const mark = markQuote(block, m.quote);
    if (!mark) block.setAttribute("data-ask-block", "");
    openDetailsAround(block);
    pinned.current = m.page;
    currentRef.current = m.page;
    setCurrent(m.page);
    (mark ?? block).scrollIntoView({ block: "center" });
    focusQuietly(block);
    say(highlightedInTranscript(labelOf(m.page), innermostSection(m.section)));
  }, [transcriptMark, markHtml, mode, ensurePage, say, labelOf, markFailed]);

  const askEnabled = !!askUrl && !!manifest?.version;
  // The controller loads the first time the section is open, and stays (it holds the question).
  if (askEnabled && sections.ask && !askSeen) setAskSeen(true);

  // Find in the whole transcript (useFind reads every chunk's text, not into the page).
  const getStartPage = useCallback(() => currentRef.current, []);
  const find = useFind({ manifest, readChunk, query: findQuery, getStartPage, onAnnounce: say });

  // Show the current match: on its page, in the transcript, wrapped in <mark>. Find searches the
  // transcript, so a match found while the original pages are showing switches the view first.
  const matchPage = find.position?.page;
  const matchHtml = matchPage ? pages[matchPage] : undefined;
  useEffect(() => {
    const root = scroller.current;
    if (!root) return;
    clearMarks(root);
    const pos = find.position;
    if (!pos || !find.query) return;
    if (mode !== "transcript") return;
    if (matchHtml === undefined) { ensurePage(pos.page).catch(() => {}); return; }
    const card = root.querySelector<HTMLElement>(`[data-page="${pos.page}"] [data-transcript]`);
    const mark = card && markMatch(card, find.query, pos.nth);
    if (!mark) return;
    // A match inside a closed <details> would be hidden.
    openDetailsAround(mark);
    pinned.current = pos.page;
    currentRef.current = pos.page;
    setCurrent(pos.page);
    mark.scrollIntoView({ block: "center" });
  }, [find.position, find.query, mode, matchHtml, ensurePage]);

  // Find searches the transcript: typing or stepping while the original pages show switches back
  // to it (the highlight is in the transcript's text). Switching by hand leaves the match alone.
  const findInTranscript = useCallback(() => { if (mode !== "transcript") setMode("transcript"); }, [mode, setMode]);
  const barFind = {
    ...find,
    next: () => { findInTranscript(); find.next(); },
    prev: () => { findInTranscript(); find.prev(); },
  };

  // The find shortcut moves focus to the box once it is showing.
  useEffect(() => {
    if (!findFocus) return;
    findInputRef.current?.focus();
    findInputRef.current?.select();
  }, [findFocus]);

  // Jump to the requested page once, when the manifest is in.
  useEffect(() => {
    if (!manifest || initialPage === undefined || initialDone.current) return;
    initialDone.current = true;
    goTo(initialPage).finally(() => { holdLoader.current = false; });
  }, [manifest, initialPage, goTo]);

  // Jump to each page asked for from outside, once the manifest is in.
  const requestDone = useRef(0);
  useEffect(() => {
    if (!manifest || !pageRequest || pageRequest.n === requestDone.current) return;
    requestDone.current = pageRequest.n;
    void goTo(pageRequest.page);
  }, [manifest, pageRequest, goTo]);

  const onLoadAll = async () => {
    setLoadingAll(true);
    say("Loading the entire document…");
    try {
      const failed = await loadAll();
      say(failed
        ? `${plural(failed, "section")} could not be loaded. Try again from the page.`
        : "The entire document is loaded. Find in page now searches all of it.");
    } finally {
      setLoadingAll(false);
    }
  };

  // Try a failed section again. Focus moves to its first page, because the retry button is
  // replaced by the page when the load works.
  // Stable, so the page cards it reaches stay memoized.
  const retryChunkAt = useCallback(async (href: string) => {
    say("Loading the section again…");
    const loaded = await retryChunk(href);
    say(loaded ? "The section is loaded." : "The section could not be loaded. Try again.");
    const first = manifest?.chunks.find((c) => c.href === href)?.first;
    if (loaded && first !== undefined) {
      requestAnimationFrame(() => {
        const label = scroller.current?.querySelector<HTMLElement>(`[data-page="${first}"] [data-page-label]`);
        if (!label) return;
        focusQuietly(label);
      });
    }
  }, [manifest, retryChunk, say]);

  // The whole transcript as one HTML file (transcript-file.ts, loaded when first asked for):
  // the download, and the print from a hidden frame, which is offered once every section is
  // loaded.
  const downloadTranscript = async () => {
    if (!manifest || downloading) return;
    setDownloading(true);
    say("Preparing the transcript download…");
    try {
      const { buildTranscriptFile, downloadFile } = await import("./transcript-file");
      const file = await buildTranscriptFile(manifest, base, readChunk, typed);
      downloadFile(file, `${base.split("/").filter(Boolean).pop() ?? "document"}-transcript.html`);
      say("The transcript download has started.");
    } catch {
      say("The transcript could not be prepared. Some pages did not load.");
    } finally {
      setDownloading(false);
    }
  };

  // Remember what is typed into a form control (the events bubble from the page cards).
  const onTyped = (e: React.SyntheticEvent) => record(typed, e.target as Element);

  // The original PDF with the typed values in its AcroForm fields, filled here in the browser
  // (fill-pdf.ts and pdf-lib load on the first click). Download, or open in a tab to print.
  // Announced to screen readers and shown under the buttons, for everyone else.
  const tell = (text: string) => { say(text); setFillMessage(text); };
  const fillOriginal = async (how: "download" | "print") => {
    if (!manifest?.original || fillingPdf) return;
    setFillingPdf(how);
    setFillMessage("");
    tell("Filling the PDF…");
    // A tab for the print has to be opened inside the click, before anything is awaited.
    const tab = how === "print" ? window.open("", "_blank") : null;
    try {
      const { collectFills, fillPdf, FillError } = await import("./fill-pdf");
      const { downloadBlob } = await import("./transcript-file");
      try {
        const html: string[] = [];
        for (const chunk of manifest.chunks) html.push(...(await readChunk(chunk.href)).values());
        const fills = collectFills(html, typed);
        const res = await fetch(resolve(base, manifest.original, manifest.version));
        if (!res.ok) throw new FillError("The original PDF could not be loaded.");
        const report = await fillPdf(await res.arrayBuffer(), fills);
        const blob = new Blob([report.bytes], { type: "application/pdf" });
        const note = [
          report.skipped.length ? ` ${plural(report.skipped.length, "field was", "fields were")} left out because the PDF would not take ${report.skipped.length === 1 ? "it" : "them"}.` : "",
          report.noAppearance.length ? ` ${plural(report.noAppearance.length, "value uses", "values use")} characters the PDF's standard font cannot draw; ${report.noAppearance.length === 1 ? "it is" : "they are"} saved in the form and show in readers that redraw fields, such as Acrobat.` : "",
          report.droppedXfa ? " This form's XFA layer was dropped so the filled values show in any PDF reader." : "",
        ].join("");
        if (how === "print") {
          if (tab) {
            const url = URL.createObjectURL(blob);
            printUrls.current.add(url);
            tab.location.href = url;
            // The tab has long since loaded it; free the bytes after a generous delay.
            window.setTimeout(() => { URL.revokeObjectURL(url); printUrls.current.delete(url); }, PRINT_URL_MS);
            tell(`The filled PDF is open in a new tab, where you can print it.${note}`);
          } else { downloadBlob(blob, "filled.pdf"); tell(`Your browser blocked the new tab, so the filled PDF was downloaded instead. Open it to print.${note}`); }
        } else {
          downloadBlob(blob, `${base.split("/").filter(Boolean).pop() ?? "document"}-filled.pdf`);
          tell(`The filled PDF download has started.${note}`);
        }
      } catch (err) {
        tab?.close();
        if (err instanceof FillError) tell(err.message);
        else throw err;
      }
    } catch {
      tab?.close();
      tell("The filled PDF could not be prepared. Try again, or download the transcript (HTML).");
    } finally {
      setFillingPdf(null);
    }
  };

  const printTranscript = async () => {
    if (!manifest || printing) return;
    setPrinting(true);
    say("Preparing the transcript to print…");
    try {
      const { buildTranscriptFile, printFile } = await import("./transcript-file");
      printFile(await buildTranscriptFile(manifest, base, readChunk, typed));
      say("The transcript is ready to print.");
    } catch {
      say("The transcript could not be prepared for printing. Some pages did not load.");
    } finally {
      setPrinting(false);
    }
  };

  // Choosing an entry in the sheet jumps to it; the sheet must not then return focus to its
  // trigger, because goTo has moved focus to the page label.
  const pickFromSheet = (page: number, id: string, text: string) => {
    pickedFromSheet.current = true;
    setContentsOpen(false);
    goTo(page, id, text);
  };

  const outline = useMemo(() => manifest?.toc.filter((e) => e.level <= OUTLINE_DEPTH) ?? [], [manifest]);

  const openHelp = useCallback(() => { setHelpMounted(true); setHelpOpen(true); }, []);

  // Crossing the breakpoint is a change of home for the sidebar, and for focus with it.
  //  - To wide with the sheet open: the sheet is a modal over a layout that has the column, so it
  //    closes (above, where `wide` is read); focus goes to the same control in the column (the
  //    answer, the box with its draft, a passage button, a contents entry), or the table of
  //    contents header when it has no twin.
  //  - To narrow with focus in the column: the control is gone. The sheet is not opened for the
  //    reader (a modal that covers the page they are reading, because the window was zoomed or
  //    turned, is a change they did not ask for); focus goes to the toolbar button that opens it,
  //    and the status says where the sidebar went. The question, the draft and the answer are kept
  //    by the controller and are there when the sheet opens (the button, or Alt+Shift+A).
  const wasWide = useRef(wide);
  useEffect(() => {
    if (wasWide.current === wide) return;
    wasWide.current = wide;
    const spot = focusSpot.current;
    if (wide) {
      pickedFromSheet.current = true; // the sheet has closed (see where `wide` is read); its button is not on screen
      if (spot?.home === "sheet") return focusWhenPossible(() => findSpot("column", spot));
    } else if (spot?.home === "column") {
      contentsTrigger.current?.focus();
      say(`The sidebar is now in a panel. Press the ${sidebarLabel(askEnabled)} button to open it.`);
    }
  }, [wide, askEnabled, say]);

  // Keyboard shortcuts (see shortcuts.ts). Alt+Shift chords only, matched on e.code. In a text
  // field only the ones that move focus away still work. Shortcuts pause while a dialog or the
  // contents sheet is open: they would act on the page behind it. The two Ask shortcuts are the
  // exception for the sheet, since the Ask item is in it, and so is the outline. The listener is replaced only when
  // something it reads changes, not on every render.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const shortcut = shortcutFor(e);
      if (!shortcut || !manifest || helpOpen) return;
      if (contentsOpen && shortcut.id !== "ask" && shortcut.id !== "answer" && shortcut.id !== "outline") return;
      if (typingIn(e.target) && !shortcut.inFields) return;
      e.preventDefault();
      switch (shortcut.id) {
        case "view":
          setMode(mode === "transcript" ? "original" : "transcript");
          break;
        case "page":
          pageInputRef.current?.focus();
          pageInputRef.current?.select();
          break;
        case "outline": {
          // Open the table of contents item if it is collapsed, then land on the entry for the page
          // being read, in whichever sidebar shows: the column, or the sheet (opened if need be).
          if (!sections.toc) flushSync(() => setSection("toc", true));
          const home = isWide() ? "column" : "sheet";
          const entry = () => {
            const root = document.querySelector<HTMLElement>(home === "column" ? "aside[aria-label]" : "[data-slot=sheet-content]");
            const links = [...(root?.querySelectorAll<HTMLElement>("nav a") ?? [])];
            let at = -1;
            outline.forEach((e, i) => { if (e.page <= current) at = i; });
            return links[Math.max(0, at)] ?? root?.querySelector<HTMLElement>("a, button") ?? null;
          };
          if (home === "sheet" && !contentsOpen) openContents();
          focusWhenPossible(entry);
          break;
        }
        case "next":
        case "prev": {
          const target = current + (shortcut.id === "next" ? 1 : -1);
          if (target < 1 || target > manifest.pageCount) {
            say(shortcut.id === "next" ? `This is the last page, page ${current}.` : `This is the first page, page ${current}.`);
          } else {
            goTo(target);
          }
          break;
        }
        case "find":
          setFindOpen(true);
          setFindFocus((n) => n + 1);
          break;
        case "ask":
          if (askEnabled) { revealAsk(); setBoxRequest((n) => n + 1); }
          break;
        case "answer":
          if (askEnabled) returnToAnswer();
          break;
        case "help":
          openHelp();
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [manifest, helpOpen, contentsOpen, mode, current, outline, goTo, say, setMode, openContents, openHelp, askEnabled, returnToAnswer, revealAsk, sections.toc, setSection]);

  if (error) {
    return (
      <div className="flex min-h-0 flex-1 flex-col bg-background text-foreground">
        {onClose ? (
          <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b px-3">
            <h2 id={headingId} className="truncate type-label font-semibold">Document unavailable</h2>
            <Button type="button" variant="outline" onClick={onClose}><X aria-hidden="true" />Close</Button>
          </div>
        ) : null}
        <div className="grid flex-1 place-items-center p-6">
          <Suspense fallback={null}>
            <ErrorState
              title="This document could not be loaded"
              description={`Its transcript files did not load (${error}). Try again in a moment.`}
              onRetry={() => window.location.reload()}
              homeHref={onClose ? undefined : "/"}
            />
          </Suspense>
        </div>
      </div>
    );
  }

  const original = manifest?.original ? resolve(base, manifest.original) : null;
  const allLoaded = manifest ? manifest.pages.every((p) => p.number in pages) : false;
  const contentsProps = {
    pageCount: manifest?.pageCount ?? null,
    outline,
    tocComplete: manifest?.tocComplete ?? true,
    lang: manifest?.lang,
    dir: manifest?.dir,
    ltrLang: manifest?.ltrLang,
    original,
    allLoaded,
    loadingAll,
    onLoadAll,
    mode,
    downloading,
    onDownloadTranscript: downloadTranscript,
    printing,
    onPrintTranscript: printTranscript,
    fillable: !!manifest?.fillable && !!manifest.original,
    fillingPdf,
    fillMessage,
    onFillPdf: fillOriginal,
  };

  // Names the column and the sheet (and the toolbar button that opens it).
  const sidebarName = sidebarLabel(askEnabled);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col bg-background text-foreground">
      {onClose ? (
        <div className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
          <h2 id={headingId} lang={manifest?.lang} dir={manifest?.dir} className="min-w-0 flex-1 truncate type-label font-semibold">{manifest?.title ?? "Loading…"}</h2>
          <Button type="button" variant="outline" onClick={onClose}>
            <X aria-hidden="true" />
            Close<span className="sr-only"> document viewer</span>
          </Button>
        </div>
      ) : null}

      <div role="toolbar" aria-label="Document view"
           className="flex min-h-11 shrink-0 flex-wrap items-center gap-2 border-b bg-background px-2 py-1 sm:px-3">
        {/* A plain button, not a SheetTrigger: the sheet's code is not here until it opens. */}
        <Button ref={contentsTrigger} variant="outline" size="icon-lg" aria-label={sidebarName} className="lg:hidden"
                aria-haspopup="dialog" aria-expanded={contentsOpen}
                onClick={() => { pickedFromSheet.current = false; openContents(); }}>
          <ListTree aria-hidden="true" />
        </Button>
        {contentsMounted ? (
          <Suspense fallback={null}>
          <ContentsSheet
            open={contentsOpen}
            onOpenChange={(open) => { if (open) pickedFromSheet.current = false; setContentsOpen(open); }}
            // After a pick, goTo has moved focus to the page label; otherwise back to the button.
            finalFocus={() => (pickedFromSheet.current ? false : contentsTrigger.current)}
            title={sidebarName}
          >
            <Sidebar where="sheet" id={fallbackId} contents={contentsProps} onPick={pickFromSheet}
                     sections={sections} setSection={setSection} askEnabled={askEnabled} onAskOpen={() => setAskSeen(true)}
                     askHeader={sheetAskHeader} askHost={setSheetAskHost} />
          </ContentsSheet>
          </Suspense>
        ) : null}
        <ViewModeToggle<ViewMode>
          value={mode}
          onChange={setMode}
          options={[
            { value: "transcript", label: "Accessible transcript", shortLabel: "Transcript", icon: <BookOpenText aria-hidden="true" /> },
            { value: "original", label: "Original pages", shortLabel: "Pages", icon: <FileText aria-hidden="true" /> },
          ]}
        />
        <PageJump current={current} count={manifest?.pageCount ?? 0} onGo={goTo} inputRef={pageInputRef} />
        {hydrated ? (
          <Suspense fallback={FIND_PLACEHOLDER}>
            <FindBar value={findQuery} find={barFind}
                     onChange={(v) => { if (v.trim()) findInTranscript(); setFindQuery(v); }}
                     open={findOpen} onOpenChange={setFindOpen} inputRef={findInputRef} />
          </Suspense>
        ) : FIND_PLACEHOLDER}
        <Button type="button" variant="outline" size="icon-lg" className="ml-auto"
                aria-label="Keyboard shortcuts" aria-keyshortcuts="Alt+Shift+K" aria-haspopup="dialog"
                onClick={openHelp}>
          <Keyboard aria-hidden="true" />
        </Button>
      </div>
      {helpMounted ? <Suspense fallback={null}><ShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} /></Suspense> : null}

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)] grid-rows-[minmax(0,1fr)] lg:grid-cols-[18rem_minmax(0,1fr)]">
        <aside ref={asideRef} aria-label={sidebarName} className="relative hidden min-h-0 flex-col overflow-y-auto border-r bg-card lg:flex">
          <Sidebar where="column" id={fallbackId} contents={contentsProps} onPick={(page, id, text) => goTo(page, id, text)}
                   sections={sections} setSection={setSection} askEnabled={askEnabled} onAskOpen={() => setAskSeen(true)}
                   askHeader={columnAskHeader} askHost={setColumnAskHost} />
        </aside>

        {/* relative: the sr-only labels inside must not escape this scroller (that would scroll the window). */}
        {/* A scrollable region must be reachable by keyboard even when no page in it holds a
            focusable element (axe scrollable-region-focusable): tabIndex 0, named as a region. */}
        <div ref={scroller} tabIndex={0} role="region" aria-label={manifest ? `Pages of ${documentTitle || manifest.title}` : "Document pages"}
             onInput={onTyped} onChange={onTyped}
             className={`relative flex min-h-0 flex-col overflow-y-auto pt-4 pb-16 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset lg:px-8 ${mode === "original" ? "gap-6 bg-muted/40 px-2 sm:px-4" : "gap-10 px-4 sm:px-6"}`}>
          {!manifest && <p aria-busy="true" className="p-8 text-muted-foreground">Loading document…</p>}
          {/* The title, set large at the head of the transcript as the standalone HTML sets
              its h1. The page's h1 is the header's, so this copy is hidden from screen readers. */}
          {manifest && mode !== "original" && !manifest.titleFromFileName && pages[1] !== undefined && !opensWithTitle(manifest, documentTitle || manifest.title) ? (
            <p aria-hidden="true" lang={manifest.lang} dir={manifest.dir} data-doc-title
               className="mx-auto -mb-4 w-full max-w-[48rem] text-[1.5rem] leading-tight sm:text-[1.875rem] font-bold text-balance">
              {documentTitle || manifest.title}
            </p>
          ) : null}
          {manifest?.pages.map((p) => {
            const chunk = chunkFor(manifest, p.number);
            return (
              <PageCard key={p.number} page={p} base={base} version={manifest.version} imageWidths={manifest.imageWidths} mode={mode}
                        lang={manifest.lang} dir={manifest.dir}
                        html={pages[p.number]} count={manifest.pageCount} contentWidth={contentWidth}
                        chunkState={chunk ? chunkStates[chunk.href] : undefined}
                        chunkHref={chunk?.href}
                        chunkFirst={chunk?.first === p.number}
                        onRetry={retryChunkAt}
                        canShowSource={!!pageSourceUrl}
                        onShowSource={showSource}
                        onGoToPage={goTo}
                        shown={shown?.items.some((it) => it.page === p.number) ? shown : null}
                        typed={typed} onBack={backToTranscript}
                        onBackToAnswer={returnToAnswer} onStep={stepEvidence} />
            );
          })}
        </div>
      </div>

      {/* The controller owns the question and the answer and draws its form into the Ask section of
          the sidebar that shows. It is outside the aside and the sheet, so a collapsing section, a
          closing sheet or a resize does not lose them. */}
      {askEnabled && hydrated && askSeen && askUrl && manifest?.version ? (
        <Suspense fallback={askHost ? createPortal(<p className="m-0 type-small text-muted-foreground">Loading…</p>, askHost) : null}>
          <AskController url={askUrl} version={manifest.version} say={say} pageLabel={labelOf}
                         lang={manifest.lang} dir={manifest.dir} canShowOnPage={canShowOnPage}
                         host={askHost} boxRequest={boxRequest} answerRequest={answerRequest}
                         active={shown?.origin === "ask" ? shown.active : null}
                         onCollapse={collapseAsk} onAnswer={onAnswer}
                         onShowOnPage={(result, i) => { leaveSheet(); showEvidenceOnPage(result, i); }}
                         onShowInTranscript={(result, i) => { leaveSheet(); showEvidenceInTranscript(result, i); }} />
        </Suspense>
      ) : null}

      {/* Always mounted (outside the aside and the sheet), so announcements are never lost. */}
      <p className="sr-only" role="status" aria-live="polite">{announcement.text}{announcement.n % 2 ? "\u00a0" : ""}</p>
    </div>
  );
}



