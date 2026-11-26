import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { listJobs } from "./record";
import { BuildStore } from "./store";
import { FakeProcs, removeDir, tempDir } from "./testkit";

let dir: string;
let procs: FakeProcs;
let clock: number;
let store: BuildStore;
beforeEach(async () => {
  dir = await tempDir();
  procs = new FakeProcs();
  clock = 1_000;
  store = new BuildStore({ dir, procs, now: () => ++clock });
});
afterEach(() => removeDir(dir));

describe("enqueue and the per-slug lock", () => {
  test("a new slug makes a queued job", async () => {
    const { job, created } = await store.enqueue("fw9", "ui");
    expect(created).toBe(true);
    expect(job).toMatchObject({ slug: "fw9", who: "ui", state: "queued", pid: null });
    expect((await store.snapshot()).active.map((j) => j.id)).toEqual([job.id]);
  });

  test("a second request for a queued slug is a no-op that returns the existing job", async () => {
    const first = await store.enqueue("fw9", "ui");
    const second = await store.enqueue("fw9", "ui");
    expect(second.created).toBe(false);
    expect(second.job.id).toBe(first.job.id);
    expect(await listJobs(dir)).toHaveLength(1); // the loser's file was removed
  });

  test("ten requests at once for one slug make exactly one job", async () => {
    const results = await Promise.all(Array.from({ length: 10 }, () => store.enqueue("fw9", "ui")));
    expect(results.filter((r) => r.created)).toHaveLength(1);
    expect(new Set(results.map((r) => r.job.id)).size).toBe(1);
    expect(await listJobs(dir)).toHaveLength(1);
    expect(await readdir(join(dir, "claims"))).toEqual(["fw9.json"]);
  });

  test("different slugs queue side by side, in order", async () => {
    const a = await store.enqueue("fw9", "ui");
    const b = await store.enqueue("resnet", "ui");
    expect((await store.snapshot()).active.map((j) => j.id)).toEqual([a.job.id, b.job.id]);
  });

  test("a command-line build in progress turns a page request into a no-op", async () => {
    const ref = procs.add(77);
    const cli = await store.enqueue("fw9", "cli", { state: "running", pid: ref.pid, pidStart: ref.start });
    const ui = await store.enqueue("fw9", "ui");
    expect(ui).toEqual({ job: cli.job, created: false });
  });

  test("a page job in the queue turns a command-line request into a no-op", async () => {
    const ui = await store.enqueue("fw9", "ui");
    const ref = procs.add(77);
    const cli = await store.enqueue("fw9", "cli", { pid: ref.pid, pidStart: ref.start });
    expect(cli.created).toBe(false);
    expect(cli.job.id).toBe(ui.job.id);
  });

  test("finishing a job gives the slug back", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    await store.finish(job.id, "failed", "boom");
    const again = await store.enqueue("fw9", "ui");
    expect(again.created).toBe(true);
    expect(again.job.id).not.toBe(job.id);
    expect((await store.get(job.id))).toMatchObject({ state: "failed", error: "boom" });
  });

  test("a claim whose process died is taken over", async () => {
    const ref = procs.add(77);
    const dead = await store.enqueue("fw9", "cli", { state: "running", pid: ref.pid, pidStart: ref.start });
    procs.alive.delete(77);
    const next = await store.enqueue("fw9", "ui");
    expect(next.created).toBe(true);
    expect(next.job.id).not.toBe(dead.job.id);
  });

  test("a claim whose pid now belongs to another program is taken over", async () => {
    const ref = procs.add(77);
    await store.enqueue("fw9", "cli", { state: "running", pid: ref.pid, pidStart: ref.start });
    procs.alive.set(77, ref.start + 1); // same pid, other process
    expect((await store.enqueue("fw9", "ui")).created).toBe(true);
  });

  test("a claim whose job file is gone is taken over", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    await Bun.file(join(dir, "jobs", `${job.id}.json`)).delete();
    expect((await store.enqueue("fw9", "ui")).created).toBe(true);
  });

  test("a slug that is not a plain name is refused before any file is made", async () => {
    for (const bad of ["../x", "a/b", "", "A", ".hidden", "x".repeat(200)]) {
      await expect(store.enqueue(bad, "ui")).rejects.toThrow();
    }
    expect(await readdir(dir)).toEqual([]);
  });
});

describe("update", () => {
  test("progress is merged into the record", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", stage: "Rendering page images", done: 2, total: 8 });
    expect(await store.get(job.id)).toMatchObject({ state: "running", stage: "Rendering page images", done: 2, total: 8 });
  });

  test("a finished job is not changed by a late write", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    await store.finish(job.id, "cancelled");
    await store.update(job.id, { state: "running", done: 5 });
    expect(await store.get(job.id)).toMatchObject({ state: "cancelled", done: null });
  });

  test("id, slug and who cannot be changed", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { slug: "evil", who: "cli", id: "x" });
    expect(await store.get(job.id)).toMatchObject({ slug: "fw9", who: "ui", id: job.id });
  });

  test("finishing stamps the time", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    const done = await store.finish(job.id, "done");
    expect(done?.finishedAt).not.toBeNull();
  });
});

describe("snapshot", () => {
  test("running jobs come before queued ones, recent is newest first and limited", async () => {
    const a = await store.enqueue("fw9", "ui");
    const b = await store.enqueue("resnet", "ui");
    await store.update(b.job.id, { state: "running", pid: null });
    const done = [];
    for (let i = 0; i < 4; i++) {
      const j = await store.enqueue(`d${i}`, "ui");
      done.push((await store.finish(j.job.id, "done"))!);
    }
    const snap = await store.snapshot(3);
    expect(snap.active.map((j) => j.id)).toEqual([b.job.id, a.job.id]);
    expect(snap.recent.map((j) => j.id)).toEqual([done[3].id, done[2].id, done[1].id]);
  });

  test("history is cut to the limit when a job is added", async () => {
    const small = new BuildStore({ dir, procs, now: () => ++clock, historyLimit: 2 });
    for (let i = 0; i < 5; i++) {
      const { job } = await small.enqueue(`d${i}`, "ui");
      await small.finish(job.id, "done");
    }
    await small.enqueue("last", "ui");
    expect((await small.snapshot()).recent.map((j) => j.slug)).toEqual(["d4", "d3"]);
  });
});

describe("recover (after a restart)", () => {
  test("a running job whose process is dead becomes interrupted and gives the slug back", async () => {
    const ref = procs.add(500);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    procs.alive.delete(500);
    const changed = await store.recover();
    expect(changed.map((j) => [j.id, j.state])).toEqual([[job.id, "interrupted"]]);
    expect((await store.get(job.id))?.error).toMatch(/ended before it finished/);
    expect((await store.enqueue("fw9", "ui")).created).toBe(true);
  });

  test("a running job whose pid is alive but is not the build is interrupted", async () => {
    const ref = procs.add(500, 5000, false);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    expect((await store.recover()).map((j) => j.state)).toEqual(["interrupted"]);
  });

  test("a running job whose pid was reused (other start time) is interrupted", async () => {
    const ref = procs.add(500);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start - 1 });
    expect((await store.recover()).map((j) => j.state)).toEqual(["interrupted"]);
  });

  test("a running job whose process is still the build is left alone", async () => {
    const ref = procs.add(500);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    expect(await store.recover()).toEqual([]);
    expect((await store.get(job.id))?.state).toBe("running");
  });

  test("a queued page job survives; a queued command-line job with a dead process does not", async () => {
    const ui = await store.enqueue("fw9", "ui");
    const ref = procs.add(600);
    const cli = await store.enqueue("resnet", "cli", { pid: ref.pid, pidStart: ref.start });
    procs.alive.delete(600);
    await store.recover();
    expect((await store.get(ui.job.id))?.state).toBe("queued");
    expect((await store.get(cli.job.id))?.state).toBe("interrupted");
  });

  test("a job with a cancel request whose process is gone becomes cancelled", async () => {
    const ref = procs.add(500);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    await store.cancel(job.id);
    procs.alive.set(500, ref.start); // the kill did not take, then the process died on its own
    procs.onKill = () => {};
    procs.alive.delete(500);
    await store.recover();
    expect((await store.get(job.id))?.state).toBe("cancelled");
  });

  test("a claim nobody holds is removed", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    await Bun.file(join(dir, "jobs", `${job.id}.json`)).delete();
    await store.recover();
    expect(await readdir(join(dir, "claims"))).toEqual([]);
  });
});

describe("cancel", () => {
  test("a queued job ends at once, with no process touched", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    const r = await store.cancel(job.id);
    expect(r.outcome).toBe("cancelled");
    expect(r.job?.state).toBe("cancelled");
    expect(procs.killed).toEqual([]);
    expect((await store.enqueue("fw9", "ui")).created).toBe(true);
  });

  test("a running job has its own process tree stopped and nothing else", async () => {
    const mine = procs.add(500);
    procs.add(501); // another build's process, must not be touched
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: mine.pid, pidStart: mine.start });
    const r = await store.cancel(job.id);
    expect(procs.killed).toEqual([mine]);
    expect(procs.alive.has(501)).toBe(true);
    expect(r.outcome).toBe("cancelled");
    expect((await store.get(job.id))?.state).toBe("cancelled");
    expect(await store.cancelRequested(job.id)).toBe(true);
  });

  test("a pid that is no longer the build is not signalled", async () => {
    const stranger = procs.add(500, 5000, false);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: stranger.pid, pidStart: stranger.start });
    await store.cancel(job.id);
    expect(procs.killed).toEqual([]);
    expect(procs.alive.has(500)).toBe(true);
    expect((await store.get(job.id))?.state).toBe("cancelled");
  });

  test("when the script ends its own job after the signal, that end state stands", async () => {
    const ref = procs.add(500);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    procs.onKill = async (r) => {
      procs.alive.delete(r.pid);
      await store.finish(job.id, "cancelled", "stopped by request");
    };
    const r = await store.cancel(job.id);
    expect(r).toMatchObject({ outcome: "cancelled", job: { state: "cancelled", error: "stopped by request" } });
  });

  test("a process that survives the signal leaves the job running (stopping)", async () => {
    const ref = procs.add(500);
    const { job } = await store.enqueue("fw9", "ui");
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    procs.onKill = () => {};
    const r = await store.cancel(job.id);
    expect(r.outcome).toBe("stopping");
    expect(r.job?.state).toBe("running");
  });

  test("a finished or unknown job reports so", async () => {
    const { job } = await store.enqueue("fw9", "ui");
    await store.finish(job.id, "done");
    expect((await store.cancel(job.id)).outcome).toBe("finished");
    expect((await store.cancel("0000000aa-deadbeef")).outcome).toBe("unknown");
  });
});

describe("adopt and the job kind", () => {
  test("a build script adopts a build job and a rerender script a rerender job, never the other", async () => {
    const ref = procs.add(4242);
    const { job } = await store.enqueue("fw9", "ui", { kind: "rerender" });
    await store.update(job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    expect(await store.adopt(job.id, "fw9", ref)).toBeNull();
    expect(await store.adopt(job.id, "fw9", ref, "build")).toBeNull();
    expect((await store.adopt(job.id, "fw9", ref, "rerender"))?.kind).toBe("rerender");
  });
});

describe("a rerender and a build of one slug (one active job per slug)", () => {
  test("a rerender asked for while a build is queued or running returns that build", async () => {
    const build = await store.enqueue("fw9", "ui");
    const queued = await store.enqueue("fw9", "ui", { kind: "rerender" });
    expect(queued).toEqual({ job: build.job, created: false });
    const ref = procs.add(901);
    await store.update(build.job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    const running = await store.enqueue("fw9", "cli", { kind: "rerender" });
    expect(running.created).toBe(false);
    expect(running.job).toMatchObject({ id: build.job.id, kind: "build" });
    expect(await listJobs(dir)).toHaveLength(1);
  });

  test("a build asked for while a rerender is queued turns it into a build: same job, same place", async () => {
    const rerender = await store.enqueue("fw9", "ui", { kind: "rerender" });
    expect(rerender.job.kind).toBe("rerender");
    const build = await store.enqueue("fw9", "ui");
    expect(build.created).toBe(false);
    expect(build.job).toMatchObject({ id: rerender.job.id, kind: "build", state: "queued", enqueuedAt: rerender.job.enqueuedAt });
    expect((await store.get(rerender.job.id))?.kind).toBe("build");
    expect(await listJobs(dir)).toHaveLength(1);
  });

  test("a build asked for while a rerender runs returns the running rerender, unchanged", async () => {
    const rerender = await store.enqueue("fw9", "ui", { kind: "rerender" });
    const ref = procs.add(902);
    await store.update(rerender.job.id, { state: "running", pid: ref.pid, pidStart: ref.start });
    const build = await store.enqueue("fw9", "ui");
    expect(build.created).toBe(false);
    expect(build.job).toMatchObject({ id: rerender.job.id, kind: "rerender", state: "running" });
    expect((await store.get(rerender.job.id))?.kind).toBe("rerender");
  });

  test("a command-line rerender already started is never turned into a build", async () => {
    const rerender = await store.enqueue("fw9", "cli", { kind: "rerender" });
    const ref = procs.add(777);
    await store.update(rerender.job.id, { pid: ref.pid, pidStart: ref.start });
    expect((await store.enqueue("fw9", "ui")).job.kind).toBe("rerender");
  });

  test("a rerender and a build asked for at once make one job", async () => {
    const results = await Promise.all([
      store.enqueue("fw9", "ui", { kind: "rerender" }), store.enqueue("fw9", "ui"), store.enqueue("fw9", "ui", { kind: "rerender" }),
    ]);
    expect(results.filter((r) => r.created)).toHaveLength(1);
    expect(await listJobs(dir)).toHaveLength(1);
  });

  test("after the job ends, a new request starts a new job of the kind asked for", async () => {
    const first = await store.enqueue("fw9", "ui", { kind: "rerender" });
    await store.finish(first.job.id, "done");
    const next = await store.enqueue("fw9", "ui");
    expect(next).toMatchObject({ created: true, job: { kind: "build" } });
  });
});
