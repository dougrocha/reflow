"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { chunkFor, loadChunkPages, loadManifest, type Manifest } from "../core";
import { PERSIST_EVENT, readStored, writeStored } from "@/lib/storage";

export type ViewMode = "transcript" | "original";

/** Where one chunk is in its load. A chunk that has not been asked for has no state. */
export type ChunkState = "loading" | "loaded" | "failed";

export type ReflowDocument = {
  manifest: Manifest | null;
  error: string | null;
  /** HTML per page number, for pages whose chunk has loaded. */
  pages: Record<number, string>;
  /** The load state of each chunk, by href. */
  chunkStates: Record<string, ChunkState>;
  /** Load the chunk holding `page` (no-op if loaded or loading). Resolves when ready. */
  ensurePage: (page: number) => Promise<void>;
  /** Load a chunk again after it failed. Resolves to whether it loaded this time. */
  retryChunk: (href: string) => Promise<boolean>;
  /**
   * The per-page HTML of one chunk, fetched once and kept. Unlike ensurePage it does not put
   * the pages on screen: find and the transcript download read chunks without mounting them.
   */
  readChunk: (href: string) => Promise<Map<number, string>>;
  /**
   * Load every chunk, for find-in-page, printing and screen-reader heading lists. A chunk that
   * fails does not stop the rest. Resolves to how many chunks could not be loaded.
   */
  loadAll: () => Promise<number>;
  mode: ViewMode;
  setMode: (mode: ViewMode) => void;
};

const MODE_KEY = "reflow:view-mode";

// The view mode is remembered per browser; read it as an external store so the server
// render ("transcript") and the first client render agree.
function subscribeMode(onChange: () => void) {
  window.addEventListener(PERSIST_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(PERSIST_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}
function readMode(): ViewMode {
  return readStored(MODE_KEY) === "original" ? "original" : "transcript";
}

/**
 * Headless state for one reflow document. `base` is its output directory URL; mount a
 * new instance (React `key`) to switch documents.
 */
export function useReflowDocument(base: string): ReflowDocument {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pages, setPages] = useState<Record<number, string>>({});
  const mode = useSyncExternalStore(subscribeMode, readMode, () => "transcript" as ViewMode);
  const [chunkStates, setChunkStates] = useState<Record<string, ChunkState>>({});
  const pending = useRef(new Map<string, Promise<void>>());
  const manifestRef = useRef<Manifest | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    loadManifest(base, ctrl.signal)
      .then((m) => { manifestRef.current = m; setManifest(m); })
      .catch((e) => { if (!ctrl.signal.aborted) setError(String(e.message ?? e)); });
    return () => ctrl.abort();
  }, [base]);

  const setMode = useCallback((m: ViewMode) => writeStored(MODE_KEY, m), []);

  const setChunkState = useCallback((href: string, state: ChunkState) => {
    setChunkStates((prev) => ({ ...prev, [href]: state }));
  }, []);

  const chunkCache = useRef(new Map<string, Promise<Map<number, string>>>());
  const readChunk = useCallback((href: string): Promise<Map<number, string>> => {
    const cached = chunkCache.current.get(href);
    if (cached) return cached;
    setChunkState(href, "loading");
    const p = loadChunkPages(base, href, undefined, manifestRef.current?.version);
    chunkCache.current.set(href, p);
    p.then(
      () => setChunkState(href, "loaded"),
      () => { chunkCache.current.delete(href); setChunkState(href, "failed"); }, // allow a retry
    );
    return p;
  }, [base, setChunkState]);

  const loadChunk = useCallback((href: string): Promise<void> => {
    const existing = pending.current.get(href);
    if (existing) return existing;
    const p = readChunk(href)
      .then((map) => setPages((prev) => ({ ...prev, ...Object.fromEntries(map) })))
      .catch((e) => {
        pending.current.delete(href); // allow a retry
        throw e;
      });
    pending.current.set(href, p);
    return p;
  }, [readChunk]);

  const ensurePage = useCallback(async (page: number) => {
    const m = manifestRef.current;
    const chunk = m && chunkFor(m, page);
    if (chunk) await loadChunk(chunk.href);
  }, [loadChunk]);

  const retryChunk = useCallback((href: string): Promise<boolean> => {
    pending.current.delete(href);
    chunkCache.current.delete(href);
    return loadChunk(href).then(() => true, () => false);
  }, [loadChunk]);

  const loadAll = useCallback(async () => {
    const m = manifestRef.current;
    if (!m) return 0;
    let failed = 0;
    for (const c of m.chunks) await loadChunk(c.href).catch(() => { failed++; });
    return failed;
  }, [loadChunk]);

  return { manifest, error, pages, chunkStates, ensurePage, retryChunk, readChunk, loadAll, mode, setMode };
}
