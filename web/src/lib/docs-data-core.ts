import { normalizeManifest } from "@/reflow/core";
import type { ReviewItem } from "@/lib/review";
import { parseBuildInfo, type BuildInfo } from "@/lib/build-info";
import type { InspectFact, InspectNode, InspectTrace, PageSidecar } from "@/lib/inspect";

// What the pages make of the files they read. The shell (docs-data.ts) reads the files and the
// publication generation and calls these. Plain data in, plain data out: no files, no cache.

/** The widths of a document's page images and its digest, for srcset URLs (see pageImageSet). */
export type PageImages = { widths?: number[]; version?: string };

type AuditFinding = {
  id: string;
  title: string;
  wcag: string;
  matterhorn?: string;
  status: "pass" | "fail" | "warn" | "skip";
  detail: string;
  pages: number[];
};
export type AuditReport = { filename: string; passed: boolean; findings: AuditFinding[] };

/** What a read was rendered from: a document, and the publication generation its files were read
 *  at or after (0 before the first publication). */
export type Stamp = { slug: string; generation: number };

export type DocumentData = Stamp & {
  /** Page count, or null when the sample has not been converted. */
  pages: number | null;
  /** The title reflow published (manifest.json), the one the viewer prints; null without a manifest
   *  or when the manifest has none. */
  title: string | null;
  /** The number each page prints, in page order (index 0 is page 1); [] without a manifest. */
  labels: string[];
  images: PageImages;
  /** The items reflow could not finish on its own (public/docs/<slug>/review.json), each with its
   *  box, page size and label; null when the document has not been converted. */
  review: ReviewItem[] | null;
  /** The source-PDF audit (`reflow audit --json`, written by docs:build), or null when it was not run. */
  audit: AuditReport | null;
  /** How the transcript was made (public/docs/<slug>/build-info.json, written by docs:build), or
   *  null for a build from before it was published. */
  build: BuildInfo | null;
  /** Why the published manifest no longer reads (a build by an older reflow, in a format since
   *  changed), or null. Such a document shows as not converted until it is built again. */
  outdated: string | null;
};

/** The files of one document as read: parsed JSON nothing has checked yet, each null when absent. */
export type DocumentFiles = {
  manifest: unknown;
  review: unknown;
  audit: unknown;
  build: unknown;
};

/** One document as the pages show it, from the files read and the stamp they were read at. A
 *  manifest that does not read (see normalizeManifest) makes the document `outdated`, with no
 *  pages, title or review. */
export function documentFrom(input: Stamp & DocumentFiles): DocumentData {
  const { slug, generation, manifest, review: rawReview, audit, build } = input;
  let pages: number | null = null;
  let title: string | null = null;
  let labels: string[] = [];
  let images: PageImages = {};
  let outdated: string | null = null;
  if (manifest !== null && manifest !== undefined) {
    try { // a manifest that is not one throws, naming its wrong fields
      const normal = normalizeManifest(manifest);
      pages = normal.pageCount || null;
      title = normal.title || null;
      labels = normal.pages.map((p) => p.label);
      images = { widths: normal.imageWidths, version: normal.version };
    } catch (e) {
      outdated = e instanceof Error ? e.message : String(e);
    }
  }
  const review = !outdated && Array.isArray(rawReview)
    ? (rawReview as ReviewItem[]).map((item) => ({
        ...item,
        bbox: item.bbox ?? null,
        pageSize: item.pageSize ?? null,
        targets: item.targets ?? [],
        pageLabel: item.page ? (labels[item.page - 1] ?? null) : null,
      }))
    : null;
  return { slug, generation, pages, title, labels, images, review, audit: (audit ?? null) as AuditReport | null, build: parseBuildInfo(build), outdated };
}

/** A page's layout sidecar as written (layout/NNNN.json under the data root). */
export type RawSidecar = {
  w: number;
  h: number;
  nodes: (Omit<InspectNode, "facts" | "text"> & {
    text: string | null;
    facts?: { code: string; source: string; detail?: string; data?: Record<string, unknown> }[];
  })[];
  sections?: PageSidecar["sections"];
  labels?: Record<string, string>;
};

export type PageData = Stamp & {
  /** The page's layout sidecar (a few KB), or null when the page or the document is missing. */
  sidecar: PageSidecar | null;
  /** The page's trace (the heuristic engine's record), or null when there is none. */
  trace: InspectTrace | null;
};

/** One page as the inspector shows it: the sidecar with its defaults filled in, and each fact's
 *  code spelled out by the sidecar's own labels. */
export function pageFrom(input: Stamp & { sidecar: RawSidecar | null; trace: InspectTrace | null }): PageData {
  const { slug, generation, sidecar: raw, trace } = input;
  let sidecar: PageSidecar | null = null;
  if (raw) {
    const labels = raw.labels ?? {};
    sidecar = {
      width: raw.w,
      height: raw.h,
      sections: raw.sections ?? [],
      nodes: raw.nodes.map((node): InspectNode => ({
        ...node,
        text: node.text ?? "",
        attrs: node.attrs ?? {},
        facts: (node.facts ?? []).map((f): InspectFact => ({ ...f, label: labels[f.code] ?? f.code })),
      })),
    };
  }
  return { slug, generation, sidecar, trace };
}
