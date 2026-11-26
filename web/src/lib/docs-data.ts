import { fullTextsOf } from "@/lib/compare";
import { cacheTag } from "next/cache";
import { isVariantKey } from "@/pipelines";
import { documentFrom, pageFrom, type DocumentData, type PageData, type RawSidecar } from "@/lib/docs-data-core";
import { buildsDir, dataRoot, docsRoot } from "@/lib/paths";
import { readAudit, readBuildInfo, readManifest, readReview, readSidecar, readTrace, readTree } from "@/lib/published";
import { readPublications } from "@/lib/builds/record";
import type { InspectTrace } from "@/lib/inspect";

export type { AuditReport, DocumentData, PageImages, Stamp } from "@/lib/docs-data-core";

// Reads of the converted documents. There are two, and each is one cache entry ("use cache")
// tagged `doc:<slug>` (docTag):
//  - readDocument: what the pages show of a document (page count, page labels, image widths and
//    digest, review items, source audit);
//  - readPage: one page's layout sidecar and trace (the inspector, and the viewer's Show on page).
//
// Each read is stamped with the document's publication generation (publications.json, see
// docs/builds.md, After a build). The stamp is read first, then the files, all inside the one entry.
// A build marks the document as publishing, writes, and only then adds one to the generation, so a
// stamp is never newer than the files read with it. Stamp and files are cached, marked stale and
// served together; a page renders the stamp (RenderedFrom), and the browser compares it with what
// has been published since. Neither read calls another cached read, so no older entry is reused
// under a newer stamp.
//
// The output only changes when a build publishes; with builds on, the web app then marks the tag
// stale, and the next request is served the old entry while the read runs again. Without the build
// tracking, a read is regenerated only by the default cache life (a request more
// than 15 minutes after it was read, in the background). A restart does not clear it: Next keeps
// prerendered and regenerated pages on disk.
//
// Two roots (the files are named in published.ts):
//  - DOCS_ROOT (public/docs/<slug>/): what a reader of the site may fetch (reflow's PUBLIC_NAMES).
//  - DATA_ROOT (.reflow/<slug>/, gitignored): the whole conversion, including the files that are
//    not served: the per-page layout sidecars, the traces and the source audit. document.json is
//    read only by readFullTexts, for the text of one page's blocks, and never kept parsed: the
//    largest is over 100 MB.
//
// The slug is checked first (a sample's, or a variant's key).
//
// This file is the shell: it reads the generation and the files. What the pages make of them
// (manifest normalization, review items, labels, sidecar defaults) is documentFrom and pageFrom in
// docs-data-core.ts, plain data in and out.

export const DOCS_ROOT = docsRoot();
export const DATA_ROOT = dataRoot();

/** The cache tag of everything read from one document. */
const docTag = (slug: string) => `doc:${slug}`;

/** The slug, if it is one of the sample documents or the key of a variant of one (src/pipelines.ts);
 *  throws otherwise. */
export function docSlug(slug: string): string {
  if (!isVariantKey(slug)) throw new Error(`unknown document: ${slug}`);
  return slug;
}

/** The document's publication generation now; 0 before its first publication is complete. */
async function generationOf(slug: string): Promise<number> {
  return (await readPublications(buildsDir(DATA_ROOT)))[slug]?.generation ?? 0;
}

/** One document as the pages show it, stamped (see the top of this file). Throws for a slug that is
 *  not a sample document. */
export async function readDocument(slug: string): Promise<DocumentData> {
  "use cache";
  cacheTag(docTag(slug));
  const generation = await generationOf(docSlug(slug));
  const [manifest, review, audit, build] = await Promise.all([
    readManifest(DOCS_ROOT, slug), readReview(DOCS_ROOT, slug), readAudit(DATA_ROOT, slug), readBuildInfo(DOCS_ROOT, slug),
  ]);
  return documentFrom({ slug, generation, manifest, review, audit, build });
}

/** The title the workspace shows for a document: the one reflow published, else the catalogue's
 *  label (a document that is not converted has no other). */
export function shownTitle(data: Pick<DocumentData, "title">, doc: { title: string }): string {
  return data.title ?? doc.title;
}

/** One page of a document, stamped (see the top of this file). Throws for a slug that is not a
 *  sample document. */
export async function readPage(slug: string, page: number): Promise<PageData> {
  "use cache";
  cacheTag(docTag(slug));
  const generation = await generationOf(docSlug(slug));
  const [raw, trace] = (await Promise.all([readSidecar(DATA_ROOT, slug, page), readTrace(DATA_ROOT, slug, page)])) as
    [RawSidecar | null, InspectTrace | null];
  return pageFrom({ slug, generation, sidecar: raw, trace });
}

/** The full text of each block on a page, by id, from the document's tree (document.json): for the
 *  blocks whose sidecar text was cut at SIDECAR_TEXT_LIMIT. {} when there is no tree. */
export async function readFullTexts(slug: string, page: number): Promise<Record<string, string>> {
  "use cache";
  cacheTag(docTag(slug));
  return fullTextsOf(await readTree(DATA_ROOT, slug), page);
}
