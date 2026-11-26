import { connectViewer } from "./parent";
import { isPlainClick, type LinkLike, pdfLink, readyDocument, resolveUrl, SITE_KEY, viewerUrl } from "./loader-rules";
import { embedSrc, exactOrigin, newInstanceId } from "./protocol";

// The loader: the one script a site adds to its pages (docs/embed.md).
//
//   <script src="https://<reflow>/embed/loader.js" data-site="KEY" defer></script>
//
// One delegated click listener, so links added later work. A click on a PDF link (loader-rules.ts)
// asks the resolver whether reflow has converted that PDF. Ready: the viewer opens over the page in a
// <dialog>. Anything else, a timeout and an error included: the page goes to the PDF, as the link
// says. The link's href and its name are never touched, so without this script, or when it fails,
// every link is the PDF link it was.
//
// What the page's CSP needs: script-src for this file, connect-src for the resolver and frame-src
// for the viewer, all on reflow's origin. No style-src: the dialog is styled through the CSSOM and
// a constructable stylesheet, which a CSP does not block, and nothing here sets a `style` attribute,
// injects a <style> or uses eval.

/** How long a click waits for the resolver before the page goes to the PDF. */
const RESOLVE_TIMEOUT_MS = 1500;
/** How long the viewer has to say it is ready before the page goes to the PDF instead. */
const READY_TIMEOUT_MS = 10_000;
/** The resolver's answer is a few hundred bytes; a longer one is not an answer. */
const MAX_ANSWER_CHARS = 4096;
const HISTORY_KEY = "reflowEmbed";

type Config = { origin: string; site: string; parent: string };

/** Where this script came from and which site it is for, or null when it cannot work: no script
 *  element to read (it was not loaded by a plain <script src>), a bad `data-site`, or a page
 *  whose origin cannot be named to the viewer (file:, a sandboxed frame). */
function configure(script: HTMLScriptElement | null): Config | null {
  const site = script?.getAttribute("data-site") ?? "";
  const parent = exactOrigin(location.origin);
  let origin: string | null = null;
  try {
    origin = exactOrigin(new URL(script?.src ?? "").origin);
  } catch {
    origin = null;
  }
  return origin !== null && parent !== null && SITE_KEY.test(site) ? { origin, site, parent } : null;
}

/** The document id the resolver says is ready for this PDF, or null: not ready, no answer in time,
 *  or an answer that fails `readyDocument`. Never throws. */
async function resolve(config: Config, pdf: string): Promise<string | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), RESOLVE_TIMEOUT_MS);
  try {
    const response = await fetch(resolveUrl(config.origin, config.site, pdf), {
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    });
    if (!response.ok) return null;
    const text = await response.text();
    return text.length > MAX_ANSWER_CHARS ? null : readyDocument(JSON.parse(text), config.origin, config.site);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** The dialog's look, set where a CSP lets it: the CSSOM, not a `style` attribute. */
function styleOverlay(dialog: HTMLDialogElement, frame: HTMLIFrameElement) {
  const set = (el: HTMLElement, props: Record<string, string>) =>
    Object.entries(props).forEach(([name, value]) => el.style.setProperty(name, value));
  set(dialog, {
    padding: "0", border: "0", margin: "auto", "max-width": "none", "max-height": "none",
    width: "min(72rem, calc(100vw - 2rem))", height: "calc(100dvh - 2rem)", overflow: "hidden",
    "border-radius": "0.5rem", outline: "none", background: "#fff", "box-shadow": "0 1rem 3rem rgba(0, 0, 0, 0.4)",
  });
  set(frame, { display: "block", width: "100%", height: "100%", border: "0" });
  // ::backdrop can only be reached from a stylesheet. A constructable one is not an inline style;
  // without it the browser's own backdrop stays.
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync("dialog[data-reflow-embed]::backdrop { background: rgba(0, 0, 0, 0.6); }");
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  } catch {
    // An older browser: the default backdrop.
  }
}

/** The page's `<base target>`: what a link without a `target` of its own opens in. */
const baseTarget = () => document.querySelector("base[target]")?.getAttribute("target") ?? "";

/** The link a click activated, as the loader reads it: the innermost `<a>` with an href on the
 *  click's composed path, so a link inside a shadow root, or one wrapping a component's shadow
 *  content, is found, and an SVG `<a>` too. Null when the click was on no link. */
export function clickedLink(event: Event): { element: HTMLElement | SVGElement; link: LinkLike } | null {
  for (const node of event.composedPath()) {
    if (node instanceof HTMLAnchorElement) {
      if (node.hasAttribute("href")) return { element: node, link: node };
    } else if (node instanceof SVGElement && node.localName === "a") {
      const raw = node.getAttribute("href") ?? node.getAttributeNS("http://www.w3.org/1999/xlink", "href");
      if (!raw) continue;
      let href: string;
      try {
        href = new URL(raw, node.baseURI).href;
      } catch {
        return null;
      }
      return { element: node, link: { href, getAttribute: (name) => node.getAttribute(name) } };
    }
  }
  return null;
}

/** Open the viewer for `src` over the page. Exactly one of `onClosed` and `onFailed` runs, once:
 *  `onFailed` when the viewer did not say it was ready in time, after the overlay and its history
 *  entry are gone, so the page can go to the PDF from where it was. Throws, having changed
 *  nothing, when the browser has no modal dialog. */
function openOverlay(link: HTMLElement | SVGElement, src: string, instance: string, onClosed: () => void, onFailed: () => void) {
  const dialog = document.createElement("dialog");
  if (typeof dialog.showModal !== "function") throw new Error("no modal dialog");
  dialog.setAttribute("data-reflow-embed", "");
  dialog.setAttribute("aria-label", "Accessible document viewer");
  const frame = document.createElement("iframe");
  frame.title = "Accessible document viewer";
  dialog.append(frame);
  styleOverlay(dialog, frame);
  // Focus the dialog, not the frame the browser would pick: until the viewer has loaded, Escape in
  // a frame that has no handler yet would do nothing, and on the dialog it closes it.
  dialog.tabIndex = -1;
  document.body.append(dialog);
  try {
    dialog.showModal();
  } catch (error) {
    dialog.remove();
    throw error;
  }

  const savedOverflow = document.documentElement.style.overflow;
  let closed = false;
  let ready = false;
  let failed = false;

  // Closing goes through the history entry this overlay added, so Back and every other way out
  // leave the history as it was: the entry is popped, and popstate does the closing.
  const finish = () => {
    if (closed) return;
    closed = true;
    clearTimeout(readyTimer);
    removeEventListener("popstate", onPop);
    connection.disconnect();
    if (dialog.open) dialog.close();
    dialog.remove();
    document.documentElement.style.overflow = savedOverflow;
    if (failed) {
      onFailed();
      return;
    }
    if (link.isConnected) link.focus();
    onClosed();
  };
  const onPop = () => finish();
  const close = () => {
    if (closed) return;
    if (history.state?.[HISTORY_KEY] === instance) history.back();
    else finish();
  };

  const connection = connectViewer({
    frame, src, instance,
    onReady: () => {
      ready = true;
      clearTimeout(readyTimer);
      // Hand the keyboard to the viewer, so its shortcuts and its Escape work, unless the reader
      // already moved focus somewhere of their own.
      if (document.activeElement === dialog) frame.focus();
    },
    onTitle: (title) => {
      dialog.setAttribute("aria-label", title);
      frame.title = `${title}: accessible version`;
    },
    onClose: close,
  });
  // The viewer posts `close` for Escape pressed inside it. Escape with focus on the dialog itself
  // arrives as `cancel`.
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  // No ready in time: close as Back would, so the history entry goes too, then the PDF.
  const readyTimer = setTimeout(() => {
    if (ready || closed) return;
    failed = true;
    close();
  }, READY_TIMEOUT_MS);

  const state = history.state;
  history.pushState({ ...(typeof state === "object" && state !== null ? state : {}), [HISTORY_KEY]: instance }, "");
  addEventListener("popstate", onPop);
  document.documentElement.style.overflow = "hidden";
  dialog.focus();
  frame.src = src;
}

/** Install the click listener for the site `script` names. Returns a function that removes it
 *  (for tests), or null when the loader cannot work here or is already installed. */
export function start(script: HTMLScriptElement | null, navigate = (url: string) => location.assign(url)): (() => void) | null {
  const config = configure(script);
  if (config === null) return null;
  const registry = Symbol.for("reflow.embed.loader");
  const flagged = window as unknown as Record<symbol, boolean>;
  if (flagged[registry]) return null;
  flagged[registry] = true;

  let busy = false;
  const onClick = (event: MouseEvent) => {
    if (!isPlainClick(event)) return;
    const clicked = clickedLink(event);
    if (clicked === null) return;
    const pdf = pdfLink(clicked.link, baseTarget());
    if (pdf === null) return;
    // The resolver answers later than the click, so the click is held now and goes to the PDF
    // below when there is nothing to open. A second click in the meantime waits for the first.
    event.preventDefault();
    if (busy) return;
    busy = true;
    const href = clicked.link.href;
    const toPdf = () => {
      busy = false;
      navigate(href);
    };
    void resolve(config, pdf).then((id) => {
      if (id === null) return toPdf();
      const instance = newInstanceId();
      const src = embedSrc(viewerUrl(config.origin, id, config.site), { parent: config.parent, instance });
      try {
        openOverlay(clicked.element, src, instance, () => (busy = false), toPdf);
      } catch {
        toPdf();
      }
    });
  };
  document.addEventListener("click", onClick);
  return () => {
    document.removeEventListener("click", onClick);
    flagged[registry] = false;
  };
}

start(document.currentScript as HTMLScriptElement | null);
