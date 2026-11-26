import { NextResponse, type NextRequest } from "next/server";
import { sampleDoc } from "@/docs";
import { buildsEnabled } from "@/lib/builds/gate";
import { isVariantKey } from "@/pipelines";
import { COMMON_HEADERS, embedCsp } from "@/lib/csp";
import { resolverStore } from "@/lib/resolver";
import { frameAncestor, NO_FRAMING } from "@/lib/resolver/embed";
import { LOADER_PATH } from "@/embed/loader-rules";

// What must happen before anything renders. Two checks a page cannot make: under Cache Components a
// page streams from inside a Suspense boundary (the layouts' loading.tsx are some), so by the time its
// code could call notFound() the status is already 200. Here the answer is a real 404: a rewrite to a
// path that does not exist, which renders the not-found page.
//
//  - /builds exists only when builds are on (@/lib/builds/gate).
//  - /docs/<slug>, /docs/<slug>/compare, /view/<slug> and /inspect/<slug> exist only for the sample documents. A lookup in
//    sampleDoc (a map built once): no file is read per request. /view and /inspect also
//    take a variant's key (<slug>~<pipeline>, src/pipelines.ts), which is parsed, not read.
//
//  - /embed/<id> is the one page another site may frame (docs/embed.md). Its headers are set here,
//    per request, because frame-ancestors depends on the query and the resolver's index
//    (@/lib/resolver/embed); next.config.ts leaves this path out of its own security headers. Only
//    a catalogue document's slug is an id (no variant, no path); anything else is a 404 that no
//    one may frame either.
//
//  - /embed/loader.js is the loader script, a static file (public/embed/loader.js), not a document
//    id: it passes through, and next.config.ts gives it its own headers.
//
//  - A published file (/docs/<slug>/<file>, and /api/files/docs/... for files built after boot)
//    asked for by any spelling but its own is a 404: see misspeltRawFile. So the matcher takes
//    every path, Next's own assets too, since "/_next/static/..%2f..%2fdocs/..." reaches a file.
//
// The API routes check their own input (/api/builds the gate, /api/source and /api/ask the slug).

const DOCUMENT_PAGE = /^\/(docs|view|inspect)\/([^/]+)(\/compare)?\/?$/;
const GATED = /^\/builds(?:\/|$)/;
const EMBED = /^\/embed\/([^/]+)\/?$/;
// A file of a published document, at its public path or at the route that serves files built
// after boot.
const RAW_FILE = /^\/(?:api\/files\/)?docs\/[^/]+\/./;
const RAW_FILE_ANY_CASE = new RegExp(RAW_FILE.source, "i");

/** The path Next looks a file up by: decoded (until it stops changing), with "\\" as "/", runs of "/"
 *  made one, and "." and ".." segments resolved. */
export function lookupPath(pathname: string): string {
  let path = pathname;
  for (let i = 0; i < 4 && path.includes("%"); i++) {
    try {
      const next = decodeURIComponent(path);
      if (next === path) break;
      path = next;
    } catch {
      break;
    }
  }
  const out: string[] = [];
  for (const seg of path.replaceAll("\\", "/").split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") out.pop();
    else out.push(seg);
  }
  return "/" + out.join("/") + (/[/\\]\.{0,2}$/.test(path) && out.length ? "/" : "");
}

/** Whether the request names a published file by any spelling but its own: an escape, a "\\", a
 *  doubled "/", a dot segment or another case. Next would serve the file, but next.config.ts matches
 *  the sandbox CSP on the raw path, so it would miss it ("/docs/fw9/index.%68tml",
 *  "/foo%2f..%2fdocs/fw9/index.html", "/_next/static/..%2f..%2fdocs/...", "/DOCS/fw9/index.html").
 *  A published file's own name has no escape, so nothing real is refused. */
function misspeltRawFile(pathname: string): boolean {
  const lookup = lookupPath(pathname);
  return RAW_FILE_ANY_CASE.test(lookup) && (lookup !== pathname || lookup.includes("%") || !RAW_FILE.test(pathname));
}

/** Whether a document page's path names a document (or, in the workspace, a variant of one);
 *  null when it is not a document page. */
function documentExists(pathname: string): boolean | null {
  const m = DOCUMENT_PAGE.exec(pathname);
  if (!m || (m[3] && m[1] !== "docs")) return null;
  let slug: string;
  try {
    slug = decodeURIComponent(m[2]);
  } catch {
    return false;
  }
  return sampleDoc(slug) !== undefined || (m[1] !== "docs" && isVariantKey(slug));
}

const notFound = (request: NextRequest) => NextResponse.rewrite(new URL("/not-found-by-proxy", request.url));

async function embed(request: NextRequest, encoded: string): Promise<NextResponse> {
  let id = "";
  try {
    id = decodeURIComponent(encoded);
  } catch {
    // Not a name: a 404.
  }
  const known = sampleDoc(id) !== undefined;
  const ancestor = known ? await frameAncestor(resolverStore(), id, request.nextUrl.searchParams) : NO_FRAMING;
  const response = known ? NextResponse.next() : notFound(request);
  for (const { key, value } of COMMON_HEADERS) response.headers.set(key, value);
  response.headers.set("Content-Security-Policy", embedCsp(ancestor));
  // The answer depends on the query and on the index, which the operator may edit at any time.
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export function proxy(request: NextRequest): NextResponse | Promise<NextResponse> {
  const { pathname } = request.nextUrl;
  if (misspeltRawFile(pathname)) return notFound(request);
  if (pathname === LOADER_PATH) return NextResponse.next();
  const embedded = EMBED.exec(pathname);
  if (embedded) return embed(request, embedded[1]);
  const known = documentExists(pathname);
  const exists = known ?? (!GATED.test(pathname) || buildsEnabled());
  return exists ? NextResponse.next() : notFound(request);
}

export const config = {
  matcher: ["/:path*"],
};
