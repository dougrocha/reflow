"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useTransition, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { fetchJson } from "@/lib/fetch-json";
import { announcements, Freshness, lowest, pollPath, type BuildsData, type Generations } from "@/lib/builds/view";
import { BuildsContext, BuildsStore, RenderedContext, type Builds } from "./context";

// Build tracking for every page (docs/builds.md). The root layout renders this around its one
// content tree, and the page is the same static page whether builds are on or off. After hydration
// it asks the server once whether builds are on (GET /api/builds/enabled). Off, that is all it does:
// no context, no polling, no live region. On, it polls GET /api/builds (every second while
// something is queued or running or the page has just fallen behind, every 5 s otherwise, not at
// all while the tab is hidden), offers rebuild, cancel and retry, and refreshes the page while a
// document it shows was read at another publication than the published one (Freshness, in
// @/lib/builds/view). Its live region, named "Build status", says when a build changes state, not
// on every tick; the viewer has a status region of its own. Only the root layout imports this
// module; pages read it through ./context.

const FAST_MS = 1000;
const IDLE_MS = 5000;

async function post(path: string, body?: unknown): Promise<{ job?: { id: string } }> {
  const res = await fetchJson(path, { body });
  if (!res.ok) throw new Error(typeof res.body.error === "string" ? res.body.error : `The server answered ${res.status}.`);
  return res.body;
}

export function BuildsProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [on, setOn] = useState(false);
  const [refreshing, startRefresh] = useTransition();
  const [data, setData] = useState<BuildsData | null>(null);
  const [now, setNow] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [focusJob, setFocusJob] = useState<string | null>(null);
  const [store] = useState(() => new BuildsStore());
  const seen = useRef(new Map<string, string>());
  const first = useRef(true);
  const active = useRef(0);
  /** What each RenderedGenerations element on the page shows. */
  const rendered = useRef(new Map<string, Generations>());
  /** While builds are on: when to refresh. */
  const freshness = useRef<Freshness | null>(null);
  const isRefreshing = useRef(false);
  const path = useRef(pathname);
  const focusedBefore = useRef<Element | null>(null);
  const mounted = useRef(true);
  const wake = useRef<() => void>(() => {});
  /** Brings the next poll forward to the quick pace when the page turns out to be behind. */
  const quicken = useRef<() => void>(() => {});

  // Asked once, after hydration. A probe that fails leaves builds off for this page load. The
  // embeddable viewer (/embed/<id>, which other sites frame) never asks, so it makes no request to
  // the builds API at all (docs/embed.md).
  useEffect(() => {
    if (path.current.startsWith("/embed/")) return;
    const controller = new AbortController();
    fetch("/api/builds/enabled", { cache: "no-store", signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => {
        if (body?.enabled === true) setOn(true);
      })
      .catch(() => {});
    return () => controller.abort();
  }, []);

  const refreshIf = useCallback((now: boolean) => {
    if (!now) return;
    focusedBefore.current = document.activeElement;
    startRefresh(() => router.refresh());
  }, [router]);

  const record = useCallback((id: string, generations: Generations | null) => {
    if (generations) rendered.current.set(id, generations);
    else rendered.current.delete(id);
    if (freshness.current) {
      refreshIf(freshness.current.shows(lowest([...rendered.current.values()])));
      quicken.current();
    }
  }, [refreshIf]);

  // After the refreshed page is in: if the element that had focus was removed (the Build button of
  // a notice that is gone), keep focus on the page, not on nothing. Otherwise focus is left alone.
  useEffect(() => {
    isRefreshing.current = refreshing;
    if (refreshing) return;
    if (freshness.current) refreshIf(freshness.current.landed());
    const before = focusedBefore.current;
    focusedBefore.current = null;
    if (!before || before === document.body || before.isConnected) return;
    if (document.activeElement === document.body || document.activeElement === null) {
      document.getElementById("main")?.focus({ preventScroll: true });
    }
  }, [refreshing, refreshIf]);

  const poll = useCallback(async (signal: AbortSignal) => {
    try {
      // A page that has stayed behind says which documents it rendered, so the server marks them again.
      const res = await fetch(pollPath(freshness.current?.report() ?? {}), { cache: "no-store", signal });
      if (res.status === 404) {
        // The server has builds off now (it restarted without them): stop asking.
        if (!signal.aborted && mounted.current) setOn(false);
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      const next: BuildsData = await res.json();
      if (signal.aborted || !mounted.current) return;
      const said = announcements(seen.current, next, first.current);
      first.current = false;
      active.current = next.active.length;
      setData(next);
      setNow(Date.now());
      setError(null);
      if (said.length) setMessage(said.slice(0, 3).join(" "));
      const fresh = freshness.current;
      if (fresh) {
        // A refresh whose transition never reported its end must not hold off every later one.
        if (!isRefreshing.current) fresh.landed();
        refreshIf(fresh.polled(next.published));
      }
    } catch {
      if (signal.aborted || !mounted.current) return;
      setError("Could not reach the server to check the builds.");
    }
  }, [refreshIf]);

  useEffect(() => {
    if (!on) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let due = 0;
    const schedule = (ms: number) => {
      clearTimeout(timer);
      due = Date.now() + ms;
      timer = setTimeout(tick, ms);
    };
    let stopped = false;
    mounted.current = true;
    seen.current = new Map();
    first.current = true;
    const fresh = new Freshness();
    fresh.shows(lowest([...rendered.current.values()]));
    freshness.current = fresh;
    let controller: AbortController | undefined;
    const tick = async () => {
      clearTimeout(timer);
      controller?.abort();
      controller = new AbortController();
      if (!document.hidden) await poll(controller.signal);
      if (!stopped) schedule(active.current > 0 || fresh.hurry ? FAST_MS : IDLE_MS);
    };
    // The page's generations arrive after the first poll's answer when it streams in (the first poll
    // is made at once, the document's data hydrates a few hundred ms later): the pace chosen then was
    // the idle one. Once the page is known to be behind, the next poll is no more than a quick one away.
    quicken.current = () => {
      if (!stopped && fresh.hurry && due - Date.now() > FAST_MS) schedule(FAST_MS);
    };
    wake.current = () => void tick();
    const onVisible = () => {
      if (!document.hidden) void tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    void tick();
    return () => {
      stopped = true;
      mounted.current = false;
      freshness.current = null;
      controller?.abort();
      clearTimeout(timer);
      wake.current = () => {};
      quicken.current = () => {};
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [on, poll]);

  // A client navigation can show a page from the router's cache (a prefetch, or back and forward),
  // read before a publication. The new page's RenderedGenerations have been recorded by now (child
  // effects run first); arriving gives one chance to refresh if they are behind.
  useEffect(() => {
    if (path.current === pathname) return;
    path.current = pathname;
    if (freshness.current) refreshIf(freshness.current.arrived());
  }, [pathname, refreshIf]);

  const run = useCallback(async (key: string, action: () => Promise<{ job?: { id: string } }>, focus: "result" | "same" | "none") => {
    setPending((p) => new Set(p).add(key));
    try {
      const result = await action();
      if (focus === "result" && result.job) setFocusJob(result.job.id);
      if (focus === "same") setFocusJob(`end:${key}`); // when the job has ended and moved to Recent
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "The request failed.");
    } finally {
      setPending((p) => {
        const next = new Set(p);
        next.delete(key);
        return next;
      });
      wake.current();
    }
  }, []);

  const value = useMemo<Builds | null>(() => on ? {
    data, now, error, pending, focusJob,
    clearFocusJob: () => setFocusJob(null),
    rebuild: (slug) => run(slug, () => post("/api/builds", { slug }), "none"),
    applyDecisions: (slug) => run(slug, () => post("/api/builds", { slug, kind: "rerender" }), "none"),
    cancel: (id) => run(id, () => post(`/api/builds/${id}/cancel`), "same"),
    retry: (id) => run(id, () => post(`/api/builds/${id}/retry`), "result"),
  } : null, [on, data, now, error, pending, focusJob, run]);
  useEffect(() => store.set(value), [store, value]);

  return (
    <RenderedContext.Provider value={record}>
      <BuildsContext.Provider value={store}>
        {children}
        {on ? (
          <div role="status" aria-live="polite" aria-atomic="true" aria-label="Build status" className="sr-only">
            {message}
          </div>
        ) : null}
      </BuildsContext.Provider>
    </RenderedContext.Provider>
  );
}
