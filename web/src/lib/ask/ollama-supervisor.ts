// Development convenience: start a local Ollama when the dev server boots (instrumentation.ts), and
// again when a question finds it gone; optionally stop it after a time unused. Used by ask.ts
// alone, only for the ollama provider, only in development. Each server it starts is owned by a
// watchdog process (ollama-watchdog.ts), which stops it when this process ends, however it ends.

import { spawn } from "node:child_process";
import { accessSync, constants, existsSync, readdirSync, readFileSync, readlinkSync } from "node:fs";
import { createServer } from "node:net";
import { delimiter, join } from "node:path";
import { createInterface } from "node:readline";
import type { AskConfig } from "./config";
import { WATCHDOG } from "./ollama-watchdog";
import { excerpt, ProviderUnavailable } from "./providers";

/** The part of a child process the supervisor uses: an `ollama serve` and the watchdog that owns
 *  its process group (ollama-watchdog.ts). */
export interface OllamaChild {
  /** The `ollama serve` process, once the watchdog has started it; undefined until then. */
  readonly pid: number | undefined;
  /** Stops the server's process group, model runners included: SIGTERM, then SIGKILL after
   *  STOP_GRACE_MS. The watchdog does the same by itself when this process ends, however it ends. */
  stop(): void;
  onOutput(cb: (chunk: string) => void): void;
  onError(cb: (err: Error) => void): void;
  /** Once the server has exited and nothing is left of its group (or it never started), or once its
   *  watchdog has gone without stopping it. `how` says which, for the log: "exited (code 1)". */
  onExit(cb: (how: string) => void): void;
}

/** Everything that touches the machine, so tests can fake it. */
export interface SupervisorIo {
  fetch: (url: string, init: { signal: AbortSignal }) => Promise<Response>;
  /** Starts `nice -n 10 ollama serve` with OLLAMA_HOST set to `bind` (host:port), under its watchdog. */
  spawn(bind: string): OllamaChild;
  /** A port nothing is listening on right now (it may be taken before the child binds it). */
  freePort(): Promise<number>;
  /** Whether process `pid` itself holds a listening TCP socket on `port`; undefined if unknown. */
  listening(pid: number | undefined, port: number): boolean | undefined;
  /** Whether `command` is an executable on PATH. */
  onPath(command: string): boolean;
  now(): number;
  sleep(ms: number): Promise<void>;
  /** A timer that does not keep the process alive. */
  setTimer(fn: () => void, ms: number): { cancel(): void };
}

type Spawned = {
  proc: OllamaChild;
  /** Its exit (or failure to start) has been seen. */
  exited: boolean;
  failure?: string;
  output: string[];
};

/** What is told to every caller, here and in every bundle of the server: plain data, never an
 *  Error, because the instrumentation bundle and the route bundle each have their own copy of this
 *  module and of ProviderUnavailable, and `instanceof` fails across them. */
type StartResult = { ok: true; host: string } | StartFailure;
type StartFailure = { ok: false; reason: string; operatorMessage: string };

/** What outlives a module reload: one per process, on globalThis. A child being stopped is not
 *  here: its watchdog owns it until it has gone. */
export type SupervisorState = {
  /** The child questions use. */
  child?: Spawned;
  /** Set once the child answers; unset while it starts. */
  host?: string;
  starting?: Promise<StartResult>;
  idle?: { cancel(): void };
  active: number;
};

export const STARTUP_MS = 20_000;
/** How many times a start may be tried when the port it chose was taken before the child bound it. */
export const MAX_SPAWNS = 3;
const POLL_MS = 250;
const PROBE_MS = 1_000;
/** How long a stopped server has to leave after SIGTERM before its group gets SIGKILL. */
export const STOP_GRACE_MS = 3_000;
const OUTPUT_LINES = 20;
const BIND_FAILED = /address already in use|EADDRINUSE|only one usage of each socket/i;

/** On when NODE_ENV is development or REFLOW_ASK_OLLAMA=auto; REFLOW_ASK_OLLAMA=off turns it off. */
export function ollamaOnDemand(env: Record<string, string | undefined>): boolean {
  const mode = (env.REFLOW_ASK_OLLAMA ?? "").trim().toLowerCase();
  if (mode === "off") return false;
  return mode === "auto" || env.NODE_ENV === "development";
}

/** REFLOW_ASK_OLLAMA_IDLE_MINUTES as milliseconds; unset or not a positive number: never stop. */
function idleMs(raw: string | undefined): number | undefined {
  const text = (raw ?? "").trim();
  const n = Number(text);
  return text !== "" && Number.isFinite(n) && n > 0 ? n * 60_000 : undefined;
}

const newState = (): SupervisorState => ({ active: 0 });

const STATE_KEY = Symbol.for("reflow.ask.ollama-supervisor");

/** The process-wide state, so a reload of this module (Next's dev server does it on every edit)
 *  finds the child that is already running instead of starting a second. */
export function globalSupervisorState(): SupervisorState {
  const g = globalThis as unknown as Record<symbol, SupervisorState | undefined>;
  return (g[STATE_KEY] ??= newState());
}

const failed = (reason: string, operatorMessage?: string): StartFailure => ({
  ok: false,
  reason,
  operatorMessage: operatorMessage ?? `Could not start Ollama: ${reason}. Run \`ollama serve\` yourself, or set OLLAMA_HOST.`,
});

/** Whether a line of /proc/net/tcp is a socket in LISTEN state on `port` whose inode is in `inodes`. */
export function listensOn(table: string, port: number, inodes: ReadonlySet<string>): boolean {
  for (const line of table.split("\n").slice(1)) {
    const f = line.trim().split(/\s+/);
    if (f.length < 10 || f[3] !== "0A") continue;
    if (parseInt(f[1].split(":")[1] ?? "", 16) === port && inodes.has(f[9])) return true;
  }
  return false;
}

export class OllamaSupervisor {
  constructor(
    private readonly o: {
      io: SupervisorIo;
      log: (line: string) => void;
      /** Where a server that is already running is looked for (localhost:11434). */
      defaultHost: string;
      /** Stop the child after this long with no call; undefined: it runs until the process exits. */
      idleMs: number | undefined;
      state: SupervisorState;
    },
  ) {}

  /** Runs `run` with the host of a working Ollama, starting one if none answers. Rejects with the
   *  abort reason when `signal` aborts, with this module's ProviderUnavailable("model-offline") when
   *  none can be had (made here from the shared plain result, not taken from whoever started it).
   *  The idle timer is reset when the call begins and again when it ends. */
  async use<T>(signal: AbortSignal, run: (host: string) => Promise<T>): Promise<T> {
    const s = this.o.state;
    const started = await until(this.ready(), signal);
    if (!started.ok) throw new ProviderUnavailable("model-offline", started.operatorMessage);
    s.active++;
    this.rearm();
    try {
      return await run(started.host);
    } finally {
      s.active--;
      this.rearm();
    }
  }

  /** Starts, or finds, the server now, without waiting for it, and logs which one it is. Never
   *  throws; a failure is logged and the next question tries again. */
  begin(): void {
    const s = this.o.state;
    void this.ready().then((started) => {
      if (!started.ok) this.o.log(`could not start Ollama: ${started.operatorMessage}`);
      else if (!s.child || s.host !== started.host) this.o.log(`using Ollama at ${started.host}`);
    });
  }

  /** Stops the child questions use, if there is one, with its model runners. */
  stop(): void {
    const s = this.o.state;
    if (s.child) this.stopChild(s.child);
  }

  private ready(): Promise<StartResult> {
    const s = this.o.state;
    if (s.child && s.host && !s.child.exited) return Promise.resolve({ ok: true, host: s.host });
    return (s.starting ??= this.start().finally(() => {
      s.starting = undefined;
    }));
  }

  private async start(): Promise<StartResult> {
    const { io, defaultHost } = this.o;
    try {
      if (await this.answers(defaultHost)) return { ok: true, host: defaultHost };
      if (!io.onPath("ollama")) {
        return failed(
          "`ollama` is not on PATH",
          `No Ollama is running at ${defaultHost} and \`ollama\` is not on PATH: install it, or run \`ollama serve\`, or set OLLAMA_HOST.`,
        );
      }
      return await this.spawnAndWait();
    } catch (e) {
      return failed(e instanceof Error ? e.message : String(e));
    }
  }

  /** Spawns until one child owns its port and answers, within STARTUP_MS in all. A child that dies
   *  because its port was taken in the meantime is replaced on a new port, up to MAX_SPAWNS times. */
  private async spawnAndWait(): Promise<StartResult> {
    const began = this.o.io.now();
    let last = failed("no attempt was made");
    for (let attempt = 1; attempt <= MAX_SPAWNS; attempt++) {
      const tried = await this.spawnOnce(began);
      if (tried.ok) return tried;
      last = tried.result;
      if (!tried.retry) break;
      this.o.log(`${last.reason}; trying another port (${attempt}/${MAX_SPAWNS})`);
    }
    return last;
  }

  private async spawnOnce(
    began: number,
  ): Promise<{ ok: true; host: string } | { ok: false; retry: boolean; result: StartFailure }> {
    const { io, log, state: s } = this.o;
    const port = await io.freePort();
    const bind = `127.0.0.1:${port}`;
    const host = `http://${bind}`;
    const child: Spawned = { proc: io.spawn(bind), exited: false, output: [] };
    s.child = child;
    s.host = undefined;
    child.proc.onOutput((chunk) => {
      for (const line of chunk.split("\n")) {
        const text = line.trim();
        if (text) child.output.push(text.slice(0, 300));
      }
      if (child.output.length > OUTPUT_LINES) child.output.splice(0, child.output.length - OUTPUT_LINES);
    });
    child.proc.onError((err) => {
      child.failure = err.message;
      child.exited = true;
      this.forget(child);
    });
    child.proc.onExit((how) => {
      child.failure ??= how;
      child.exited = true;
      if (s.child === child) {
        log(`ollama serve (pid ${child.proc.pid}) ${child.failure}`);
        this.forget(child);
      }
    });

    // Ready is the child itself holding the port and answering on it: an answer alone could come
    // from another service that took the port after freePort() let go of it.
    let why: string;
    let retry = false;
    for (;;) {
      if (child.exited) {
        why = `ollama serve ${child.failure ?? "exited"} before it answered`;
        retry = BIND_FAILED.test(child.output.join("\n"));
        break;
      }
      const listening = io.listening(child.proc.pid, port);
      if (listening === undefined) {
        why = "cannot establish Ollama listener ownership on this host; automatic startup requires readable Linux /proc";
        break;
      }
      if (listening && (await this.answers(host)) && !child.exited) {
        s.host = host;
        log(`started Ollama pid ${child.proc.pid} on ${bind} (answering after ${io.now() - began} ms)`);
        this.rearm();
        return { ok: true, host };
      }
      if (io.now() - began >= STARTUP_MS) {
        why = `ollama serve did not answer ${host}/api/version within ${STARTUP_MS / 1000} s`;
        break;
      }
      await io.sleep(POLL_MS);
    }
    log(`${why}; its last output: ${excerpt(child.output.slice(-10).join("\n"), 1000)}`);
    this.stopChild(child);
    return { ok: false, retry, result: failed(why) };
  }

  private async answers(host: string): Promise<boolean> {
    try {
      const res = await this.o.io.fetch(`${host}/api/version`, { signal: AbortSignal.timeout(PROBE_MS) });
      await res.body?.cancel();
      return res.ok;
    } catch {
      return false;
    }
  }

  /** (Re)starts the idle countdown. Nothing counts down while a call is running. */
  private rearm(): void {
    const s = this.o.state;
    s.idle?.cancel();
    s.idle = undefined;
    if (this.o.idleMs === undefined || !s.child || !s.host || s.active > 0) return;
    const after = this.o.idleMs;
    s.idle = this.o.io.setTimer(() => {
      s.idle = undefined;
      if (!s.child || s.active > 0) return;
      this.o.log(`ollama serve (pid ${s.child.proc.pid}) was idle for ${after / 60_000} min; stopping it`);
      this.stopChild(s.child);
    }, after);
  }

  /** Questions stop using `child`. Its watchdog still owns it until it has exited. */
  private forget(child: Spawned): void {
    const s = this.o.state;
    if (s.child !== child) return;
    s.child = undefined;
    s.host = undefined;
    s.idle?.cancel();
    s.idle = undefined;
  }

  private stopChild(child: Spawned): void {
    this.forget(child);
    if (!child.exited) child.proc.stop();
  }
}

/** `p`, or the abort reason as soon as `signal` aborts. */
function until<T>(p: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    p.then(
      (v) => (signal.removeEventListener("abort", onAbort), resolve(v)),
      (e) => (signal.removeEventListener("abort", onAbort), reject(e)),
    );
  });
}

/** Linux: whether one of pid's fds is a socket in LISTEN on 127.0.0.1:`port`. The child binds IPv4,
 *  so /proc/net/tcp is the table to read, and it must be readable; /proc/net/tcp6 is not read (it
 *  does not exist when IPv6 is disabled). A pid that is gone holds nothing. Missing or unreadable
 *  /proc is unknown: an HTTP answer must never substitute for ownership. */
function ownsListener(pid: number | undefined, port: number): boolean | undefined {
  if (!existsSync("/proc/self/fd")) return undefined;
  if (pid === undefined) return false;
  let fds: string[];
  try {
    fds = readdirSync(`/proc/${pid}/fd`);
  } catch (e) {
    // The watchdog, not this process, reaps the server, so it can be gone before its exit is seen here.
    return (e as NodeJS.ErrnoException).code === "ENOENT" ? false : undefined;
  }
  const inodes = new Set<string>();
  for (const fd of fds) {
    try {
      const m = /^socket:\[(\d+)\]$/.exec(readlinkSync(`/proc/${pid}/fd/${fd}`));
      if (m) inodes.add(m[1]);
    } catch {
      /* closed meanwhile */
    }
  }
  try {
    return listensOn(readFileSync("/proc/net/tcp", "utf8"), port, inodes);
  } catch {
    return undefined;
  }
}

const exited = (code: number | null, signal: string | null) => `exited (${signal ?? `code ${code}`})`;

/** `nice -n 10 ollama serve` under its watchdog (ollama-watchdog.ts), which this process talks to
 *  over the watchdog's stdin and stdout. */
function realChild(bind: string): OllamaChild {
  if (process.platform !== "linux") {
    throw new Error("automatic Ollama startup requires Linux process groups and readable /proc; run `ollama serve` yourself, or set OLLAMA_HOST");
  }
  const watchdog = spawn(process.execPath, ["-e", WATCHDOG, String(STOP_GRACE_MS)], {
    // Not detached: it stays in this server's process group, so a terminal's signals reach it.
    // NODE_OPTIONS is the server's (an inspector port, preloads), which the watchdog must not take.
    env: { ...process.env, OLLAMA_HOST: bind, NODE_OPTIONS: undefined },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let pid: number | undefined;
  let status: { code: number | null; signal: string | null } | undefined;
  const output: ((chunk: string) => void)[] = [];
  const errors: ((err: Error) => void)[] = [];
  // Read always, so the watchdog never waits on a full pipe.
  watchdog.stderr.on("data", (b: Buffer) => {
    for (const cb of output) cb(b.toString("utf8"));
  });
  createInterface({ input: watchdog.stdout }).on("line", (line) => {
    let message: { pid?: unknown; exit?: typeof status; error?: unknown };
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (typeof message.pid === "number") pid = message.pid;
    if (message.exit) status = message.exit;
    if (typeof message.error === "string") for (const cb of errors) cb(new Error(message.error));
  });
  watchdog.on("error", (err) => {
    for (const cb of errors) cb(err);
  });
  // Writing to a watchdog that has exited: its exit is reported on close.
  watchdog.stdin.on("error", () => {});
  return {
    get pid() {
      return pid;
    },
    stop: () => void watchdog.stdin.write("stop\n"),
    onOutput: (cb) => void output.push(cb),
    onError: (cb) => void errors.push(cb),
    // After close, not exit: by then all of the server's output has been read. Without a status the
    // watchdog itself died (SIGKILL), and the server may still be running.
    onExit: (cb) =>
      void watchdog.on("close", (code, signal) => {
        if (status) cb(exited(status.code, status.signal));
        else if (pid === undefined) cb(`was not started: its watchdog ${exited(code, signal)}`);
        else cb(`lost its watchdog, which ${exited(code, signal)}; if it still runs, stop it with \`kill -- -${pid}\``);
      }),
  };
}

/** The real thing; scripts/ask-ollama-real.ts drives it with real processes. */
export const realSupervisorIo: SupervisorIo = {
  fetch: (url, init) => fetch(url, init),
  spawn: realChild,
  freePort: () =>
    new Promise((resolve, reject) => {
      const server = createServer();
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => {
        const address = server.address();
        const port = typeof address === "object" && address ? address.port : 0;
        server.close(() => (port ? resolve(port) : reject(new Error("no free port"))));
      });
    }),
  listening: ownsListener,
  onPath: (command) =>
    (process.env.PATH ?? "").split(delimiter).some((dir) => {
      try {
        accessSync(join(dir || ".", command), constants.X_OK);
        return true;
      } catch {
        return false;
      }
    }),
  now: () => Date.now(),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  setTimer: (fn, ms) => {
    const timer = setTimeout(fn, ms);
    timer.unref();
    return { cancel: () => clearTimeout(timer) };
  },
};

/** The supervisor for `config`, or null when it should not run: the provider is not ollama, it is
 *  off for this environment (see ollamaOnDemand), or OLLAMA_HOST names a server to use as it is.
 *  REFLOW_ASK_OLLAMA_IDLE_MINUTES, when set, is how long the child may sit unused before it is stopped. */
export function ollamaSupervisorFor(
  config: AskConfig,
  env: Record<string, string | undefined>,
  log: (line: string) => void,
  io: SupervisorIo = realSupervisorIo,
  state: SupervisorState = globalSupervisorState(),
): OllamaSupervisor | null {
  if (config.provider !== "ollama" || !ollamaOnDemand(env)) return null;
  if ((env.OLLAMA_HOST ?? "").trim() !== "") return null;
  return new OllamaSupervisor({ io, log, defaultHost: config.host, idleMs: idleMs(env.REFLOW_ASK_OLLAMA_IDLE_MINUTES), state });
}
