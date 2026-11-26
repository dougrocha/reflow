import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { join } from "node:path";
import { GET, POST } from "@/app/api/builds/route";
import { GET as ENABLED } from "@/app/api/builds/enabled/route";
import { POST as CANCEL } from "@/app/api/builds/[id]/cancel/route";
import { POST as RETRY } from "@/app/api/builds/[id]/retry/route";
import { buildsEnabled } from "./gate";
import { crossSite } from "../http";
import { Runner } from "./runner";
import { BuildStore } from "./store";
import { makeBuilds, RENEW_AFTER_MS, setBuildsForTests, type Builds } from "./system";
import { FakeProcs, fakeSpawn, removeDir, tempDir } from "./testkit";
import { Freshness, pollPath } from "./view";

let dir: string;
let procs: FakeProcs;
let fake: ReturnType<typeof fakeSpawn>;
let store: BuildStore;
let revalidated: string[];
let installed: string[];
let modelAsks: number;
const SAVED = { ...process.env };

beforeEach(async () => {
  dir = await tempDir();
  procs = new FakeProcs();
  fake = fakeSpawn(procs);
  store = new BuildStore({ dir, procs });
  revalidated = [];
  installed = [];
  modelAsks = 0;
  setBuildsForTests(server());
  process.env.REFLOW_BUILDS = "1";
});
afterEach(async () => {
  setBuildsForTests(undefined);
  process.env = { ...SAVED };
  await removeDir(dir);
});

/** One server process's objects over the shared store: its own runner and its own cache. */
const server = (record: string[] = revalidated) => makeBuilds({
  store, runner: new Runner(store, { concurrency: 1, spawn: fake.spawn }),
  docsRoot: join(dir, "docs"), slugs: () => ["fw9", "resnet", "p17"],
  revalidate: async (slug) => void record.push(slug),
  models: async () => { modelAsks++; return installed; },
});

const URL_ = "http://localhost:3447/api/builds";
const post = (body: unknown, headers: Record<string, string> = {}, raw?: string) =>
  POST(new Request(URL_, {
    method: "POST", headers: { "content-type": "application/json", host: "localhost:3447", origin: "http://localhost:3447", ...headers },
    body: raw ?? JSON.stringify(body),
  }));
const get = (headers: Record<string, string> = {}) => GET(new Request(URL_, { headers: { host: "localhost:3447", ...headers } }));
const idCall = (fn: (req: Request, ctx: never) => Promise<Response>, action: string, id: string, headers: Record<string, string> = {}) =>
  fn(new Request(`${URL_}/${id}/${action}`, { method: "POST", headers: { host: "localhost:3447", origin: "http://localhost:3447", ...headers } }),
    { params: Promise.resolve({ id }) } as never);

describe("the gate", () => {
  test("on for development and for REFLOW_BUILDS=1 only", () => {
    expect(buildsEnabled({ NODE_ENV: "development" })).toBe(true);
    expect(buildsEnabled({ NODE_ENV: "production", REFLOW_BUILDS: "1" })).toBe(true);
    expect(buildsEnabled({ NODE_ENV: "production" })).toBe(false);
    expect(buildsEnabled({ NODE_ENV: "production", REFLOW_BUILDS: "true" })).toBe(false);
    expect(buildsEnabled({ NODE_ENV: "production", REFLOW_BUILDS: "0" })).toBe(false);
    expect(buildsEnabled({})).toBe(false);
  });

  test("off: every route is an empty 404 and nothing is queued or spawned", async () => {
    delete process.env.REFLOW_BUILDS;
    const { job } = await store.enqueue("fw9", "ui");
    const responses = [
      await get(), await post({ slug: "fw9" }),
      await idCall(CANCEL, "cancel", job.id), await idCall(RETRY, "retry", job.id),
    ];
    for (const r of responses) {
      expect(r.status).toBe(404);
      expect(await r.text()).toBe("");
    }
    expect(fake.children).toHaveLength(0);
    expect((await store.get(job.id))?.state).toBe("queued");
  });

  test("the probe answers 200 either way, says only whether builds are on, and starts nothing", async () => {
    let made = 0;
    setBuildsForTests(new Proxy({} as Builds, { get: () => { made++; return undefined; } }));
    delete process.env.REFLOW_BUILDS;
    const off = await ENABLED();
    expect(off.status).toBe(200);
    expect(off.headers.get("cache-control")).toBe("no-store");
    expect(await off.json()).toEqual({ enabled: false });
    process.env.REFLOW_BUILDS = "1";
    expect(await (await ENABLED()).json()).toEqual({ enabled: true });
    expect(made).toBe(0); // never touched the store, the runner or the record
    expect(fake.children).toHaveLength(0);
  });

  test("on in development without the variable", async () => {
    delete process.env.REFLOW_BUILDS;
    (process.env as Record<string, string>).NODE_ENV = "development";
    expect((await get()).status).toBe(200);
  });
});

describe("request checks", () => {
  test("cross-site and same-site POSTs are refused before anything happens", async () => {
    for (const site of ["cross-site", "same-site", "none"]) {
      const r = await post({ slug: "fw9" }, { "sec-fetch-site": site });
      expect(r.status).toBe(403);
    }
    expect(await store.snapshot()).toEqual({ active: [], recent: [] });
  });

  test("same-origin is allowed; a GET from the address bar (none) too", async () => {
    expect((await post({ slug: "fw9" }, { "sec-fetch-site": "same-origin" })).status).toBe(202);
    expect((await get({ "sec-fetch-site": "none" })).status).toBe(200);
    expect((await get({ "sec-fetch-site": "cross-site" })).status).toBe(403);
  });

  test("an Origin for another host is refused, the same host is allowed", async () => {
    expect((await post({ slug: "fw9" }, { origin: "http://evil.example" })).status).toBe(403);
    expect((await post({ slug: "fw9" }, { origin: "http://localhost:3447" })).status).toBe(202);
    expect(crossSite(new Request("http://a/x", { headers: { origin: "not a url" } }))).not.toBeNull();
  });

  test("a request to a name that is not loopback is refused, a DNS-rebinding page's too", async () => {
    const rebound = { host: "evil.example:3447", origin: "http://evil.example:3447" };
    expect((await post({ slug: "fw9" }, rebound)).status).toBe(403);
    expect((await get(rebound)).status).toBe(403);
    const { job } = await store.enqueue("p17", "ui");
    await store.finish(job.id, "failed", "boom");
    expect((await idCall(CANCEL, "cancel", job.id, rebound)).status).toBe(403);
    expect((await idCall(RETRY, "retry", job.id, rebound)).status).toBe(403);
    expect(await GET(new Request(URL_))).toHaveProperty("status", 403); // no Host at all
    expect(fake.children).toHaveLength(0);
    for (const host of ["localhost:3447", "127.0.0.1:3447", "[::1]:3447"]) {
      expect((await get({ host })).status).toBe(200);
    }
  });

  test("a write with no Origin is refused: a browser always sends one", async () => {
    const bare = new Request(URL_, { method: "POST", headers: { "content-type": "application/json", host: "localhost:3447" }, body: JSON.stringify({ slug: "fw9" }) });
    expect((await POST(bare)).status).toBe(403);
    expect(await store.snapshot()).toEqual({ active: [], recent: [] });
  });

  test("the body must be JSON, an object, small, and hold a slug string", async () => {
    expect((await post(null, { "content-type": "text/plain" })).status).toBe(400);
    expect((await post(null, {}, "{nope")).status).toBe(400);
    expect((await post([])).status).toBe(400);
    expect((await post({})).status).toBe(400);
    expect((await post({ slug: 5 })).status).toBe(400);
    expect(await store.snapshot()).toEqual({ active: [], recent: [] });
  });

  test("a body over the 1,000-byte cap is 413, by its length or by its Content-Length", async () => {
    const big = await post({ slug: "fw9", pad: "x".repeat(2_000) });
    expect(big.status).toBe(413);
    expect(await big.text()).toContain("too big");
    expect((await post({ slug: "fw9" }, { "content-length": "999999" })).status).toBe(413);
    expect(await store.snapshot()).toEqual({ active: [], recent: [] });
  });

  test("a slug that is not a known document is refused: no path or argument reaches the child", async () => {
    for (const slug of ["nope", "../fw9", "fw9 --out /tmp", "fw9/../x", "FW9", "", "-x", "fw9\n"]) {
      expect((await post({ slug })).status).toBe(400);
    }
    expect(fake.children).toHaveLength(0);
    expect(await store.snapshot()).toEqual({ active: [], recent: [] });
  });

  test("a variant the variants list would not offer is refused: only fixed pipelines and installed Ollama models", async () => {
    installed = ["gemma4:e4b"];
    for (const slug of ["fw9~nope", "fw9~ollama-llama3_2", "fw9~ollama-gemma4", "fw9~ollama-x", "nope~haiku", "fw9~default", "fw9~"]) {
      expect((await post({ slug })).status).toBe(400);
    }
    expect(fake.children).toHaveLength(0);
    expect(await store.snapshot()).toEqual({ active: [], recent: [] });
    expect((await post({ slug: "fw9~ollama-gemma4_e4b" })).status).toBe(202);
    expect((await post({ slug: "fw9~haiku" })).status).toBe(202);
  });

  test("a fixed pipeline's key is not checked against Ollama: no request to it per enqueue", async () => {
    await post({ slug: "fw9~haiku" });
    await post({ slug: "fw9" });
    expect(modelAsks).toBe(0);
  });

  test("responses are not cached", async () => {
    expect((await get()).headers.get("cache-control")).toBe("no-store");
    expect((await post({ slug: "fw9" })).headers.get("cache-control")).toBe("no-store");
  });
});

describe("the routes", () => {
  test("POST enqueues and starts: 202 with the job; the same slug again is 200 and the same job", async () => {
    const first = await post({ slug: "fw9" });
    expect(first.status).toBe(202);
    const a = await first.json();
    expect(a.created).toBe(true);
    expect(a.job).toMatchObject({ slug: "fw9", who: "ui" });
    const second = await post({ slug: "fw9" });
    expect(second.status).toBe(200);
    expect(await second.json()).toMatchObject({ created: false, job: { id: a.job.id } });
    expect(fake.children).toHaveLength(1);
  });

  test("POST {kind: \"rerender\"} makes a rerender job; any other kind is 400 and makes none", async () => {
    for (const kind of ["build", "rebuild", "", null, 1]) {
      expect((await post({ slug: "fw9", kind })).status).toBe(400);
    }
    expect((await store.snapshot()).active).toEqual([]);
    const r = await post({ slug: "fw9", kind: "rerender" });
    expect(r.status).toBe(202);
    expect((await r.json()).job).toMatchObject({ slug: "fw9", kind: "rerender" });
    expect(fake.children[0].job.kind).toBe("rerender");
    expect((await post({ slug: "resnet" })).status).toBe(202);
    expect((await store.snapshot()).active.map((j) => j.kind)).toEqual(["rerender", "build"]);
  });

  test("a rerender asked for while the build is queued returns the build; a build while a rerender is queued makes it a build", async () => {
    await post({ slug: "fw9" }); // takes the one slot
    const queuedBuild = await (await post({ slug: "resnet" })).json();
    expect(await (await post({ slug: "resnet", kind: "rerender" })).json()).toMatchObject({ created: false, job: { id: queuedBuild.job.id, kind: "build" } });
    const queuedRerender = await (await post({ slug: "p17", kind: "rerender" })).json();
    const turned = await (await post({ slug: "p17" })).json();
    expect(turned).toMatchObject({ created: false, job: { id: queuedRerender.job.id, kind: "build" } });
  });

  test("retry keeps the kind of the job it retries", async () => {
    const { job } = await (await post({ slug: "fw9", kind: "rerender" })).json();
    fake.children[0].exit(1);
    await store.finish(job.id, "failed", "boom");
    const again = await idCall(RETRY, "retry", job.id);
    expect(again.status).toBe(202);
    expect((await again.json()).job).toMatchObject({ slug: "fw9", kind: "rerender" });
  });

  test("GET lists the queue in order, the history, the concurrency and the built times", async () => {
    await post({ slug: "fw9" });
    await post({ slug: "resnet" });
    const done = await store.enqueue("p17", "cli");
    await store.finish(done.job.id, "failed", "boom");
    const body = await (await get()).json();
    expect(body.concurrency).toBe(1);
    expect(body.active.map((j: { slug: string; state: string }) => [j.slug, j.state])).toEqual([["fw9", "running"], ["resnet", "queued"]]);
    expect(body.recent.map((j: { slug: string }) => j.slug)).toEqual(["p17"]);
    expect(body.built).toEqual({ fw9: null, resnet: null, p17: null });
  });

  test("GET settles a job whose process died", async () => {
    const { job } = await (await post({ slug: "fw9" })).json();
    procs.alive.delete(1000);
    const body = await (await get()).json();
    expect(body.active).toEqual([]);
    expect(body.recent[0]).toMatchObject({ id: job.id, state: "interrupted" });
  });

  test("a publication has its document's cached data expired once, whatever the job's state", async () => {
    const ref = procs.add(77);
    const { job } = await store.enqueue("fw9", "cli", { state: "running", pid: ref.pid, pidStart: ref.start });
    expect((await (await get()).json()).published).toEqual({});
    // Published, still running (the audit is next): expired at the next look, before `done`.
    await store.publish(job.id, async () => {});
    const body = await (await get()).json();
    expect(body.published).toEqual({ fw9: 1 });
    expect(body.active.map((j: { state: string }) => j.state)).toEqual(["running"]);
    expect(revalidated).toEqual(["fw9"]);
    await get();
    expect(revalidated).toEqual(["fw9"]);
    // A cancelled build that had published (P2-C): its output is still expired.
    await store.publish(job.id, async () => {});
    await store.finish(job.id, "cancelled");
    expect((await (await get()).json()).published).toEqual({ fw9: 2 });
    expect(revalidated).toEqual(["fw9", "fw9"]);
    // A failed job that never published expires nothing.
    const failed = await store.enqueue("p17", "cli");
    await store.finish(failed.job.id, "failed", "x");
    await get();
    expect(revalidated).toEqual(["fw9", "fw9"]);
  });

  test("a publication that fails halfway still counts, and the error is not swallowed", async () => {
    const { job } = await store.enqueue("fw9", "cli", { state: "running" });
    await expect(store.publish(job.id, async () => { throw new Error("disk full"); })).rejects.toThrow("disk full");
    expect((await store.publications()).fw9).toMatchObject({ generation: 1, publishing: null });
  });

  test("a publisher that died mid-write is recorded by recovery, then expired", async () => {
    const ref = procs.add(77);
    const { job } = await store.enqueue("fw9", "cli", { state: "running", pid: ref.pid, pidStart: ref.start });
    let entered!: () => void;
    const inside = new Promise<void>((r) => (entered = r));
    void store.publish(job.id, () => { entered(); return new Promise(() => {}); }); // never returns: the process "dies"
    await inside;
    expect((await store.publications()).fw9).toMatchObject({ generation: 0, publishing: job.id });
    // A first publication still being written is nothing to expire, and nothing to settle.
    expect((await (await get()).json()).published).toEqual({});
    expect(revalidated).toEqual([]);
    expect(await store.needsTransaction(1)).toBe(false);
    await store.recover();
    expect((await store.publications()).fw9?.generation).toBe(0); // its process is alive: not yet
    procs.alive.delete(77);
    expect(await store.needsTransaction(1)).toBe(true);
    await store.recover();
    expect((await store.publications()).fw9).toMatchObject({ generation: 1, publishing: null });
    expect((await store.get(job.id))?.state).toBe("interrupted");
    expect((await (await get()).json()).published).toEqual({ fw9: 1 });
    expect(revalidated).toEqual(["fw9"]);
    expect(await store.needsTransaction(1)).toBe(false);
  });
  test("cancel: a queued job ends, a running one has its process stopped, a finished one is 409", async () => {
    await post({ slug: "fw9" });
    const { job: queued } = await (await post({ slug: "resnet" })).json();
    const r = await idCall(CANCEL, "cancel", queued.id);
    expect(r.status).toBe(200);
    expect((await r.json()).job.state).toBe("cancelled");
    expect(procs.killed).toEqual([]);

    const running = (await store.snapshot()).active[0];
    const r2 = await idCall(CANCEL, "cancel", running.id);
    expect(r2.status).toBe(200);
    expect(procs.killed.map((k) => k.pid)).toEqual([running.pid!]);
    expect((await idCall(CANCEL, "cancel", running.id)).status).toBe(409);
    expect((await idCall(CANCEL, "cancel", "0000000aa-deadbeef")).status).toBe(404);
    expect((await idCall(CANCEL, "cancel", "../../etc")).status).toBe(404);
  });

  test("retry: only failed, interrupted or cancelled jobs; it makes a new job for the same slug", async () => {
    const { job } = await (await post({ slug: "fw9" })).json();
    expect((await idCall(RETRY, "retry", job.id)).status).toBe(409); // still running
    await idCall(CANCEL, "cancel", job.id);
    const retried = await idCall(RETRY, "retry", job.id);
    expect(retried.status).toBe(202);
    const body = await retried.json();
    expect(body.job.id).not.toBe(job.id);
    expect(body.job.slug).toBe("fw9");
    // while it is queued or running, retrying the old job again is a no-op
    const again = await idCall(RETRY, "retry", job.id);
    expect(again.status).toBe(200);
    expect((await again.json()).job.id).toBe(body.job.id);

    const done = await store.enqueue("p17", "cli");
    await store.finish(done.job.id, "done");
    expect((await idCall(RETRY, "retry", done.job.id)).status).toBe(409);
    expect((await idCall(RETRY, "retry", "0000000aa-deadbeef")).status).toBe(404);
  });
});

test("many publications cost one expiry per document, and history stays bounded", async () => {
  const small = new BuildStore({ dir, procs, historyLimit: 5 });
  for (let i = 0; i < 40; i++) {
    const { job } = await small.enqueue(i % 2 ? "fw9" : "resnet", "cli", { state: "running" });
    await small.publish(job.id, async () => {});
    await small.finish(job.id, i % 3 ? "done" : "cancelled");
  }
  await small.enqueue("p17", "cli"); // enqueue prunes
  expect((await small.snapshot(100)).recent.length).toBeLessThanOrEqual(5);
  const body = await (await get()).json();
  expect(body.published).toEqual({ resnet: 20, fw9: 20 });
  expect(revalidated.sort()).toEqual(["fw9", "resnet"]);
  await get();
  expect(revalidated).toHaveLength(2);
}, 30_000);

test("each server expires its own cache: a second server or a restarted one expires every publication once", async () => {
  const { job } = await store.enqueue("fw9", "cli", { state: "running" });
  await store.publish(job.id, async () => {});
  await get();
  expect(revalidated).toEqual(["fw9"]);
  const other: string[] = [];
  setBuildsForTests(server(other)); // another process over the same data root (or this one, restarted)
  expect((await (await get()).json()).published).toEqual({ fw9: 1 });
  expect(other).toEqual(["fw9"]);
  await get();
  expect(other).toEqual(["fw9"]);
});

test("concurrent polls share one expiry pass", async () => {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  const calls: string[] = [];
  setBuildsForTests(makeBuilds({
    store, runner: new Runner(store, { concurrency: 1, spawn: fake.spawn }),
    docsRoot: join(dir, "docs"), slugs: () => ["fw9", "resnet", "p17"],
    revalidate: async (slug) => { calls.push(slug); await gate; },
  }));
  const { job } = await store.enqueue("fw9", "cli", { state: "running" });
  await store.publish(job.id, async () => {});
  const polls = [get(), get(), get()].map((p) => p.then(async (r) => (await r.json()).published));
  await new Promise((r) => setTimeout(r, 100));
  expect(calls).toEqual(["fw9"]);
  release();
  expect(await Promise.all(polls)).toEqual([{ fw9: 1 }, { fw9: 1 }, { fw9: 1 }]);
  expect(calls).toEqual(["fw9"]);
});

test("an expiry is asked for last, and remembered only when every one was asked for: a pass that fails is redone", async () => {
  const order: string[] = [];
  let failOn: string | null = "resnet";
  setBuildsForTests(makeBuilds({
    store, runner: new Runner(store, { concurrency: 1, spawn: fake.spawn }),
    docsRoot: join(dir, "docs"), slugs: () => ["fw9", "resnet", "p17"],
    revalidate: async (slug) => {
      order.push(`expire ${slug}`);
      if (slug === failOn) throw new Error("the cache would not take it");
    },
  }));
  const snapshot = store.snapshot.bind(store);
  store.snapshot = (...args) => { order.push("snapshot"); return snapshot(...args); };
  for (const slug of ["fw9", "resnet"]) {
    const { job } = await store.enqueue(slug, "cli", { state: "running" });
    await store.publish(job.id, async () => {});
    await store.finish(job.id, "done");
  }
  // In Next the handler throws, and every expiry it asked for is dropped: none may be remembered.
  await expect(get()).rejects.toThrow("would not take it");
  expect(order).toEqual(["snapshot", "expire fw9", "expire resnet"]);
  failOn = null;
  order.length = 0;
  expect((await (await get()).json()).published).toEqual({ fw9: 1, resnet: 1 });
  expect(order).toEqual(["snapshot", "expire fw9", "expire resnet"]); // both asked again
  order.length = 0;
  await get();
  expect(order).toEqual(["snapshot"]);
});

// Next applies a route handler's revalidateTag only after the handler has returned
// (finalizeResponse, then pendingWaitUntil): the answer can reach a page before the tag is marked
// stale. Then, with the "max" profile, the first request for the data is served the stale entry while
// it is read again in the background. This stands in for Next's tag cache: an entry per document,
// made as readDocument makes it (the generation read first, then the files), marks queued until the
// request is over, and stale-while-revalidate. A page is what it rendered: the generation and the
// content of the entry it was served.
test("a page rendered before the mark is applied, or from the stale entry, shows the old content with its old generation, and heals", async () => {
  const disk = { content: "A" };
  const entries = new Map<string, { generation: number; content: string; stale: boolean }>();
  const queued: string[] = [];
  let regenerating: Promise<void> = Promise.resolve();
  setBuildsForTests(makeBuilds({
    store, runner: new Runner(store, { concurrency: 1, spawn: fake.spawn }),
    docsRoot: join(dir, "docs"), slugs: () => ["fw9", "resnet", "p17"],
    revalidate: async (slug) => void queued.push(slug),
  }));
  const read = async () => {
    const generation = (await store.publications()).fw9?.generation ?? 0;
    const entry = { generation, content: disk.content, stale: false };
    entries.set("fw9", entry);
    return entry;
  };
  const render = async () => {
    await regenerating; // a regeneration started by an earlier request has finished by now
    const entry = entries.get("fw9");
    if (!entry) return read();
    if (entry.stale) regenerating = read().then(() => {});
    return entry;
  };
  const applyQueued = () => {
    for (const slug of queued.splice(0)) {
      const entry = entries.get(slug);
      if (entry) entry.stale = true;
    }
  };
  const seen: { generation: number; content: string }[] = [];
  const page = async () => {
    const { generation, content } = await render();
    seen.push({ generation, content });
    return { generation, content };
  };
  const publishAs = async (content: string) => {
    const { job } = await store.enqueue("fw9", "cli", { state: "running" });
    await store.publish(job.id, async () => void (disk.content = content));
    await store.finish(job.id, "done");
  };

  await publishAs("A");
  await (await get()).json();
  applyQueued();
  const f = new Freshness();
  f.shows({ fw9: (await page()).generation });
  expect(f.polled((await (await get()).json()).published)).toBe(false); // current: no refresh

  await publishAs("B");
  const answer = await (await get()).json(); // the handler has returned; its mark is still queued
  expect(answer.published).toEqual({ fw9: 2 });
  expect(queued).toEqual(["fw9"]);
  // A new tab loads now: the old entry, and it says so.
  const loaded = await page();
  expect(loaded).toEqual({ generation: 1, content: "A" });
  const tab = new Freshness();
  tab.shows({ fw9: loaded.generation });
  expect(tab.polled(answer.published)).toBe(true); // its own first poll: behind, refresh
  tab.shows({ fw9: (await page()).generation }); // the refresh is still too early
  expect(tab.landed()).toBe(false); // no loop: it waits for its next poll
  expect(tab.behind).toBe(true);

  applyQueued(); // the request is over: Next marks the tag stale
  let next = (await (await get()).json()).published;
  expect(queued).toEqual([]); // nothing new to ask for
  expect(tab.polled(next)).toBe(true); // retried: served the stale entry, which starts the read again
  tab.shows({ fw9: (await page()).generation });
  expect(tab.landed()).toBe(false);
  expect(tab.behind).toBe(true);
  // The open page, refreshing now, gets the new entry.
  expect(f.polled(next)).toBe(true);
  f.shows({ fw9: (await page()).generation });
  f.landed();
  expect(f.behind).toBe(false);

  next = (await (await get()).json()).published;
  expect(tab.polled(next)).toBe(true); // retried again: this time it renders publication 2
  tab.shows({ fw9: (await page()).generation });
  expect(tab.landed()).toBe(false);
  expect(tab.behind).toBe(false);
  expect(tab.polled(next)).toBe(false); // and stays quiet

  // What every render showed: an entry's generation and its content always agree.
  for (const shown of seen) expect(shown.content).toBe(shown.generation === 2 ? "B" : "A");
  expect(seen.at(-1)).toEqual({ generation: 2, content: "B" });
});

const getBehind = (report: Record<string, number>) =>
  GET(new Request(`http://localhost:3447${pollPath(report)}`, { headers: { host: "localhost:3447" } }));

describe("a page that stays behind a publication says so, and the document is marked again", () => {
  let clock: number;
  let marked: string[];
  const publish = async (slug = "fw9") => {
    const { job } = await store.enqueue(slug, "cli", { state: "running" });
    await store.publish(job.id, async () => {});
    await store.finish(job.id, "done");
  };
  const serverWith = (revalidate: (slug: string) => Promise<void>) => makeBuilds({
    store, runner: new Runner(store, { concurrency: 1, spawn: fake.spawn }),
    docsRoot: join(dir, "docs"), slugs: () => ["fw9", "resnet", "p17"], revalidate, now: () => clock,
  });
  beforeEach(() => {
    clock = 1_000_000;
    marked = [];
    setBuildsForTests(serverWith(async (slug) => void marked.push(slug)));
  });

  test("marked again for a report below the published generation, no sooner than RENEW_AFTER_MS after the last mark; a renewal is a mark", async () => {
    await publish();
    await get();
    expect(marked).toEqual(["fw9"]); // the mark for generation 1
    clock += RENEW_AFTER_MS - 1;
    await getBehind({ fw9: 0 });
    expect(marked).toEqual(["fw9"]); // too soon
    clock += 1;
    expect((await (await getBehind({ fw9: 0 })).json()).published).toEqual({ fw9: 1 });
    expect(marked).toEqual(["fw9", "fw9"]);
    await getBehind({ fw9: 0 });
    expect(marked).toHaveLength(2); // the renewal counts: not every second
    clock += RENEW_AFTER_MS;
    await getBehind({ fw9: 0 });
    expect(marked).toHaveLength(3);
  });

  test("a report that is not behind marks nothing: equal, newer, a document with no publication, junk", async () => {
    await publish();
    await get();
    clock += RENEW_AFTER_MS * 10;
    for (const report of [{ fw9: 1 }, { fw9: 7 }, { resnet: 0 }] as Record<string, number>[]) await getBehind(report);
    await GET(new Request("http://localhost:3447/api/builds?behind=nope:0,fw9:x,fw9:-3,:4,fw9", { headers: { host: "localhost:3447" } }));
    await GET(new Request("http://localhost:3447/api/builds?behind", { headers: { host: "localhost:3447" } }));
    expect(marked).toEqual(["fw9"]);
  });

  test("a document marked in the poll for a new publication is not marked twice in it", async () => {
    await publish();
    await get();
    clock += RENEW_AFTER_MS;
    await publish();
    await getBehind({ fw9: 1 });
    expect(marked).toEqual(["fw9", "fw9"]); // generation 2, once
  });

  test("the reports of polls that share a pass, and of the next one, are all heard", async () => {
    let gated = false;
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    setBuildsForTests(serverWith(async (slug) => { marked.push(slug); if (gated) await gate; }));
    for (const slug of ["fw9", "resnet", "p17"]) await publish(slug);
    await get();
    expect(marked.sort()).toEqual(["fw9", "p17", "resnet"]);
    marked.length = 0;
    clock += RENEW_AFTER_MS;
    gated = true;
    const polls = [getBehind({ fw9: 0 }), getBehind({ resnet: 0 }), getBehind({ p17: 0, fw9: 0 })];
    await new Promise((r) => setTimeout(r, 100));
    release();
    await Promise.all(polls);
    expect(marked.sort()).toEqual(["fw9", "p17", "resnet"]); // each once, whichever pass heard it
  });

  test("a pass that fails marks nothing and remembers nothing: the page says it again with its next poll", async () => {
    await publish();
    await get();
    clock += RENEW_AFTER_MS;
    let fail = true;
    setBuildsForTests(serverWith(async (slug) => { if (fail) throw new Error("the cache would not take it"); marked.push(slug); }));
    fail = false;
    await get(); // first pass of this server: the mark for generation 1
    marked.length = 0;
    clock += RENEW_AFTER_MS;
    fail = true;
    await expect(getBehind({ fw9: 0 })).rejects.toThrow("would not take it");
    fail = false;
    await getBehind({ fw9: 0 }); // the same report, the next poll
    expect(marked).toEqual(["fw9"]);
  });
});

// Next compares a mark with the time a cache entry was SAVED, not with when the data in it was read.
// A render that read publication A, still running when publication B finished and its mark was
// asked for, saves A's page after the mark, and Next takes that entry as fresh. The server has
// remembered B and asks for nothing more. The stamp tells a page it is behind; only a mark made after
// the save can replace the entry. This stands in for Next's tag cache and full-route entry with
// exactly that rule (production proof: web/scripts/crossing/crossing-mark.ts).
test("a publication that overtakes a render leaves a fresh entry with old data, and the report of a page that stays behind gets it replaced", async () => {
  type Entry = { generation: number; content: string; savedAt: number };
  const disk = { content: "A" };
  let time = 1_000_000;
  const tick = () => ++time;
  let entry: Entry | undefined;
  let markedAt = 0;
  const queued: string[] = [];
  let hold: Promise<void> = Promise.resolve();
  let pending: Promise<void> | undefined;
  setBuildsForTests(makeBuilds({
    store, runner: new Runner(store, { concurrency: 1, spawn: fake.spawn }),
    docsRoot: join(dir, "docs"), slugs: () => ["fw9", "resnet", "p17"],
    revalidate: async (slug) => void queued.push(slug), now: () => time,
  }));
  const render = async () => { // what readDocument and its route do: read, take some time, save
    const generation = (await store.publications()).fw9?.generation ?? 0;
    const content = disk.content;
    await hold;
    entry = { generation, content, savedAt: tick() };
    pending = undefined;
  };
  const stale = () => !!entry && entry.savedAt <= markedAt;
  const request = async () => { // no entry: render and wait. A stale entry is served while a render starts.
    if (!entry) await render();
    else if (stale() && !pending) pending = render();
    const served = { generation: entry!.generation, content: entry!.content, fresh: !stale() };
    return served;
  };
  const poll = async (report: Record<string, number> = {}) => {
    const answer = await (await getBehind(report)).json();
    if (queued.splice(0).length) markedAt = tick(); // the handler has returned: Next applies the marks
    return answer.published as Record<string, number>;
  };
  const publishAs = async (content: string) => {
    const { job } = await store.enqueue("fw9", "cli", { state: "running" });
    await store.publish(job.id, async () => void (disk.content = content));
    await store.finish(job.id, "done");
  };
  const shown: { generation: number; content: string }[] = [];
  const page = async () => {
    const served = await request();
    shown.push({ generation: served.generation, content: served.content });
    return served;
  };

  await publishAs("A");
  await poll();
  expect(await page()).toEqual({ generation: 1, content: "A", fresh: true });

  await publishAs("B");
  await poll(); // marks generation 2: the entry is stale
  let release!: () => void;
  hold = new Promise<void>((r) => (release = r));
  expect(await page()).toEqual({ generation: 1, content: "A", fresh: false }); // served stale; the render reads B and waits

  await publishAs("C");
  expect(await poll()).toEqual({ fw9: 3 }); // marks generation 3 (applied once this request is over)
  expect(entry!.generation).toBe(1);
  release();
  await pending;
  expect(entry!.generation).toBe(2); // the render saves B's page after C's mark

  // The entry is fresh, and old. Plain polls ask for nothing: the server has remembered generation 3.
  for (let i = 0; i < 5; i++) {
    time += 1000;
    expect(await poll()).toEqual({ fw9: 3 });
    expect(await page()).toEqual({ generation: 2, content: "B", fresh: true });
  }
  expect(queued).toEqual([]);

  // A tab shows it. It refreshes at each poll answer and gets the same page, then reports.
  const tab = new Freshness();
  tab.shows({ fw9: 2 });
  expect(tab.polled(await poll())).toBe(true); // its first poll: behind
  tab.shows({ fw9: (await page()).generation });
  tab.landed();
  let reported: Record<string, number> = {};
  for (let i = 0; i < 12 && tab.behind; i++) {
    time += 1000;
    const report = tab.report();
    if (Object.keys(report).length && !Object.keys(reported).length) reported = report;
    if (tab.polled(await poll(report))) {
      tab.shows({ fw9: (await page()).generation });
      tab.landed();
    }
  }
  expect(reported).toEqual({ fw9: 2 }); // after STUCK_POLLS answers behind, what it rendered
  expect(tab.behind).toBe(false); // the mark that followed replaced the entry
  expect(await page()).toEqual({ generation: 3, content: "C", fresh: true });
  time += 1000;
  expect(tab.polled(await poll(tab.report()))).toBe(false); // current: quiet
  expect(tab.report()).toEqual({});

  // What every render showed: an entry's generation and its content always agree.
  for (const s of shown) expect(s.content).toBe({ 1: "A", 2: "B", 3: "C" }[s.generation as 1 | 2 | 3]);
});

test("a poll takes no lock when nothing is to be done, even with a live build in every slot", async () => {
  const { job } = await (await post({ slug: "fw9" })).json(); // dispatched: one transaction
  await post({ slug: "resnet" }); // queued behind it
  const dispatch = store.dispatch.bind(store);
  const recover = store.recover.bind(store);
  let transactions = 0;
  store.dispatch = (...args) => { transactions++; return dispatch(...args); };
  store.recover = () => { transactions++; return recover(); };
  for (let i = 0; i < 5; i++) expect((await get()).status).toBe(200);
  expect(transactions).toBe(0);
  // The running build's process ends: the next poll settles it and starts resnet, in one transaction.
  procs.alive.delete(1000);
  const body = await (await get()).json();
  expect(transactions).toBe(1);
  expect(body.recent[0]).toMatchObject({ id: job.id, state: "interrupted" });
  expect(body.active.map((j: { slug: string; state: string }) => [j.slug, j.state])).toEqual([["resnet", "running"]]);
});
