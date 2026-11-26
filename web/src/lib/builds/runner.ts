import { spawn as nodeSpawn } from "node:child_process";
import { closeSync, mkdirSync, openSync } from "node:fs";
import { jobsDir } from "../paths";
import { isFinished, logFile } from "./record";
import type { JobKind, JobRecord } from "./view";
import { BuildStore, type EnqueueResult } from "./store";

// The runner: takes the page's queued jobs and runs them, `concurrency` at a time, as
// `nice -n 19 bun scripts/build-docs.ts <slug>` children of the server. It does not do the
// build and does not write progress: scripts/build-docs.ts does, for the page and for the command
// line alike. The runner starts the process, records its pid, notices when it exits, and fills
// the next free slot. Everything it knows is in the build record, so a restart loses nothing.
// See docs/builds.md.

export type Child = { pid: number | undefined; exited: Promise<{ code: number | null; signal: string | null }> };
/** How a job's process is started. Faked in tests. */
export type Spawn = (job: JobRecord) => Child;

export type RunnerOptions = { concurrency: number; spawn: Spawn; pollMs?: number };

export class Runner {
  private pumping: Promise<void> = Promise.resolve();
  private waiting: Promise<void> | undefined;
  private stopped = false;
  /** Set while timer passes keep failing, so a lasting failure is logged once, not every tick. */
  private failing = false;
  private timer: ReturnType<typeof setInterval> | undefined;

  constructor(readonly store: BuildStore, private readonly opts: RunnerOptions) {}

  get concurrency(): number {
    return this.opts.concurrency;
  }

  /** Settle jobs a dead process left running, then run whatever is queued; then again every
   *  `pollMs`, so a build that outlived a restart still frees its slot. The timer is installed
   *  first: if this first pass fails (a slow or failing lock), the next tick tries again. The
   *  returned promise is that first pass; the server does not wait for it. Idempotent. */
  start(): Promise<void> {
    if (this.timer !== undefined) return this.pumping;
    this.stopped = false;
    this.timer = setInterval(() => {
      void this.pump().then(() => { this.failing = false; }, (error) => {
        if (!this.failing) console.error("The build runner's pass failed; it keeps retrying every tick.", error);
        this.failing = true;
      });
    }, this.opts.pollMs ?? 1000);
    this.timer.unref();
    return this.pump();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    clearInterval(this.timer);
    this.timer = undefined;
    await this.pumping;
  }

  /** Add a job for the page. A slug already queued or running gets its existing job back. */
  async enqueue(slug: string, kind: JobKind = "build"): Promise<EnqueueResult> {
    const result = await this.store.enqueue(slug, "ui", { kind });
    if (result.created) await this.pump();
    return result;
  }

  /** Fill the free slots from the front of the queue. Calls are run one after another; callers that
   *  arrive while a pass waits for the current one share that waiting pass, which starts after all
   *  of them asked and so sees whatever each of them wrote. */
  pump(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.waiting) return this.waiting;
    const next = this.pumping.then(() => {
      this.waiting = undefined;
      return this.fill();
    });
    this.waiting = next;
    this.pumping = next.catch(() => {});
    return next;
  }

  private async fill(): Promise<void> {
    // Nothing a transaction would change (idle, or every slot busy with live builds): no lock, so
    // no helper process is started on this tick or this poll.
    if (this.stopped || !(await this.store.needsTransaction(this.opts.concurrency))) return;
    for (const { job, child } of await this.store.dispatch(this.opts.concurrency, this.opts.spawn)) {
      void child.exited.then(async (exit) => {
        const after = await this.store.get(job.id);
        if (after && !isFinished(after) && !(await this.store.holds(after))) {
          if (await this.store.cancelRequested(job.id)) await this.store.finish(job.id, "cancelled");
          else await this.store.finish(job.id, "failed", `The build process exited with ${exit.signal ?? `code ${exit.code}`}.`);
        }
        await this.pump();
      }).catch((error) => console.error("Build exit handling failed", error));
    }
  }

  cancel(id: string) {
    return this.store.cancel(id);
  }
}

/** The arguments of `nice` for a job: `-n 19 <bun> scripts/build-docs.ts --stage-cache [--rerender] <slug>`.
 *  `--stage-cache` reuses the reading of each PDF (OCR, extraction) kept in the main checkout; the
 *  published output is the same (docs/provenance.md). */
export function buildArgs(job: JobRecord, bun: string): string[] {
  return ["-n", "19", bun, "scripts/build-docs.ts", "--stage-cache", ...(job.kind === "rerender" ? ["--rerender"] : []), job.slug];
}

/** The real spawn: `nice -n 19 <bun> scripts/build-docs.ts --stage-cache <slug>` in `web/`, output to the job's log file. */
export function nodeSpawnBuild(opts: { webDir: string; buildsDir: string; env?: NodeJS.ProcessEnv }): Spawn {
  return (job) => {
    mkdirSync(jobsDir(opts.buildsDir), { recursive: true });
    const log = openSync(logFile(opts.buildsDir, job.id), "a");
    const env = opts.env ?? process.env;
    const bun = env.REFLOW_BUILDS_BUN || "bun";
    try {
      const proc = nodeSpawn("nice", buildArgs(job, bun), {
        cwd: opts.webDir,
        env: { ...env, REFLOW_BUILD_JOB: job.id },
        stdio: ["ignore", log, log],
      });
      const exited = new Promise<{ code: number | null; signal: string | null }>((resolve) => {
        proc.once("error", () => resolve({ code: null, signal: null }));
        proc.once("close", (code, signal) => resolve({ code, signal }));
      });
      return { pid: proc.pid, exited };
    } finally {
      closeSync(log); // the child has its own copy
    }
  };
}
