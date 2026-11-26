import { expect, test } from "bun:test";
import { newJob } from "./record";
import { schedule } from "./schedule";
import type { JobRecord } from "./view";

// schedule() on plain data: no store, no files, no processes.

let n = 0;
/** A job record with a fixed id and enqueue time, so the order is the order the tests give. */
function job(id: string, slug: string, fields: Partial<JobRecord> = {}): JobRecord {
  return { ...newJob(slug, "ui", 1000 + n++, fields), id };
}
const owning = (...jobs: JobRecord[]) => new Map(jobs.map((j) => [j.slug, j.id]));
const plan = (
  jobs: JobRecord[],
  opts: { concurrency?: number; claims?: Map<string, string>; cancels?: string[] } = {},
) => {
  const out = schedule({
    jobs,
    claims: opts.claims ?? owning(...jobs),
    cancelRequested: new Set(opts.cancels ?? []),
    concurrency: opts.concurrency ?? 3,
  });
  return { free: out.free, steps: out.steps.map((s) => `${s.action}:${s.job.id}`) };
};

test("concurrency: the queued jobs start while slots are free, first enqueued first", () => {
  const cases: { name: string; running: number; queued: number; concurrency: number; free: number; steps: string[] }[] = [
    { name: "idle", running: 0, queued: 0, concurrency: 3, free: 3, steps: [] },
    { name: "fewer queued than slots", running: 0, queued: 2, concurrency: 3, free: 3, steps: ["start:q0", "start:q1"] },
    { name: "one slot", running: 2, queued: 3, concurrency: 3, free: 1, steps: ["start:q0", "start:q1", "start:q2"] },
    { name: "all slots busy", running: 3, queued: 2, concurrency: 3, free: 0, steps: [] },
    { name: "over-full (concurrency lowered)", running: 4, queued: 1, concurrency: 3, free: -1, steps: [] },
    { name: "concurrency 1", running: 0, queued: 2, concurrency: 1, free: 1, steps: ["start:q0", "start:q1"] },
  ];
  for (const c of cases) {
    const jobs = [
      ...Array.from({ length: c.running }, (_, i) => job(`r${i}`, `run${i}`, { state: "running" })),
      ...Array.from({ length: c.queued }, (_, i) => job(`q${i}`, `queue${i}`)),
    ];
    expect({ name: c.name, ...plan(jobs, { concurrency: c.concurrency }) }).toEqual({ name: c.name, free: c.free, steps: c.steps });
  }
});

test("FIFO: queued jobs are ordered by enqueue time, then id, whatever order they are listed in", () => {
  const late = job("a-late", "late", { enqueuedAt: 5000 });
  const early = job("z-early", "early", { enqueuedAt: 4000 });
  const tieB = job("b", "tie-b", { enqueuedAt: 4500 });
  const tieA = job("a", "tie-a", { enqueuedAt: 4500 });
  expect(plan([late, tieB, early, tieA]).steps).toEqual(["start:z-early", "start:a", "start:b", "start:a-late"]);
});

test("a command-line job takes no page slot and is never started by the runner", () => {
  const cli = job("cli1", "fw9", { who: "cli", state: "running" });
  const cliQueued = job("cli2", "resnet", { who: "cli" });
  const page = job("p1", "naca");
  expect(plan([cli, cliQueued, page], { concurrency: 1 })).toEqual({ free: 1, steps: ["start:p1"] });
});

test("finished jobs are ignored: they hold no slot and are not started", () => {
  const done = ["succeeded", "failed", "cancelled", "interrupted"].map((state, i) =>
    job(`d${i}`, `done${i}`, { state: state as JobRecord["state"], finishedAt: 2000 }));
  expect(plan([...done, job("q", "next")], { concurrency: 1 })).toEqual({ free: 1, steps: ["start:q"] });
});

test("one build per document: a queued job whose claim names another job is interrupted, not started", () => {
  const stale = job("old", "fw9");
  const holder = job("new", "fw9");
  // The claim names `new`; `old` lost it.
  expect(plan([stale, holder], { claims: owning(holder) }).steps).toEqual(["interrupt:old", "start:new"]);
});

test("a claim held by a job the snapshot lacks, or no claim at all, interrupts the queued job", () => {
  const a = job("a", "fw9");
  const b = job("b", "resnet");
  const claims = new Map([["fw9", "other-process-job"]]);
  expect(plan([a, b], { claims }).steps).toEqual(["interrupt:a", "interrupt:b"]);
});

test("a running job does not make a second queued job of its slug startable", () => {
  const running = job("r", "fw9", { state: "running" });
  const queued = job("q", "fw9");
  expect(plan([running, queued], { claims: owning(running) })).toEqual({ free: 2, steps: ["interrupt:q"] });
});

test("a cancel marker ends a queued job as cancelled, and the jobs behind it still start", () => {
  const a = job("a", "fw9");
  const b = job("b", "resnet");
  expect(plan([a, b], { cancels: ["a"] }).steps).toEqual(["cancel:a", "start:b"]);
});

test("a lost claim wins over a cancel marker", () => {
  const a = job("a", "fw9");
  expect(plan([a], { claims: new Map(), cancels: ["a"] }).steps).toEqual(["interrupt:a"]);
});

test("a cancel marker on a running job changes nothing here", () => {
  const running = job("r", "fw9", { state: "running" });
  expect(plan([running, job("q", "resnet")], { cancels: ["r"], concurrency: 2 })).toEqual({ free: 1, steps: ["start:q"] });
});

test("every queued job gets a step, so a failed spawn can hand its slot to the next one", () => {
  const jobs = [job("a", "s1"), job("b", "s2"), job("c", "s3")];
  expect(plan(jobs, { concurrency: 1 })).toEqual({ free: 1, steps: ["start:a", "start:b", "start:c"] });
});

test("it does not change the records it is given", () => {
  const jobs = [job("b", "s2", { enqueuedAt: 9 }), job("a", "s1", { enqueuedAt: 8 })];
  const before = JSON.stringify(jobs);
  plan(jobs);
  expect(JSON.stringify(jobs)).toBe(before);
  expect(jobs.map((j) => j.id)).toEqual(["b", "a"]);
});
