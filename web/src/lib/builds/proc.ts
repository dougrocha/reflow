import { spawn } from "node:child_process";
import { processHelper } from "./lock";
import { readFile } from "node:fs/promises";

// What the build code needs to know about processes, from /proc (Linux). A build is found by its
// pid AND its start time: a pid alone can be reused by an unrelated program after the build died.
// Nothing here matches processes by name or pattern to kill them: only the pids of one job's
// process tree are ever signalled.

export type ProcRef = { pid: number; start: number };

export interface Procs {
  /** Start time of a live process (ticks since boot, from /proc), or null when it is gone or a zombie. */
  start(pid: number): Promise<number | null>;
  /** Whether `ref` (pid and start time) is a live process. */
  isAlive(ref: ProcRef): Promise<boolean>;
  /** Whether `ref` is still the build process `jobId` started (alive, same start time, and
   *  scripts/build-docs.ts in its command line; for a job the page started, its job id in the environment). */
  isBuild(ref: ProcRef, jobId: string, who: "ui" | "cli"): Promise<boolean>;
  /** Stop `ref` and every process below it: SIGTERM, then SIGKILL after `graceMs` for the ones that stayed. */
  killTree(ref: ProcRef, graceMs?: number): Promise<void>;
}

/** The fields of /proc/<pid>/stat after the command name: [0] is the state letter (field 3). */
function statFields(text: string): string[] | null {
  const close = text.lastIndexOf(")"); // the command may hold spaces and parentheses
  return close < 0 ? null : text.slice(close + 2).split(" ");
}

async function readStat(pid: number): Promise<{ state: string; ppid: number; start: number } | null> {
  try {
    const f = statFields(await readFile(`/proc/${pid}/stat`, "utf8"));
    if (!f) return null;
    // f[0]=state(3) f[1]=ppid(4) ... starttime is field 22 = f[19]
    return { state: f[0], ppid: Number(f[1]), start: Number(f[19]) };
  } catch {
    return null;
  }
}

async function start(pid: number): Promise<number | null> {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const s = await readStat(pid);
  return s && s.state !== "Z" && s.state !== "X" ? s.start : null;
}

async function isBuild(ref: ProcRef, jobId: string, who: "ui" | "cli"): Promise<boolean> {
  if ((await start(ref.pid)) !== ref.start) return false;
  try {
    const argv = (await readFile(`/proc/${ref.pid}/cmdline`, "utf8")).split("\0");
    if (!argv.some((a) => /(^|\/)build-docs\.ts$/.test(a))) return false;
  } catch {
    return false;
  }
  if (who === "ui") {
    try {
      const env = (await readFile(`/proc/${ref.pid}/environ`, "utf8")).split("\0");
      if (!env.includes(`REFLOW_BUILD_JOB=${jobId}`)) return false;
    } catch {
      return false;
    }
  }
  return true;
}

/** Signals go through Linux pidfds, pinned before validation by the helper. The helper runs in a
 *  session of its own (`detached`), so a Ctrl-C to the terminal's group cannot stop it between
 *  freezing the tree and resuming it; it also ignores INT, TERM and HUP once it has started, and
 *  resumes whatever it froze before it exits. Only SIGKILL can stop it in between: then it runs once
 *  more, which resumes the same tree and stops it. */
async function killTree(root: ProcRef, graceMs = 5000): Promise<void> {
  const killedBy = await stopOnce(root, graceMs);
  if (killedBy === null) return;
  const again = await stopOnce(root, graceMs);
  if (again !== null) throw new Error(`The build stop helper was killed by ${again}, twice.`);
}

/** One run of the stop helper: null when it finished, the signal when something killed it. */
function stopOnce(root: ProcRef, graceMs: number): Promise<NodeJS.Signals | null> {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [processHelper(), "stop", JSON.stringify(root), String(graceMs / 1000)], { stdio: ["ignore", "ignore", "pipe"], detached: true });
    let error = "";
    child.stderr.on("data", (chunk) => { error += String(chunk); });
    child.once("error", reject);
    child.once("close", (code, signal) => {
      if (signal !== null) resolve(signal);
      else if (code === 0) resolve(null);
      else reject(new Error(error || `Build stop helper exited ${code}.`));
    });
  });
}

async function isAlive(ref: ProcRef): Promise<boolean> {
  return (await start(ref.pid)) === ref.start;
}

export const realProcs: Procs = { start, isBuild, killTree, isAlive };
