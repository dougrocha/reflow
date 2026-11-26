import type { ReviewItem } from "@/lib/review";

// The URLs of the site's pages and of a document's published files. `key` is a document's storage
// key: its slug, or `<slug>~<pipeline>` for a variant (src/pipelines.ts).

export const overviewUrl = (slug: string): string => `/docs/${slug}`;

/** The PDF a document was converted from. */
export const pdfHref = (slug: string): string => `/pdfs/${slug}.pdf`;

/** Where a variant's published files are served: /docs/<key>/. */
export const docBase = (key: string): string => `/docs/${key}/`;

export function viewerUrl({ slug, page }: { slug: string; page?: number | null }): string {
  return page ? `/view/${slug}?page=${page}` : `/view/${slug}`;
}

/** The embeddable viewer of a registered document for one site of the resolver's index
 *  (src/lib/resolver/embed.ts). The loader adds `parent` and `instance` (src/embed/protocol.ts). */
export const embedUrl = (id: string, site: string): string =>
  `/embed/${encodeURIComponent(id)}?site=${encodeURIComponent(site)}`;

/** The inspector's deep link: a page of one document, optionally with one item selected. */
export function inspectUrl({ slug, page, item }: { slug: string; page?: number | null; item?: string }): string {
  const query = new URLSearchParams();
  if (page) query.set("page", String(page));
  if (item) query.set("item", item);
  const search = query.toString();
  return `/inspect/${slug}${search ? `?${search}` : ""}`;
}

/** The inspector link of one queue item: a document item also names its code, since its id is the document's. */
export function itemUrl(slug: string, item: ReviewItem): string {
  const url = inspectUrl({ slug, page: item.page, item: item.id });
  return item.role === "doc" && item.warn ? `${url}${url.includes("?") ? "&" : "?"}warn=${encodeURIComponent(item.warn)}` : url;
}

/** The compare screen's URL: its columns (pipeline names) and its page. */
export function compareUrl(slug: string, cols: string[], page?: number | null): string {
  const q = new URLSearchParams();
  if (cols.length) q.set("cols", cols.join(","));
  if (page && page > 1) q.set("page", String(page));
  const s = q.toString().replace(/%2C/g, ",");
  return `/docs/${slug}/compare${s ? `?${s}` : ""}`;
}
