import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { newJob } from "./record";
import { buildArgs, Runner, type RunnerOptions } from "./runner";
import { BuildStore } from "./store";
import { FakeProcs, fakeSpawn, removeDir, tempDir, tick } from "./testkit";

let dir: string;
let procs: FakeProcs;
let clock: number;
const runners: Runner[] = [];
function runner(store: BuildStore, options: RunnerOptions) {
  const r = new Runner(store, options);
  runners.push(r);
  return r;
}
const newStore = () => new BuildStore({ dir, procs, now: () => ++clock });
beforeEach(async () => {
  dir = await tempDir();
  procs = new FakeProcs();
  clock = 1_000;
});
afterEach(async () => {
  await Promise.all(runners.splice(0).map((r) => r.stop()));
  await removeDir(dir);
});

describe("the runner", () => {
  test("runs one job at a time by default, in queue order", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    const a = await running.enqueue("fw9");
    const b = await running.enqueue("resnet");
    const c = await running.enqueue("p17");
    expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9"]);
    expect((await running.store.get(a.job.id))).toMatchObject({ state: "running", pid: 1000, pidStart: 10_000 });
    expect((await running.store.get(b.job.id))?.state).toBe("queued");

    fake.children[0].exit(0);
    await tick();
    expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9", "resnet"]);
    fake.children[1].exit(0);
    await tick();
    expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9", "resnet", "p17"]);
    expect((await running.store.get(c.job.id))?.state).toBe("running");
  });

  test("the concurrency is configurable", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 2, spawn: fake.spawn });
    for (const slug of ["fw9", "resnet", "p17"]) await running.enqueue(slug);
    expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9", "resnet"]);
    fake.children[1].exit(0);
    await tick();
    expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9", "resnet", "p17"]);
  });

  test("a second request for a slug that is queued or running starts nothing", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    const first = await running.enqueue("fw9");
    const again = await running.enqueue("fw9");
    expect(again).toEqual({ job: (await running.store.get(first.job.id))!, created: false });
    expect(fake.children).toHaveLength(1);
  });

  test("a process that exits without ending its job fails the job and the next one starts", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    const a = await running.enqueue("fw9");
    await running.enqueue("resnet");
    fake.children[0].exit(3);
    await tick();
    expect(await running.store.get(a.job.id)).toMatchObject({ state: "failed", error: "The build process exited with code 3." });
    expect(fake.children).toHaveLength(2);
  });

  test("a job the script ended itself is not overwritten when the process exits", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    const a = await running.enqueue("fw9");
    await running.store.finish(a.job.id, "done");
    fake.children[0].exit(0);
    await tick();
    expect((await running.store.get(a.job.id))?.state).toBe("done");
  });

  test("a cancelled running job is marked cancelled when its process goes", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    const a = await running.enqueue("fw9");
    procs.onKill = () => {}; // the process is stopped by exiting, below
    await running.cancel(a.job.id);
    fake.children[0].exit(null, "SIGTERM");
    await tick();
    expect((await running.store.get(a.job.id))?.state).toBe("cancelled");
  });

  test("a job that cannot be started fails with the reason and does not block the queue", async () => {
    const bad = fakeSpawn(procs, { failToStart: true });
    const running = runner(newStore(), { concurrency: 1, spawn: bad.spawn });
    const a = await running.enqueue("fw9");
    expect(await running.store.get(a.job.id)).toMatchObject({ state: "failed" });
    expect((await running.store.get(a.job.id))?.error).toMatch(/Could not start the build: spawn bun ENOENT/);
    expect((await running.store.enqueue("resnet", "ui")).created).toBe(true);
  });

  test("a queued job survives a restart and runs when the new runner starts", async () => {
    const quiet = fakeSpawn(procs);
    const first = runner(newStore(), { concurrency: 1, spawn: quiet.spawn });
    await first.enqueue("fw9");
    await first.enqueue("resnet");
    // The server dies: fw9's process dies with it; resnet was never started.
    procs.alive.delete(1000);
    expect((await first.store.snapshot()).active.map((j) => j.state)).toEqual(["running", "queued"]);

    const fake = fakeSpawn(procs);
    const second = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    await second.start();
    expect(fake.children.map((c) => c.job.slug)).toEqual(["resnet"]);
    expect((await second.store.snapshot()).recent.map((j) => [j.slug, j.state])).toEqual([["fw9", "interrupted"]]);
  });

  test("on start, a running job whose process is gone is interrupted, then the queue runs", async () => {
    const store = newStore();
    const ref = procs.add(900);
    const stuck = await store.enqueue("fw9", "ui");
    await store.update(stuck.job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    const waiting = await store.enqueue("resnet", "ui");
    procs.alive.delete(900);
    const fake = fakeSpawn(procs);
    const running = runner(store, { concurrency: 1, spawn: fake.spawn });
    await running.start();
    expect((await store.get(stuck.job.id))?.state).toBe("interrupted");
    expect(fake.children.map((c) => c.job.id)).toEqual([waiting.job.id]);
  });

  test("a build still running from before the restart takes a slot", async () => {
    const store = newStore();
    const ref = procs.add(900);
    const live = await store.enqueue("fw9", "ui");
    await store.update(live.job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    await store.enqueue("resnet", "ui");
    const fake = fakeSpawn(procs);
    const running = runner(store, { concurrency: 1, spawn: fake.spawn });
    await running.start();
    expect(fake.children).toHaveLength(0);
  });

  test("a queued job cancelled before it ran never starts", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    await running.enqueue("fw9");
    const b = await running.enqueue("resnet");
    await running.cancel(b.job.id);
    fake.children[0].exit(0);
    await tick();
    expect(fake.children).toHaveLength(1);
    expect((await running.store.get(b.job.id))?.state).toBe("cancelled");
  });

  test("command-line builds do not take a page slot", async () => {
    const store = newStore();
    const ref = procs.add(900);
    await store.enqueue("p17", "cli", { state: "running", pid: ref.pid, pidStart: ref.start });
    const fake = fakeSpawn(procs);
    const running = runner(store, { concurrency: 1, spawn: fake.spawn });
    await running.enqueue("fw9");
    expect(fake.children).toHaveLength(1);
  });
});

describe("the build program's command line", () => {
  const job = (kind: "build" | "rerender") => newJob("fw9", "ui", 1, { kind });
  test("the stage cache for every job, and --rerender for a rerender job and for no other", () => {
    expect(buildArgs(job("build"), "bun")).toEqual(["-n", "19", "bun", "scripts/build-docs.ts", "--stage-cache", "fw9"]);
    expect(buildArgs(job("rerender"), "bun")).toEqual(["-n", "19", "bun", "scripts/build-docs.ts", "--stage-cache", "--rerender", "fw9"]);
  });

  test("the runner starts a rerender job the same way as a build and keeps its kind", async () => {
    const fake = fakeSpawn(procs);
    const running = runner(newStore(), { concurrency: 1, spawn: fake.spawn });
    await running.enqueue("fw9", "rerender");
    expect(fake.children[0].job.kind).toBe("rerender");
  });
});
