// Server only: what the lists show of each document, from its one cached read (docs-data.ts).
import { SAMPLE_DOCS, sampleDoc, type Collection } from "@/docs";
import { readDocument, type DocumentData } from "@/lib/docs-data";
import { groupByType, type ReviewItem } from "@/lib/review";
import { docBase, itemUrl, pdfHref } from "./urls";

export type DocumentSummary = {
  slug: string;
  /** The publication generation the summary was read at (see docs-data.ts); a page that shows the
   *  summary renders it (RenderedFrom). */
  generation: number;
  title: string;
  description: string;
  /** The home page section it is listed in. */
  collection: Collection;
  pdfHref: string;
  /** Reflow output directory, e.g. "/docs/resnet/". */
  base: string;
  /** Page count, or null when the document has not been converted. */
  pages: number | null;
  /** Review items in walk order (least trustworthy page first), or null when not converted. */
  items: ReviewItem[] | null;
  errors: number;
  warnings: number;
  /** Inspector link to the first issue, or null when there are none. */
  firstIssueHref: string | null;
  /** The issue type with the most items, or null. */
  topIssue: { type: string; count: number } | null;
};

export function summarize(data: DocumentData): DocumentSummary {
  const doc = sampleDoc(data.slug)!;
  const items = data.pages && data.review ? data.review : null;
  const list = items ?? [];
  const errors = list.filter((i) => i.severity === "error").length;
  const first = list[0];
  const groups = groupByType(list);
  return {
    slug: doc.slug,
    generation: data.generation,
    title: doc.title,
    description: doc.description,
    collection: doc.collection,
    pdfHref: pdfHref(doc.slug),
    base: docBase(doc.slug),
    pages: data.pages,
    items,
    errors,
    warnings: list.length - errors,
    firstIssueHref: first ? itemUrl(doc.slug, first) : null,
    topIssue: groups[0] ? { type: groups[0][0], count: groups[0][1].length } : null,
  };
}

export function documentSummaries(): Promise<DocumentSummary[]> {
  return Promise.all(SAMPLE_DOCS.map(async (d) => summarize(await readDocument(d.slug))));
}
