// The Ollama supervisor (src/lib/ask/ollama-supervisor.ts) and its watchdog (ollama-watchdog.ts)
// against real processes: a fake `ollama` executable on PATH, real signals, real sockets, a real
// `next dev`, and a real terminal (a pty) for Ctrl+C and for closing the window. The unit tests fake
// all of that; this is what proves the shutdown, the port check and the error handling across
// Next's bundles actually work.
//
//   bun run ask:ollama [--port 3457]
//
// Needs `bun run docs:build resnet` (the Next scenarios ask about it). Never port 3000. About
// 35 s. It stops only processes it makes: by their recorded PIDs, including runner
// descendants and watchdogs. Standalone workers probe an unused port; a Node preload rejects
// Next's localhost:11434 probe without opening a socket, so the operator's existing Ollama is never
// contacted or stopped. OLLAMA_HOST is unset. Run it at normal niceness: one check reads the fake
// server's nice value as exactly 10.

import { spawn, type ChildProcess } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
// eslint-disable-next-line no-restricted-imports -- drives the supervisor with real io; the public surface only starts it
import { ollamaSupervisorFor, realSupervisorIo, globalSupervisorState, STOP_GRACE_MS } from "../src/lib/ask/ollama-supervisor";
// eslint-disable-next-line no-restricted-imports -- exercises the real provider against a colliding service
import { createProvider, ProviderUnavailable } from "../src/lib/ask/providers";
import { dataRoot } from "../src/lib/paths";
import { FILE_NAMES } from "../src/lib/published";
import { sleep } from "./lib/net";

const web = resolve(import.meta.dir, "..");
const argv = process.argv.slice(2);
const flag = (name: string): string | undefined => argv[argv.indexOf(name) + 1] && argv.includes(name) ? argv[argv.indexOf(name) + 1] : undefined;

// ---- the fake ollama ------------------------------------------------------------------------

/** A node script named `ollama`. Env: FAKE_LOG (jsonl of what happened), FAKE_IGNORE_TERM=1 (ignore
 *  SIGTERM), FAKE_FAIL_AFTER_MS (exit 1 after that long without serving), FAKE_LISTEN_DELAY_MS (bind
 *  only after that long, as the real one takes a moment to). It binds OLLAMA_HOST
 *  like the real one; a bind error is printed and exits 1, like the real one's. Its spawn event
 *  names its parent, which is the watchdog. */
const FAKE = `#!/usr/bin/env node
const http = require("node:http");
const fs = require("node:fs");
const log = (e) => fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify({ pid: process.pid, ...e }) + "\\n");
log({ event: "spawn", bind: process.env.OLLAMA_HOST, nice: require("node:os").getPriority(0), ppid: process.ppid });
let runner;
const stop = (signal) => {
  log({ event: signal });
  if (process.env.FAKE_IGNORE_TERM === "1") return;
  setTimeout(() => {
    log({ event: "cleanup-runner" });
    if (runner) runner.kill("SIGTERM");
    process.exit(0);
  }, Number(process.env.FAKE_TERM_DELAY_MS || 0));
};
process.on("SIGTERM", () => stop("SIGTERM"));
process.on("SIGINT", () => stop("SIGINT"));
function serve() {
  if (process.env.FAKE_FAIL_AFTER_MS) {
    setTimeout(() => { log({ event: "fail" }); process.exit(1); }, Number(process.env.FAKE_FAIL_AFTER_MS));
    return;
  }
  const [host, port] = process.env.OLLAMA_HOST.split(":");
  const server = http.createServer((req, res) => {
    log({ event: req.method + " " + req.url });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ version: "fake" }));
  });
  server.on("error", (e) => { console.error("listen tcp " + process.env.OLLAMA_HOST + ": bind: " + (e.code === "EADDRINUSE" ? "address already in use" : e.message)); process.exit(1); });
  setTimeout(() => server.listen(Number(port), host, () => console.error("Listening on " + process.env.OLLAMA_HOST)), Number(process.env.FAKE_LISTEN_DELAY_MS || 0));
}
if (process.env.FAKE_RUNNER === "1") {
  runner = require("node:child_process").spawn(process.execPath, ["-e", \`
    const fs = require("node:fs");
    fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify({pid: process.pid, event: "runner", owner: process.ppid}) + "\\\\n");
    process.on("SIGTERM", () => {
      if (process.env.FAKE_RUNNER_IGNORE_TERM !== "1") process.exit(0);
    });
    process.send("ready");
    process.disconnect();
    setInterval(() => {}, 1 << 30);
  \`], {stdio: ["ignore", "ignore", "ignore", "ipc"]});
  runner.once("message", serve);
} else serve();
if (process.env.FAKE_EXIT_AFTER_MS) setTimeout(() => process.exit(1), Number(process.env.FAKE_EXIT_AFTER_MS));
setInterval(() => {}, 1 << 30);
`;

const dir = mkdtempSync(join(tmpdir(), "ask-ollama-real-"));
writeFileSync(join(dir, "ollama"), FAKE);
chmodSync(join(dir, "ollama"), 0o755);
const isolate = join(dir, "isolate.cjs");
writeFileSync(isolate, `
const nativeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
  if (url.hostname === "localhost" && url.port === "11434") {
    console.log("ISOLATED default Ollama probe");
    return Promise.reject(new TypeError("ask:ollama isolated default probe"));
  }
  return nativeFetch(input, init);
};
`);
const noProc = join(dir, "no-proc.cjs");
writeFileSync(noProc, `
const fs = require("node:fs");
const exists = fs.existsSync;
fs.existsSync = (path) => path === "/proc/self/fd" ? false : exists(path);
require("node:module").syncBuiltinESMExports();
`);
// A kernel booted with ipv6.disable=1 has no /proc/net/tcp6.
const noTcp6 = join(dir, "no-tcp6.cjs");
writeFileSync(noTcp6, `
const fs = require("node:fs");
const read = fs.readFileSync;
fs.readFileSync = function (path, ...rest) {
  if (path === "/proc/net/tcp6") throw Object.assign(new Error("ENOENT: no such file or directory, open '/proc/net/tcp6'"), { code: "ENOENT" });
  return read.call(this, path, ...rest);
};
require("node:module").syncBuiltinESMExports();
`);
// In the Next server process only: once STALL_TRIGGER exists, block its event loop for STALL_MS,
// as a big synchronous job does, logging when that starts and (if it lives) when it ends.
const stall = join(dir, "stall.cjs");
writeFileSync(stall, `
const fs = require("node:fs");
if ((process.argv[1] || "").endsWith("/next/dist/server/lib/start-server.js") && process.env.STALL_TRIGGER) {
  const log = (event) => fs.appendFileSync(process.env.STALL_LOG, JSON.stringify({ pid: process.pid, event }) + "\\n");
  log("server");
  const timer = setInterval(() => {
    if (!fs.existsSync(process.env.STALL_TRIGGER)) return;
    clearInterval(timer);
    log("stall-start");
    const end = Date.now() + Number(process.env.STALL_MS);
    while (Date.now() < end) {}
    log("stall-end");
  }, 20);
  timer.unref();
}
`);

// ---- helpers --------------------------------------------------------------------------------

const results: { name: string; ok: boolean; detail: string }[] = [];
const started: ChildProcess[] = [];
/** Every fake server and runner, every watchdog, and every process of a terminal scenario. */
const fakePids = new Set<number>();
const watchdogPids = new Set<number>();
const otherPids = new Set<number>();
const fakeLogs = new Set<string>();
let workerBundle: string;

type Stat = { state: string; ppid: number; pgid: number; sid: number; comm: string };

function stat(pid: number): Stat | undefined {
  try {
    const raw = readFileSync(`/proc/${pid}/stat`, "utf8");
    const f = raw.slice(raw.lastIndexOf(")") + 2).split(" ");
    return { state: f[0], ppid: Number(f[1]), pgid: Number(f[2]), sid: Number(f[3]), comm: raw.slice(raw.indexOf("(") + 1, raw.lastIndexOf(")")) };
  } catch {
    return undefined;
  }
}

function alive(pid: number): boolean {
  const s = stat(pid);
  return s !== undefined && s.state !== "Z";
}

/** SIGKILL to a test process that is still running; it may exit between the check and the kill. */
function killLeft(pid: number) {
  try {
    if (alive(pid)) process.kill(pid, "SIGKILL");
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ESRCH") throw e;
  }
}

/** `root` and every process below it, from /proc. */
function descendants(root: number): number[] {
  const kids = new Map<number, number[]>();
  for (const name of readdirSync("/proc")) {
    if (!/^\d+$/.test(name)) continue;
    const s = stat(Number(name));
    if (s) kids.set(s.ppid, [...(kids.get(s.ppid) ?? []), Number(name)]);
  }
  const out: number[] = [];
  for (const todo = [root]; todo.length; ) {
    const pid = todo.pop()!;
    out.push(pid);
    todo.push(...(kids.get(pid) ?? []));
  }
  return out;
}

async function until(what: string, ms: number, ok: () => boolean | Promise<boolean>): Promise<boolean> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await ok()) return true;
    await sleep(50);
  }
  return false;
}

type Event = { pid: number; event: string; bind?: string; nice?: number; owner?: number; ppid?: number };

function events(log: string): Event[] {
  if (!existsSync(log)) return [];
  return readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

function track(log: string): number[] {
  const all = events(log);
  for (const e of all) if (e.event === "spawn" || e.event === "runner") fakePids.add(e.pid);
  for (const e of all) if (e.event === "spawn" && e.ppid) watchdogPids.add(e.ppid);
  return all.filter((e) => e.event === "spawn").map((e) => e.pid);
}

/** The first server of `log`, its runner and its watchdog. */
function group(log: string) {
  const [server] = track(log);
  const all = events(log);
  return {
    server,
    runner: all.find((e) => e.event === "runner")?.pid,
    watchdog: all.find((e) => e.event === "spawn")?.ppid,
    signalled: (signal: string) => events(log).some((e) => e.pid === server && e.event === signal),
  };
}

const gone = (...pids: (number | undefined)[]) => pids.every((pid) => pid !== undefined && !alive(pid));

function record(name: string, ok: boolean, detail: string) {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `\n        ${detail.replace(/\n/g, "\n        ")}` : ""}`);
}

type Worker = { proc: ChildProcess; lines: string[]; exit: Promise<{ code: number | null; signal: string | null }> };

/** Runs this file as a worker for `scenario`, with the fake ollama first on PATH. `job`: in a process
 *  group of its own, as a shell runs a command. */
async function worker(scenario: string, log: string, extra: Record<string, string> = {}, o: { preload?: string; job?: boolean } = {}): Promise<Worker> {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PATH: `${dir}:${process.env.PATH}`,
    FAKE_LOG: log,
    NODE_ENV: "development",
    ...extra,
  };
  delete env.OLLAMA_HOST;
  delete env.REFLOW_ASK_OLLAMA;
  fakeLogs.add(log);
  const proc = spawn("node", [
    ...(o.preload ? ["--require", o.preload] : []),
    workerBundle, "--worker", scenario, "--unused", String(await realSupervisorIo.freePort()),
  ], {
    env,
    stdio: ["ignore", "pipe", "pipe"],
    detached: o.job ?? false,
  });
  started.push(proc);
  const lines: string[] = [];
  for (const s of [proc.stdout, proc.stderr]) s?.on("data", (b: Buffer) => lines.push(...b.toString().split("\n").filter(Boolean)));
  proc.once("close", () => writeFileSync(`${log}.process.log`, lines.join("\n")));
  const exit = new Promise<{ code: number | null; signal: string | null }>((r) => proc.on("exit", (code, signal) => r({ code, signal })));
  return { proc, lines, exit };
}

const ready = (w: Worker) => until("worker ready", 15_000, () => w.lines.some((l) => l.startsWith("READY ")));

// ---- the workers: the supervisor with real io, in a process of its own -----------------------

async function runWorker(scenario: string, unused: number) {
  const config = { provider: "ollama", model: "m", host: `http://127.0.0.1:${unused}` } as const;
  const log = (line: string) => console.log(`LOG ${line}`);
  let io = realSupervisorIo;
  const squatters: Server[] = [];
  if (scenario === "port-race" || scenario === "no-proc-race") {
    // Another service takes the first port the supervisor picks, right after it is let go.
    let first = true;
    io = {
      ...realSupervisorIo,
      freePort: async () => {
        const port = await realSupervisorIo.freePort();
        if (first) {
          first = false;
          const other = createServer((req, res) => {
            console.log(`UNRELATED ${req.method} ${req.url}`);
            let body = "";
            req.on("data", (chunk: Buffer) => { body += chunk.toString(); });
            req.on("end", () => {
              if (body) console.log(`UNRELATED BODY ${body}`);
              res.writeHead(200, { "content-type": "application/json" });
              res.end(JSON.stringify({ version: "other", message: { content: "wrong-service" } }));
            });
          });
          squatters.push(other);
          await new Promise<void>((r) => other.listen(port, "127.0.0.1", r));
          console.log(`SQUATTER listening on ${port}`);
        }
        return port;
      },
    };
  }
  const supervisor = ollamaSupervisorFor(config, { NODE_ENV: "development" }, log, io)!;
  if (scenario === "no-proc-race" || scenario === "no-proc") {
    try {
      const provider = createProvider(config, { withHost: (signal, run) => supervisor.use(signal, run) });
      const answer = await provider.complete({ system: "test", user: "review-private-passage", schema: {} }, new AbortController().signal);
      console.log(`UNSAFE question result ${answer}`);
      process.exitCode = 1;
    } catch (e) {
      if (!(e instanceof ProviderUnavailable)) throw e;
      console.log(`CLOSED ${e.why} ${e.operatorMessage}`);
    } finally {
      for (const s of squatters) s.close();
      supervisor.stop();
    }
    return;
  }
  // A shell job that outlives its terminal's hangup: only the watchdog can act on the SIGHUP.
  if (scenario === "serve-hup") process.on("SIGHUP", () => console.log("WORKER SIGHUP ignored"));
  const host = await supervisor.use(new AbortController().signal, async (h) => h);
  console.log(`READY ${host} child=${globalSupervisorState().child?.proc.pid}`);
  if (scenario === "exit") process.exit(0);
  if (scenario === "stop-then-exit") {
    supervisor.stop(); // forgotten at once, still dying
    process.exit(0);
  }
  if (scenario === "unexpected-child-exit") {
    if (!await until("child exited", 5_000, () => globalSupervisorState().child === undefined)) throw new Error("child did not exit");
    process.exit(0);
  }
  if (scenario === "port-race") {
    await sleep(200);
    for (const s of squatters) s.close();
    process.exit(0);
  }
  setInterval(() => {}, 1 << 30); // wait for a signal
}

if (argv.includes("--worker")) {
  await runWorker(flag("--worker")!, Number(flag("--unused")));
  // runWorker returns while the interval keeps the process up
} else {
  await main();
}

// ---- the scenarios --------------------------------------------------------------------------

async function main() {
  const t0 = Date.now();
  console.log(`Evidence directory: ${dir}`);
  const build = await Bun.build({
    entrypoints: [import.meta.path],
    outdir: dir,
    target: "node",
    define: { "import.meta.dir": JSON.stringify(import.meta.dir), "import.meta.path": JSON.stringify(import.meta.path) },
  });
  if (!build.success) throw new Error(build.logs.join("\n"));
  workerBundle = join(dir, "ask-ollama-real.js");
  try {
    await scenarios();
  } finally {
    for (const log of fakeLogs) track(log);
    for (const p of started) if (p.pid && p.exitCode === null && p.signalCode === null) p.kill("SIGKILL");
    const mine = [...fakePids, ...watchdogPids, ...otherPids];
    for (const pid of mine) killLeft(pid);
    await until("test processes gone", 3_000, () => mine.every((pid) => !alive(pid)));
    console.log(`Cleanup: ${fakePids.size} recorded server/runner PIDs, ${watchdogPids.size} watchdogs, ${otherPids.size} terminal-scenario PIDs; survivors ${JSON.stringify(mine.filter(alive))}`);
  }
  const bad = results.filter((r) => !r.ok);
  console.log(`\n${results.length - bad.length} of ${results.length} scenarios passed in ${Math.round((Date.now() - t0) / 1000)} s`);
  process.exit(bad.length ? 1 : 0);
}

async function scenarios() {
  // 1. A child that ignores SIGTERM, a worker told to stop: the worker goes at once; its watchdog
  //    SIGTERMs the group, then SIGKILLs it after the grace.
  {
    const log = join(dir, "s1.jsonl");
    const w = await worker("serve", log, { FAKE_IGNORE_TERM: "1" });
    await ready(w);
    const g = group(log);
    const t = Date.now();
    w.proc.kill("SIGTERM");
    const down = await until("group gone", 15_000, () => gone(g.server, g.watchdog));
    const took = Date.now() - t;
    const exit = await Promise.race([w.exit, sleep(10_000).then(() => null)]);
    record(
      "SIGTERM to the server: a child that ignores SIGTERM gets SIGTERM, then SIGKILL after the grace, from its watchdog",
      down && took >= STOP_GRACE_MS - 500 && g.signalled("SIGTERM") && exit?.signal === "SIGTERM",
      `child ${g.server} and watchdog ${g.watchdog} gone: ${down} after ${took} ms; SIGTERM seen: ${g.signalled("SIGTERM")}; worker exit: ${JSON.stringify(exit)}`,
    );
  }

  // 2. process.exit() with a child that ignores SIGTERM: the end of the watchdog's stdin stops it.
  {
    const log = join(dir, "s2.jsonl");
    const w = await worker("exit", log, { FAKE_IGNORE_TERM: "1" });
    const exit = await w.exit;
    const g = group(log);
    const down = await until("group gone", 15_000, () => gone(g.server, g.watchdog));
    record(
      "process.exit(): a child that ignores SIGTERM is SIGTERMed and then SIGKILLed by its watchdog",
      down && g.signalled("SIGTERM") && exit.code === 0,
      `child ${g.server} and watchdog ${g.watchdog} gone: ${down}; worker ${JSON.stringify(exit)}`,
    );
  }

  // 3. A child already being stopped (forgotten, in its kill grace) when the server exits.
  {
    const log = join(dir, "s3.jsonl");
    const w = await worker("stop-then-exit", log, { FAKE_IGNORE_TERM: "1" });
    const exit = await w.exit;
    const g = group(log);
    const down = await until("group gone", 15_000, () => gone(g.server, g.watchdog));
    record("exit during a child's kill grace: the child is still reached and SIGKILLed", down && exit.code === 0, `child ${g.server} and watchdog ${g.watchdog} gone: ${down}; worker ${JSON.stringify(exit)}`);
  }

  // 4. A cooperative child: stopped by SIGTERM alone, and the worker exits on the signal. Also the
  //    layout: the server leads a session of its own; its watchdog stays in the worker's group.
  {
    const log = join(dir, "s4.jsonl");
    const w = await worker("serve", log);
    await ready(w);
    const g = group(log);
    const server = stat(g.server);
    const watchdog = g.watchdog === undefined ? undefined : stat(g.watchdog);
    const caller = stat(w.proc.pid!);
    const layout = server?.pgid === g.server && server?.sid === g.server && watchdog?.comm === "reflow-ollama-w" &&
      watchdog?.ppid === w.proc.pid && watchdog?.pgid === caller?.pgid;
    const t = Date.now();
    w.proc.kill("SIGINT");
    const down = await until("group gone", 10_000, () => gone(g.server, g.watchdog));
    const exit = await Promise.race([w.exit, sleep(5_000).then(() => null)]);
    const mine = events(log).filter((e) => e.pid === g.server);
    const nice = mine.find((e) => e.event === "spawn")?.nice;
    record(
      "SIGINT to the server: a cooperative child (run at nice 10, leading its own session) leaves on SIGTERM",
      down && exit !== null && g.signalled("SIGTERM") && nice === 10 && layout,
      `child ${g.server} nice ${nice} pgid ${server?.pgid} sid ${server?.sid}; watchdog ${g.watchdog} (${watchdog?.comm}) pgid ${watchdog?.pgid} ppid ${watchdog?.ppid}, worker ${w.proc.pid} pgid ${caller?.pgid}; gone after ${Date.now() - t} ms; events ${mine.map((e) => e.event).join(", ")}; worker ${JSON.stringify(exit)}`,
    );
  }

  // 5. Another service takes the chosen port: it must never be used; a new port is tried. Again
  //    without /proc/net/tcp6, as on a kernel with IPv6 disabled: the IPv4 bind needs only
  //    /proc/net/tcp. There each child binds a second late, so the ownership check reads the tables
  //    while the child's PID is known and nothing is bound yet.
  for (const preload of [undefined, noTcp6]) {
    const name = preload ? "no-tcp6" : "s5";
    const log = join(dir, `${name}.jsonl`);
    const w = await worker("port-race", log, preload ? { FAKE_LISTEN_DELAY_MS: "1000" } : {}, { preload });
    await ready(w);
    const exit = await w.exit;
    const pids = track(log);
    const binds = events(log).filter((e) => e.event === "spawn").map((e) => e.bind);
    const ready1 = w.lines.find((l) => l.startsWith("READY ")) ?? "";
    const unrelated = w.lines.filter((l) => l.startsWith("UNRELATED"));
    record(
      preload
        ? "no /proc/net/tcp6 (IPv6 disabled): startup still works, and a port taken before the child binds it is still refused"
        : "a port taken before the child binds it: the other service is not used, a second child is",
      binds.length === 2 && binds[0] !== binds[1] && ready1.includes(String(binds[1]).split(":")[1]) && unrelated.length === 0 && exit.code === 0,
      `spawns ${JSON.stringify(binds)}; ${ready1 || "never ready"}; requests the unrelated service got: ${unrelated.length}`,
    );
    for (const p of pids) killLeft(p);
  }

  // Actual runners ignore SIGTERM so a direct-PID kill cannot accidentally pass these checks.
  for (const scenario of ["exit", "stop-then-exit", "unexpected-child-exit", "serve"]) {
    const log = join(dir, `runner-${scenario}.jsonl`);
    const w = await worker(scenario, log, {
      FAKE_RUNNER: "1", FAKE_RUNNER_IGNORE_TERM: "1", FAKE_TERM_DELAY_MS: "150",
      ...(scenario === "unexpected-child-exit" ? { FAKE_EXIT_AFTER_MS: "1500" } : {}),
    });
    const up = await ready(w);
    if (scenario === "serve") w.proc.kill("SIGTERM");
    const exit = await Promise.race([w.exit, sleep(10_000).then(() => null)]);
    const g = group(log);
    const down = await until("group gone", 10_000, () => gone(g.server, g.runner, g.watchdog));
    record(
      `${scenario}: both the owned server and its SIGTERM-ignoring runner are gone`,
      up && exit !== null && down && events(log).find((e) => e.event === "runner")?.owner === g.server,
      `server ${g.server}, runner ${g.runner}, watchdog ${g.watchdog}, gone ${down}; worker ${JSON.stringify(exit)}`,
    );
  }

  for (const scenario of ["no-proc", "no-proc-race"]) {
    const log = join(dir, `${scenario}.jsonl`);
    const w = await worker(scenario, log, {}, { preload: noProc });
    const exit = await Promise.race([w.exit, sleep(10_000).then(() => null)]);
    track(log);
    const unrelated = w.lines.filter((l) => l.startsWith("UNRELATED"));
    record(
      `${scenario}: missing /proc fails closed before any request or passage reaches the candidate port`,
      exit?.code === 0 && w.lines.some((l) => l.startsWith("CLOSED model-offline") && l.includes("listener ownership")) && unrelated.length === 0 && !w.lines.some((l) => l.startsWith("UNSAFE")) && (scenario !== "no-proc-race" || w.lines.some((l) => l.startsWith("SQUATTER listening"))),
      w.lines.join("\n"),
    );
  }

  // The server dies without running another line (an OOM kill, next dev's SIGKILL after 100 ms):
  // the watchdog sees its stdin end and stops a group that ignores SIGTERM, runner included.
  {
    const log = join(dir, "parent-sigkill.jsonl");
    const w = await worker("serve", log, { FAKE_IGNORE_TERM: "1", FAKE_RUNNER: "1", FAKE_RUNNER_IGNORE_TERM: "1" });
    const up = await ready(w);
    const g = group(log);
    const t = Date.now();
    w.proc.kill("SIGKILL");
    const down = await until("group gone", 15_000, () => gone(g.server, g.runner, g.watchdog));
    const took = Date.now() - t;
    const exit = await Promise.race([w.exit, sleep(5_000).then(() => null)]);
    record(
      "SIGKILL to the server: its watchdog SIGTERMs the group, SIGKILLs server and runner after the grace, and exits",
      up && exit?.signal === "SIGKILL" && down && g.signalled("SIGTERM") && took >= STOP_GRACE_MS - 500,
      `server ${g.server}, runner ${g.runner}, watchdog ${g.watchdog} gone: ${down} after ${took} ms; SIGTERM seen: ${g.signalled("SIGTERM")}; worker ${JSON.stringify(exit)}`,
    );
  }

  // A terminal's SIGHUP to the whole job, with a server that survives it: the group is stopped
  // through the watchdog's own signal, not the end of its stdin, and the server sees the exit.
  {
    const log = join(dir, "job-sighup.jsonl");
    const w = await worker("serve-hup", log, { FAKE_RUNNER: "1", FAKE_RUNNER_IGNORE_TERM: "1" }, { job: true });
    const up = await ready(w);
    const g = group(log);
    const inJob = g.watchdog !== undefined && stat(g.watchdog)?.pgid === w.proc.pid;
    process.kill(-w.proc.pid!, "SIGHUP");
    const down = await until("group gone", 10_000, () => gone(g.server, g.runner, g.watchdog));
    const seen = await until("exit logged", 5_000, () => w.lines.some((l) => l.includes(`ollama serve (pid ${g.server}) exited`)));
    const serverUp = alive(w.proc.pid!) && w.lines.includes("WORKER SIGHUP ignored");
    w.proc.kill("SIGTERM");
    await Promise.race([w.exit, sleep(5_000)]);
    record(
      "SIGHUP to the job's process group: the watchdog stops the group (runner included) while the server stays up and sees the exit",
      up && inJob && down && seen && serverUp && g.signalled("SIGTERM"),
      `job ${w.proc.pid}; watchdog ${g.watchdog} in the job: ${inJob}; server ${g.server}, runner ${g.runner} gone: ${down}; server process still up: ${serverUp}; exit logged: ${seen}`,
    );
  }

  // Through a real `next dev`: the boot start's failure reaches a question as 503 model-offline,
  //       and a child that ignores SIGTERM does not outlive the server.
  const manifest = join(dataRoot(process.env, web), "resnet", FILE_NAMES.manifest);
  if (!existsSync(manifest)) {
    record("next dev scenarios", false, "run `bun run docs:build resnet` first");
    return;
  }
  const version: unknown = JSON.parse(readFileSync(manifest, "utf8")).v;
  if (typeof version !== "string") throw new Error("manifest has no version");
  const port = Number(flag("--port") ?? await realSupervisorIo.freePort());
  if (port === 3000) throw new Error("never port 3000");

  function nextEnv(log: string, extra: Record<string, string>, preloads: string[] = []): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = {
      ...process.env, PATH: `${dir}:${process.env.PATH}`, FAKE_LOG: log, NEXT_TELEMETRY_DISABLED: "1",
      REFLOW_ASK_PROVIDER: "ollama",
      NODE_OPTIONS: [process.env.NODE_OPTIONS ?? "", ...[isolate, ...preloads].map((p) => `--require=${p}`)].join(" "),
      ...extra,
    };
    for (const k of ["OLLAMA_HOST", "REFLOW_ASK_OLLAMA", "NODE_ENV", "REFLOW_ASK_OLLAMA_IDLE_MINUTES"]) delete env[k];
    fakeLogs.add(log);
    return env;
  }
  const nextArgs = ["node_modules/next/dist/bin/next", "dev", "--webpack", "--port", String(port), "--hostname", "127.0.0.1"];

  async function next(name: string, extra: Record<string, string>) {
    const log = join(dir, `${name}.jsonl`);
    const proc = spawn("node", nextArgs, { cwd: web, env: nextEnv(log, extra), stdio: ["ignore", "pipe", "pipe"] });
    started.push(proc);
    const lines: string[] = [];
    for (const s of [proc.stdout, proc.stderr]) s?.on("data", (b: Buffer) => lines.push(...b.toString().split("\n")));
    proc.once("close", () => writeFileSync(`${log}.process.log`, lines.join("\n")));
    const exit = new Promise<{ code: number | null; signal: string | null }>((r) => proc.on("exit", (code, signal) => r({ code, signal })));
    const up = await until("next ready", 60_000, () => lines.some((l) => l.includes("Ready in")));
    return { proc, lines, exit, log, up };
  }

  /** `next dev` typed into an interactive bash on a real pty, as a person runs it; up once the
   *  Ollama it starts answers. */
  async function nextOnPty(name: string, extra: Record<string, string>, preloads: string[] = []) {
    const log = join(dir, `${name}.jsonl`);
    let screen = "";
    const decoder = new TextDecoder();
    const shell = Bun.spawn(["bash", "--norc", "--noprofile", "-i"], {
      cwd: web,
      env: { ...nextEnv(log, extra, preloads), PS1: "$ ", TERM: "xterm" },
      terminal: { cols: 200, rows: 50, data: (_t, d) => void (screen += decoder.decode(d, { stream: true })) },
    });
    otherPids.add(shell.pid);
    shell.terminal!.write(`node ${nextArgs.join(" ")}\n`);
    const up = await until("ollama started", 90_000, () => screen.includes("started Ollama pid"));
    const tree = descendants(shell.pid);
    for (const pid of tree) otherPids.add(pid);
    const launcher = tree.find((pid) => stat(pid)?.ppid === shell.pid);
    const g = group(log);
    const watchdog = g.watchdog === undefined ? undefined : stat(g.watchdog);
    const server = stat(g.server);
    // The watchdog is in the terminal's foreground job; the Ollama server is not.
    const layout = launcher !== undefined && watchdog?.pgid === stat(launcher)?.pgid && server?.sid === g.server && server?.pgid === g.server;
    const isolated = screen.includes("ISOLATED default Ollama probe");
    const save = () => writeFileSync(`${log}.pty.log`, screen);
    return { shell, log, up, tree, g, layout, isolated, save };
  }

  {
    const n = await next("s6", { FAKE_FAIL_AFTER_MS: "3000" });
    const pendingAtReady = n.up && !events(n.log).some((e) => e.event === "fail");
    let status = 0;
    let body = "";
    if (n.up) {
      const res = await fetch(`http://127.0.0.1:${port}/api/ask/resnet`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: "What is residual learning?", version }),
        signal: AbortSignal.timeout(60_000),
      });
      status = res.status;
      body = await res.text();
    }
    n.proc.kill("SIGTERM");
    await Promise.race([n.exit, sleep(15_000)]);
    const pids = track(n.log);
    record(
      "next dev, boot start fails while a question waits: 503 model-offline, not 502 provider-failed",
      pendingAtReady && pids.length === 1 && status === 503 && body === '{"kind":"unavailable","why":"model-offline"}' && n.lines.some((l) => l.includes("ISOLATED default Ollama probe")),
      `Next ready during startup: ${pendingAtReady}; attempts ${pids.length}; status ${status} ${body}`,
    );
    await until("s6 watchdog gone", 10_000, () => [...watchdogPids].every((pid) => !alive(pid)));
  }

  {
    const n = await next("s7", { FAKE_IGNORE_TERM: "1", FAKE_RUNNER: "1", FAKE_RUNNER_IGNORE_TERM: "1" });
    const up = n.up && (await until("child started", 15_000, () => n.lines.some((l) => l.includes("started Ollama pid"))));
    const g = group(n.log);
    n.proc.kill("SIGTERM");
    const exit = await Promise.race([n.exit, sleep(20_000).then(() => null)]);
    const down = await until("group gone", 15_000, () => gone(g.server, g.runner, g.watchdog));
    record(
      "next dev stopped with SIGTERM: neither an uncooperative server nor its runner outlives Next",
      up && exit !== null && down && n.lines.some((l) => l.includes("ISOLATED default Ollama probe")),
      `child ${g.server}, runner ${g.runner}, watchdog ${g.watchdog}, gone ${down}; next exit ${JSON.stringify(exit)}`,
    );
  }

  {
    const n = await next("next-delayed-runner", { FAKE_TERM_DELAY_MS: "150", FAKE_RUNNER: "1", FAKE_RUNNER_IGNORE_TERM: "1" });
    const up = n.up && await until("child started", 15_000, () => n.lines.some((l) => l.includes("started Ollama pid")));
    const g = group(n.log);
    n.proc.kill("SIGTERM");
    const exit = await Promise.race([n.exit, sleep(20_000).then(() => null)]);
    const down = await until("group gone", 10_000, () => gone(g.server, g.runner, g.watchdog));
    record(
      "next dev: forced exit before the server's delayed runner cleanup still kills its whole group",
      up && exit !== null && down && events(n.log).find((e) => e.event === "runner")?.owner === g.server && n.lines.some((l) => l.includes("ISOLATED default Ollama probe")),
      `child ${g.server}, runner ${g.runner}, watchdog ${g.watchdog}, gone ${down}; next exit ${JSON.stringify(exit)}`,
    );
  }

  // The terminal window closes: bash gets SIGHUP and passes it to the job. Next's processes die of
  // it without running any JavaScript; the watchdog, in the same job, stops the Ollama group.
  {
    const t = await nextOnPty("pty-close", { FAKE_RUNNER: "1", FAKE_RUNNER_IGNORE_TERM: "1" });
    const { server, runner, watchdog } = t.g;
    t.shell.terminal!.close();
    const down = await until("everything gone", 15_000, () => gone(server, runner, watchdog, ...t.tree));
    t.save();
    record(
      "a closed terminal (pty hangup): Next, the Ollama server, its SIGTERM-ignoring runner and the watchdog are all gone",
      t.up && t.isolated && t.layout && down && t.g.signalled("SIGTERM"),
      `bash ${t.shell.pid}; tree ${JSON.stringify(t.tree)}; server ${server}, runner ${runner}, watchdog ${watchdog}; watchdog in the job, server in its own session: ${t.layout}; all gone: ${down}; survivors ${JSON.stringify([server, runner, watchdog, ...t.tree].filter((p) => p !== undefined && alive(p)))}`,
    );
  }

  // Ctrl+C while the Next server's event loop is blocked: next dev SIGKILLs it after 100 ms, so its
  // JavaScript never runs again. The tty's SIGINT reaches the watchdog, and its stdin ends.
  {
    const stallLog = join(dir, "pty-ctrl-c.stall.jsonl");
    const trigger = join(dir, "pty-ctrl-c.trigger");
    const t = await nextOnPty(
      "pty-ctrl-c",
      { FAKE_RUNNER: "1", FAKE_RUNNER_IGNORE_TERM: "1", STALL_LOG: stallLog, STALL_TRIGGER: trigger, STALL_MS: "8000" },
      [stall],
    );
    const { server, runner, watchdog } = t.g;
    const stalls = () => (existsSync(stallLog) ? readFileSync(stallLog, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l) as { pid: number; event: string }) : []);
    const nextServer = stalls().find((e) => e.event === "server")?.pid;
    writeFileSync(trigger, "");
    const stalled = await until("server stalled", 10_000, () => stalls().some((e) => e.event === "stall-start"));
    t.shell.terminal!.write("\x03");
    const down = await until("everything gone", 15_000, () => gone(nextServer, server, runner, watchdog));
    const resumed = stalls().some((e) => e.event === "stall-end");
    t.shell.terminal!.close();
    await until("terminal gone", 10_000, () => gone(...t.tree));
    t.save();
    record(
      "Ctrl+C while the Next server is blocked past next dev's 100 ms deadline: the Ollama server, its runner and the watchdog are all gone",
      t.up && t.isolated && t.layout && nextServer !== undefined && stalled && down && !resumed && t.g.signalled("SIGTERM"),
      `next server ${nextServer} stalled: ${stalled}, ran again: ${resumed}; server ${server}, runner ${runner}, watchdog ${watchdog}; all gone: ${down}; survivors ${JSON.stringify([nextServer, server, runner, watchdog].filter((p) => p !== undefined && alive(p)))}`,
    );
  }
}
