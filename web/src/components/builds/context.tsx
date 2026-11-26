"use client";

import { createContext, useContext, useEffect, useId, useSyncExternalStore } from "react";
import type { BuildsData, Generations } from "@/lib/builds/view";

// What the pages read of the build tracking (BuildsProvider, in builds-provider.tsx, provides it).
// Pages and their components import this module, never the provider's: the root layout must be the
// provider's only importer. Next merges the client manifests of the root layout and of the pages in
// root route groups, such as (site)/page, so a module the layout shares with the home page would be
// loaded on every page with the home page's chunks.
//
// The builds reach components through a store, not as a context value. The provider's context
// value never changes: a changed one makes React render again every Suspense boundary below the root
// that has not hydrated yet, and one still streaming (the viewer's) is then rendered on the client
// beside the server's copy, which stays in the document until React reveals it.

export type Builds = {
  data: BuildsData | null;
  /** Epoch ms of the last poll, for "5 min ago". */
  now: number;
  /** A sentence when the last request failed, else null. */
  error: string | null;
  /** Slugs and job ids with a request in flight. */
  pending: ReadonlySet<string>;
  rebuild: (slug: string) => Promise<void>;
  /** Queue a job that applies the document's recorded decisions to its built model (kind "rerender"). */
  applyDecisions: (slug: string) => Promise<void>;
  cancel: (id: string) => Promise<void>;
  retry: (id: string) => Promise<void>;
  /** A job row that should take focus when it appears (after Cancel or Retry): a job id, or `end:<id>` for the row of that job once it has ended. */
  focusJob: string | null;
  clearFocusJob: () => void;
};

/** The builds of a page, null while they are off; BuildsProvider sets them. */
export class BuildsStore {
  private value: Builds | null = null;
  private readonly listeners = new Set<() => void>();
  get = (): Builds | null => this.value;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  set(value: Builds | null): void {
    if (value === this.value) return;
    this.value = value;
    for (const listener of this.listeners) listener();
  }
}

const off = () => null;

export const BuildsContext = createContext(new BuildsStore());
/** Records (or, with null, forgets) the generations one RenderedGenerations element shows. */
export const RenderedContext = createContext<(id: string, generations: Generations | null) => void>(() => {});

/** The builds of this page, or null when builds are off (and while hydrating: the server rendered
 *  the page with them off). */
export function useBuilds(): Builds | null {
  const store = useContext(BuildsContext);
  return useSyncExternalStore(store.subscribe, store.get, off);
}

/** Rendered by RenderedFrom with the publication generation each document's data on the page was
 *  read at. Renders nothing. */
export function RenderedGenerations({ generations }: { generations: Generations }) {
  const record = useContext(RenderedContext);
  const id = useId();
  const key = JSON.stringify(generations);
  useEffect(() => {
    record(id, JSON.parse(key));
    return () => record(id, null);
  }, [record, id, key]);
  return null;
}
