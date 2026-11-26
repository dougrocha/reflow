import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { BuildStore } from "./store";
import { Runner } from "./runner";
import { newJob, writeJob } from "./record";
import { FakeProcs, fakeSpawn, removeDir, tempDir } from "./testkit";

let dir: string;
let procs: FakeProcs;
const runners: Runner[] = [];
beforeEach(async () => { dir = await tempDir(); procs = new FakeProcs(); });
afterEach(async () => { for (const runner of runners.splice(0)) await runner.stop(); await removeDir(dir); });
const store = () => new BuildStore({ dir, procs });
function runner(spawn = fakeSpawn(procs).spawn, pollMs?: number) {
  const r = new Runner(store(), { concurrency: 1, spawn, pollMs });
  runners.push(r);
  return r;
}
const wait = (ms = 80) => new Promise((r) => setTimeout(r, ms));
function barrier() {
  let release!: () => void;
  let entered!: () => void;
  return { entered: new Promise<void>((r) => { entered = r; }), wait: new Promise<void>((r) => { release = r; }), arrive: () => entered(), release: () => release() };
}

// Pause after spawn but before PID publication, exactly where the old runner
// left the job queued. Two independent stores operate on the real filesystem.
test("two servers cannot dispatch the same job or exceed the shared UI slot during PID handoff", async () => {
  const fake = fakeSpawn(procs);
  const a = runner(fake.spawn);
  const b = runner(fake.spawn);
  await a.store.enqueue("fw9", "ui");
  await a.store.enqueue("resnet", "ui");
  const gate = barrier();
  const start = procs.start.bind(procs);
  procs.start = async (pid) => { if (pid === 1000) { gate.arrive(); await gate.wait; } return start(pid); };
  const first = a.pump();
  await gate.entered;
  const second = b.pump();
  await wait();
  try { expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9"]); } finally { gate.release(); }
  await Promise.all([first, second]);
  expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9"]);
});

test("a losing enqueue is never executable while it checks a live CLI holder", async () => {
  const ref = procs.add(77);
  const cli = await store().enqueue("fw9", "cli", { state: "running", pid: ref.pid, pidStart: ref.start });
  const gate = barrier();
  const isBuild = procs.isBuild.bind(procs);
  let paused = false;
  procs.isBuild = async (r) => { if (r.pid === 77 && !paused) { paused = true; gate.arrive(); await gate.wait; } return isBuild(r); };
  const enqueue = store().enqueue("fw9", "ui");
  await gate.entered;
  const fake = fakeSpawn(procs);
  const pumping = runner(fake.spawn).pump();
  await wait();
  gate.release();
  const result = await enqueue;
  await pumping;
  expect(result).toMatchObject({ created: false, job: { id: cli.job.id } });
  expect(fake.children).toHaveLength(0);
});

test("recovery cannot unlink the live claim that takes over a stale holder", async () => {
  const first = await store().enqueue("fw9", "ui");
  // A crash between publishing a terminal record and releasing its claim.
  await writeJob(dir, { ...first.job, state: "done", finishedAt: Date.now() });
  const gate = barrier();
  const recovering = store();
  const holds = recovering.holds.bind(recovering);
  recovering.holds = async (job) => {
    if (job?.id === first.job.id) { gate.arrive(); await gate.wait; }
    return holds(job);
  };
  const recovery = recovering.recover();
  await gate.entered;
  const next = store().enqueue("fw9", "ui");
  await wait();
  gate.release();
  await recovery;
  const acquired = await next;
  expect(JSON.parse(await readFile(join(dir, "claims/fw9.json"), "utf8"))).toEqual({ job: acquired.job.id });
  expect((await store().enqueue("fw9", "ui")).created).toBe(false);
});

test("cancel during queued-to-running handoff stops the published child before allowing retry", async () => {
  const fake = fakeSpawn(procs);
  const r = runner(fake.spawn);
  const { job } = await r.store.enqueue("fw9", "ui");
  const gate = barrier();
  const start = procs.start.bind(procs);
  procs.start = async (pid) => { gate.arrive(); await gate.wait; return start(pid); };
  const pumping = r.pump();
  await gate.entered;
  const cancel = store().cancel(job.id);
  await wait();
  gate.release();
  await pumping;
  await cancel;
  expect(procs.killed).toEqual([{ pid: 1000, start: 10000 }]);
  expect((await store().get(job.id))?.state).toBe("cancelled");
  expect((await store().enqueue("fw9", "ui")).created).toBe(true);
});

test("an unclaimed or wrong-PID UI job cannot be adopted", async () => {
  const { job } = await store().enqueue("fw9", "ui");
  expect(await store().adopt(job.id, "fw9", { pid: 77, start: 770 })).toBeNull();
  const orphan = newJob("resnet", "ui", Date.now(), { state: "running", pid: 78, pidStart: 780 });
  await writeJob(dir, orphan);
  expect(await store().adopt(orphan.id, "resnet", { pid: 78, start: 780 })).toBeNull();
});

test("a surviving converter retains exclusion even when its script dies or tries to finish", async () => {
  const script = procs.add(77);
  const child = procs.add(78);
  const { job } = await store().enqueue("fw9", "cli", { state: "running", pid: script.pid, pidStart: script.start, childPid: child.pid, childStart: child.start });
  expect((await store().finish(job.id, "cancelled"))?.state).toBe("running");
  procs.alive.delete(77);
  await store().recover();
  expect((await store().enqueue("fw9", "ui")).created).toBe(false);
  await store().cancel(job.id);
  expect(procs.killed).toEqual([child]);
  expect((await store().enqueue("fw9", "ui")).created).toBe(true);
});

test("a restarted server advances the queue after an external build exits without a browser poll", async () => {
  const ref = procs.add(77);
  await store().enqueue("fw9", "ui", { state: "running", pid: ref.pid, pidStart: ref.start });
  await store().enqueue("resnet", "ui");
  const fake = fakeSpawn(procs);
  await runner(fake.spawn, 50).start();
  expect(fake.children).toHaveLength(0);
  procs.alive.delete(77);
  for (let i = 0; i < 30 && fake.children.length === 0; i++) await wait(50);
  expect(fake.children.map((c) => c.job.slug)).toEqual(["resnet"]);
});

// The guards below are each reached with every other condition true, so removing one of them
// changes the outcome (the earlier "wrong-PID" check failed on the job state first).

test("dispatch never starts a queued job whose claim another job holds", async () => {
  const { job } = await store().enqueue("fw9", "ui");
  // A live command-line build owns the slug, and the queued record has not caught up with that.
  const ref = procs.add(77);
  const owner = newJob("fw9", "cli", Date.now(), { state: "running", pid: ref.pid, pidStart: ref.start });
  await writeJob(dir, owner);
  await mkdir(join(dir, "claims"), { recursive: true });
  await writeFile(join(dir, "claims/fw9.json"), JSON.stringify({ job: owner.id }));
  const fake = fakeSpawn(procs);
  await runner(fake.spawn).pump();
  expect(fake.children).toHaveLength(0);
  expect(await store().get(job.id)).toMatchObject({ state: "interrupted", error: "The build no longer owns its document claim." });
  expect(await store().get(owner.id)).toMatchObject({ state: "running" });
  expect(JSON.parse(await readFile(join(dir, "claims/fw9.json"), "utf8"))).toEqual({ job: owner.id });
});

test("adoption needs exactly the PID and the start time the dispatcher recorded", async () => {
  const fake = fakeSpawn(procs);
  const r = runner(fake.spawn);
  const { job } = await r.store.enqueue("fw9", "ui");
  await r.pump();
  const dispatched = (await store().get(job.id))!;
  expect(dispatched).toMatchObject({ state: "running", pid: 1000, pidStart: 10_000 });
  const ref = { pid: dispatched.pid!, start: dispatched.pidStart! };
  expect(await store().adopt(job.id, "fw9", { ...ref, pid: ref.pid + 1 })).toBeNull(); // another process
  expect(await store().adopt(job.id, "fw9", { ...ref, start: ref.start + 1 })).toBeNull(); // the PID, reused
  expect(await store().adopt(job.id, "resnet", ref)).toBeNull(); // another document
  expect(await store().adopt(job.id, "fw9", ref)).toMatchObject({ id: job.id, state: "running" });
});

test("the runner's timer is installed even when its first pass fails, and a later tick dispatches", async () => {
  await store().enqueue("fw9", "ui");
  const fake = fakeSpawn(procs);
  const r = runner(fake.spawn, 50);
  const dispatch = r.store.dispatch.bind(r.store);
  let calls = 0;
  r.store.dispatch = async (...args) => {
    if (calls++ === 0) throw new Error("the lock helper failed");
    return dispatch(...args);
  };
  await expect(r.start()).rejects.toThrow("the lock helper failed");
  for (let i = 0; i < 40 && fake.children.length === 0; i++) await wait(50);
  expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9"]);
});

test("an idle runner takes no lock; work written by another process is still picked up", async () => {
  const fake = fakeSpawn(procs);
  const r = runner(fake.spawn, 20);
  let transactions = 0;
  const dispatch = r.store.dispatch.bind(r.store);
  r.store.dispatch = (...args) => { transactions++; return dispatch(...args); };
  await r.start();
  await wait(200);
  expect(transactions).toBe(0);
  // Another server enqueues: its claim is the work this runner sees without a lock.
  await store().enqueue("fw9", "ui");
  for (let i = 0; i < 40 && fake.children.length === 0; i++) await wait(25);
  expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9"]);
  await store().finish(fake.children[0].job.id, "done");
  fake.children[0].exit(0);
  await wait(100);
  const settled = transactions;
  await wait(200);
  expect(transactions).toBe(settled);
});

test("with every slot busy with a live build, neither the timer nor a pump takes the lock; a freed slot is filled", async () => {
  await store().enqueue("fw9", "ui");
  await store().enqueue("resnet", "ui");
  const fake = fakeSpawn(procs);
  const r = runner(fake.spawn, 20);
  let transactions = 0;
  const dispatch = r.store.dispatch.bind(r.store);
  r.store.dispatch = (...args) => { transactions++; return dispatch(...args); };
  await r.start();
  expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9"]);
  expect(transactions).toBe(1);
  await wait(200);
  for (let i = 0; i < 5; i++) await r.pump();
  expect(transactions).toBe(1); // resnet waits for the one slot, which a live build holds
  await r.store.finish(fake.children[0].job.id, "done");
  fake.children[0].exit(0);
  for (let i = 0; i < 40 && fake.children.length < 2; i++) await wait(25);
  expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9", "resnet"]);
  const after = transactions;
  await wait(200);
  expect(transactions).toBe(after);
});

test("the lock-free check finds each kind of work settle and dispatch do, and nothing else", async () => {
  const s = store();
  expect(await s.needsTransaction(1)).toBe(false);
  // A live command-line build: nothing to do.
  const cli = procs.add(77);
  const { job } = await s.enqueue("fw9", "cli", { state: "running", pid: cli.pid, pidStart: cli.start });
  expect(await s.needsTransaction(1)).toBe(false);
  // Its process ends: settle has a job to end.
  procs.alive.delete(77);
  expect(await s.needsTransaction(1)).toBe(true);
  await s.recover();
  expect(await s.needsTransaction(1)).toBe(false);
  // A claim left by a crash between ending a job and releasing its claim.
  await mkdir(join(dir, "claims"), { recursive: true });
  await writeFile(join(dir, "claims/fw9.json"), JSON.stringify({ job: job.id }));
  expect(await s.needsTransaction(1)).toBe(true);
  await s.recover();
  expect(await s.needsTransaction(1)).toBe(false);
  // A queued page job: work while a slot is free, none while live page builds fill every slot.
  const ui = procs.add(80);
  await s.enqueue("resnet", "ui", { state: "running", pid: ui.pid, pidStart: ui.start });
  await s.enqueue("p17", "ui");
  expect(await s.needsTransaction(1)).toBe(false);
  expect(await s.needsTransaction(2)).toBe(true);
});

test("pumps that arrive while one waits share it, and it starts after the last of them asked", async () => {
  const fake = fakeSpawn(procs);
  const r = new Runner(store(), { concurrency: 2, spawn: fake.spawn });
  runners.push(r);
  let checks = 0;
  const check = r.store.needsTransaction.bind(r.store);
  r.store.needsTransaction = (...args) => { checks++; return check(...args); };
  const gate = barrier();
  const dispatch = r.store.dispatch.bind(r.store);
  r.store.dispatch = async (...args) => { gate.arrive(); await gate.wait; return dispatch(...args); };
  await r.store.enqueue("fw9", "ui");
  const first = r.pump();
  await gate.entered;
  const waiting = [r.pump(), r.pump(), r.pump()];
  await r.store.enqueue("resnet", "ui"); // written after those pumps asked; the shared pass sees it
  r.store.dispatch = dispatch;
  gate.release();
  await Promise.all([first, ...waiting]);
  expect(checks).toBe(2);
  expect(fake.children.map((c) => c.job.slug)).toEqual(["fw9", "resnet"]);
});

test("the lock-free check sees a dead job whose claim a live build has already taken over", async () => {
  const s = store();
  const old = procs.add(77);
  const { job } = await s.enqueue("fw9", "cli", { state: "running", pid: old.pid, pidStart: old.start });
  procs.alive.delete(77);
  // A new page build took the stale claim; the old record still says running, and only settle ends it.
  const live = procs.add(80);
  expect((await s.enqueue("fw9", "ui", { state: "running", pid: live.pid, pidStart: live.start })).created).toBe(true);
  expect(await s.needsTransaction(1)).toBe(true);
  await s.recover();
  expect(await s.get(job.id)).toMatchObject({ state: "interrupted" });
  expect(await s.needsTransaction(1)).toBe(false);
});

test("a dead job whose claim a finished takeover removed is still settled when a server starts", async () => {
  const s = store();
  const old = procs.add(77);
  const { job } = await s.enqueue("fw9", "cli", { state: "running", pid: old.pid, pidStart: old.start });
  procs.alive.delete(77);
  // A later build takes the dead holder's claim and finishes, which removes the claim. Nothing is left
  // that points at the old record: no claim and no publication in progress, only the record itself.
  const next = await s.enqueue("fw9", "cli", { state: "running", pid: procs.add(80).pid, pidStart: 800 });
  expect(next.created).toBe(true);
  await s.finish(next.job.id, "done");
  expect(await readdir(join(dir, "claims"))).toEqual([]);
  expect((await s.publications()).fw9?.publishing ?? null).toBeNull();
  expect(await s.get(job.id)).toMatchObject({ state: "running" });
  expect(await s.needsTransaction(1)).toBe(true);
  const r = runner(fakeSpawn(procs).spawn, 20);
  let transactions = 0;
  const dispatch = r.store.dispatch.bind(r.store);
  r.store.dispatch = (...args) => { transactions++; return dispatch(...args); };
  await r.start();
  expect(transactions).toBe(1);
  expect(await s.get(job.id)).toMatchObject({ state: "interrupted" });
  // Settled, so the server is idle again and takes no lock.
  await wait(200);
  expect(transactions).toBe(1);
  expect(await s.needsTransaction(1)).toBe(false);
});
