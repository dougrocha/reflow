import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { exactOrigin } from "../embed/protocol";
import { DEMO_BASE, DEMO_DOCUMENTS, DEMO_UNREGISTERED } from "../lib/resolver/demo";

// The demo customer page (docs/embed.md, "Trying it"): a site on an origin of its own that
// includes reflow's loader the way a customer would. It serves three things and nothing else:
//
//  - `DEMO_BASE/` : web/demo-site/index.html with reflow's origin filled in;
//  - `DEMO_BASE/files/<name>.pdf` : a PDF from public/pdfs, only for the paths the demo site registered
//    (DEMO_DOCUMENTS) and the one it deliberately did not (DEMO_UNREGISTERED). The path is looked
//    up as written in a fixed table; no part of it ever reaches the file system;
//  - a 404 for everything else.
//
// The page's CSP is the one docs/embed.md prescribes for a customer: reflow's origin for the
// loader (script-src), the resolver call (connect-src) and the viewer's frame (frame-src), and no
// other source. `default-src 'none'` makes that the whole policy, so the page has no inline style.
// src/app/demo-site serves it from this server, at the demo origin (src/lib/resolver/demo.ts).

export type DemoConfig = {
  /** Reflow's origin: where the loader, the resolver and the viewer are. An exact origin. */
  reflowOrigin: string;
  /** Where public/pdfs and the page are; the defaults are this checkout's. */
  pdfsDir?: string;
  pageFile?: string;
};

// The web folder: the server and the tests run there.
const WEB = process.cwd();
const PLACEHOLDER = "{{REFLOW_ORIGIN}}";

/** The files the server may send: the demo's links, to the PDF each stands for. */
export const DEMO_FILES: Readonly<Record<string, string>> = { ...DEMO_DOCUMENTS, ...DEMO_UNREGISTERED };

/** The customer's CSP, for reflow at `reflowOrigin`. */
export const demoCsp = (reflowOrigin: string): string =>
  [
    "default-src 'none'",
    `script-src ${reflowOrigin}`,
    `connect-src ${reflowOrigin}`,
    `frame-src ${reflowOrigin}`,
    "base-uri 'none'",
    "frame-ancestors 'none'",
  ].join("; ");

const HEADERS = { "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store" };

const text = (status: number, body: string, extra: Record<string, string> = {}): Response =>
  new Response(body, { status, headers: { ...HEADERS, "Content-Type": "text/plain; charset=utf-8", ...extra } });

/** The answer to one request. Throws when `cfg.reflowOrigin` is not an exact origin, since it is
 *  written into the page and its policy. */
export function demoResponse(req: Request, cfg: DemoConfig): Response {
  const origin = exactOrigin(cfg.reflowOrigin);
  if (origin === null) throw new Error(`reflowOrigin must be an exact http(s) origin, got ${JSON.stringify(cfg.reflowOrigin)}`);
  if (req.method !== "GET" && req.method !== "HEAD") return text(405, "Method not allowed", { Allow: "GET, HEAD" });
  const { pathname } = new URL(req.url);

  if (pathname === DEMO_BASE || pathname === `${DEMO_BASE}/` || pathname === `${DEMO_BASE}/index.html`) {
    const page = readFileSync(cfg.pageFile ?? join(WEB, "demo-site", "index.html"), "utf8").replaceAll(PLACEHOLDER, origin);
    return new Response(req.method === "HEAD" ? null : page, {
      headers: { ...HEADERS, "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": demoCsp(origin) },
    });
  }

  const slug = Object.hasOwn(DEMO_FILES, pathname) ? DEMO_FILES[pathname] : undefined;
  if (slug !== undefined) {
    const file = join(cfg.pdfsDir ?? join(WEB, "public", "pdfs"), `${slug}.pdf`);
    if (existsSync(file)) {
      return new Response(req.method === "HEAD" ? null : readFileSync(file), {
        headers: { ...HEADERS, "Content-Type": "application/pdf" },
      });
    }
  }
  return text(404, "Not found");
}
