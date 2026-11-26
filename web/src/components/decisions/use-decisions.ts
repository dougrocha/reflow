"use client";

import { useEffect, useSyncExternalStore } from "react";
import { useBuilds } from "@/components/builds/context";
import { IDLE, publishedGeneration, storeFor, type DecisionsState } from "@/lib/decisions/client-store";
import type { Body, DecisionsAnswer, Sent } from "@/lib/decisions/client";
import type { DecisionView } from "@/lib/decisions/status";

type UseDecisions = {
  /** False while builds are off (and while hydrating): render no decision control. */
  enabled: boolean;
  state: DecisionsState;
  data: DecisionsAnswer | null;
  decisions: DecisionView[];
  pending: number;
  refresh: () => Promise<void>;
  post: (body: Body) => Promise<Sent>;
};

const NONE: DecisionView[] = [];

/** A document's decisions, shared by every component on the page that calls it. Nothing is read
 *  until the builds are on (`useBuilds()` is not null): with them off the routes do not exist. */
export function useDecisions(slug: string): UseDecisions {
  const builds = useBuilds();
  const enabled = builds !== null;
  const generation = publishedGeneration(builds?.data, slug);
  const store = storeFor(slug);
  const state = useSyncExternalStore(store.subscribe, store.get, () => IDLE);
  const idle = state.status === "idle";
  useEffect(() => {
    if (enabled && idle) void store.refresh();
  }, [enabled, idle, store]);
  // A build published this document: the same tab's list is read again, with no reload.
  useEffect(() => { store.noteGeneration(generation); }, [store, generation]);
  // Another tab or the command line may have written: read again when this tab comes back.
  useEffect(() => {
    if (!enabled) return;
    const onVisible = () => { if (document.visibilityState === "visible") void store.refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [enabled, store]);
  return {
    enabled, state, data: state.data, decisions: state.data?.decisions ?? NONE, pending: state.data?.pending ?? 0,
    refresh: store.refresh, post: store.post,
  };
}
