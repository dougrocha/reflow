// What the loader (./loader) decides without touching the page: which links are PDF links, which
// clicks it may take, and whether the resolver's answer is one it may open. No imports from the app
// and no side effects, so the rules are tested alone (loader.test.ts) and the loader's bundle stays
// small. docs/embed.md has the rules in prose.

/** Where the app serves the loader script (built by scripts/build-loader.ts), beside the viewer's
 *  /embed/<id> pages. */
export const LOADER_PATH = "/embed/loader.js";

/** A site key as the resolver's index writes it (src/lib/resolver/index.ts). */
export const SITE_KEY = /^[A-Za-z0-9_-]{1,64}$/;

/** The longest link the loader sends to the resolver, which answers a longer one with a 400. */
export const MAX_URL_CHARS = 2048;

/** A document id as a /embed/<id> path has it: a catalogue slug. Never a path or a dot segment. */
const DOCUMENT_ID = /^[A-Za-z0-9_-]{1,128}$/;

/** What the loader reads of a link. An `HTMLAnchorElement` has all of it: `href` is the resolved
 *  URL, and `getAttribute` reads the attributes the page wrote. */
export type LinkLike = {
  href: string;
  getAttribute: (name: string) => string | null;
};

/** The PDF URL to ask the resolver about when this link is one the loader may take, else null.
 *
 *  A link is a PDF link when its URL path ends in `.pdf` (any case, query and fragment ignored), it
 *  has `type="application/pdf"`, or it carries `data-reflow` (so a link such as `/download?id=3` can
 *  be named, and the resolver decides). `data-reflow="off"` opts a link out whatever else it has.
 *  A `download` link, a link whose target is not `_self`, and a URL that is not http or https are
 *  left to the browser. The target is the link's own `target`, or else `baseTarget`, the page's
 *  `<base target>`, as the browser decides it. */
export function pdfLink(link: LinkLike, baseTarget = ""): string | null {
  const flag = link.getAttribute("data-reflow");
  if (flag !== null && flag.trim().toLowerCase() === "off") return null;
  if (link.getAttribute("download") !== null) return null;
  const target = (link.getAttribute("target") ?? baseTarget).trim().toLowerCase();
  if (target !== "" && target !== "_self") return null;
  let url: URL;
  try {
    url = new URL(link.href);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const type = (link.getAttribute("type") ?? "").split(";")[0].trim().toLowerCase();
  if (!/\.pdf$/i.test(url.pathname) && type !== "application/pdf" && flag === null) return null;
  url.hash = "";
  return url.href.length <= MAX_URL_CHARS ? url.href : null;
}

/** What the loader reads of a click. A `MouseEvent` has all of it. */
export type ClickLike = {
  button: number;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  defaultPrevented: boolean;
};

/** Whether a click is the plain activation of a link: the main button, no modifier (those open a
 *  tab, a window or a download), and not already handled by the page. Enter on a focused link
 *  arrives as the same click event, so this covers the keyboard too. */
export function isPlainClick(event: ClickLike): boolean {
  return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && !event.defaultPrevented;
}

/** The resolver's answer is data from the network, not a command. This is the document id from a
 *  `{status: "ready", viewer}` answer when the viewer is this origin's `/embed/<id>?site=<site>`,
 *  and null otherwise (any other status, any other origin, path, parameter, fragment or
 *  credential). The loader builds the frame's URL from the id itself (`viewerUrl`) and never
 *  loads what the answer wrote. `origin` is the origin the loader script came from. */
export function readyDocument(answer: unknown, origin: string, site: string): string | null {
  if (typeof answer !== "object" || answer === null || Array.isArray(answer)) return null;
  const { status, viewer } = answer as Record<string, unknown>;
  if (status !== "ready" || typeof viewer !== "string" || viewer.length > MAX_URL_CHARS) return null;
  let url: URL;
  try {
    url = new URL(viewer, origin);
  } catch {
    return null;
  }
  if (url.origin !== origin || url.username !== "" || url.password !== "" || url.hash !== "") return null;
  const params = [...url.searchParams];
  if (params.length !== 1 || params[0][0] !== "site" || params[0][1] !== site) return null;
  const match = /^\/embed\/([^/]+)$/.exec(url.pathname);
  if (!match) return null;
  let id: string;
  try {
    id = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  return DOCUMENT_ID.test(id) ? id : null;
}

/** The viewer's URL on `origin` for a document and a site. The frame's `parent` and `instance`
 *  are added by `embedSrc` (./protocol). */
export const viewerUrl = (origin: string, id: string, site: string): string =>
  `${origin}/embed/${encodeURIComponent(id)}?site=${encodeURIComponent(site)}`;

/** The resolver's URL for a PDF link. */
export const resolveUrl = (origin: string, site: string, pdf: string): string =>
  `${origin}/v1/resolve?site=${encodeURIComponent(site)}&url=${encodeURIComponent(pdf)}`;
