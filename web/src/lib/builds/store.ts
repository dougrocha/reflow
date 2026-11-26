import { mkdir, readdir, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import {
  cancelRequested, compareQueue, isActive, isFinished, listJobs, markCancel, newJob, prune, readJob, readPublications,
  writeAtomic, writeJob, type Publications,
} from "./record";
import type { JobKind, JobRecord, JobState, Who } from "./view";
import { assertSlug, SLUG_RE } from "../../pipelines";
import { claimsDir, publicationsFile } from "../paths";
import { transaction } from "./lock";
import { realProcs, type Procs, type ProcRef } from "./proc";
import type { Child, Spawn } from "./runner";
import { schedule } from "./schedule";

const HISTORY_LIMIT = 50;
export type Snapshot = { active: JobRecord[]; recent: JobRecord[] };
export type EnqueueResult = { job: JobRecord; created: boolean };
type Options = { dir: string; procs?: Procs; now?: () => number; historyLimit?: number };
type Terminal = Exclude<JobState, "queued" | "running">;
const scriptRef = (j: JobRecord): ProcRef | null => j.pid === null || j.pidStart === null ? null : { pid: j.pid, start: j.pidStart };
const childRef = (j: JobRecord): ProcRef | null => j.childPid === null || j.childStart === null ? null : { pid: j.childPid, start: j.childStart };

/** All read/modify/write decisions use the same kernel lock, including CLI adoption.
 * Private methods run inside that transaction. Process termination runs outside it so the
 * script can finish its own records while cancellation waits for the process tree to exit. */
export class BuildStore {
  readonly dir: string;
  readonly procs: Procs;
  private readonly now: () => number;
  private readonly historyLimit: number;

  constructor(opts: Options) {
    this.dir = opts.dir;
    this.procs = opts.procs ?? realProcs;
    this.now = opts.now ?? Date.now;
    this.historyLimit = opts.historyLimit ?? HISTORY_LIMIT;
  }

  private claimPath = (slug: string) => join(claimsDir(this.dir), `${slug}.json`);
  private publicationsPath = () => publicationsFile(this.dir);
  get(id: string) { return readJob(this.dir, id); }

  async snapshot(recentLimit = 20): Promise<Snapshot> {
    const jobs = await listJobs(this.dir);
    return {
      active: jobs.filter(isActive).sort((a, b) => Number(b.state === "running") - Number(a.state === "running") || compareQueue(a, b)),
      recent: jobs.filter(isFinished).sort((a, b) => (b.finishedAt ?? 0) - (a.finishedAt ?? 0) || compareQueue(b, a)).slice(0, recentLimit),
    };
  }

  /** What each document last published. No lock: the file is only ever replaced whole. */
  publications(): Promise<Publications> {
    return readPublications(this.dir);
  }

  private async changePublication(slug: string, change: (p: Publications[string]) => Publications[string]): Promise<number> {
    const all = await this.publications();
    all[slug] = change(all[slug] ?? { generation: 0, publishedAt: null, publishing: null });
    await mkdir(this.dir, { recursive: true });
    await writeAtomic(this.publicationsPath(), `${JSON.stringify(all)}\n`);
    return all[slug].generation;
  }

  /** Run `write`, which changes what the job's document serves (its public files, its audit), and
   *  record it as a new publication, whatever the job's state ends up being. The job is marked as
   *  publishing first, so if its process dies halfway recovery still records the publication.
   *  The generation goes up even when `write` throws: it may have changed some files. */
  async publish(id: string, write: () => Promise<void>): Promise<number> {
    const job = await transaction(this.dir, async () => {
      const job = await this.get(id);
      if (!job || isFinished(job)) throw new Error(`Job ${id} is not building, so it cannot publish.`);
      await this.changePublication(job.slug, (p) => ({ ...p, publishing: job.id }));
      return job;
    });
    let failure: unknown = null;
    try {
      await write();
    } catch (e) {
      failure = e;
    }
    const generation = await transaction(this.dir, () => this.changePublication(job.slug, (p) => ({
      generation: p.generation + 1, publishedAt: this.now(), publishing: p.publishing === job.id ? null : p.publishing,
    })));
    if (failure !== null) throw failure;
    return generation;
  }

  /** Whether dispatch (settle, then fill free page slots) would change anything, read without the
   *  lock, so a runner with nothing to do starts no helper process. The same conditions as settle()
   *  and dispatch(): an active job, a claim or a publication in progress whose process is gone, or a
   *  queued page job while fewer than `concurrency` page jobs run. The job records are always read:
   *  an active job does not always hold its slug's claim (a takeover replaces the claim of a dead
   *  holder, and the claim goes when the new job ends), so a job that never settled would be missed
   *  if the hint looked only at claims and publications. A writer racing this read is seen by the
   *  next pump. */
  async needsTransaction(concurrency: number): Promise<boolean> {
    const claims = (await readdir(claimsDir(this.dir)).catch(() => [] as string[]))
      .filter((n) => n.endsWith(".json") && SLUG_RE.test(n.slice(0, -5)));
    const publishing = Object.values(await this.publications()).flatMap((p) => p.publishing === null ? [] : [p.publishing]);
    const jobs = await listJobs(this.dir);
    const byId = new Map(jobs.map((j) => [j.id, j]));
    const active = jobs.filter(isActive);
    for (const job of active) if (!await this.holds(job)) return true;
    for (const name of claims) {
      const id = await this.claimHolder(name.slice(0, -5));
      if (!await this.holds(id ? byId.get(id) ?? null : null)) return true;
    }
    for (const id of publishing) if (!await this.holds(byId.get(id) ?? null)) return true;
    const running = active.filter((j) => j.who === "ui" && j.state === "running").length;
    return running < concurrency && active.some((j) => j.who === "ui" && j.state === "queued");
  }

  private async childAlive(job: JobRecord): Promise<boolean> {
    const ref = childRef(job);
    return ref !== null && this.procs.isAlive(ref);
  }

  async holds(job: JobRecord | null): Promise<boolean> {
    if (!job) return false;
    // Keep exclusion even for records an older writer ended before its converter stopped.
    if (await this.childAlive(job)) return true;
    if (isFinished(job)) return false;
    if (job.state === "queued" && job.who === "ui") return true;
    const ref = scriptRef(job);
    if (ref) return this.procs.isBuild(ref, job.id, job.who);
    return job.runnerPid !== null && job.runnerStart !== null
      && realProcs.isAlive({ pid: job.runnerPid, start: job.runnerStart });
  }

  private async claimHolder(slug: string): Promise<string | null> {
    try {
      const value: unknown = JSON.parse(await readFile(this.claimPath(slug), "utf8"));
      return value !== null && typeof value === "object" && "job" in value && typeof value.job === "string" ? value.job : null;
    } catch { return null; }
  }

  async enqueue(slug: string, who: Who, fields: Partial<JobRecord> = {}): Promise<EnqueueResult> {
    assertSlug(slug);
    return transaction(this.dir, async () => {
      const id = await this.claimHolder(slug);
      const holder = id ? await this.get(id) : null;
      if (holder && await this.holds(holder)) {
        // One active job per slug. A rerender asked for while a build is active is that build, which
        // applies the decisions too. A build asked for while a page's rerender is still queued turns
        // it into a build (the job keeps its id and its place); while the rerender runs, it is returned.
        if ((fields.kind ?? "build") === "build" && holder.kind === "rerender" && holder.who === "ui" && holder.state === "queued") {
          const turned: JobRecord = { ...holder, kind: "build" };
          await writeJob(this.dir, turned);
          return { job: turned, created: false };
        }
        return { job: holder, created: false };
      }
      const job = newJob(slug, who, this.now(), fields);
      await mkdir(claimsDir(this.dir), { recursive: true });
      // The claim is published first. A crash before the job write leaves a stale claim;
      // a runnable record without its claim is never intentionally published.
      await writeAtomic(this.claimPath(slug), JSON.stringify({ job: job.id }));
      await writeJob(this.dir, job);
      await prune(this.dir, this.historyLimit, this.now());
      return { job, created: true };
    });
  }

  private async change(id: string, patch: Partial<JobRecord>): Promise<JobRecord | null> {
    const job = await this.get(id);
    if (!job || isFinished(job)) return job;
    const next: JobRecord = { ...job, ...patch, id: job.id, slug: job.slug, who: job.who, kind: job.kind };
    if (isFinished(next)) {
      if (await this.childAlive(job)) return job;
      if (next.finishedAt === null) next.finishedAt = this.now();
    }
    await writeJob(this.dir, next);
    if (isFinished(next) && await this.claimHolder(next.slug) === next.id) await rm(this.claimPath(next.slug), { force: true });
    return next;
  }

  update(id: string, patch: Partial<JobRecord>) { return transaction(this.dir, () => this.change(id, patch)); }
  finish(id: string, state: Terminal, error: string | null = null) {
    return this.update(id, { state, error, stage: null, done: null, total: null });
  }

  cancelRequested(id: string) { return cancelRequested(this.dir, id); }

  /** Atomically reserve a global UI slot, spawn, and hand the PID to exactly one child.
   * A child cannot adopt until this transaction ends. A crash before PID publication makes
   * adoption fail, so that child cannot start a converter after recovery frees the slot. */
  dispatch(concurrency: number, spawn: Spawn): Promise<{ job: JobRecord; child: Child }[]> {
    return transaction(this.dir, async () => {
      await this.settle();
      const { active } = await this.snapshot();
      // The lock is held, so what is read here is what the plan is applied to.
      const claims = new Map<string, string>();
      const cancels = new Set<string>();
      for (const job of active.filter((j) => j.who === "ui" && j.state === "queued")) {
        const holder = await this.claimHolder(job.slug);
        if (holder !== null) claims.set(job.slug, holder);
        if (await this.cancelRequested(job.id)) cancels.add(job.id);
      }
      const plan = schedule({ jobs: active, claims, cancelRequested: cancels, concurrency });
      let free = plan.free;
      const started: { job: JobRecord; child: Child }[] = [];
      for (const { job, action } of plan.steps) {
        if (free <= 0) break;
        if (action === "interrupt") {
          await this.change(job.id, { state: "interrupted", error: "The build no longer owns its document claim." });
          continue;
        }
        if (action === "cancel") { await this.change(job.id, { state: "cancelled" }); continue; }
        await this.change(job.id, { state: "running", startedAt: this.now(), runnerPid: process.pid, runnerStart: await realProcs.start(process.pid) });
        try {
          const child = spawn(job);
          if (child.pid === undefined) {
            await child.exited;
            throw new Error("The build program could not be started.");
          }
          await this.change(job.id, { pid: child.pid, pidStart: await this.procs.start(child.pid), runnerPid: null, runnerStart: null });
          started.push({ job, child });
          free--;
        } catch (e) {
          await this.change(job.id, { state: "failed", error: `Could not start the build: ${e instanceof Error ? e.message : String(e)}` });
        }
      }
      return started;
    });
  }

  /** Only the dispatched process may adopt, once, while it still owns the slug. */
  adopt(id: string, slug: string, ref: ProcRef, kind: JobKind = "build"): Promise<JobRecord | null> {
    return transaction(this.dir, async () => {
      const job = await this.get(id);
      if (!job || job.who !== "ui" || job.state !== "running" || job.slug !== slug || job.kind !== kind
        || job.pid !== ref.pid || job.pidStart !== ref.start || await this.claimHolder(slug) !== id
        || await this.cancelRequested(id)) return null;
      return job;
    });
  }

  async cancel(id: string): Promise<{ job: JobRecord | null; outcome: "cancelled" | "stopping" | "finished" | "unknown" }> {
    const before = await transaction(this.dir, async () => {
      const job = await this.get(id);
      if (!job || isFinished(job)) return job;
      // One CLI invocation shares a converter. Make that cancellation scope explicit and
      // mark all its still-active jobs, so none is reported as an unexplained interruption.
      for (const peer of (await listJobs(this.dir)).filter(isActive)) {
        if (peer.id === id || (job.who === "cli" && peer.who === "cli" && peer.pid === job.pid && peer.pidStart === job.pidStart)) {
          await markCancel(this.dir, peer.id);
        }
      }
      if (job.who === "ui" && job.state === "queued") return this.change(id, { state: "cancelled" });
      return job;
    });
    if (!before) return { job: null, outcome: "unknown" };
    if (isFinished(before)) return { job: before, outcome: await this.cancelRequested(id) ? "cancelled" : "finished" };
    const script = scriptRef(before);
    if (script && await this.procs.isBuild(script, id, before.who)) await this.procs.killTree(script);
    // The script may have been killed separately; its recorded converter still owns exclusion.
    const current = await this.get(id);
    const worker = current && childRef(current);
    if (worker && await this.procs.isAlive(worker)) await this.procs.killTree(worker);
    await this.recover();
    const after = await this.get(id);
    return { job: after, outcome: after && isFinished(after) ? "cancelled" : "stopping" };
  }

  recover(): Promise<JobRecord[]> {
    return transaction(this.dir, () => this.settle());
  }

  /** End the jobs whose processes are gone, free their claims, and record the publication of a
   *  publisher that died mid-write. Runs inside a transaction. */
  private async settle(): Promise<JobRecord[]> {
    const changed: JobRecord[] = [];
    for (const job of (await listJobs(this.dir)).filter(isActive)) {
      if (await this.holds(job)) continue;
      const cancelled = await this.cancelRequested(job.id);
      const done = await this.change(job.id, {
        state: cancelled ? "cancelled" : "interrupted",
        error: cancelled ? null : "The build process ended before it finished (the server restarted or the process was stopped).",
      });
      if (done) changed.push(done);
    }
    const claims = await readdir(claimsDir(this.dir)).catch(() => []);
    for (const name of claims.filter((n) => n.endsWith(".json"))) {
      const slug = name.slice(0, -5);
      if (!SLUG_RE.test(slug)) continue;
      const id = await this.claimHolder(slug);
      if (!await this.holds(id ? await this.get(id) : null)) await rm(this.claimPath(slug), { force: true });
    }
    for (const [slug, p] of Object.entries(await this.publications())) {
      if (p.publishing === null || await this.holds(await this.get(p.publishing))) continue;
      await this.changePublication(slug, (q) => ({ generation: q.generation + 1, publishedAt: this.now(), publishing: null }));
    }
    return changed;
  }
}
