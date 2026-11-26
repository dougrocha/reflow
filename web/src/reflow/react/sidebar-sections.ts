"use client";

import { useCallback } from "react";
import { isRecord } from "@/lib/guards";
import { readStored, usePersisted, writeStored } from "@/lib/storage";

// Which items of the viewer's sidebar accordion are open: "Table of contents" and "Ask a question". Kept for
// the tab's session (sessionStorage): a reload keeps them, a new tab starts from the defaults.
// Both can be open at once.

export type SectionId = "toc" | "ask";
export type Sections = Record<SectionId, boolean>;

/** Table of contents open, Ask closed until the reader opens it. Also the server render. */
export const DEFAULT_SECTIONS: Sections = { toc: true, ask: false };

const KEY = "reflow:sidebar-sections";

/** The stored JSON as sections: a missing or malformed field takes its default. Null if not an object. */
export function parseSections(raw: unknown): Sections | null {
  if (!isRecord(raw)) return null;
  const o = raw as Record<string, unknown>;
  return {
    toc: typeof o.toc === "boolean" ? o.toc : DEFAULT_SECTIONS.toc,
    ask: typeof o.ask === "boolean" ? o.ask : DEFAULT_SECTIONS.ask,
  };
}

function current(): Sections {
  try {
    const raw = readStored(KEY, "session");
    return (raw ? parseSections(JSON.parse(raw)) : null) ?? DEFAULT_SECTIONS;
  } catch {
    return DEFAULT_SECTIONS;
  }
}

export function useSidebarSections() {
  const [sections] = usePersisted(KEY, DEFAULT_SECTIONS, parseSections, "session");
  // Merges into what is stored now, so two changes in one tick do not undo each other.
  const setOpen = useCallback((id: SectionId, open: boolean) => {
    const now = current();
    if (now[id] !== open) writeStored(KEY, JSON.stringify({ ...now, [id]: open }), "session");
  }, []);
  return [sections, setOpen] as const;
}
