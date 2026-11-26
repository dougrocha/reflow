import { randomBytes } from "node:crypto";
import { mkdir, readdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { isFiniteNumber, isRecord } from "../guards";
import { SLUG_RE } from "../../pipelines";
import { jobsDir, publicationsFile } from "../paths";
import { JOB_KINDS, JOB_STATES, type JobRecord, type JobState, type Who } from "./view";

// The build record: one JSON file per job under <builds dir>/jobs/, each written atomically
// (a temp file, then rename), so a reader sees the old file or the new one and never half of one.
// See docs/builds.md.

const TERMINAL: readonly JobState[] = ["done", "failed", "cancelled", "interrupted"];
export const isFinished = (job: Pick<JobRecord, "state">): boolean => TERMINAL.includes(job.state);
export const isActive = (job: Pick<JobRecord, "state">): boolean => !isFinished(job);

const ID_RE = /^[a-z0-9]{6,12}-[0-9a-f]{8}$/;
export const isJobId = (id: unknown): id is string => typeof id === "string" && ID_RE.test(id);

/** Sorts by time, so the order of ids is the order of creation. */
function newJobId(now = Date.now()): string {
  return `${now.toString(36).padStart(9, "0")}-${randomBytes(4).toString("hex")}`;
}

export function newJob(slug: string, who: Who, now: number, fields: Partial<JobRecord> = {}): JobRecord {
  return {
    id: newJobId(now), slug, who, kind: "build", state: "queued", stage: null, done: null, total: null,
    enqueuedAt: now, startedAt: null, finishedAt: null, error: null, pid: null, pidStart: null,
    runnerPid: null, runnerStart: null, childPid: null, childStart: null, ...fields,
  };
}

const jobFile = (dir: string, id: string) => join(jobsDir(dir), `${id}.json`);
const cancelFile = (dir: string, id: string) => join(jobsDir(dir), `${id}.cancel`);
export const logFile = (dir: string, id: string) => join(jobsDir(dir), `${id}.log`);

const num = (v: unknown): number | null => (isFiniteNumber(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

/** A record from parsed JSON, or null when it is not one. Unknown fields are dropped. */
export function parseJob(raw: unknown): JobRecord | null {
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Record<string, unknown>;
  if (!isJobId(r.id) || typeof r.slug !== "string" || !SLUG_RE.test(r.slug)) return null;
  if (r.who !== "ui" && r.who !== "cli") return null;
  if (!JOB_STATES.includes(r.state as JobState)) return null;
  const kind = r.kind === undefined ? "build" : JOB_KINDS.find((k) => k === r.kind);
  if (kind === undefined) return null;
  const enqueuedAt = num(r.enqueuedAt);
  if (enqueuedAt === null) return null;
  return {
    id: r.id, slug: r.slug, who: r.who, kind, state: r.state as JobState,
    stage: str(r.stage), done: num(r.done), total: num(r.total), enqueuedAt,
    startedAt: num(r.startedAt), finishedAt: num(r.finishedAt), error: str(r.error),
    pid: num(r.pid), pidStart: num(r.pidStart), runnerPid: num(r.runnerPid), runnerStart: num(r.runnerStart),
    childPid: num(r.childPid), childStart: num(r.childStart),
  };
}

/** Write a file so a reader never sees half of it. */
export async function writeAtomic(path: string, text: string): Promise<void> {
  const tmp = `${path}.${process.pid}.${randomBytes(3).toString("hex")}.tmp`;
  try {
    await writeFile(tmp, text);
    await rename(tmp, path);
  } catch (e) {
    await rm(tmp, { force: true });
    throw e;
  }
}

export async function writeJob(dir: string, job: JobRecord): Promise<void> {
  await mkdir(jobsDir(dir), { recursive: true });
  await writeAtomic(jobFile(dir, job.id), `${JSON.stringify(job)}\n`);
}

export async function readJob(dir: string, id: string): Promise<JobRecord | null> {
  if (!isJobId(id)) return null;
  try {
    return parseJob(JSON.parse(await readFile(jobFile(dir, id), "utf8")));
  } catch {
    return null;
  }
}

/** Every readable job, oldest first by (enqueuedAt, id). A file that does not parse is skipped. */
export async function listJobs(dir: string): Promise<JobRecord[]> {
  let names: string[];
  try {
    names = await readdir(jobsDir(dir));
  } catch {
    return [];
  }
  const jobs = await Promise.all(
    names.filter((n) => n.endsWith(".json")).map((n) => readJob(dir, n.slice(0, -".json".length))),
  );
  return jobs.filter((j): j is JobRecord => j !== null).sort(compareQueue);
}

export const compareQueue = (a: JobRecord, b: JobRecord): number =>
  a.enqueuedAt - b.enqueuedAt || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

async function removeJob(dir: string, id: string): Promise<void> {
  if (!isJobId(id)) return;
  for (const file of [jobFile(dir, id), cancelFile(dir, id), logFile(dir, id)]) await rm(file, { force: true });
}

/** Ask for a job to be cancelled. A marker file of its own, so no progress write can overwrite it.
 *  Created exclusively (`wx`): it never writes through a symlink that someone put in its place. */
export async function markCancel(dir: string, id: string): Promise<void> {
  await mkdir(jobsDir(dir), { recursive: true });
  try {
    await writeFile(cancelFile(dir, id), "", { flag: "wx" });
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
  }
}

export async function cancelRequested(dir: string, id: string): Promise<boolean> {
  try {
    await stat(cancelFile(dir, id));
    return true;
  } catch {
    return false;
  }
}

/** Delete the files of jobs past the newest `keep` finished ones, and temp files a crash left.
 *  Nothing else depends on old jobs: what a document last published is in publications.json. */
export async function prune(dir: string, keep: number, now: number): Promise<void> {
  const finished = (await listJobs(dir)).filter(isFinished);
  for (const job of finished.slice(0, Math.max(0, finished.length - keep))) await removeJob(dir, job.id);
  let names: string[] = [];
  try {
    names = await readdir(jobsDir(dir));
  } catch {
    return;
  }
  for (const name of names.filter((n) => n.endsWith(".tmp"))) {
    const path = join(jobsDir(dir), name);
    try {
      if (now - (await stat(path)).mtimeMs > 60_000) await rm(path, { force: true });
    } catch { /* gone already */ }
  }
}

/** What a document last published, one entry per slug in <builds dir>/publications.json. Written
 *  under the coordination lock by BuildStore.publish and recovery; read without it. */
export type Publication = {
  /** Goes up by one with every publication of the document (public files or audit). */
  generation: number;
  publishedAt: number | null;
  /** The job whose write is in progress (it may be half done), else null. */
  publishing: string | null;
};
export type Publications = Record<string, Publication>;

/** What each document last published (<builds dir>/publications.json), or {} when nothing has.
 *  No lock: the file is only ever replaced whole. */
export async function readPublications(dir: string): Promise<Publications> {
  try {
    return parsePublications(JSON.parse(await readFile(publicationsFile(dir), "utf8")));
  } catch {
    return {};
  }
}

function parsePublications(raw: unknown): Publications {
  const out: Publications = {};
  if (!isRecord(raw)) return out;
  for (const [slug, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!SLUG_RE.test(slug) || typeof value !== "object" || value === null) continue;
    const v = value as Record<string, unknown>;
    const generation = num(v.generation);
    if (generation === null || !Number.isInteger(generation) || generation < 0) continue;
    out[slug] = { generation, publishedAt: num(v.publishedAt), publishing: isJobId(v.publishing) ? v.publishing : null };
  }
  return out;
}
