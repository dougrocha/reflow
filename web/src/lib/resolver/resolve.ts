import { docsRoot } from "@/lib/paths";
import { readManifest } from "@/lib/published";
import { normalizeManifest } from "@/reflow/core";
import { embedUrl } from "@/lib/urls";
import { MAX_URL_CHARS, normalizeSourceUrl } from "./url";
import { SITE_KEY, type ResolverStore } from "./index";
import { publicOrigin } from "./origin";

// GET /v1/resolve?site=<key>&url=<pdf url>: does this PDF link of this site have a conversion that
// is ready? A loader on the customer's page asks at click time, so the answer has two parts only:
//   {status: "ready", viewer: <absolute URL of the embeddable viewer, /embed/<id>?site=<key>>}
//   {status: "unknown"}
// "unknown" is the one answer for everything that is not ready: an unknown site, an origin the
// site does not list, a URL the site has no document for, a document with no published manifest.
// Which of those it was is never told, so the endpoint cannot be used to list sites or documents,
// and one site cannot see another's. Only a request that is not a question (a missing or malformed
// `site` or `url`) is a 400, which depends on the request alone.
//
// CORS: the loader runs on the site's own pages, so the answer is readable across origins only
// for an origin the site lists (`Access-Control-Allow-Origin` is that one origin, never `*`, and
// the response varies by Origin). A request with no Origin header (the same origin, curl) is
// answered; `site=` is an identifier, not authentication.
//
// The viewer URL is absolute, on the origin the request was made to (./origin, publicOrigin).
//
// Caching: a minute, shared, varying by Origin and by Host (the viewer URL names the host). The
// viewer URL is not versioned, so a longer life would keep a link on the PDF after its
// conversion is ready.

const CACHE = { "Cache-Control": "public, max-age=60", Vary: "Origin, Host" };

export type ResolverDeps = {
  store: ResolverStore;
  /** Whether a registered document has a published conversion. */
  isReady: (id: string) => Promise<boolean>;
  /** The environment publicOrigin reads (REFLOW_PUBLIC_ORIGIN); the process's by default. */
  env?: Record<string, string | undefined>;
};

/** Whether `public/docs/<id>/manifest.json` is a manifest the viewer can read. */
export async function manifestIsReady(id: string, root = docsRoot()): Promise<boolean> {
  try {
    const raw = await readManifest(root, id);
    return raw !== null && normalizeManifest(raw).pageCount > 0;
  } catch {
    return false;
  }
}

const bad = (error: string): Response => Response.json({ error }, { status: 400, headers: { "Cache-Control": "no-store" } });

/** A query parameter that is there exactly once, or null. */
function single(params: URLSearchParams, name: string): string | null {
  const all = params.getAll(name);
  return all.length === 1 && all[0] !== "" ? all[0] : null;
}

export async function resolveRequest(req: Request, deps: ResolverDeps): Promise<Response> {
  const params = new URL(req.url).searchParams;
  const key = single(params, "site");
  const rawUrl = single(params, "url");
  if (key === null || !SITE_KEY.test(key)) return bad("site is required");
  if (rawUrl === null || rawUrl.length > MAX_URL_CHARS) return bad("url is required");
  const source = normalizeSourceUrl(rawUrl);
  if (source === null) return bad("url must be an absolute http or https URL");

  const unknown = (): Response => Response.json({ status: "unknown" }, { headers: CACHE });
  const site = await deps.store.site(key);
  if (!site) return unknown();
  const origin = req.headers.get("origin");
  if (origin !== null && !site.origins.includes(origin)) return unknown();

  // From here the origin is one the site lists, so it may read the answer, "unknown" included.
  const headers = new Headers(CACHE);
  if (origin !== null) headers.set("Access-Control-Allow-Origin", origin);
  const id = site.documents.get(source);
  if (id === undefined || !(await deps.isReady(id))) return Response.json({ status: "unknown" }, { headers });
  const viewer = new URL(embedUrl(id, site.key), publicOrigin(req, deps.env)).href;
  return Response.json({ status: "ready", viewer }, { headers });
}
