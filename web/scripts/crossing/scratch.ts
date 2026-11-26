// The crossing check's scratch tree and processes (crossing-mark.ts). The check publishes over and
// over into the tree its server reads, and builds with `next build`, so it runs on a copy of web/
// in the temp folder, never in the checkout (whose public/docs, .reflow and .next it would rewrite).
// Every child gets an environment built from nothing (childEnv), so no inherited REFLOW_*, GIT_* or
// NODE_OPTIONS can send a write elsewhere. Every child is owned: started in a group of its own,
// identified by pid and start time, and stopped through realProcs.killTree, never by name.
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { chmodSync, closeSync, copyFileSync, cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, openSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { buildsDir, docsRoot, pdfsRoot } from "../../src/lib/paths";
import { auditPath } from "../../src/lib/published";
import { realProcs, type ProcRef } from "../../src/lib/builds/proc";
import { sleep } from "../lib/net";
import { gitCommonDir, mainCheckout } from "../lib/checkout";
import { disableRemark, HarnessError } from "./verdict";

export type Scratch = {
  root: string;
  web: string;
  data: string;
  fixture: string;
  logs: string;
  /** The preload's gate (trace.cjs) and the manifest read it holds. */
  gate: string;
  manifest: string;
  /** 128 random bits: served as /crossing-run.txt, and in every child's CROSSING_RUN. */
  nonce: string;
  /** The checkout under test (the parent of web/). */
  checkout: string;
};

type Phase = "build" | "serve" | "cli" | "browser";

/** Ports the check never uses: the operator's dev server, Ollama, and the shared services. */
const FORBIDDEN_PORTS = [3000, 11434, 11999];
/** The default candidates (3447 is the operator's production server, 3457 ask:ollama, 3417 the Ask docs). */
export const PORTS = Array.from({ length: 19 }, (_, i) => 3481 + i);

export function parsePort(text: string): number {
  const port = Number(text);
  if (!/^\d+$/.test(text) || port < 1024 || port > 65535 || FORBIDDEN_PORTS.includes(port)) {
    throw new HarnessError(`--port ${text}: give a port from 1024 to 65535 other than ${FORBIDDEN_PORTS.join(", ")}`);
  }
  return port;
}

const INHERITED = ["PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ"];

/** A child's whole environment: an allowlist from the parent, then the check's own settings. The three
 *  output roots must resolve inside the scratch tree. */
export function childEnv(s: Scratch, phase: Phase, parent: Record<string, string | undefined> = process.env): Record<string, string> {
  const env: Record<string, string> = {};
  const keep = phase === "browser" ? ["PATH", "HOME", "DISPLAY"] : INHERITED;
  for (const name of keep) if (parent[name] !== undefined) env[name] = parent[name];
  env.CROSSING_RUN = s.nonce;
  if (phase === "browser") return env;
  const roots = {
    REFLOW_DATA_ROOT: s.data,
    REFLOW_DOCS_OUT: docsRoot(s.web),
    REFLOW_PDFS_OUT: pdfsRoot(s.web),
  };
  for (const [name, root] of Object.entries(roots)) {
    if (!realpathSync(root).startsWith(`${s.root}/`)) throw new HarnessError(`${name} ${root} is not inside the scratch tree ${s.root}`);
  }
  Object.assign(env, roots, {
    NEXT_TELEMETRY_DISABLED: "1",
    // Ask must not start Ollama, reach it, or use an Anthropic key.
    REFLOW_ASK_OLLAMA: "off", REFLOW_ASK_PROVIDER: "ollama", OLLAMA_HOST: "127.0.0.1:9",
    GIT_CEILING_DIRECTORIES: dirname(s.root),
  });
  if (phase === "build") env.NODE_OPTIONS = "--max-old-space-size=4096";
  if (phase === "serve") {
    Object.assign(env, {
      REFLOW_BUILDS: "1",
      NODE_OPTIONS: `--require=${join(s.web, "scripts/crossing/trace.cjs")}`,
      CROSSING_GATE: s.gate, CROSSING_MANIFEST: s.manifest,
    });
  }
  if (phase === "cli") Object.assign(env, { PYTHONPATH: join(s.checkout, "src"), CROSSING_FIXTURE: s.fixture });
  return env;
}

/** Skipped by the copy of web/: outputs, dependencies, and (at any depth) .env files. */
const SKIP = new Set(["node_modules", ".next", ".reflow", "public", "tsconfig.tsbuildinfo", ".dev-bg.log"]);

/** Copies the working tree's web/ (committed or not), refusing any symlink in what it copies. */
export function copyWeb(from: string, to: string, top = true): void {
  mkdirSync(to, { recursive: true });
  for (const entry of readdirSync(from, { withFileTypes: true })) {
    if ((top && SKIP.has(entry.name)) || entry.name === "node_modules" || entry.name.startsWith(".env")) continue;
    const src = join(from, entry.name);
    if (lstatSync(src).isSymbolicLink()) throw new HarnessError(`${src} is a symlink; the scratch copy refuses it`);
    if (entry.isDirectory()) copyWeb(src, join(to, entry.name), false);
    else copyFileSync(src, join(to, entry.name));
  }
}

/** git as the harness sees it: no GIT_* variable, so no ceiling and no inherited repository. */
const gitEnv = (): Record<string, string> => ({ PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: process.env.HOME ?? "/" });

/** The main checkout's venv python (the converter's PUBLIC_NAMES read runs on it). */
export function mainPython(checkout: string): string {
  return join(mainCheckout(checkout, gitEnv()), ".venv/bin/python");
}

const audit = (n: number) => `${JSON.stringify({ filename: "fw9.pdf", passed: false, findings: [
  { id: "CROSSING-FIXTURE", title: `crossing fixture audit ${n}`, wcag: "1.3.1", status: "fail", detail: "the seed of the crossing fixture", pages: [] },
] })}\n`;

/** The scratch tree (its layout is at the top of crossing-mark.ts). `control` disables the re-mark
 *  in the copy of system.ts; it returns how often the text matched. */
export function makeScratch(opts: { checkout: string; python: string; tmp?: string; control?: boolean }): { scratch: Scratch; mutations: number } {
  const tmp = realpathSync(opts.tmp ?? tmpdir());
  if (gitCommonDir(tmp, gitEnv()) !== null) throw new HarnessError(`the temp folder ${tmp} is inside a git checkout; set TMPDIR elsewhere`);
  const root = realpathSync(mkdtempSync(join(tmp, "reflow-crossing-")));
  const web = join(root, "web");
  const s: Scratch = {
    root, web, data: join(root, "data"), fixture: join(root, "fixture"), logs: join(root, "logs"),
    gate: join(root, "read-gate.fifo"), manifest: join(web, "public/docs/fw9/manifest.json"),
    nonce: randomBytes(16).toString("hex"), checkout: opts.checkout,
  };
  try {
    return { scratch: s, mutations: fill(s, opts) };
  } catch (e) {
    removeScratch(s);
    throw e;
  }
}

function fill(s: Scratch, opts: { checkout: string; python: string; control?: boolean }): number {
  const { root, web } = s;
  copyWeb(join(opts.checkout, "web"), web);
  // web/src/reflow/react/transcript.css imports the converter's stylesheet from outside web/.
  copyWeb(join(opts.checkout, "src/reflow/viewer"), join(root, "src/reflow/viewer"));
  symlinkSync(realpathSync(join(opts.checkout, "web/node_modules")), join(web, "node_modules"));
  let mutations = 0;
  if (opts.control) {
    const file = join(web, "src/lib/builds/system.ts");
    const patched = disableRemark(readFileSync(file, "utf8"));
    mutations = patched.matches;
    writeFileSync(file, patched.source);
  }
  const template = join(s.fixture, "template/fw9");
  cpSync(join(opts.checkout, "web/scripts/crossing/fixture/fw9"), template, { recursive: true });
  // The seed holds an audit, so every build removes it first and makes three publications.
  cpSync(template, join(s.data, "fw9"), { recursive: true });
  writeFileSync(auditPath(s.data, "fw9"), audit(0));
  cpSync(template, join(docsRoot(web), "fw9"), { recursive: true });
  mkdirSync(pdfsRoot(web));
  writeFileSync(join(web, "public/crossing-run.txt"), s.nonce);
  mkdirSync(join(root, "samples"));
  writeFileSync(join(root, "samples/fw9.pdf"), "%PDF-1.4\n%%EOF\n");
  // build-docs.ts takes its converter from <repo>/.venv/bin; git discovery is pinned to the scratch tree.
  mkdirSync(join(root, ".venv/bin"), { recursive: true });
  copyFileSync(join(opts.checkout, "web/scripts/crossing/fixture-reflow.py"), join(root, ".venv/bin/reflow"));
  writeFileSync(join(root, ".venv/bin/python"), `#!/bin/sh\nexec '${opts.python}' "$@"\n`);
  chmodSync(join(root, ".venv/bin/reflow"), 0o755);
  chmodSync(join(root, ".venv/bin/python"), 0o755);
  mkdirSync(s.logs);
  return mutations;
}

export type Child = ProcRef & { role: string; exited: Promise<number | null> };

/** The processes of one run, and its cleanup. */
export class Owned {
  readonly children: Child[] = [];
  private hooks: (() => unknown)[] = [];
  private cleaning?: Promise<ProcRef[]>;

  constructor(private readonly nonce: string, private readonly logs: string) {}

  /** Runs first in cleanup, before any process is stopped (release the gate, close the browser). */
  before(hook: () => unknown): void {
    this.hooks.push(hook);
  }

  /** Starts `cmd` in a process group of its own, output to <logs>/<role>.log. `exited` resolves with
   *  its exit code (null for a signal), or rejects after `timeoutMs`. */
  async own(role: string, cmd: string, args: string[], opts: { cwd: string; env: Record<string, string>; timeoutMs?: number }): Promise<Child> {
    if (this.cleaning) throw new HarnessError("the run is stopping");
    const fd = openSync(join(this.logs, `${role}.log`), "a");
    const child = spawn(cmd, args, { cwd: opts.cwd, env: opts.env as unknown as NodeJS.ProcessEnv, detached: true, stdio: ["ignore", fd, fd] });
    closeSync(fd);
    const exit = new Promise<number | null>((resolve) => child.once("exit", (code) => resolve(code)));
    await new Promise<void>((resolve, reject) => {
      child.once("spawn", resolve);
      child.once("error", (e) => reject(new HarnessError(`${role}: could not start ${cmd}: ${e.message}`)));
    });
    const start = await realProcs.start(child.pid!);
    if (start === null) throw new HarnessError(`${role}: exited before it could be identified`);
    const exited = opts.timeoutMs === undefined ? exit : Promise.race([exit, new Promise<never>((_, reject) => {
      const timer = setTimeout(() => reject(new HarnessError(`${role} ran longer than ${opts.timeoutMs! / 1000} s`)), opts.timeoutMs);
      void exit.then(() => clearTimeout(timer));
    })]);
    exited.catch(() => {}); // a caller that stopped waiting must not crash the run
    const owned = { role, pid: child.pid!, start, exited };
    this.children.push(owned);
    return owned;
  }

  /** Once, however often it is called: the hooks, then every owned tree stopped (newest first), then
   *  every process left with this run's nonce. Resolves with the processes that survived. */
  cleanup(): Promise<ProcRef[]> {
    return (this.cleaning ??= (async () => {
      for (const hook of this.hooks) {
        try { await hook(); } catch (e) { console.error(`cleanup: ${String(e)}`); }
      }
      for (const c of [...this.children].reverse()) await realProcs.killTree(c, 5000);
      for (const p of await withNonce(this.nonce)) await realProcs.killTree(p, 2000);
      return withNonce(this.nonce);
    })());
  }
}

/** This user's processes whose environment holds CROSSING_RUN=<nonce>: descendants of the run that
 *  outlived their parent. */
export async function withNonce(nonce: string): Promise<ProcRef[]> {
  const found: ProcRef[] = [];
  for (const name of await readdir("/proc")) {
    if (!/^\d+$/.test(name)) continue;
    const pid = Number(name);
    try {
      if (!(await readFile(`/proc/${pid}/environ`, "utf8")).split("\0").includes(`CROSSING_RUN=${nonce}`)) continue;
    } catch { continue; } // gone, or another user's
    const start = await realProcs.start(pid);
    if (start !== null) found.push({ pid, start });
  }
  return found;
}

/** Whether nothing visibly listens on 127.0.0.1:port. Only a hint: the server's own bind is the claim. */
function looksFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = createServer();
    probe.once("error", () => resolve(false));
    probe.listen(port, "127.0.0.1", () => probe.close(() => resolve(true)));
  });
}

/** Starts the server (`command(port)`) on the first candidate it can bind, and accepts it only when
 *  the owned child is alive and GET /crossing-run.txt answers this run's nonce. A child that exits
 *  with EADDRINUSE moves on to the next candidate (three tries at most). */
export async function claimServer(owned: Owned, s: Scratch, ports: number[], command: (port: number) => { cmd: string; args: string[]; env: Record<string, string> }, readyMs = 60_000): Promise<{ port: number; base: string; server: Child }> {
  let tries = 0;
  for (const port of ports) {
    if (FORBIDDEN_PORTS.includes(port) || !(await looksFree(port))) continue;
    if (++tries > 3) break;
    const { cmd, args, env } = command(port);
    const role = `server-${port}`;
    const server = await owned.own(role, cmd, args, { cwd: s.web, env });
    let exited = false;
    void server.exited.then(() => { exited = true; });
    const base = `http://127.0.0.1:${port}`;
    const deadline = Date.now() + readyMs;
    while (!exited && Date.now() < deadline) {
      try {
        const answer = await fetch(`${base}/crossing-run.txt`, { signal: AbortSignal.timeout(2000) });
        if (answer.ok && (await answer.text()).trim() === s.nonce && await realProcs.isAlive(server)) return { port, base, server };
      } catch { /* not up yet */ }
      await sleep(200);
    }
    if (!exited) throw new HarnessError(`the server on ${port} did not answer with this run's nonce within ${readyMs / 1000} s`);
    const log = readFileSync(join(s.logs, `${role}.log`), "utf8");
    if (!log.includes("EADDRINUSE")) throw new HarnessError(`the server on ${port} exited before it was ready:\n${log.slice(-2000)}`);
  }
  throw new HarnessError(`no free port among ${ports.join(", ")} (three tries at most)`);
}

/** Copies the run's logs, the converter's calls and the build record into `out`. */
export function saveLogs(s: Scratch, out: string): void {
  mkdirSync(out, { recursive: true });
  for (const [from, to] of [[s.logs, "logs"], [join(s.fixture, "calls.log"), "calls.log"], [buildsDir(s.data), "builds"]]) {
    if (existsSync(from)) cpSync(from, join(out, to), { recursive: true });
  }
}

export function removeScratch(s: Scratch): void {
  rmSync(s.root, { recursive: true, force: true });
}
