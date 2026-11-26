import { fetchJson } from "../fetch-json";
import { send, type Body, type DecisionsAnswer, type Sent } from "./client";

/** A document's published generation as the builds poll reports it: undefined until the poll has
 *  answered, then 0 for a document no build has published here yet (a seeded copy), so its first
 *  build is a change. */
export function publishedGeneration(data: { published?: Record<string, number> } | null | undefined,
                                    slug: string): number | undefined {
  return data ? (data.published?.[slug] ?? 0) : undefined;
}

// One shared copy of a document's decisions for every component on the page (the panel, the
// decided node, the issue list, the decisions list). `useDecisions(slug)` subscribes to it. A
// plain class so it can be tested without React.

export type DecisionsState =
  | { status: "idle"; data: null; error: null }
  | { status: "loading"; data: DecisionsAnswer | null; error: null }
  | { status: "ready"; data: DecisionsAnswer; error: null }
  | { status: "error"; data: DecisionsAnswer | null; error: string };

export const IDLE: DecisionsState = { status: "idle", data: null, error: null };

export class DecisionsStore {
  private state: DecisionsState = IDLE;
  private readonly listeners = new Set<() => void>();
  private inflight: Promise<void> | null = null;

  constructor(readonly slug: string, private readonly doFetch: typeof fetch = ((input, init) => fetch(input, init)) as typeof fetch) {}

  get = (): DecisionsState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  private set(state: DecisionsState): void {
    this.state = state;
    for (const l of this.listeners) l();
  }

  private generation: number | undefined;
  /** The document's published generation, as the builds poll reports it. A change means a build
   *  published: the list (applied, undone) is read again. The first sighting only records it. */
  noteGeneration = (generation: number | undefined): void => {
    if (generation === undefined) return;
    const was = this.generation;
    this.generation = generation;
    if (was !== undefined && was !== generation && this.state.status !== "idle") void this.refresh();
  };

  /** Read the list again. Concurrent calls share one request. */
  refresh = (): Promise<void> => {
    if (this.inflight) return this.inflight;
    this.set({ status: "loading", data: this.state.data, error: null });
    this.inflight = (async () => {
      try {
        const { ok, status, body } = await fetchJson(`/api/decisions/${this.slug}`, undefined, this.doFetch);
        if (ok) this.set({ status: "ready", data: body as unknown as DecisionsAnswer, error: null });
        else this.set({ status: "error", data: this.state.data, error: typeof body.error === "string" ? body.error : `The decisions could not be read (${status}).` });
      } catch {
        this.set({ status: "error", data: this.state.data, error: "The decisions could not be read." });
      } finally {
        this.inflight = null;
      }
    })();
    return this.inflight;
  };

  /** Send one choice; on success the list is read again before this resolves. */
  post = async (body: Body): Promise<Sent> => {
    const sent = await send(this.slug, body, this.doFetch);
    if (sent.ok) await this.refresh();
    return sent;
  };
}

const stores = new Map<string, DecisionsStore>();
export function storeFor(slug: string): DecisionsStore {
  let s = stores.get(slug);
  if (!s) stores.set(slug, (s = new DecisionsStore(slug)));
  return s;
}
