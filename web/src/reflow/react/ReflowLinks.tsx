"use client";

import { useCallback, useId, useSyncExternalStore, type ReactNode } from "react";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { parsePage } from "@/lib/pages";
import { DocumentViewer } from "./DocumentViewer";

type Props = {
  /** PDF link href (pathname) → reflow output directory for that PDF. */
  documents: Record<string, string>;
  children: ReactNode;
};

const PREFIX = "#reflow=";
const CLEARED = "reflow:hash-cleared";

// The open document lives in the URL hash (#reflow=/pdfs/report.pdf&page=40): shareable, the
// browser's Back button closes the viewer, and a reload returns to the page being read. The
// page mirrors the /view page's ?page=N, in the hash because the dialog is on another page's URL.
function subscribeHash(onChange: () => void) {
  window.addEventListener("hashchange", onChange);
  window.addEventListener(CLEARED, onChange);
  return () => {
    window.removeEventListener("hashchange", onChange);
    window.removeEventListener(CLEARED, onChange);
  };
}
function readHash(): string {
  return location.hash.startsWith(PREFIX) ? location.hash : "";
}
/** The PDF and page named by a #reflow= hash; the PDF is null when the hash names none. */
function parseHash(hash: string): { pdf: string | null; page: number | null } {
  if (!hash.startsWith(PREFIX)) return { pdf: null, page: null };
  try {
    const params = new URLSearchParams(hash.slice(1));
    return { pdf: params.get("reflow"), page: parsePage(params.get("page")) };
  } catch {
    // A malformed escape such as #reflow=%E0 is not a document: show none, rather than throw.
    return { pdf: null, page: null };
  }
}
function hashFor(pdf: string, page: number | null): string {
  return `${PREFIX}${encodeURIComponent(pdf)}${page && page > 1 ? `&page=${page}` : ""}`;
}

/**
 * Wraps ordinary content. Clicking a link to a PDF that has a reflow version opens the
 * accessible viewer in a modal dialog instead of downloading the PDF. Links keep
 * working as plain downloads without JavaScript, and modified clicks (new tab, save)
 * behave as usual.
 */
export function ReflowLinks({ documents, children }: Props) {
  const titleId = useId();
  const hash = useSyncExternalStore(subscribeHash, readHash, () => "");
  const { pdf, page } = parseHash(hash);
  const base = pdf ? documents[pdf] : undefined;

  // Closing the dialog (Escape, outside click, or the viewer's Close button) clears the hash.
  const clearHash = useCallback(() => {
    if (!parseHash(location.hash).pdf) return;
    history.replaceState(null, "", location.pathname + location.search);
    window.dispatchEvent(new Event(CLEARED));
  }, []);

  // The page at the reading line is kept in the hash, without a history entry per page.
  const onPageChange = useCallback((current: number) => {
    const open = parseHash(location.hash).pdf;
    if (!open) return;
    const next = hashFor(open, current);
    if (next !== location.hash) history.replaceState(null, "", next);
  }, []);

  const onClick = (e: React.MouseEvent) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = (e.target as Element).closest<HTMLAnchorElement>("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || !documents[url.pathname]) return;
    e.preventDefault();
    location.hash = hashFor(url.pathname, null);
  };

  return (
    <>
      {/* Click delegation only: the links inside stay the focusable controls. */}
      <div onClick={onClick}>{children}</div>
      <Dialog open={!!base} onOpenChange={(open) => { if (!open) clearHash(); }}>
        <DialogContent
          showCloseButton={false}
          overlayClassName="bg-black/50"
          aria-labelledby={titleId}
          className="grid h-[calc(100dvh_-_2rem)] w-[min(100vw_-_2rem,90rem)] max-w-none grid-cols-[minmax(0,1fr)] grid-rows-[minmax(0,1fr)] gap-0 overflow-hidden rounded-xl p-0 sm:max-w-none max-[40rem]:h-dvh max-[40rem]:w-screen max-[40rem]:rounded-none"
        >
          {base && (
            <DocumentViewer
              key={base}
              base={base}
              onClose={clearHash}
              titleId={titleId}
              initialPage={page ?? undefined}
              onPageChange={onPageChange}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
