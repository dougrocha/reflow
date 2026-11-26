// Headless core: loads a reflow output directory (manifest.json + chunk pages) with no
// UI and no framework. React bindings live in ./react; a plain-script embed could reuse
// this file as is.

import { MANIFEST_SCHEMA, type ManifestChunk, type PublishedManifest, type TocItem } from "./manifest.gen";
import { isInsideBase, sanitizeChunk } from "./sanitize";
import { validate } from "./schema";
import { pageImagePath } from "@/lib/pages";

// manifest.json's own types are generated from schema/manifest.schema.json (manifest.gen.ts).
// The types below are the document as the viewer reads it: normalizeManifest expands the
// manifest's sparse pages into one entry per page.
export type { ManifestChunk };

/** How much transcript text a page has, for estimating how tall its transcript is. */
type PageText = {
  chars: number; // characters of text
  blocks: number; // paragraphs, headings, list items, table rows, figures, captions
  lines: number; // rows of text: a paragraph that breaks its lines counts each line
};

export type ManifestPage = {
  number: number;
  label: string;
  width: number; // points
  height: number; // points
  image: string | null; // relative to the document base
  chunk: string; // relative href of the chunk page that holds this page
  text: PageText;
};

/** A contents entry, with the id of its heading in the chunk (the fragment of its href). */
export type TocEntry = TocItem & { id: string };

export type Manifest = Pick<PublishedManifest, "title" | "lang" | "dir" | "ltrLang" | "pageCount" |
  "fillable" | "chunks" | "tocComplete" | "imageWidths" | "titleFromFileName" | "titleOnPage"> & {
  /** The original PDF, relative to the document base. */
  original: string | null;
  pages: ManifestPage[];
  toc: TocEntry[];
  /** Digest of the published output (see resolve); absent when the converter wrote none. */
  version?: string;
};

/**
 * Join a document base ("/docs/resnet/") and a relative path from its output. With `version`
 * (the manifest's digest of the output) the URL gets "?v=<digest>": pages, figures, chunks and
 * assets are served as immutable only when versioned, so a reconverted document is fetched
 * again instead of coming from a cache that may hold it for a year.
 *
 * `base` is a path on the page's own origin ("/docs/resnet/"), or a document host's URL:
 * absolute ("https://cdn.example/docs/resnet/") or protocol-relative ("//cdn.example/docs/resnet/",
 * which takes the page's scheme). A path-only base gives a root-relative path ("/docs/resnet/…")
 * and an absolute or protocol-relative one gives an absolute URL on its origin. Any other scheme
 * ("javascript:", "data:", "file:") and any other way of naming a host (a backslash after the
 * slashes, "https:/host") throws, since a base may come from a query parameter.
 *
 * `path` is relative to `base`. It cannot leave the base's origin: an absolute or "//host" path
 * keeps only its path and query, on the base's origin. A fragment in `path` is dropped (the
 * link rewriting in loadChunkPages expects a link to a chunk page not to carry one). A path with
 * a scheme other than http(s) throws.
 * A cross-origin base also needs the document host to send CORS headers for the viewer's fetches.
 */
export function resolve(base: string, path: string, version?: string): string {
  const relative = /^\/\/[^/\\\s]/.test(base);
  const page = globalThis.location?.protocol;
  const root = new URL((relative ? (page === "http:" || page === "https:" ? page : "https:") : "") + base, "http://x");
  let origin = "";
  if (root.protocol !== "http:" && root.protocol !== "https:") {
    throw new Error(`The document base must be an http(s) URL or a path, not "${root.protocol}": ${base}`);
  } else if (relative || /^https?:\/\/[^/\\\s]/i.test(base)) {
    origin = root.origin;
  } else if (/^[\x00-\x20]|^\/\/|[\\\x00-\x1f]|^[a-z][a-z0-9+.-]*:/i.test(base) || root.host !== "x") {
    throw new Error(`The document base is not a path or an http(s) URL: ${base}`);
  }
  const url = new URL(path, root);
  // A "data:/\\host/x" path would keep its backslashes and name a host in the result.
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error(`Not a relative path: ${path}`);
  if (version && !url.searchParams.has("v")) url.searchParams.set("v", version);
  // "/.//host/x" parses to the path "//host/x", which a page would read as a host.
  return origin + (origin ? url.pathname : url.pathname.replace(/^\/{2,}/, "/")) + url.search;
}

/** A page image as the `src` and `srcSet` of an <img>. */
export type PageImageSet = { src: string; srcSet?: string };

/**
 * The page image at every width the converter wrote. `image` is the first width's file
 * (pages/NNNN.webp); the others are pages/NNNN-<width>.webp. Every candidate gets the same
 * "?v=" as `src` (see `resolve`), so a reconverted document is fetched again at all widths.
 * One width, or none known, gives `src` alone. Pair `srcSet` with a `sizes` that matches the
 * displayed width: without it the browser assumes the image fills the viewport.
 */
export function pageImageSet(base: string, image: string, widths: number[] | undefined,
                             version?: string): PageImageSet {
  const src = resolve(base, image, version);
  if (!widths || widths.length < 2) return { src };
  const srcSet = widths
    .map((w, i) => `${i === 0 ? src : resolve(base, image.replace(/\.webp$/, `-${w}.webp`), version)} ${w}w`)
    .join(", ");
  return { src, srcSet };
}

/** Load and normalize manifest.json from a reflow output directory. */
export async function loadManifest(base: string, signal?: AbortSignal): Promise<Manifest> {
  const res = await fetch(resolve(base, "manifest.json"), { signal });
  if (!res.ok) throw new Error(`manifest.json: HTTP ${res.status}`);
  return normalizeManifest(await res.json());
}

/** manifest.json as the viewer reads it. Throws, naming the first fields that are wrong, when
 *  `raw` is not a manifest that schema/manifest.schema.json describes. */
export function normalizeManifest(raw: unknown): Manifest {
  const errors = validate(MANIFEST_SCHEMA, raw);
  if (errors.length) {
    const more = errors.length > 3 ? ` (and ${errors.length - 3} more)` : "";
    throw new Error(`manifest.json does not match its schema: ${errors.slice(0, 3).join("; ")}${more}`);
  }
  const m = raw as PublishedManifest;
  // A chunk's href is fetched from the document's base: it names a file of the document.
  m.chunks.forEach((c, i) => {
    if (!isInsideBase(c.href)) throw new Error(`chunks[${i}].href: not a path inside the document: ${c.href}`);
  });
  const chunkFor = new Map<number, string>();
  for (const chunk of m.chunks) {
    for (let p = chunk.first; p <= chunk.last; p++) chunkFor.set(p, chunk.href);
  }
  // Each text count is a step k on a geometric scale: count = base^k - 1, within 5% of the count.
  const count = (k: number) => Math.round(Math.pow(m.textBase, k) - 1);
  const pages: ManifestPage[] = [];
  for (let n = 1; n <= m.pageCount; n++) {
    const override = m.pages[String(n)] ?? {};
    const [chars, blocks, lines] = m.text[n - 1] ?? [0, 0, 0];
    pages.push({
      number: n,
      label: override.label ?? String(n + m.labelOffset),
      width: override.w ?? m.pageSize[0],
      height: override.h ?? m.pageSize[1],
      image: m.imageWidths ? pageImagePath(n) : null,
      chunk: chunkFor.get(n) ?? "",
      text: { chars: count(chars), blocks: count(blocks), lines: count(lines) },
    });
  }
  return {
    title: m.title,
    lang: m.lang,
    dir: m.dir,
    ltrLang: m.ltrLang,
    pageCount: m.pageCount,
    original: "original.pdf",
    pages,
    chunks: m.chunks,
    imageWidths: m.imageWidths,
    fillable: m.fillable,
    tocComplete: m.tocComplete,
    titleFromFileName: m.titleFromFileName,
    titleOnPage: m.titleOnPage,
    // "v" is the format number (2) unless the converter wrote a digest of the output into it.
    version: typeof m.v === "string" ? m.v : undefined,
    toc: m.toc.map((e) => ({ ...e, id: e.href.split("#")[1] ?? "" })),
  };
}

/**
 * Fetch a chunk page and split it into per-page HTML. The markup is sanitized first
 * (sanitizeChunk): the HTML is the converter's rendering of an untrusted PDF, and the viewer
 * puts it on the page as it is. Relative URLs (figures, page images) are then rewritten against
 * `base` (versioned, see `resolve`), and our static "Page N" label is dropped: the viewer
 * draws its own page separators.
 */
export async function loadChunkPages(base: string, href: string,
                                     signal?: AbortSignal, version?: string): Promise<Map<number, string>> {
  const res = await fetch(resolve(base, href, version), { signal });
  if (!res.ok) throw new Error(`${href}: HTTP ${res.status}`);
  const doc = new DOMParser().parseFromString(await res.text(), "text/html");
  const pages = new Map<number, string>();
  for (const pg of doc.querySelectorAll<HTMLElement>(".chunk-content .pg")) {
    // A page is the renderer's <div>. Any other element (a <script> or <textarea> with the class)
    // holds raw text, which sanitizeChunk cannot see and which becomes markup when it is inserted.
    if (!(pg instanceof HTMLDivElement)) continue;
    pg.querySelector(".pg-label")?.remove();
    sanitizeChunk(pg);
    for (const el of pg.querySelectorAll<HTMLElement>("[src], [href], [srcset]")) {
      // A link into a chunk ("0002.html#p5", or a note's "0002.html#n-3" and its backlink
      // "#r-3") would be resolved to a file URL, which opens the raw chunk and leaves the viewer.
      // Keep only the fragment; the viewer jumps to that page or element.
      const fragment = /^(?![a-z]+:|\/)[^#]*\.html#((?:p\d+|[nr]-.+))$/i.exec(el.getAttribute("href") ?? "");
      if (fragment) el.setAttribute("href", `#${fragment[1]}`);
      for (const attr of ["src", "href"]) {
        const v = el.getAttribute(attr);
        if (v && !/^([a-z]+:|\/|#)/i.test(v)) el.setAttribute(attr, resolve(base, v, version));
      }
      // Every candidate of a srcset is a path in the document (sanitizeChunk allows no other).
      const srcset = el.getAttribute("srcset");
      if (srcset) {
        el.setAttribute("srcset", srcset.split(",").map((c) => {
          const [url, ...descriptor] = c.trim().split(/\s+/);
          return [resolve(base, url, version), ...descriptor].join(" ");
        }).join(", "));
      }
    }
    pages.set(Number(pg.dataset.page), pg.innerHTML);
  }
  return pages;
}

export function chunkFor(manifest: Manifest, page: number): ManifestChunk | undefined {
  return manifest.chunks.find((c) => c.first <= page && page <= c.last);
}
