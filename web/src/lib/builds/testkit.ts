import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Procs, ProcRef } from "./proc";
import type { Child, Spawn } from "./runner";
import type { JobRecord } from "./view";

// Fakes for the build tests: processes that are numbers in a map, and a spawn that starts nothing.

export async function tempDir(): Promise<string> {
  return mkdtemp(join(tmpdir(), "reflow-builds-test-"));
}
export const removeDir = (dir: string) => rm(dir, { recursive: true, force: true });

export class FakeProcs implements Procs {
  /** pid -> start time of the processes that are alive. */
  alive = new Map<number, number>();
  /** pids that count as the build (the rest are alive but something else). */
  builds = new Set<number>();
  /** Every ref killTree was called with. */
  killed: ProcRef[] = [];
  /** What killTree does besides recording: by default the process dies. */
  onKill: (ref: ProcRef) => void | Promise<void> = (ref) => void this.alive.delete(ref.pid);

  add(pid: number, start = pid * 10, isBuild = true): ProcRef {
    this.alive.set(pid, start);
    if (isBuild) this.builds.add(pid);
    return { pid, start };
  }
  async start(pid: number) {
    return this.alive.get(pid) ?? null;
  }
  async isAlive(ref: ProcRef) { return this.alive.get(ref.pid) === ref.start; }
  async isBuild(ref: ProcRef) {
    return this.alive.get(ref.pid) === ref.start && this.builds.has(ref.pid);
  }
  async killTree(ref: ProcRef) {
    this.killed.push(ref);
    await this.onKill(ref);
  }
}

type FakeChild = Child & { job: JobRecord; exit: (code?: number | null, signal?: string | null) => void };

/** A Spawn whose children are controlled by the test. Pids count up from 1000. */
export function fakeSpawn(procs: FakeProcs, opts: { failToStart?: boolean } = {}) {
  const children: FakeChild[] = [];
  let next = 1000;
  const spawn: Spawn = (job) => {
    if (opts.failToStart) throw new Error("spawn bun ENOENT");
    const pid = next++;
    procs.add(pid);
    let resolve!: (v: { code: number | null; signal: string | null }) => void;
    const exited = new Promise<{ code: number | null; signal: string | null }>((r) => (resolve = r));
    const child: FakeChild = {
      pid, exited, job,
      exit: (code = 0, signal = null) => {
        procs.alive.delete(pid);
        resolve({ code, signal });
      },
    };
    children.push(child);
    return child;
  };
  return { spawn, children };
}

export const tick = () => new Promise((r) => setTimeout(r, 200));
