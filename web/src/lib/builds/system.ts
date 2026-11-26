import { SAMPLE_DOCS } from "../../docs";
import { variantFolders } from "../variants/built";
import { installedModels } from "../variants/ollama";
import { buildsEnabled } from "./gate";
import { buildsDir, dataRoot, docsRoot } from "../paths";
import { BuildStore } from "./store";
import { nodeSpawnBuild, Runner } from "./runner";
import type { Generations } from "./view";

// The server's one set of build objects, made from the environment on first use and kept on
// globalThis: the route handlers and instrumentation.ts are bundled separately (and dev reloads
// modules), and each would otherwise start a runner of its own.

export type Builds = {
  store: BuildStore;
  runner: Runner;
  /** Where the published files are (public/docs). */
  docsRoot: string;
  /** The documents a build can be started for. */
  slugs: () => string[];
  /** The Ollama models a build may be started with: the vision models installed (what the
   *  variants list offers). Asked only for a variant of an Ollama pipeline. */
  models: () => Promise<string[]>;
  /** Mark stale this server's cached reads (`doc:<slug>`) of every document published since it
   *  last asked, once per document however many publications there were. Needs a request, and Next
   *  applies the mark only after that request's handler has returned (and not at all if it throws),
   *  so call it last. Then the next request for the data is served the stale entry while Next reads
   *  it again. Returns what each document has published (its generation, when
   *  above 0): not a claim about this server's cache. A page knows what it shows from the
   *  generation rendered with its data (docs/builds.md, After a build).
   *
   *  `behind` is what pages that are still behind say they rendered (a page that has been behind for
   *  several poll answers says so with its poll): slug -> generation. Next compares a mark with the
   *  time a cache entry was SAVED, so an entry rendered from an older publication but saved after the
   *  mark for the newer one is fresh, and nothing but another mark replaces it. A document a page
   *  reports behind its published generation, whose mark this server asked for at least
   *  RENEW_AFTER_MS ago, is marked again. */
  invalidatePublished: (behind?: Generations) => Promise<Record<string, number>>;
};

/** How long after this server last marked a document before a report that a page is still behind it
 *  marks it again. Reports come with every poll of a stuck page (every second at first), and a mark
 *  makes the document's entries stale: this keeps a page that stays behind from marking continuously. */
export const RENEW_AFTER_MS = 3000;

const KEY = Symbol.for("reflow.builds");
type Holder = { [KEY]?: Builds };

/** Page builds at once: REFLOW_BUILDS_CONCURRENCY (1 to 8), else this. The builds share the machine's
 *  worker budget and GPU lease (src/reflow/budget.py), so more of them fill idle time (the serial
 *  steps of one conversion) rather than adding processes. */
export const DEFAULT_BUILDS_CONCURRENCY = 3;

export function concurrencyFrom(env: Record<string, string | undefined>): number {
  const n = Number(env.REFLOW_BUILDS_CONCURRENCY);
  return Number.isInteger(n) && n >= 1 && n <= 8 ? n : DEFAULT_BUILDS_CONCURRENCY;
}

// Stale-while-revalidate ("max", the profile Next recommends), not { expire: 0 }. With an expired
// entry, the first RSC request for a prerendered page (a page's router.refresh()) is answered 404
// with an empty body under Cache Components, and the browser then reloads the whole page: focus,
// scroll and what the reader had open are lost (docs/builds.md, Why the poll marks entries stale). A stale answer is
// safe: it carries its old generation, and the page asks again at its next poll.
async function revalidateDoc(slug: string): Promise<void> {
  const revalidateTag = await tagger();
  revalidateTag(`doc:${slug}`, "max");
}

type RevalidateTag = (tag: string, profile: string | { expire?: number }) => void;
let tagger = async (): Promise<RevalidateTag> => (await import("next/cache")).revalidateTag;

/** Tests: the function revalidateDoc calls in place of Next's (undefined puts Next's back). It lets a
 *  test see the arguments the production callback gives revalidateTag, which no stand-in for the
 *  callback can (profile-guard in system.test.ts). */
export function setRevalidateTagForTests(fn: RevalidateTag | undefined): void {
  tagger = fn ? async () => fn : async () => (await import("next/cache")).revalidateTag;
}

/** The objects around a store. `revalidate` asks for one document's cached reads to be marked stale
 *  (in Next, queued until the request's handler returns). Each Builds keeps what it has asked for in
 *  its own memory, as each server process has a cache of its own. */
export function makeBuilds(opts: Pick<Builds, "store" | "runner" | "docsRoot" | "slugs"> & {
  /** Defaults to no models. */
  models?: Builds["models"];
  revalidate: (slug: string) => Promise<void>;
  /** The clock for RENEW_AFTER_MS. */
  now?: () => number;
}): Builds {
  const clock = opts.now ?? Date.now;
  /** slug -> the generation this server has last asked to mark stale for. */
  const asked = new Map<string, number>();
  /** slug -> when this server last asked for a mark of it, for any reason. */
  const markedAt = new Map<string, number>();
  /** What pages have said they are behind since the last pass: slug -> the lowest generation. */
  const reported = new Map<string, number>();
  let passes: Promise<unknown> = Promise.resolve();
  let waiting: Promise<Record<string, number>> | undefined;
  // One pass at a time. Callers that arrive while a pass waits share it: it reads publications.json
  // after all of them asked, so each answer covers every publication completed before its request.
  const invalidate = async () => {
    const current = new Map(Object.entries(await opts.store.publications())
      // Generation 0 is a first publication still being written: nothing to mark yet.
      .flatMap(([slug, p]) => p.generation > 0 ? [[slug, p.generation] as const] : []));
    const changed = [...new Set([...asked.keys(), ...current.keys()])].filter((slug) => asked.get(slug) !== current.get(slug));
    // A page behind a generation whose mark was asked for long enough ago: that mark did not reach
    // what it rendered from (see invalidatePublished). A document marked in this pass is not marked twice.
    const reports = new Map(reported);
    reported.clear();
    const at = clock();
    const renewed = [...reports].filter(([slug, rendered]) =>
      !changed.includes(slug) && rendered < (current.get(slug) ?? 0) && at - (markedAt.get(slug) ?? -Infinity) >= RENEW_AFTER_MS,
    ).map(([slug]) => slug);
    for (const slug of [...changed, ...renewed]) await opts.revalidate(slug);
    // Remembered only once every mark was asked for: a pass that throws remembers none, so the
    // next poll asks again (and a page that is still behind says so again).
    for (const slug of changed) {
      const generation = current.get(slug);
      if (generation === undefined) asked.delete(slug);
      else asked.set(slug, generation);
    }
    for (const slug of [...changed, ...renewed]) markedAt.set(slug, at);
    return Object.fromEntries(current);
  };
  return {
    store: opts.store, runner: opts.runner, docsRoot: opts.docsRoot, slugs: opts.slugs,
    models: opts.models ?? (async () => []),
    invalidatePublished: (behind = {}) => {
      // Before joining or making a pass: one that has not started reads them when it does.
      for (const [slug, generation] of Object.entries(behind)) reported.set(slug, Math.min(reported.get(slug) ?? generation, generation));
      if (waiting) return waiting;
      const next = passes.then(() => {
        waiting = undefined;
        return invalidate();
      });
      waiting = next;
      passes = next.catch(() => {});
      return next;
    },
  };
}

export function getBuilds(): Builds {
  const holder = globalThis as Holder;
  if (!holder[KEY]) {
    const dir = buildsDir(dataRoot());
    const store = new BuildStore({ dir });
    const runner = new Runner(store, {
      concurrency: concurrencyFrom(process.env),
      spawn: nodeSpawnBuild({ webDir: process.cwd(), buildsDir: dir }),
    });
    holder[KEY] = makeBuilds({
      store, runner, docsRoot: docsRoot(),
      slugs: () => [...SAMPLE_DOCS.map((d) => d.slug), ...variantFolders(docsRoot())],
      models: installedModels,
      revalidate: revalidateDoc,
    });
  }
  return holder[KEY];
}

/** When the server starts (instrumentation.ts): with builds on, start the runner so jobs queued
 *  before a restart run without anyone opening a page. Not awaited: a slow build lock must not
 *  hold up the server's start or anything else started there. */
export function startBuildsRunner(): void {
  if (!buildsEnabled()) return;
  getBuilds().runner.start().catch((error) => console.error("The build runner's first pass failed; it retries every second.", error));
}

/** Tests: use these objects, or (with undefined) make them again from the environment. */
export function setBuildsForTests(builds: Builds | undefined): void {
  (globalThis as Holder)[KEY] = builds;
}
