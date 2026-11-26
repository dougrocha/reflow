import { createReadStream } from "node:fs";
import { realpath, stat } from "node:fs/promises";
import { extname, join, sep } from "node:path";
import { Readable } from "node:stream";
import { rawFileCsp } from "./csp";

// Serves the files a build publishes (public/docs/<slug>/..., public/pdfs/<slug>.pdf) from disk on
// every request. `next start` indexes `public/` once, at boot, and answers 404 for a file added
// after that, so a document built for the first time while the server runs would have no manifest
// and no page images until a restart. next.config.ts sends those 404s here (a `fallback` rewrite,
// which runs only after the static files were checked), so files that existed at boot are still
// served by Next itself and everything new is served by this. docs/builds.md.
//
// It serves only the public tree of a known document: the slug must be one of the sample documents,
// each path segment is checked, and the resolved file (symlinks followed) must still be inside
// that document's folder. The whole conversion in DATA_ROOT (sidecars, traces, audit) is not here.

const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".webp": "image/webp",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".pdf": "application/pdf",
  ".txt": "text/plain; charset=utf-8",
};

/** The folders of a document that are immutable at a versioned URL (see next.config.ts). */
const VERSIONED_DIRS = new Set(["pages", "figures", "chunks", "assets"]);

/** A path segment that is a plain name: no separators, no dot names, no NUL, no control characters. */
function plain(segment: string): boolean {
  return segment !== "" && !segment.startsWith(".") && !/[\\/\0-\x1f]/.test(segment);
}

/** The Cache-Control the static files get: next.config.ts's rules (immutable only at a versioned URL
 *  of pages, figures, chunks and assets; revalidate for the rest of those and the entry JSON), and
 *  Next's own default for everything else (index.html, the PDFs). */
export function cacheControlFor(rest: string[], versioned: boolean): string {
  if (VERSIONED_DIRS.has(rest[0]) && rest.length > 1) {
    return versioned ? "public, max-age=31536000, immutable" : "public, max-age=0, must-revalidate";
  }
  if (rest.length === 1 && /^(manifest|toc|review)\.json$/.test(rest[0])) return "public, max-age=0, must-revalidate";
  return "public, max-age=0";
}

/**
 * The file for a request, or null. `kind` is "docs" (segments: slug, then the path inside the
 * document) or "pdfs" (one segment: `<slug>.pdf`). `root` is `public/`.
 */
export async function resolvePublicFile(
  root: string, kind: "docs" | "pdfs", segments: string[], slugs: readonly string[],
): Promise<{ file: string; rest: string[] } | null> {
  if (!segments.every(plain)) return null;
  let slug: string;
  let rest: string[];
  if (kind === "pdfs") {
    if (segments.length !== 1 || !segments[0].endsWith(".pdf")) return null;
    slug = segments[0].slice(0, -".pdf".length);
    rest = [segments[0]];
  } else {
    if (segments.length < 2) return null;
    [slug, ...rest] = segments;
  }
  if (!slugs.includes(slug)) return null;
  if (!(extname(rest[rest.length - 1]).toLowerCase() in TYPES)) return null;
  const base = join(root, kind, kind === "pdfs" ? "" : slug);
  const file = join(base, ...rest);
  if (!file.startsWith(base + sep)) return null;
  try {
    // Symlinks are followed here, and the real path must still be inside the real folder.
    const real = await realpath(file);
    const realBase = await realpath(base);
    if (!real.startsWith(realBase + sep) || !(await stat(real)).isFile()) return null;
    return { file: real, rest };
  } catch {
    return null;
  }
}

/** The headers a document's own file gets besides its type and caching, as next.config.ts gives
 *  them: its pages (HTML, SVG) run sandboxed (src/lib/csp.ts), and the chunk pages and toc.json,
 *  which the sandboxed standalone viewer fetches from its opaque origin, are readable across origins. */
export function rawFileHeaders(kind: "docs" | "pdfs", rest: string[]): Record<string, string> {
  if (kind !== "docs") return {};
  const headers: Record<string, string> = {};
  if (/\.(?:html|svg)$/i.test(rest[rest.length - 1])) headers["Content-Security-Policy"] = rawFileCsp();
  if (rest[0] === "chunks" || (rest.length === 1 && rest[0] === "toc.json")) headers["Access-Control-Allow-Origin"] = "*";
  return headers;
}

const NOT_FOUND = () => new Response("Not found", { status: 404, headers: { "Cache-Control": "no-store" } });

/** The response for a file of a build: 200 with a stream, 304 when the browser's copy is current, or 404. */
export async function servePublicFile(
  req: Request, root: string, kind: "docs" | "pdfs", segments: string[], slugs: readonly string[],
): Promise<Response> {
  const found = await resolvePublicFile(root, kind, segments, slugs);
  if (!found) return NOT_FOUND();
  const info = await stat(found.file);
  const etag = `W/"${info.size.toString(16)}-${Math.round(info.mtimeMs).toString(16)}"`;
  const headers = new Headers({
    "Content-Type": TYPES[extname(found.file).toLowerCase()],
    "Cache-Control": cacheControlFor(found.rest, new URL(req.url).searchParams.has("v")),
    ETag: etag,
    "Last-Modified": info.mtime.toUTCString(),
    "X-Content-Type-Options": "nosniff",
    ...rawFileHeaders(kind, found.rest),
  });
  if (req.headers.get("if-none-match")?.split(",").map((t) => t.trim()).includes(etag)) {
    return new Response(null, { status: 304, headers });
  }
  headers.set("Content-Length", String(info.size));
  if (req.method === "HEAD") return new Response(null, { headers });
  return new Response(Readable.toWeb(createReadStream(found.file)) as unknown as ReadableStream, { headers });
}
