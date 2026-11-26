import { compareQueue, isActive } from "./record";
import type { JobRecord } from "./view";

// The scheduling decision, as a function of plain data. BuildStore.dispatch reads the active jobs,
// the claim of each queued page job's document and the cancel markers, calls schedule(), and
// applies the answer; this file touches no file and no process. See docs/builds.md.

export type ScheduleInput = {
  /** The job records; finished ones are ignored. */
  jobs: readonly JobRecord[];
  /** slug -> the id of the job its claim (claims/<slug>.json) names. A slug with no readable claim is absent. */
  claims: ReadonlyMap<string, string>;
  /** The ids of jobs with a cancel marker. */
  cancelRequested: ReadonlySet<string>;
  /** How many page jobs may run at once. */
  concurrency: number;
};

/** What to do with one queued page job.
 *  - `start`: take its slot and spawn its build.
 *  - `cancel`: a cancel marker waits for it, so end it as cancelled.
 *  - `interrupt`: its claim names another job (or none), so it no longer owns its document. */
export type Step = { job: JobRecord; action: "start" | "cancel" | "interrupt" };

export type Schedule = {
  /** Concurrency minus the page jobs that are running: the slots left to fill. Zero or less when none. */
  free: number;
  /** One step per queued page job, first enqueued first. The shell applies them in order and stops
   *  once `free` starts have succeeded: a spawn that fails frees its slot for the next `start`, so
   *  more `start` steps than `free` may be used, and the steps after the last one applied are
   *  never looked at. Empty when no slot is free. */
  steps: Step[];
};

/** Only page ("ui") jobs take the global slots: a command-line job runs in its own process. A
 *  queued job runs only while it owns its claim, and a cancelled one never starts. */
export function schedule(input: ScheduleInput): Schedule {
  const active = input.jobs.filter(isActive);
  const free = input.concurrency - active.filter((j) => j.who === "ui" && j.state === "running").length;
  if (free <= 0) return { free, steps: [] };
  const steps = active
    .filter((j) => j.who === "ui" && j.state === "queued")
    .sort(compareQueue)
    .map((job): Step => ({
      job,
      action: input.claims.get(job.slug) !== job.id ? "interrupt" : input.cancelRequested.has(job.id) ? "cancel" : "start",
    }));
  return { free, steps };
}
