// What the pages show for the build record, and when they refresh: plain code over the API's data,
// so the wording and the rules are tested and the components only lay them out. No Node or React
// imports: the browser uses this file. The job record's types are here for the same reason; the
// code that reads and writes the record is in record.ts.

import { plural } from "../format";

export const JOB_STATES = ["queued", "running", "done", "failed", "cancelled", "interrupted"] as const;
export type JobState = (typeof JOB_STATES)[number];
export type Who = "ui" | "cli";
/** What a job does: a full build, or applying the recorded decisions to the saved model (`reflow rerender`). */
export const JOB_KINDS = ["build", "rerender"] as const;
export type JobKind = (typeof JOB_KINDS)[number];

export type JobRecord = {
  id: string;
  slug: string;
  who: Who;
  /** A record written before kinds existed has none and is a build. */
  kind: JobKind;
  state: JobState;
  /** The stage reflow is in ("Rendering page images"), or null. */
  stage: string | null;
  /** Units done in this stage (pages, chunks), or null. */
  done: number | null;
  /** Units in this stage; null when not known. */
  total: number | null;
  enqueuedAt: number;
  startedAt: number | null;
  finishedAt: number | null;
  error: string | null;
  /** The build process (scripts/build-docs.ts) and its start time from /proc, or null. */
  pid: number | null;
  pidStart: number | null;
  /** The server process that dispatched a page job (set when the job is taken off the queue, before
   *  the build process exists), so a job whose dispatcher died is not mistaken for a live one. */
  runnerPid: number | null;
  runnerStart: number | null;
  /** The converter the build script started (`reflow convert-many`): when the script is killed alone
   *  it can outlive it, and the slug stays taken while it runs. */
  childPid: number | null;
  childStart: number | null;
};

/** slug -> a publication generation (publications.json). 0, or no entry, is "none yet". */
export type Generations = Record<string, number>;

export type BuildsData = {
  concurrency: number;
  /** Running jobs, then queued ones in order. */
  active: JobRecord[];
  /** The newest finished jobs first. */
  recent: JobRecord[];
  /** When each document was last published (epoch ms), or null. */
  built: Record<string, number | null>;
  /** What each document has published: its generation now, whatever this server's cache holds.
   *  A page whose data was read at another generation is behind (Freshness). */
  published: Generations;
};

/** The lowest generation of each document over several reads: what a page that rendered all of them
 *  is sure to show. */
export function lowest(reads: Generations[]): Generations {
  const out: Generations = {};
  for (const read of reads) for (const [slug, g] of Object.entries(read)) out[slug] = Math.min(out[slug] ?? g, g);
  return out;
}

/** The documents a page shows at another generation than the published one. Only the documents it
 *  rendered count; 0 and no entry are the same. */
export function behind(rendered: Generations, published: Generations): string[] {
  return Object.keys(rendered).filter((slug) => (rendered[slug] || 0) !== (published[slug] ?? 0));
}

/** The query value of a poll that reports stuck documents: `fw9:154,resnet:0`. */
export function behindParam(reported: Generations): string {
  return Object.entries(reported).map(([slug, generation]) => `${slug}:${generation}`).join(",");
}

/** The path of a poll: `/api/builds`, with the report of a page that is stuck when it has one. */
export function pollPath(reported: Generations): string {
  return Object.keys(reported).length ? `/api/builds?behind=${encodeURIComponent(behindParam(reported))}` : "/api/builds";
}

/** What a poll's `behind` query value says, for the slugs `known` accepts. Anything that is not a
 *  `slug:generation` pair of a known slug, and anything past `max` pairs, is ignored. */
export function parseBehind(value: string | null, known: (slug: string) => boolean, max = 100): Generations {
  const out: Generations = {};
  for (const pair of (value ?? "").split(",").slice(0, max)) {
    const colon = pair.lastIndexOf(":");
    const slug = pair.slice(0, colon), text = pair.slice(colon + 1);
    const generation = Number(text);
    if (colon > 0 && /^\d+$/.test(text) && Number.isSafeInteger(generation) && known(slug)) out[slug] = Math.min(out[slug] ?? generation, generation);
  }
  return out;
}

/**
 * When a page refreshes to show what has been published (docs/builds.md, After a build). The page
 * renders the generation each document's data was read at (RenderedFrom); a poll says what is
 * published. While they differ the page is behind, and it refreshes, but only:
 *  - once per poll answer, and once after arriving at another path (a client navigation, which can
 *    show a page from the router's cache);
 *  - never while a refresh is in flight.
 * A refresh that comes back still behind (this server had not marked the data stale yet, or served
 * the stale entry while reading it again) waits for the next poll answer, which tries again; so a
 * stale page heals once the new data is read, and never loops. A page that is current, or shows no document, never refreshes.
 *
 * Some data never comes by waiting: a render that began before a publication and was saved after
 * the mark for it is a fresh cache entry with old data, and the server will not mark again unless it
 * is told. A page that has been behind for STUCK_POLLS poll answers in a row says which documents,
 * and at which generation it rendered them (report()), with its next poll.
 *
 * Each method returns whether to refresh now. After a true, a refresh is in flight until landed().
 */
const QUICK_TRIES = 10;
/** Poll answers in a row that find the page behind before it tells the server. The two refreshes
 *  that are normally answered from an older copy (the server's stale entry, then the browser's own
 *  HTTP cache) are over by then. */
export const STUCK_POLLS = 3;

export class Freshness {
  private published: Generations | null = null;
  private rendered: Generations = {};
  private chance = false;
  private inFlight = false;
  private behindPolls = 0;

  /** A poll answered with what is published. */
  polled(published: Generations): boolean {
    this.published = published;
    this.chance = true;
    this.behindPolls = this.behind ? this.behindPolls + 1 : 0;
    return this.decide();
  }

  /** The page arrived at another path. */
  arrived(): boolean {
    this.chance = true;
    return this.decide();
  }

  /** What the page shows now (its rendered generations changed: a render, a refresh, a navigation). */
  shows(rendered: Generations): boolean {
    this.rendered = rendered;
    return this.decide();
  }

  /** The refresh has landed, or failed. */
  landed(): boolean {
    this.inFlight = false;
    return this.decide();
  }

  get behind(): boolean {
    return this.published !== null && behind(this.rendered, this.published).length > 0;
  }

  /** What to tell the server with the next poll: the documents the page is still behind on after
   *  STUCK_POLLS poll answers, each with the generation the page rendered. Empty otherwise. */
  report(): Generations {
    if (this.published === null || this.behindPolls < STUCK_POLLS) return {};
    return Object.fromEntries(behind(this.rendered, this.published).map((slug) => [slug, this.rendered[slug] || 0]));
  }

  /** Whether to poll at the quick pace: behind, for the first QUICK_TRIES polls of it. The new data
   *  normally arrives within two; a page still behind after that keeps retrying at the idle pace. */
  get hurry(): boolean {
    return this.behind && this.behindPolls <= QUICK_TRIES;
  }

  private decide(): boolean {
    if (!this.chance || this.inFlight || !this.behind) return false;
    this.chance = false;
    this.inFlight = true;
    return true;
  }
}

type ProgressView = { value: number | null; max: number | null; text: string };

type StatusView = {
  tone: "unbuilt" | "built" | "queued" | "running" | "failed" | "stopped";
  /** The one-line status, e.g. "Building: Rendering page images". */
  text: string;
  progress: ProgressView | null;
  /** The job behind it, if any. */
  job: JobRecord | null;
  builtAt: number | null;
};

export function progressOf(job: JobRecord): ProgressView | null {
  if (job.state !== "running") return null;
  if (job.done !== null && job.total !== null && job.total > 0) {
    return { value: Math.min(job.done, job.total), max: job.total, text: `${job.done.toLocaleString("en-US")} of ${job.total.toLocaleString("en-US")}` };
  }
  return { value: null, max: null, text: job.stage ? "In progress" : "Starting" };
}

/** 1-based place among the queued jobs, or null when the job is not queued. */
export function queuePosition(data: BuildsData, job: JobRecord): number | null {
  if (job.state !== "queued") return null;
  const i = data.active.filter((j) => j.state === "queued").findIndex((j) => j.id === job.id);
  return i < 0 ? null : i + 1;
}

/** The job that says what is happening to a document: its queued or running one, else its newest finished one. */
export function latestJob(data: BuildsData, slug: string): JobRecord | null {
  return data.active.find((j) => j.slug === slug) ?? data.recent.find((j) => j.slug === slug) ?? null;
}

export function formatAgo(then: number, now: number): string {
  const s = Math.max(0, Math.round((now - then) / 1000));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return `${plural(d, "day")} ago`;
}

/** What the job does, for the status line: "Building", or "Applying decisions" for a rerender. */
export const doing = (job: Pick<JobRecord, "kind">) => job.kind === "rerender" ? "Applying decisions" : "Building";
const noun = (job: Pick<JobRecord, "kind">) => job.kind === "rerender" ? "Applying decisions" : "Build";
/** The Apply decisions button: busy while any job of the document is active or being asked for, and
 *  then its label says what is going on (a build applies the decisions too). */
export function applyDecisionsView(data: BuildsData, slug: string, asking: boolean): { busy: boolean; label: string } {
  const job = data.active.find((j) => j.slug === slug);
  if (job?.state === "running") return { busy: true, label: job.kind === "rerender" ? "Applying…" : "Building…" };
  if (job) return { busy: true, label: "Queued" };
  return asking ? { busy: true, label: "Starting…" } : { busy: false, label: "Apply decisions" };
}

const STOPPED: Record<string, string> = { cancelled: "cancelled", interrupted: "interrupted" };

export function statusView(data: BuildsData, slug: string, now: number): StatusView {
  const builtAt = data.built[slug] ?? null;
  const job = latestJob(data, slug);
  const base = { job, builtAt, progress: null as ProgressView | null };
  if (job?.state === "queued") {
    const place = queuePosition(data, job);
    return { ...base, tone: "queued", text: place && place > 1 ? `Queued, number ${place}` : "Queued, next" };
  }
  if (job?.state === "running") {
    return { ...base, tone: "running", text: `${doing(job)}${job.stage ? `: ${job.stage}` : ""}`, progress: progressOf(job) };
  }
  // A failed, cancelled or interrupted build is shown until the document is built again.
  if (job && job.state !== "done" && (job.finishedAt ?? 0) >= (builtAt ?? 0)) {
    if (job.state === "failed") return { ...base, tone: "failed", text: `${noun(job)} failed${job.error ? `: ${job.error}` : ""}` };
    return { ...base, tone: "stopped", text: `${noun(job)} ${STOPPED[job.state] ?? "stopped"}` };
  }
  if (builtAt !== null) return { ...base, tone: "built", text: `Built ${formatAgo(builtAt, now)}` };
  return { ...base, tone: "unbuilt", text: "Not built" };
}

const SAID: Record<JobRecord["kind"], Record<string, string>> = {
  build: { queued: "queued", running: "building", done: "built", cancelled: "build cancelled", interrupted: "build interrupted" },
  rerender: {
    queued: "queued to apply decisions", running: "applying decisions", done: "decisions applied",
    cancelled: "applying decisions cancelled", interrupted: "applying decisions interrupted",
  },
};

/**
 * What to announce for a new snapshot: one sentence per job whose state changed since `seen` (job id
 * to state), never for progress, and none for the jobs already there on the first look (`seen`
 * empty and `first`). Updates `seen`.
 */
export function announcements(seen: Map<string, string>, data: BuildsData, first: boolean): string[] {
  const out: string[] = [];
  for (const job of [...data.active, ...data.recent]) {
    const before = seen.get(job.id);
    seen.set(job.id, job.state);
    if (first || before === job.state) continue;
    if (before === undefined && job.state !== "queued" && job.state !== "running" && !isRecent(job)) continue;
    out.push(job.state === "failed" ? `${job.slug}: ${job.kind === "rerender" ? "applying decisions failed" : "build failed"}.${job.error ? ` ${job.error}` : ""}` : `${job.slug}: ${SAID[job.kind][job.state]}.`);
  }
  return out;
}

// A finished job we never saw start only counts if it just ended (a fast build between two polls).
function isRecent(job: JobRecord): boolean {
  return job.finishedAt !== null && Date.now() - job.finishedAt < 15_000;
}
