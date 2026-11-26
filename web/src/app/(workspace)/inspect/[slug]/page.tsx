import { Suspense } from "react";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import type { ReviewBar } from "@/lib/inspect";
import { InspectorWorkspace } from "@/components/inspector/Workspace";
import { WorkspaceSkeleton } from "@/components/app/workspace-skeleton";
import { NotConvertedWorkspace } from "@/components/app/not-converted";
import { RenderedFrom } from "@/components/builds/rendered-from";
import { slugParams } from "@/docs";
import { variantOf } from "@/pipelines";
import { pageImageSet } from "@/reflow/core";
import { readDocument, readPage, shownTitle } from "@/lib/docs-data";
import { builtVariants } from "@/lib/variants/built";
import { issueType, itemFor, reviewNodeIds, suggestionText } from "@/lib/review";
import { clampPage, pageImagePath, pageText, parsePage } from "@/lib/pages";
import { docBase, inspectUrl, itemUrl, viewerUrl } from "@/lib/urls";

export const generateStaticParams = slugParams;

function firstParam(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export async function generateMetadata(props: PageProps<"/inspect/[slug]">): Promise<Metadata> {
  const { slug } = await props.params;
  const doc = variantOf(slug)?.doc;
  return doc ? { title: `${shownTitle(await readDocument(slug), doc)}: inspector` } : {};
}

export default function InspectPage(props: PageProps<"/inspect/[slug]">) {
  // No <main> here: the workspace renders the header and the one main landmark.
  return (
    <Suspense fallback={<WorkspaceSkeleton variant="inspect" />}>
      <Inspector params={props.params} searchParams={props.searchParams} />
    </Suspense>
  );
}

async function Inspector({
  params,
  searchParams,
}: {
  params: PageProps<"/inspect/[slug]">["params"];
  searchParams: PageProps<"/inspect/[slug]">["searchParams"];
}) {
  const { slug } = await params;
  const query = await searchParams;
  const doc = variantOf(slug)?.doc;
  if (!doc) notFound();

  const [data, variants] = await Promise.all([readDocument(slug), builtVariants(doc.slug)]);
  if (!data.pages) return <NotConvertedWorkspace data={data} slug={slug} doc={doc} current="inspect" page={parsePage(query.page)} variants={variants} />;

  // The review walk: review.json's order, the least trustworthy page first, the order the
  // dashboard's first-issue link opens. ?item= selects one item; without ?page=, the item's page
  // is shown.
  const itemId = firstParam(query.item);
  const ordered = data.review ?? [];
  const selectedItem = itemFor(ordered, itemId, firstParam(query.warn));
  const pageCount = data.pages;
  const requested = clampPage(parsePage(query.page) ?? selectedItem?.page ?? 1, pageCount);

  // Every node on the page that has a box, from its layout sidecar (.reflow/<slug>/layout/NNNN.json,
  // a few KB): what reflow decided, the role, which engine made it, its confidence, its attributes
  // and its facts. The trace (.reflow/<slug>/trace/NNNN.json, written by the heuristic engine) holds
  // the page's lines, why each was dropped, and the reading-order split; null for output without one.
  const pageData = await readPage(slug, requested);
  const layout = pageData.sidecar;
  if (!layout) notFound();
  const images = data.images;
  const labelOf = (n: number) => data.labels[n - 1] ?? null;

  const hrefOf = (i: (typeof ordered)[number]) => itemUrl(slug, i);
  const pageItems = ordered.filter((i) => i.page === requested);
  // A document item (the language, decisions made on another PDF) has no page: it is selected wherever the page is.
  if (selectedItem && selectedItem.page === null && !pageItems.includes(selectedItem)) pageItems.push(selectedItem);
  const index = selectedItem ? ordered.indexOf(selectedItem) : -1;
  const review: ReviewBar = {
    total: ordered.length,
    position: index >= 0 ? index + 1 : null,
    current: selectedItem ? { type: issueType(selectedItem), page: selectedItem.page ? pageText(selectedItem.page, selectedItem.pageLabel) : null } : null,
    // Walking from no selection: Next goes to the first issue on this page, else the first.
    prevIssueHref: index > 0 ? hrefOf(ordered[index - 1]) : null,
    nextIssueHref:
      index >= 0
        ? index + 1 < ordered.length ? hrefOf(ordered[index + 1]) : null
        : pageItems[0] ? hrefOf(pageItems[0]) : ordered[0] ? hrefOf(ordered[0]) : null,
    pageIssues: pageItems.map((i) => ({
      id: i.id,
      nodeIds: reviewNodeIds(i),
      label: `${i.severity === "error" ? "Error" : "Warning"}: ${issueType(i)}${i.group ? ` (${i.group.nodeIds.length} blocks)` : ""}`,
      href: hrefOf(i),
      selected: i === selectedItem,
      error: i.severity === "error",
      // An item with no node has only its box: drawn as one line, so the page shows where to look.
      suggestions: reviewNodeIds(i).length === 0
        ? [{ text: i.text, bbox: i.bbox }]
        : (i.suggestions ?? []).map((s) => ({ text: suggestionText(s), bbox: s.bbox })),
      item: i,
    })),
  };

  const counts = new Map<number, number>();
  for (const i of ordered) if (i.page) counts.set(i.page, (counts.get(i.page) ?? 0) + 1);
  const pagesWithIssues = [...counts]
    .map(([page, count]) => ({ page, count, label: labelOf(page) }))
    .sort((a, b) => a.page - b.page);

  return (
    <>
      <RenderedFrom reads={[data, pageData]} />
      <InspectorWorkspace
        key={images.version ?? "unversioned"}
        build={data.build}
        variants={variants}
        slug={slug}
        title={shownTitle(data, doc)}
        base={docBase(slug)}
        page={requested}
        pageCount={pageCount}
        pageText={pageText(requested, labelOf(requested))}
        width={layout.width}
        height={layout.height}
        image={pageImageSet(docBase(slug), pageImagePath(requested), images.widths, images.version)}
        nodes={layout.nodes}
        trace={pageData.trace}
        selectedId={itemId && layout.nodes.some((n) => n.id === itemId) ? itemId : null}
        review={review}
        prevPageHref={requested > 1 ? inspectUrl({ slug, page: requested - 1 }) : null}
        nextPageHref={requested < pageCount ? inspectUrl({ slug, page: requested + 1 }) : null}
        pagesWithIssues={pagesWithIssues}
        viewerHref={viewerUrl({ slug, page: requested })}
        rev={images.version ?? null}
        supersedes={firstParam(query.supersedes) ?? null}
      />
    </>
  );
}
