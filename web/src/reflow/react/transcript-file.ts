// The whole transcript as one HTML file, for the viewer's download and print actions. Loaded
// on demand (import() from DocumentViewer): most readers never download or print.

import { resolve, type Manifest } from "../core";
import { escapeHtml } from "@/lib/format";
import { pageText } from "@/lib/pages";
import { fillMarkup, type TypedValues } from "./form-values";

/**
 * Every page's HTML in a single document with the stylesheet inlined. (The printable
 * index.html is a shell that fills in chunks as you scroll, so printing or saving it would
 * miss the sections not yet loaded.) Page images and figures stay links to this site, so
 * they need a connection. What the reader typed into form fields (`values`) is written into the
 * controls' value, checked and selected attributes; with nothing typed the markup is as converted.
 */
export async function buildTranscriptFile(
  manifest: Manifest,
  base: string,
  readChunk: (href: string) => Promise<Map<number, string>>,
  values?: TypedValues,
): Promise<string> {
  const sections: string[] = [];
  for (const chunk of manifest.chunks) {
    for (const [n, html] of await readChunk(chunk.href)) {
      const label = pageText(n, manifest.pages[n - 1]?.label);
      // The wrapper and label the chunk pages publish (render/chunks.py), so viewer.css styles them.
      sections.push(`<div class="pg" id="p${n}" data-page="${n}"><p class="pg-label"><span>Page ${escapeHtml(label)}</span></p>${values ? fillMarkup(html, values) : html}</div>`);
    }
  }
  const css = await fetch(resolve(base, "assets/viewer.css", manifest.version)).then((r) => (r.ok ? r.text() : "")).catch(() => "");
  const title = escapeHtml(manifest.title);
  const body = sections.join("\n").replace(/(src|href)="\/(?!\/)/g, `$1="${location.origin}/`);
  return `<!doctype html>\n<html lang="${escapeHtml(manifest.lang)}" dir="${escapeHtml(manifest.dir)}">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>${title}</title>\n<style>${css}</style>\n</head>\n<body>\n<main id="content" class="reflow-transcript">\n<h1>${title}</h1>\n${body}\n</main>\n</body>\n</html>\n`;
}

/** Offer the file as a download named after the document. */
export function downloadFile(file: string, name: string) {
  downloadBlob(new Blob([file], { type: "text/html" }), name);
}

export function downloadBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/**
 * Print the file from a hidden frame, so the reader stays on the page. The frame is removed
 * after printing, or after ten minutes if the print dialog never reports back.
 */
export function printFile(file: string) {
  const url = URL.createObjectURL(new Blob([file], { type: "text/html" }));
  const frame = document.createElement("iframe");
  const cleanup = () => {
    frame.remove();
    URL.revokeObjectURL(url);
  };
  frame.setAttribute("aria-hidden", "true");
  frame.tabIndex = -1;
  frame.style.cssText = "position:fixed;width:0;height:0;border:0;visibility:hidden";
  frame.onload = () => {
    // A frame the page cannot reach (a policy blocked the blob: URL and the browser put its
    // error page there) throws on access; print in a new tab instead.
    try {
      const win = frame.contentWindow;
      if (!win) throw new Error("no frame window");
      win.addEventListener("afterprint", cleanup, { once: true });
      win.focus();
      win.print();
    } catch {
      cleanup();
      printInTab(file);
    }
  };
  window.setTimeout(cleanup, 10 * 60_000);
  frame.src = url;
  document.body.append(frame);
}

/** Open the file in a new tab to print from there; download it if the browser blocks the tab. */
function printInTab(file: string) {
  const blob = new Blob([file], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  const tab = window.open(url, "_blank");
  if (!tab) {
    URL.revokeObjectURL(url);
    downloadBlob(blob, "transcript.html");
    return;
  }
  window.setTimeout(() => URL.revokeObjectURL(url), 10 * 60_000);
}
