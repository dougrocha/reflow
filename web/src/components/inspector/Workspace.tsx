"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { WorkspaceHeader } from "@/components/app/workspace-header";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { PageImageSet } from "@/reflow/core";
import { usePersisted } from "@/lib/storage";
import type { BuildInfo } from "@/lib/build-info";
import type { VariantTab } from "@/lib/variants/view";

import { styleFor, type InspectNode, type InspectTrace, type ReviewBar } from "@/lib/inspect";
import { PagePanel, type Zoom } from "./PagePanel";
import { TEXT_SCALES, TranscriptPanel } from "./TranscriptPanel";
import { DetailsPanel } from "./DetailsPanel";
import { Help, InspectorToolbar, MoreMenu } from "./TopBar";
import { PANEL_LABELS, PANEL_ORDER, type PanelId } from "./panels";
import { DEFAULT_LAYOUT, LAYOUT_KEY, migrateLayout, validLayout } from "./layout-state";
import { DockLayout } from "./DockLayout";
import { inspectUrl } from "@/lib/urls";
import { useMediaQuery } from "@/lib/media";
import { typingIn } from "@/reflow/react/shortcuts";

const TEXT_KEY = "reflow:inspect-text-size";

function validScale(raw: unknown): number | null {
  return typeof raw === "number" && (TEXT_SCALES as number[]).includes(raw) ? raw : null;
}

const DESKTOP = "(min-width: 48rem)";

type WorkspaceProps = {
  slug: string;
  title: string;
  base: string;
  page: number;
  pageCount: number;
  pageText: string;
  width: number;
  height: number;
  image: PageImageSet;
  nodes: InspectNode[];
  trace: InspectTrace | null;
  selectedId: string | null;
  review: ReviewBar;
  prevPageHref: string | null;
  nextPageHref: string | null;
  pagesWithIssues: { page: number; count: number; label: string | null }[];
  viewerHref: string;
  /** The manifest `v` of the build this page shows: a decision is bound to it. */
  rev: string | null;
  /** `?supersedes=`: the orphaned decision a choice made here replaces. */
  supersedes: string | null;
  /** How the transcript was made (the header's panel). */
  build: BuildInfo | null;
  /** The document's built variants, for the tab strip. */
  variants: VariantTab[];
};

export function InspectorWorkspace(props: WorkspaceProps) {
  const { slug, title, base, page, pageCount, pageText, width, height, image, nodes, trace, selectedId, review, pagesWithIssues, prevPageHref, nextPageHref, viewerHref, rev, supersedes, build, variants } = props;
  const router = useRouter();

  const [layout, setLayout] = usePersisted(LAYOUT_KEY, DEFAULT_LAYOUT, validLayout);
  useEffect(() => { migrateLayout(); }, []);
  const [textScale, setTextScale] = usePersisted(TEXT_KEY, 1, validScale);
  const isDesktop = useMediaQuery(DESKTOP);

  // Layers: roles hidden on the page, and the text lines. Roles that are off by default start hidden.
  const [hidden, setHidden] = useState<Set<string>>(
    () => new Set([...new Set(nodes.map((n) => n.role))].filter((r) => !styleFor(r).on)),
  );
  const [showLines, setShowLines] = useState(false);
  const toggleRole = useCallback((role: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(role)) next.delete(role);
      else next.add(role);
      return next;
    });
  }, []);

  const [zoom, setZoom] = useState<Zoom>({ fit: "width", percent: 100 });
  const [tab, setTab] = useState<PanelId>("page");

  // Selection follows the URL: a change of ?item= (a link, or the keyboard) replaces it. A click
  // on a box or a block changes it without a round trip.
  const [selected, setSelected] = useState<string | null>(selectedId);
  const [seenSelectedId, setSeenSelectedId] = useState(selectedId);
  if (selectedId !== seenSelectedId) {
    setSeenSelectedId(selectedId);
    setSelected(selectedId);
  }

  const reviewIds = useMemo(() => new Set(review.pageIssues.flatMap((i) => i.nodeIds)), [review.pageIssues]);
  const select = useCallback((id: string) => {
    setSelected(id);
    // A flagged node also moves the URL, so the issue bar and a shared link follow it. Replaced,
    // not pushed: Back returns to the issue before, not to every box clicked.
    if (reviewIds.has(id)) router.replace(inspectUrl({ slug, page, item: id }), { scroll: false });
  }, [reviewIds, router, slug, page]);

  // j and k move between issues, [ and ] between pages. Ignored while typing in a field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      if (typingIn(e.target)) return;
      const href =
        e.key === "j" ? review.nextIssueHref
        : e.key === "k" ? review.prevIssueHref
        : e.key === "]" ? nextPageHref
        : e.key === "[" ? prevPageHref
        : null;
      if (!href) return;
      e.preventDefault();
      router.push(href);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router, review.nextIssueHref, review.prevIssueHref, nextPageHref, prevPageHref]);

  // The panels on show. On a desktop the dock places them; phones show one at a time, as tabs.
  const { visible } = layout;
  const shown = PANEL_ORDER.filter((p) => visible.includes(p));
  const activeTab = shown.includes(tab) ? tab : shown[0];

  const selectedNode = nodes.find((n) => n.id === selected) ?? null;
  // An item with no node (an orphaned decision) is selected by the URL alone, and only while no node is.
  const pageIssue = (selected !== null
    ? review.pageIssues.find((i) => i.nodeIds.includes(selected))
    : review.pageIssues.find((i) => i.selected && i.nodeIds.length === 0)) ?? null;
  const decide = useMemo(() => ({
    slug, rev, supersedes,
    // An orphaned decision is replaced by deciding on one of the page's other issues.
    redecide: review.pageIssues.filter((i) => i.item.role !== "decision" && (i.item.suggestions?.length || i.item.role === "figure"))
      .map((i) => ({ label: i.label, href: i.href })),
  }), [slug, rev, supersedes, review.pageIssues]);
  const issue = pageIssue?.label ?? null;
  const issueSeverity = pageIssue ? (pageIssue.error ? "error" : "warning") : null;
  const suggestions = useMemo(() => pageIssue?.suggestions ?? [], [pageIssue]);

  const togglePanel = (id: PanelId) => {
    const next = visible.includes(id)
      ? visible.filter((p) => p !== id)
      : PANEL_ORDER.filter((p) => p === id || visible.includes(p));
    if (next.length === 0) return;
    setLayout({ ...layout, visible: next });
  };

  // Reset layout: every panel shown, and the dock back to its default places.
  const [resets, setResets] = useState(0);
  const resetLayout = () => {
    setLayout(DEFAULT_LAYOUT);
    setResets((n) => n + 1);
  };

  const renderPage = () => (
    <PagePanel
      title={title}
      pageText={pageText}
      image={image}
      width={width}
      height={height}
      nodes={nodes}
      trace={trace}
      selected={selected}
      suggestions={suggestions}
      onSelect={select}
      hidden={hidden}
      onToggleRole={toggleRole}
      showLines={showLines}
      onToggleLines={() => setShowLines((v) => !v)}
      zoom={zoom}
      setZoom={setZoom}
    />
  );

  const renderTranscript = () => (
    <TranscriptPanel
      title={title}
      base={base}
      version={rev}
      page={page}
      pageText={pageText}
      nodes={nodes}
      selected={selected}
      onSelect={select}
      textScale={textScale}
      onTextScale={setTextScale}
    />
  );

  const details = () => (
    <DetailsPanel
      node={selectedNode}
      item={pageIssue?.item ?? null}
      decide={decide}
      issue={issue}
      issueSeverity={issueSeverity}
      suggestions={suggestions}
      collapsible={false}
      collapsed={false}
      onToggleCollapsed={() => {}}
    />
  );

  const desktopBody = (
    <DockLayout visible={visible} resetSignal={resets} contents={{ page: renderPage, transcript: renderTranscript, details }} />
  );

  // Phones keep the tabs. Details here is the full panel, or the hint when nothing is selected.
  const phoneBody = (
    <Tabs value={activeTab} onValueChange={(v) => setTab(v as PanelId)} className="h-full gap-0">
      <TabsList className="m-2 self-start">
        {shown.map((id) => (
          <TabsTrigger key={id} value={id}>{PANEL_LABELS[id]}</TabsTrigger>
        ))}
      </TabsList>
      {shown.map((id) => (
        <TabsContent key={id} value={id} className="min-h-0 flex-1">
          {id === "page" ? renderPage() : id === "transcript" ? renderTranscript() : details()}
        </TabsContent>
      ))}
    </Tabs>
  );

  return (
    <TooltipProvider>
      <WorkspaceHeader slug={slug} title={title} current="inspect" viewHref={viewerHref} inspectHref={inspectUrl({ slug, page })} build={build} variants={variants} page={page}>
        <Help />
        <MoreMenu
          className="md:hidden"
          slug={slug}
          page={page}
          pageCount={pageCount}
          pagesWithIssues={pagesWithIssues}
          visible={visible}
          onTogglePanel={togglePanel}
          onResetLayout={resetLayout}
        />
      </WorkspaceHeader>
      <main id="main" tabIndex={-1} className="flex min-h-0 flex-1 flex-col focus:outline-none">
        <InspectorToolbar
          slug={slug}
          page={page}
          pageText={pageText}
          pageCount={pageCount}
          prevPageHref={prevPageHref}
          nextPageHref={nextPageHref}
          pagesWithIssues={pagesWithIssues}
          review={review}
          visible={visible}
          onTogglePanel={togglePanel}
          onResetLayout={resetLayout}
        />
        <div className="min-h-0 flex-1">{isDesktop ? desktopBody : phoneBody}</div>
      </main>
    </TooltipProvider>
  );
}
