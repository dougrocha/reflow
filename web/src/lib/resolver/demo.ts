import { exactOrigin } from "@/embed/protocol";
import type { Site } from "./index";
import { normalizeSourceUrl } from "./url";

// The built-in demo site: a customer page on an origin of its own that tries the loader against
// this server. It is a route of this server (src/app/demo-site, served by src/demo-site/server.ts)
// at DEMO_BASE, reached at REFLOW_DEMO_ORIGIN, which scripts/dev sets to another origin than the
// app's (http://127.0.0.1:<port> beside http://localhost:<port>). docs/embed.md, "Trying it".
//
// It is a site of the resolver's index like any other, made in code instead of read from a file,
// so the demo runs the real resolver, framing and CORS rules. Its one origin comes from the
// operator's environment (REFLOW_DEMO_ORIGIN) and from nothing a request says; with it unset or not
// an exact origin there is no demo site, and the key `demo` is an unknown site.

export const DEMO_SITE_KEY = "demo";

/** Where the demo customer page and its PDFs are on its origin. */
export const DEMO_BASE = "/demo-site";

/** The demo page's PDF links (path on the demo origin) and the converted document each stands
 *  for. The page links to these and to one more that is deliberately not here
 *  (DEMO_UNREGISTERED), so a click that falls back to the PDF can be seen too. */
export const DEMO_DOCUMENTS: Record<string, string> = {
  [`${DEMO_BASE}/files/fw9.pdf`]: "fw9",
  [`${DEMO_BASE}/files/accident-insurance-flyer.pdf`]: "az-accident-insurance-flyer",
};

/** A PDF the demo page links to that the site has no document for: the resolver says `unknown`
 *  and the click goes to the PDF. Value: the file under public/pdfs that the demo server sends. */
export const DEMO_UNREGISTERED: Record<string, string> = {
  [`${DEMO_BASE}/files/resnet.pdf`]: "resnet",
};

/** The demo site for this origin, or null unless `origin` is an exact http(s) origin. */
export function demoSite(origin: string | undefined): Site | null {
  const exact = exactOrigin(origin);
  if (exact === null) return null;
  const documents = new Map<string, string>();
  for (const [path, id] of Object.entries(DEMO_DOCUMENTS)) {
    const url = normalizeSourceUrl(exact + path);
    if (url !== null) documents.set(url, id);
  }
  return { key: DEMO_SITE_KEY, origins: [exact], documents };
}

/** The demo customer page's origin (REFLOW_DEMO_ORIGIN), or null when it is unset or not exact. */
export const demoOrigin = (env: Record<string, string | undefined> = process.env): string | null =>
  exactOrigin(env.REFLOW_DEMO_ORIGIN);
