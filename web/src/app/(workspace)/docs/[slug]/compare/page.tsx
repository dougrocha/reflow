import { Suspense } from "react";
import { notFound } from "next/navigation";
import { CompareScreen, type CompareColumn } from "@/components/compare/compare-screen";
import { WorkspaceSkeleton } from "@/components/app/workspace-skeleton";
import { RenderedFrom } from "@/components/builds/rendered-from";
import { sampleDoc, slugParams } from "@/docs";
import { DEFAULT_PIPELINE, variantKey } from "@/pipelines";
import { SIDECAR_TEXT_LIMIT, blocksOf, parseCols } from "@/lib/compare";
import { readDocument, readFullTexts, readPage, shownTitle } from "@/lib/docs-data";
import { clampPage, pageImagePath, parsePage } from "@/lib/pages";
import { builtVariants } from "@/lib/variants/built";
import { pageImageSet } from "@/reflow/core";
import { docBase } from "@/lib/urls";

// The compare screen: one page of a document, its original image, then a column per built
// variant. ?cols= names the columns (pipelines) in order, ?page= the page.

export const generateStaticParams = slugParams;

export async function generateMetadata(props: PageProps<"/docs/[slug]/compare">) {
  const { slug } = await props.params;
  const doc = sampleDoc(slug);
  if (!doc) return { title: "Document not found" };
  return { title: `${shownTitle(await readDocument(slug), doc)}: compare` };
}

export default function ComparePage(props: PageProps<"/docs/[slug]/compare">) {
  return (
    <Suspense fallback={<WorkspaceSkeleton variant="inspect" />}>
      <Compare params={props.params} searchParams={props.searchParams} />
    </Suspense>
  );
}


async function Compare({ params, searchParams }: {
  params: PageProps<"/docs/[slug]/compare">["params"];
  searchParams: PageProps<"/docs/[slug]/compare">["searchParams"];
}) {
  const { slug } = await params;
  const query = await searchParams;
  const doc = sampleDoc(slug);
  if (!doc) notFound();

  const built = await builtVariants(slug);
  if (built.length === 0) notFound();
  const names = parseCols(query.cols, built.map((b) => b.name));
  // The original page comes from the default build when there is one (every variant renders the
  // same PDF's pages), else from the first column.
  const originalKey = built.some((b) => b.name === DEFAULT_PIPELINE) ? slug : variantKey(slug, names[0]);
  const original = await readDocument(originalKey);
  const pageCount = original.pages ?? 1;
  const page = clampPage(parsePage(query.page) ?? 1, pageCount);

  const [originalPage, ...reads] = await Promise.all([
    readPage(originalKey, page),
    ...names.map(async (name) => {
      const key = variantKey(slug, name);
      try {
        const [data, pageData] = await Promise.all([readDocument(key), readPage(key, page)]);
        if (data.outdated) throw new Error(data.outdated);
        // A block's sidecar text is cut; compare its whole text.
        const cut = pageData.sidecar?.nodes.some((n) => (n.text?.length ?? 0) >= SIDECAR_TEXT_LIMIT);
        const full = cut ? await readFullTexts(key, page) : {};
        return { name, key, data, pageData, full, error: null };
      } catch (e) {
        // One build that does not read leaves the others on screen, with its column saying why.
        return { name, key, data: null, pageData: null, full: {}, error: e instanceof Error ? e.message : String(e) };
      }
    }),
  ]);
  const columns: CompareColumn[] = reads.map(({ name, key, data, pageData, full, error }) => ({
    name,
    key,
    label: built.find((b) => b.name === name)!.label,
    build: data?.build ?? null,
    blocks: pageData?.sidecar ? blocksOf(pageData.sidecar.nodes.map((n) => (full[n.id] ? { ...n, text: full[n.id] } : n))) : null,
    error,
  }));
  const base = docBase(originalKey);

  return (
    <>
      <RenderedFrom reads={[original, originalPage, ...reads.flatMap((r) => (r.data && r.pageData ? [r.data, r.pageData] : []))]} />
      <CompareScreen
        slug={slug}
        title={shownTitle(original, doc)}
        page={page}
        pageCount={pageCount}
        labels={original.labels}
        image={pageImageSet(base, pageImagePath(page), original.images.widths, original.images.version)}
        pageSize={originalPage.sidecar ? [originalPage.sidecar.width, originalPage.sidecar.height] : null}
        pdfLines={(originalPage.trace?.lines ?? []).filter((l) => !l.artifact).map((l) => l.text)}
        // The plain page image is the smallest width.
        thumbs={Array.from({ length: pageCount }, (_, i) =>
          pageImageSet(base, pageImagePath(i + 1), undefined, original.images.version).src)}
        columns={columns}
        built={built.map(({ name, label }) => ({ name, label }))}
      />
    </>
  );
}
