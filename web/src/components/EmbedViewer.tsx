"use client";

import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { DocumentViewer } from "@/reflow/react/DocumentViewer";
import { typingIn } from "@/reflow/react/shortcuts";
import { connectParent, embedContext, type EmbedContext, type ParentConnection } from "@/embed/viewer";

// The /embed page: the viewer, and its side of the embed protocol (src/embed/protocol.ts) when it
// is framed for a parent. Framed, it says `ready` and the title once it listens, `page` as the
// reader moves, and `close` on its Close button or Escape; it goes to each page the parent asks
// for. Not framed (the shareable hosted page), it is the viewer alone, with no Close button.

let context: EmbedContext | null | undefined;
const noSubscription = () => () => {};
/** The frame's context, read once: the URL and the parent do not change while the page lives. */
const clientContext = () => (context === undefined ? (context = embedContext(window)) : context);

export function EmbedViewer({ slug, title, base, initialPage }: { slug: string; title: string; base: string; initialPage?: number }) {
  // Null on the server and in the first client render, so hydration matches.
  const framed = useSyncExternalStore(noSubscription, clientContext, () => null);
  const titleId = useId();
  const connection = useRef<ParentConnection | null>(null);
  const lastPage = useRef(initialPage ?? 1);
  const [request, setRequest] = useState<{ page: number; n: number }>();

  useEffect(() => {
    if (!framed) return;
    let n = 0;
    const c = connectParent(window, framed, (page) => setRequest({ page, n: ++n }));
    connection.current = c;
    c.send({ type: "ready" });
    c.send({ type: "title", title });
    c.send({ type: "page", page: lastPage.current });
    return () => {
      c.disconnect();
      connection.current = null;
    };
  }, [framed, title]);

  const onPageChange = useCallback((page: number) => {
    lastPage.current = page;
    connection.current?.send({ type: "page", page });
  }, []);
  const close = useCallback(() => connection.current?.send({ type: "close" }), []);

  // Escape closes the frame, unless something inside the viewer has it: a field, an open dialog
  // or sheet, or a handler that already used it.
  useEffect(() => {
    if (!framed) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || typingIn(e.target) || document.querySelector("[role=dialog]")) return;
      close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [framed, close]);

  return (
    <main id="main" tabIndex={-1} className="flex min-h-0 flex-1 flex-col focus:outline-none">
      {framed ? null : <h1 className="sr-only">{title}</h1>}
      <DocumentViewer base={base} documentTitle={title} initialPage={initialPage} onPageChange={onPageChange}
                      onClose={framed ? close : undefined} titleId={framed ? titleId : undefined}
                      pageSourceUrl={(page) => `/api/source/${slug}/${page}`} pageRequest={request} />
    </main>
  );
}
