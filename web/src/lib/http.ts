import { connection } from "next/server";
import { isRecord } from "./guards";

// What the API routes share: the answers, the same-site check and the capped body read. A browser
// must say the request is from this site; a request with no Sec-Fetch-Site (curl, a script) is
// let through, which is not authentication, which is why the gate exists (docs/builds.md).

export const NO_STORE = { "Cache-Control": "no-store" };

export const json = (body: unknown, status = 200): Response => Response.json(body, { status, headers: NO_STORE });
export const fail = (status: number, error: string): Response => json({ error }, status);
/** Empty, like a route that does not exist. */
export const notFound = (): Response => new Response(null, { status: 404, headers: NO_STORE });

/** Whether an `Origin` header names `host`, a `Host` header (compared without case). One reading
 *  of "the same site" for every route's check. */
export function originIsHost(origin: string, host: string): boolean {
  try {
    return new URL(origin).host === host.toLowerCase();
  } catch {
    return false;
  }
}

/**
 * An error sentence when the request is not from this site, else null. `Sec-Fetch-Site`, when
 * present, must be same-origin (a GET typed into the address bar sends "none", allowed for GET);
 * `Origin`, when present, must be the host the request was made to.
 */
export function crossSite(req: Request): string | null {
  const site = req.headers.get("sec-fetch-site");
  const allowed = req.method === "GET" || req.method === "HEAD" ? ["same-origin", "none"] : ["same-origin"];
  if (site !== null && !allowed.includes(site)) return "Cross-site requests are refused.";
  const origin = req.headers.get("origin");
  if (origin !== null && !originIsHost(origin, req.headers.get("host") ?? new URL(req.url).host)) {
    return "Cross-origin requests are refused.";
  }
  return null;
}

// The checks of the routes that start or change something (/api/builds, /api/decisions;
// docs/review-decisions.md, "Recording decisions from the web"). They stop a web page: a cross-site
// page, a DNS-rebinding page, a page on another origin of the same machine. They do NOT stop a
// program that can reach the port and sends `Host: localhost` with a matching `Origin`: a route
// cannot see who connected. So the server is started on 127.0.0.1 (web/package.json,
// docs/builds.md), and the docs never say that writes are limited to local processes.

const LOOPBACK = /^(?:localhost|127\.0\.0\.1|\[::1\])(?::(\d{1,5}))?$/i;

/** Whether the request was made to a loopback name: `localhost`, `127.0.0.1` or `[::1]`, without
 *  case, optionally followed by `:` and a port from 1 to 65535. Nothing else passes: no other
 *  name, no trailing dot, no userinfo, no empty or missing `Host`. There is no setting that
 *  widens the list. */
export function loopbackHost(req: Request): boolean {
  const host = req.headers.get("host");
  if (host === null) return false;
  const m = LOOPBACK.exec(host);
  if (!m) return false;
  if (m[1] === undefined) return true;
  const port = Number(m[1]);
  return port >= 1 && port <= 65535;
}

/** An error sentence when a write is not from this site, else null. `Origin` must be present and
 *  its host must equal `Host` (compared without case); `Sec-Fetch-Site`, when present, must be
 *  `same-origin`. Browsers send `Origin` on every POST. */
export function sameOriginWrite(req: Request): string | null {
  const site = req.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return "Cross-site requests are refused.";
  const origin = req.headers.get("origin");
  const host = req.headers.get("host");
  if (origin === null || host === null) return "Cross-origin requests are refused.";
  return originIsHost(origin, host) ? null : "Cross-origin requests are refused.";
}

/** The refusal for a request to a route that only this machine's own pages may call, or null: the
 *  loopback Host first (a DNS-rebinding page carries the attacker's name), then the same-site rules,
 *  a stricter one for a write. The builds and decisions routes both start with this. */
export function localOnly(req: Request): Response | null {
  if (!loopbackHost(req)) return fail(403, "Only this machine's own pages may use this route.");
  const refused = req.method === "GET" || req.method === "HEAD" ? crossSite(req) : sameOriginWrite(req);
  return refused ? fail(403, refused) : null;
}

/** Make a route a per-request handler, so what it reads from the environment is read at request
 *  time and `next build` cannot bake an answer into the route. Outside a Next request (the unit
 *  tests) there is nothing to wait for. */
export async function perRequest(): Promise<void> {
  try {
    await connection();
  } catch (e) {
    if (!(e instanceof Error && e.message.includes("outside a request scope"))) throw e;
  }
}

/** The body as bytes, or null once it passes `cap`, by its Content-Length or as it is read (the
 *  header may be missing or wrong). A route handler has no body limit of its own, so the body is
 *  dropped as it streams in rather than held whole first. */
export async function readCapped(req: Request, cap: number): Promise<Uint8Array | null> {
  if (Number(req.headers.get("content-length")) > cap) return null;
  const chunks: Uint8Array[] = [];
  let size = 0;
  if (req.body) {
    const reader = req.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > cap) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) {
    bytes.set(c, at);
    at += c.byteLength;
  }
  return bytes;
}

const MAX_BODY_BYTES = 1000;

/** The JSON object in a POST body, or an error sentence with the status to answer (400, or 413 for a
 *  body over the cap). The builds routes keep the 1,000 bytes; the decisions routes pass their own. */
export async function readJsonObject(
  req: Request, cap: number = MAX_BODY_BYTES,
): Promise<{ body: Record<string, unknown> } | { error: string; status: 400 | 413 }> {
  if (!/^application\/json\s*(;|$)/i.test(req.headers.get("content-type") ?? "")) {
    return { error: "Send application/json.", status: 400 };
  }
  const bytes = await readCapped(req, cap);
  if (bytes === null) return { error: "The request is too big.", status: 413 };
  try {
    const body = JSON.parse(new TextDecoder().decode(bytes));
    if (isRecord(body)) return { body };
  } catch { /* fall through */ }
  return { error: "The body must be a JSON object.", status: 400 };
}
