"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Manifest } from "../core";
import { countIn, normalizeQuery, textOfHtml } from "./find";

// How many chunks are read at once. Each is one request and a parse; a few in flight keep the
// network busy without starving the page of the main thread.
const CONCURRENCY = 4;
// The count on screen is refreshed at most this often while the index fills in.
const REFRESH_MS = 150;
// Wait this long after the last keystroke before searching.
const DEBOUNCE_MS = 200;

type FindPosition = { page: number; nth: number };

export type FindState = {
  /** The query as searched (folded, 2+ characters), or "" for none. */
  query: string;
  /** Matches among the pages read so far. */
  total: number;
  /** 1-based place of the current match among them, or 0. */
  rank: number;
  position: FindPosition | null;
  /** True while chunks are still being read: `total` can still grow. */
  searching: boolean;
  /** Share of the chunks read, 0 to 1. */
  progress: number;
  /** Chunks that could not be read. */
  failed: number;
  next: () => void;
  prev: () => void;
  /** Start reading the document's text ahead of the first query (on focus in the find box). */
  warm: () => void;
};

// Give the main thread back without waiting for a timer: timers are clamped to a second in a
// background tab, a message channel is not.
function yieldToMain(): Promise<void> {
  return new Promise((resolve) => {
    const { port1, port2 } = new MessageChannel();
    port1.onmessage = () => { port1.close(); resolve(); };
    port2.postMessage(null);
  });
}

/**
 * Find in the whole transcript. The text of every page is read once, chunk by chunk, into a
 * map kept outside React state (so a 2,600-page document does not mount 16 MB of HTML): the
 * chunk holding the page being read first, then onwards, then the earlier ones. A search counts
 * matches per page; stepping moves through them in page order. Matches are counted among the
 * pages read so far, and `searching` says whether more may come.
 */
export function useFind({ manifest, readChunk, query, getStartPage, onAnnounce }: {
  manifest: Manifest | null;
  readChunk: (href: string) => Promise<Map<number, string>>;
  /** What the person typed. */
  query: string;
  /** The page at the reading line: the first match is the first one at or after it. */
  getStartPage: () => number;
  /** Polite spoken messages: the current match, or that there is none. */
  onAnnounce: (text: string) => void;
}): FindState {
  const texts = useRef(new Map<number, string>());
  const counts = useRef({ q: "", perPage: new Map<number, number>() });
  const started = useRef(false);
  const failedRef = useRef(0);
  const chunksDone = useRef(0);
  const lastRefresh = useRef(0);
  const startPage = useRef(1);
  const announcedNone = useRef("");
  const activeRef = useRef("");
  const [version, setVersion] = useState(0);
  const [stats, setStats] = useState({ progress: 0, failed: 0 });
  const [searching, setSearching] = useState(false);
  const [active, setActive] = useState("");
  const [position, setPosition] = useState<FindPosition | null>(null);
  const [summary, setSummary] = useState({ total: 0, rank: 0 });
  const announce = useRef(onAnnounce);
  useEffect(() => { announce.current = onAnnounce; });

  // The query settles before it is searched; a new query starts from the reading line.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const q = normalizeQuery(query);
      if (q === activeRef.current) return;
      activeRef.current = q;
      startPage.current = getStartPage();
      setPosition(null);
      setActive(q);
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query, getStartPage]);

  const warm = useCallback(() => {
    if (started.current || !manifest) return;
    started.current = true;
    setSearching(true);
    const chunks = manifest.chunks;
    const here = getStartPage();
    const i0 = Math.max(0, chunks.findIndex((c) => c.first <= here && here <= c.last));
    const order = [...chunks.slice(i0), ...chunks.slice(0, i0)];
    let cursor = 0;
    const worker = async () => {
      while (cursor < order.length) {
        const chunk = order[cursor++];
        try {
          for (const [n, html] of await readChunk(chunk.href)) texts.current.set(n, textOfHtml(html));
        } catch {
          failedRef.current++;
        }
        chunksDone.current++;
        const now = performance.now();
        if (now - lastRefresh.current > REFRESH_MS) {
          lastRefresh.current = now;
          setStats({ progress: chunksDone.current / order.length, failed: failedRef.current });
          setVersion((v) => v + 1);
        }
        await yieldToMain();
      }
    };
    Promise.all(Array.from({ length: CONCURRENCY }, worker)).then(() => {
      setStats({ progress: 1, failed: failedRef.current });
      setSearching(false);
      setVersion((v) => v + 1);
    });
  }, [manifest, readChunk, getStartPage]);

  // A query starts the reading if nothing has yet.
  useEffect(() => { if (active) warm(); }, [active, warm]);

  // Count matches per page (only pages not yet counted for this query), pick the first match if
  // none is chosen, and work out the rank of the current one. Runs as the index grows.
  useEffect(() => {
    const c = counts.current;
    if (c.q !== active) { c.q = active; c.perPage = new Map(); }
    if (!active) return;
    let total = 0;
    for (const [page, text] of texts.current) {
      let n = c.perPage.get(page);
      if (n === undefined) { n = countIn(text, active); c.perPage.set(page, n); }
      total += n;
    }
    let pos = position;
    if (!pos && total > 0) {
      // The first match from the reading line on; if there is none after it, and every page has
      // been read, the first in the document.
      const pages = [...c.perPage].filter(([, n]) => n > 0).map(([p]) => p).sort((a, b) => a - b);
      const from = pages.find((p) => p >= startPage.current);
      const page = from ?? (searching ? undefined : pages[0]);
      if (page !== undefined) {
        pos = { page, nth: 0 };
        setPosition(pos);
      }
    }
    let rank = 0;
    if (pos) {
      rank = pos.nth + 1;
      for (const [page, n] of c.perPage) if (page < pos.page) rank += n;
    }
    setSummary((s) => (s.total === total && s.rank === rank ? s : { total, rank }));
    if (!searching && total === 0 && announcedNone.current !== active) {
      announcedNone.current = active;
      announce.current(`No matches for ${active}.`);
    }
  }, [active, version, position, searching]);

  // Say which match is current whenever the person steps (or the first is chosen). The rank is
  // worked out here from the counts, because the summary state is a render behind.
  const searchingRef = useRef(searching);
  useEffect(() => { searchingRef.current = searching; });
  useEffect(() => {
    if (!position) return;
    let total = 0;
    let rank = position.nth + 1;
    for (const [page, n] of counts.current.perPage) {
      total += n;
      if (page < position.page) rank += n;
    }
    announce.current(`Match ${rank} of ${total}${searchingRef.current ? " so far" : ""}, page ${position.page}`);
  }, [position]);

  const step = useCallback((dir: 1 | -1) => {
    const c = counts.current;
    if (!c.q) return;
    const pages = [...c.perPage].filter(([, n]) => n > 0).sort((a, b) => a[0] - b[0]);
    if (pages.length === 0) return;
    setPosition((pos) => {
      if (!pos) return { page: pages[0][0], nth: 0 };
      const i = pages.findIndex(([p]) => p === pos.page);
      const here = i >= 0 ? pages[i][1] : 0;
      if (dir === 1) {
        if (i >= 0 && pos.nth + 1 < here) return { page: pos.page, nth: pos.nth + 1 };
        const after = pages.find(([p]) => p > pos.page) ?? pages[0];
        return { page: after[0], nth: 0 };
      }
      if (pos.nth > 0) return { page: pos.page, nth: pos.nth - 1 };
      const before = [...pages].reverse().find(([p]) => p < pos.page) ?? pages[pages.length - 1];
      return { page: before[0], nth: before[1] - 1 };
    });
  }, []);

  const next = useCallback(() => step(1), [step]);
  const prev = useCallback(() => step(-1), [step]);

  return {
    query: active,
    total: active ? summary.total : 0,
    rank: active ? summary.rank : 0,
    position,
    searching: searching && !!active,
    progress: stats.progress,
    failed: stats.failed,
    next,
    prev,
    warm,
  };
}
