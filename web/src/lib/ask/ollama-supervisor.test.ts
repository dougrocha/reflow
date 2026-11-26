import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { createServer, type AddressInfo } from "node:net";
import { Script } from "node:vm";
import { createProvider, ProviderUnavailable } from "./providers";
import {
  globalSupervisorState,
  listensOn,
  MAX_SPAWNS,
  ollamaOnDemand,
  ollamaSupervisorFor,
  OllamaSupervisor,
  realSupervisorIo,
  STARTUP_MS,
  type OllamaChild,
  type SupervisorIo,
  type SupervisorState,
} from "./ollama-supervisor";
import { WATCHDOG } from "./ollama-watchdog";

const DEFAULT_HOST = "http://localhost:11434";
const OLLAMA = { provider: "ollama", model: "gemma4:e4b", host: DEFAULT_HOST } as const;
const MIN = 60_000;
const live = () => new AbortController().signal;

class FakeChild implements OllamaChild {
  readonly pid: number;
  /** How many times its watchdog was asked to stop it. */
  stops = 0;
  private out: ((c: string) => void)[] = [];
  private exits: ((how: string) => void)[] = [];
  constructor(readonly bind: string, pid: number, private readonly onDeath: (c: FakeChild) => void) {
    this.pid = pid;
  }
  stop() {
    this.stops++;
    if (!this.lingers) this.die(null, "SIGTERM");
  }
  /** Still shutting down after stop(), until die(). */
  lingers = false;
  dead = false;
  die(code: number | null, signal: string | null) {
    if (this.dead) return;
    this.dead = true;
    this.onDeath(this);
    for (const cb of this.exits) cb(`exited (${signal ?? `code ${code}`})`);
  }
  say(text: string) {
    for (const cb of this.out) cb(text);
  }
  onOutput(cb: (c: string) => void) { this.out.push(cb); }
  onError() {}
  onExit(cb: (how: string) => void) { this.exits.push(cb); }
}

function rig(o: { external?: boolean; hasBinary?: boolean; bootMs?: number | null; squat?: number[] } = {}) {
  let now = 0;
  let nextPort = 41000;
  let nextPid = 100;
  const timers: { at: number; fn: () => void; live: boolean }[] = [];
  /** Hosts that answer /api/version. */
  const up = new Set<string>(o.external ? [DEFAULT_HOST] : []);
  const children: FakeChild[] = [];
  const probes: string[] = [];
  /** "127.0.0.1:port" of every child that holds its socket. */
  const listening = new Set<string>();
  const log: string[] = [];
  const io: SupervisorIo = {
    fetch: (async (url: string) => {
      probes.push(url);
      const host = url.replace(/\/api\/version$/, "");
      if (!up.has(host)) throw new TypeError("fetch failed");
      return new Response("{}", { status: 200 });
    }) as unknown as typeof fetch,
    spawn: (bind) => {
      const port = Number(bind.split(":")[1]);
      // Someone else took the port between freePort() and the child's bind: it answers, the child cannot bind.
      const squatted = (o.squat ?? []).includes(port);
      const child = new FakeChild(bind, nextPid++, (c) => {
        listening.delete(c.bind);
        if (!squatted) up.delete(`http://${c.bind}`);
      });
      children.push(child);
      if (squatted) {
        up.add(`http://${bind}`);
        timers.push({
          at: now + 50,
          live: true,
          fn: () => {
            child.say(`listen tcp ${bind}: bind: address already in use\n`);
            child.die(1, null);
          },
        });
      } else if (o.bootMs !== null && o.bootMs !== undefined) {
        timers.push({
          at: now + o.bootMs,
          live: true,
          fn: () => {
            if (child.dead) return;
            up.add(`http://${bind}`);
            listening.add(bind);
          },
        });
      }
      return child;
    },
    freePort: async () => nextPort++,
    listening: (_pid, port) => listening.has(`127.0.0.1:${port}`),
    onPath: () => o.hasBinary ?? true,
    now: () => now,
    sleep: (ms) => new Promise((resolve) => timers.push({ at: now + ms, live: true, fn: resolve })),
    setTimer: (fn, ms) => {
      const t = { at: now + ms, fn, live: true };
      timers.push(t);
      return { cancel: () => void (t.live = false) };
    },
  };
  const flush = () => new Promise<void>((r) => setImmediate(r));
  /** Moves the fake clock forward, running every timer that falls due, in order. */
  async function advance(ms: number) {
    const target = now + ms;
    await flush();
    for (;;) {
      const due = timers.filter((t) => t.live && t.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      due.live = false;
      now = Math.max(now, due.at);
      due.fn();
      await flush();
    }
    now = target;
    await flush();
  }
  const state: SupervisorState = { active: 0 };
  const make = (idleMs?: number) =>
    new OllamaSupervisor({ io, log: (l) => log.push(l), defaultHost: DEFAULT_HOST, idleMs, state });
  return { io, up, children, probes, log, advance, state, make, opts: o };
}

/** Runs the call, advancing the clock until it settles (a cold start waits on the fake clock). */
async function settle<T>(r: ReturnType<typeof rig>, p: Promise<T>, ms = 30_000): Promise<PromiseSettledResult<T>> {
  const out = p.then(
    (value) => ({ status: "fulfilled", value }) as const,
    (reason) => ({ status: "rejected", reason }) as const,
  );
  let done: PromiseSettledResult<T> | undefined;
  void out.then((v) => (done = v));
  for (let waited = 0; !done && waited < ms; waited += 250) await r.advance(250);
  return done ?? out;
}

const hostOf = (s: OllamaSupervisor, signal = live()) => s.use(signal, async (h) => h);

describe("ollamaOnDemand", () => {
  test("on in development and with REFLOW_ASK_OLLAMA=auto; off in production, unset, or off", () => {
    expect(ollamaOnDemand({ NODE_ENV: "development" })).toBe(true);
    expect(ollamaOnDemand({ NODE_ENV: "production", REFLOW_ASK_OLLAMA: "auto" })).toBe(true);
    expect(ollamaOnDemand({ REFLOW_ASK_OLLAMA: " AUTO " })).toBe(true);
    expect(ollamaOnDemand({ NODE_ENV: "production" })).toBe(false);
    expect(ollamaOnDemand({})).toBe(false);
    expect(ollamaOnDemand({ NODE_ENV: "development", REFLOW_ASK_OLLAMA: "off" })).toBe(false);
    expect(ollamaOnDemand({ NODE_ENV: "production", REFLOW_ASK_OLLAMA: "off" })).toBe(false);
  });
});

describe("ollamaSupervisorFor", () => {
  const r = rig();
  const make = (env: Record<string, string>, config = OLLAMA) =>
    ollamaSupervisorFor(config, env, () => {}, r.io, r.state);

  test("is made in development for ollama", () => {
    expect(make({ NODE_ENV: "development" })).not.toBeNull();
  });
  test("is not made in production, or with REFLOW_ASK_OLLAMA=off", () => {
    expect(make({ NODE_ENV: "production" })).toBeNull();
    expect(make({ NODE_ENV: "development", REFLOW_ASK_OLLAMA: "off" })).toBeNull();
  });
  test("is not made when OLLAMA_HOST is set, so it never spawns beside a named server", () => {
    expect(make({ NODE_ENV: "development", OLLAMA_HOST: "127.0.0.1:11969" })).toBeNull();
    expect(make({ REFLOW_ASK_OLLAMA: "auto", OLLAMA_HOST: "gpu-box" })).toBeNull();
  });
  test("is not made for the anthropic provider", () => {
    expect(make({ NODE_ENV: "development" }, { provider: "anthropic", model: "m", apiKey: "k" } as never)).toBeNull();
  });
});

describe("OllamaSupervisor", () => {
  test("reuses a server that answers on localhost:11434 and never spawns", async () => {
    const r = rig({ external: true });
    const s = r.make();
    expect(await hostOf(s)).toBe(DEFAULT_HOST);
    expect(await hostOf(s)).toBe(DEFAULT_HOST);
    expect(r.children).toHaveLength(0);
    expect(r.probes.every((u) => u === `${DEFAULT_HOST}/api/version`)).toBe(true);
    // An external server is never stopped, however long it sits.
    await r.advance(60 * MIN);
    expect(r.children).toHaveLength(0);
  });

  test("an external server that goes away is noticed on the next question", async () => {
    const r = rig({ external: true, bootMs: 500 });
    const s = r.make();
    expect(await hostOf(s)).toBe(DEFAULT_HOST);
    r.up.delete(DEFAULT_HOST);
    const second = await settle(r, hostOf(s));
    expect(second).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41000" });
    expect(r.children).toHaveLength(1);
  });

  test("spawns on a free port when nothing answers, waits for /api/version, then runs the call there", async () => {
    const r = rig({ bootMs: 3_000 });
    const s = r.make();
    const seen: string[] = [];
    const out = await settle(r, s.use(live(), async (h) => (seen.push(h), "done")));
    expect(out).toEqual({ status: "fulfilled", value: "done" });
    expect(r.children).toHaveLength(1);
    expect(r.children[0].bind).toBe("127.0.0.1:41000");
    expect(seen).toEqual(["http://127.0.0.1:41000"]);
    expect(r.probes).toContain("http://127.0.0.1:41000/api/version");
    // The default host was looked at first.
    expect(r.probes[0]).toBe(`${DEFAULT_HOST}/api/version`);
  });

  test("the call does not run until the server answers", async () => {
    const r = rig({ bootMs: 5_000 });
    const s = r.make();
    let ran = false;
    const p = s.use(live(), async () => (ran = true));
    await r.advance(4_000);
    expect(ran).toBe(false);
    await r.advance(1_500);
    await p;
    expect(ran).toBe(true);
  });

  test("a second question reuses the child it started", async () => {
    const r = rig({ bootMs: 1_000 });
    const s = r.make();
    await settle(r, hostOf(s));
    expect(await hostOf(s)).toBe("http://127.0.0.1:41000");
    expect(r.children).toHaveLength(1);
  });

  test("concurrent cold starts share one spawn", async () => {
    const r = rig({ bootMs: 2_000 });
    const s = r.make();
    const all = Promise.all([hostOf(s), hostOf(s), hostOf(s)]);
    const out = await settle(r, all);
    expect(out).toEqual({ status: "fulfilled", value: ["http://127.0.0.1:41000", "http://127.0.0.1:41000", "http://127.0.0.1:41000"] });
    expect(r.children).toHaveLength(1);
  });

  test("a start that times out is model-offline, logs the reason and the child's output, and stops the child", async () => {
    const r = rig({ bootMs: null });
    const s = r.make();
    const p = s.use(live(), async () => "never");
    const out = await settle(r, p, STARTUP_MS + 5_000);
    expect(out.status).toBe("rejected");
    const err = (out as PromiseRejectedResult).reason as ProviderUnavailable;
    expect(err).toBeInstanceOf(ProviderUnavailable);
    expect(err.why).toBe("model-offline");
    expect(r.children).toHaveLength(1);
    expect(r.children[0].stops).toBe(1);
    expect(r.log.join("\n")).toContain("did not answer");
    expect(r.state.child).toBeUndefined();
  });

  test("the log of a failed start carries the last lines of the child's output", async () => {
    const r = rig({ bootMs: null });
    const s = r.make();
    const p = s.use(live(), async () => "never");
    await r.advance(10);
    r.children[0].say("Error: listen tcp: address already in use\n");
    const out = await settle(r, p, STARTUP_MS + 5_000);
    expect(out.status).toBe("rejected");
    expect(r.log.join("\n")).toContain("address already in use");
  });

  test("a child that exits before it answers fails the start at once", async () => {
    const r = rig({ bootMs: null });
    const s = r.make();
    const p = s.use(live(), async () => "never");
    await r.advance(300);
    r.children[0].die(1, null);
    const out = await settle(r, p, 2_000);
    expect(out.status).toBe("rejected");
    expect(((out as PromiseRejectedResult).reason as ProviderUnavailable).why).toBe("model-offline");
    expect(r.log.join("\n")).toContain("code 1");
  });

  test("after a failed start the next question tries again", async () => {
    const r = rig({ bootMs: null });
    const s = r.make();
    await settle(r, s.use(live(), async () => 0), STARTUP_MS + 5_000);
    expect(r.children).toHaveLength(1);
    // Now the server comes up in time.
    r.opts.bootMs = 300;
    expect(await settle(r, hostOf(s))).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41001" });
    expect(r.children).toHaveLength(2);
  });

  test("ollama missing from PATH is model-offline with an operator message, and nothing is spawned", async () => {
    const r = rig({ hasBinary: false });
    const out = await settle(r, hostOf(r.make()));
    expect(out.status).toBe("rejected");
    const err = (out as PromiseRejectedResult).reason as ProviderUnavailable;
    expect(err.why).toBe("model-offline");
    expect(err.operatorMessage).toContain("not on PATH");
    expect(r.children).toHaveLength(0);
  });

  test("stops the child after the idle time, and the next question starts a new one", async () => {
    const r = rig({ bootMs: 500 });
    const s = r.make(10 * MIN);
    await settle(r, hostOf(s));
    const first = r.children[0];
    await r.advance(10 * MIN - 1_000);
    expect(first.stops).toBe(0);
    await r.advance(2_000);
    expect(first.stops).toBe(1);
    expect(r.state.child).toBeUndefined();

    expect(await settle(r, hostOf(s))).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41001" });
    expect(r.children).toHaveLength(2);
    expect(r.children[1].stops).toBe(0);
  });

  test("every question that reaches the provider resets the idle timer", async () => {
    const r = rig({ bootMs: 500 });
    const s = r.make(10 * MIN);
    await settle(r, hostOf(s));
    await r.advance(9 * MIN);
    await hostOf(s);
    await r.advance(9 * MIN);
    expect(r.children[0].stops).toBe(0);
    await r.advance(2 * MIN);
    expect(r.children[0].stops).toBe(1);
  });

  test("a call that outlasts the idle time is not cut off", async () => {
    const r = rig({ bootMs: 500 });
    const s = r.make(1 * MIN);
    let release!: () => void;
    const hold = new Promise<void>((res) => (release = res));
    const p = s.use(live(), () => hold);
    await settle(r, Promise.resolve());
    await r.advance(5 * MIN);
    expect(r.children[0].stops).toBe(0);
    release();
    await p;
    await r.advance(MIN + 1_000);
    expect(r.children[0].stops).toBe(1);
  });

  test("a reloaded module (a new supervisor over the same global state) reuses the running child", async () => {
    const r = rig({ bootMs: 500 });
    await settle(r, hostOf(r.make()));
    const reloaded = r.make();
    expect(await hostOf(reloaded)).toBe("http://127.0.0.1:41000");
    expect(r.children).toHaveLength(1);
  });

  test("a reload during a cold start joins the start in progress", async () => {
    const r = rig({ bootMs: 2_000 });
    const a = hostOf(r.make());
    await r.advance(100);
    const b = hostOf(r.make());
    expect(await settle(r, Promise.all([a, b]))).toEqual({
      status: "fulfilled",
      value: ["http://127.0.0.1:41000", "http://127.0.0.1:41000"],
    });
    expect(r.children).toHaveLength(1);
  });

  test("a child that dies on its own is replaced by the next question", async () => {
    const r = rig({ bootMs: 500 });
    const s = r.make();
    await settle(r, hostOf(s));
    r.children[0].die(137, null);
    expect(r.state.child).toBeUndefined();
    expect(await settle(r, hostOf(s))).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41001" });
    expect(r.children).toHaveLength(2);
  });

  test("an idle stop asks the child's watchdog once; a child still shutting down holds up neither the next start nor the new child", async () => {
    const r = rig({ bootMs: 500 });
    const s = r.make(MIN);
    await settle(r, hostOf(s));
    r.children[0].lingers = true;
    await r.advance(MIN + 1_000);
    expect(r.children[0].stops).toBe(1);
    expect(r.state.child).toBeUndefined();
    expect(await settle(r, hostOf(s))).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41001" });
    r.children[0].die(null, "SIGKILL");
    expect(r.state.child?.proc).toBe(r.children[1]);
    expect(r.state.host).toBe("http://127.0.0.1:41001");
    expect(r.log.join("\n")).not.toContain(`pid ${r.children[0].pid}) exited`);
    s.stop();
    expect(r.children.map((c) => c.stops)).toEqual([1, 1]);
  });

  test("a caller that goes away gets the abort reason; the start carries on for the others", async () => {
    const r = rig({ bootMs: 2_000 });
    const s = r.make();
    const gone = new AbortController();
    const a = s.use(gone.signal, async () => "a");
    const b = hostOf(s);
    await r.advance(100);
    gone.abort(new Error("gone"));
    await expect(a).rejects.toThrow("gone");
    expect(await settle(r, b)).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41000" });
    expect(r.children).toHaveLength(1);
  });

  test("a start nobody waits for any more still gets an idle stop", async () => {
    const r = rig({ bootMs: 2_000 });
    const s = r.make(MIN);
    const gone = new AbortController();
    const a = s.use(gone.signal, async () => "a");
    await r.advance(100);
    gone.abort(new Error("gone"));
    await a.catch(() => {});
    await r.advance(3_000);
    expect(r.state.host).toBe("http://127.0.0.1:41000");
    await r.advance(MIN + 100);
    expect(r.children[0].stops).toBe(1);
  });
});

describe("with the ollama provider", () => {
  test("posts to the host the supervisor chose, and the idle timer sees the call", async () => {
    const r = rig({ bootMs: 500 });
    const s = r.make();
    const urls: string[] = [];
    const provider = createProvider(OLLAMA, {
      withHost: (signal, run) => s.use(signal, run),
      fetch: (async (url: string) => {
        urls.push(url);
        return Response.json({ message: { content: "{}" } });
      }) as unknown as typeof fetch,
    });
    const out = await settle(r, provider.complete({ system: "s", user: "u", schema: {} }, live()));
    expect(out).toEqual({ status: "fulfilled", value: "{}" });
    expect(urls).toEqual(["http://127.0.0.1:41000/api/chat"]);
  });

  test("a model that is not pulled stays model-offline and names ollama pull", async () => {
    const r = rig({ external: true });
    const s = r.make();
    const provider = createProvider(OLLAMA, {
      withHost: (signal, run) => s.use(signal, run),
      fetch: (async () => Response.json({ error: "model 'gemma4:e4b' not found" }, { status: 404 })) as unknown as typeof fetch,
    });
    const err = await provider.complete({ system: "s", user: "u", schema: {} }, live()).catch((e) => e);
    expect(err).toBeInstanceOf(ProviderUnavailable);
    expect(err.why).toBe("model-offline");
    expect(err.operatorMessage).toContain("ollama pull gemma4:e4b");
  });
});

describe("started at boot (begin)", () => {
  const forEnv = (r: ReturnType<typeof rig>, env: Record<string, string>) =>
    ollamaSupervisorFor(OLLAMA, { NODE_ENV: "development", ...env }, (l) => r.log.push(l), r.io, r.state)!;

  test("spawns before any question, without waiting for the server to answer", async () => {
    const r = rig({ bootMs: 5_000 });
    const s = forEnv(r, {});
    expect(s.begin()).toBeUndefined();
    await r.advance(10);
    expect(r.children).toHaveLength(1);
    expect(r.state.host).toBeUndefined();
    expect(r.log).toEqual([]);
    await r.advance(6_000);
    expect(r.state.host).toBe("http://127.0.0.1:41000");
    expect(r.log).toHaveLength(1);
    expect(r.log[0]).toMatch(/^started Ollama pid 100 on 127\.0\.0\.1:41000/);
  });

  test("logs which server it is using when one answers on 11434, and spawns nothing", async () => {
    const r = rig({ external: true });
    forEnv(r, {}).begin();
    await r.advance(10);
    expect(r.log).toEqual([`using Ollama at ${DEFAULT_HOST}`]);
    expect(r.children).toHaveLength(0);
  });

  test("a question that arrives during the boot start shares it", async () => {
    const r = rig({ bootMs: 3_000 });
    const boot = forEnv(r, {});
    boot.begin();
    await r.advance(500);
    // The module reloads, or the route's own supervisor, meanwhile.
    const asked = hostOf(forEnv(r, {}));
    expect(await settle(r, asked)).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41000" });
    expect(r.children).toHaveLength(1);
    expect(r.log).toHaveLength(1);
  });

  test("a question after the boot start reuses the child", async () => {
    const r = rig({ bootMs: 500 });
    forEnv(r, {}).begin();
    await r.advance(2_000);
    expect(await hostOf(forEnv(r, {}))).toBe("http://127.0.0.1:41000");
    expect(r.children).toHaveLength(1);
  });

  test("a boot start that fails is logged, never thrown, and the next question tries again", async () => {
    const r = rig({ hasBinary: false });
    forEnv(r, {}).begin();
    await r.advance(10);
    expect(r.log.join("\n")).toContain("could not start Ollama");
    expect(r.log.join("\n")).toContain("not on PATH");
    r.io.onPath = () => true;
    r.opts.bootMs = 300;
    expect(await settle(r, hostOf(forEnv(r, {})))).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41000" });
  });

  test("a child that died is restarted by the next question, not at once", async () => {
    const r = rig({ bootMs: 500 });
    const s = forEnv(r, {});
    s.begin();
    await r.advance(2_000);
    r.children[0].die(1, null);
    await r.advance(60_000);
    expect(r.children).toHaveLength(1);
    expect(await settle(r, hostOf(s))).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41001" });
  });

  test("with no idle setting the child runs until the process exits", async () => {
    const r = rig({ bootMs: 500 });
    const s = forEnv(r, {});
    s.begin();
    await r.advance(2_000);
    await hostOf(s);
    await r.advance(24 * 60 * MIN);
    expect(r.children[0].stops).toBe(0);
    expect(r.state.child).toBeDefined();
  });

  test("unset, empty, zero and non-numeric REFLOW_ASK_OLLAMA_IDLE_MINUTES never stop it", async () => {
    for (const value of ["", "0", "-3", "soon"]) {
      const r = rig({ bootMs: 500 });
      const s = forEnv(r, { REFLOW_ASK_OLLAMA_IDLE_MINUTES: value });
      s.begin();
      await r.advance(2_000);
      await r.advance(60 * MIN);
      expect(r.children[0].stops).toBe(0);
    }
  });

  test("REFLOW_ASK_OLLAMA_IDLE_MINUTES opts in to the idle stop, from the boot start on", async () => {
    const r = rig({ bootMs: 500 });
    const s = forEnv(r, { REFLOW_ASK_OLLAMA_IDLE_MINUTES: "2" });
    s.begin();
    await r.advance(2_000);
    await r.advance(MIN);
    expect(r.children[0].stops).toBe(0);
    await r.advance(MIN);
    expect(r.children[0].stops).toBe(1);
    // and the next question starts it again
    expect(await settle(r, hostOf(s))).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41001" });
  });
});

describe("the start result is plain data", () => {
  test("a failed start is told to every caller as a plain object, not as an Error of this module's class", async () => {
    const r = rig({ bootMs: null });
    const s = r.make();
    const joined = s.use(live(), async () => "never").catch((e) => e);
    await r.advance(10);
    const shared = r.state.starting!;
    await r.advance(STARTUP_MS + 1_000);
    const told = await shared;
    expect(told).not.toBeInstanceOf(Error);
    expect(Object.getPrototypeOf(told)).toBe(Object.prototype);
    expect(told.ok).toBe(false);
    expect(told).toEqual({ ok: false, reason: expect.stringContaining("did not answer"), operatorMessage: expect.stringContaining("Could not start Ollama") });
    // A caller turns it into its own ProviderUnavailable.
    const err = await joined;
    expect(err).toBeInstanceOf(ProviderUnavailable);
    expect(err.why).toBe("model-offline");
  });

  test("a question that joins a boot start which fails is model-offline", async () => {
    const r = rig({ bootMs: null });
    const boot = r.make();
    boot.begin();
    await r.advance(100);
    const asked = await settle(r, r.make().use(live(), async () => "never"), STARTUP_MS + 1_000);
    expect(asked.status).toBe("rejected");
    expect(((asked as PromiseRejectedResult).reason as ProviderUnavailable).why).toBe("model-offline");
  });

  test("an unexpected failure while starting (no free port) is model-offline too, and never throws out of begin()", async () => {
    const r = rig();
    r.io.freePort = async () => {
      throw new Error("no free port");
    };
    const s = r.make();
    s.begin();
    await r.advance(10);
    expect(r.log.join("\n")).toContain("no free port");
    const err = await s.use(live(), async () => 0).catch((e) => e);
    expect(err).toBeInstanceOf(ProviderUnavailable);
  });
});

describe("a port taken before the child binds it", () => {
  test("unknown listener ownership fails closed without probing the candidate or running the question", async () => {
    const r = rig({ bootMs: 0 });
    r.io.listening = () => undefined;
    const hosts: string[] = [];
    const out = await settle(r, r.make().use(live(), async (host) => hosts.push(host)));
    expect(out.status).toBe("rejected");
    if (out.status !== "rejected") throw new Error("expected a setup failure");
    expect(out.reason).toBeInstanceOf(ProviderUnavailable);
    expect(out.reason.why).toBe("model-offline");
    expect(r.probes).toEqual([`${DEFAULT_HOST}/api/version`]);
    expect(hosts).toEqual([]);
    expect(r.log.join("\n")).toContain("cannot establish Ollama listener ownership");
    expect(r.children).toHaveLength(1);
    expect(r.children[0].stops).toBe(1);
    expect(r.state.child).toBeUndefined();
  });

  test("the answer of another service on that port is not taken for the child; a new port is tried", async () => {
    const r = rig({ bootMs: 500, squat: [41000] });
    const s = r.make();
    const out = await settle(r, hostOf(s));
    expect(out).toEqual({ status: "fulfilled", value: "http://127.0.0.1:41001" });
    expect(r.children.map((c) => c.bind)).toEqual(["127.0.0.1:41000", "127.0.0.1:41001"]);
    // Nothing was ever asked of the squatter after it answered the first probe's turn.
    expect(r.log.join("\n")).toContain("trying another port (1/");
    expect(r.state.child?.proc).toBe(r.children[1]);
  });

  test("the call never runs against the other service, even while it answers", async () => {
    const r = rig({ bootMs: 500, squat: [41000] });
    const hosts: string[] = [];
    await settle(r, r.make().use(live(), async (h) => hosts.push(h)));
    expect(hosts).toEqual(["http://127.0.0.1:41001"]);
  });

  test("when every port is taken it gives up after MAX_SPAWNS and is model-offline", async () => {
    const r = rig({ squat: [41000, 41001, 41002, 41003] });
    const out = await settle(r, hostOf(r.make()));
    expect(out.status).toBe("rejected");
    expect(((out as PromiseRejectedResult).reason as ProviderUnavailable).why).toBe("model-offline");
    expect(r.children).toHaveLength(MAX_SPAWNS);
    expect(r.state.child).toBeUndefined();
  });

  test("a child that exits for another reason is not retried", async () => {
    const r = rig({ bootMs: null });
    const p = r.make().use(live(), async () => 0);
    await r.advance(200);
    r.children[0].say("panic: something else\n");
    r.children[0].die(2, null);
    const out = await settle(r, p, 2_000);
    expect(out.status).toBe("rejected");
    expect(r.children).toHaveLength(1);
  });

  test("the retries share the one startup budget", async () => {
    const r = rig({ bootMs: null });
    const p = r.make().use(live(), async () => 0);
    await r.advance(15_000);
    r.children[0].say("bind: address already in use\n");
    r.children[0].die(1, null);
    const out = await settle(r, p, STARTUP_MS);
    expect(out.status).toBe("rejected");
    // The second child had only what was left of the 20 s.
    expect(r.children).toHaveLength(2);
    expect(r.children[1].stops).toBe(1);
  });

  test("a child that does not hold the port is not ready however well the port answers", async () => {
    const r = rig({ bootMs: null });
    r.up.add("http://127.0.0.1:41000"); // an unrelated service on the port the child was given
    const out = await settle(r, hostOf(r.make()), STARTUP_MS + 1_000);
    expect(out.status).toBe("rejected");
    expect(((out as PromiseRejectedResult).reason as ProviderUnavailable).why).toBe("model-offline");
    expect(r.children[0].stops).toBe(1);
  });
});

describe("listensOn", () => {
  const table = [
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
    "   0: 0100007F:B2D5 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 100 0 0 10 0",
    "   1: 0100007F:B2D5 0100007F:9999 01 00000000:00000000 00:00000000 00000000  1000        0 777 1 0000000000000000 100 0 0 10 0",
    "   2: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 555 1 0000000000000000 100 0 0 10 0",
  ].join("\n");
  test("is true for a LISTEN socket on the port whose inode the process holds", () => {
    expect(listensOn(table, 0xb2d5, new Set(["12345"]))).toBe(true);
    expect(listensOn(table, 8080, new Set(["555"]))).toBe(true);
  });
  test("is false for another process's socket on that port, a connected socket, or another port", () => {
    expect(listensOn(table, 0xb2d5, new Set(["999"]))).toBe(false);
    expect(listensOn(table, 0xb2d5, new Set(["777"]))).toBe(false);
    expect(listensOn(table, 8080, new Set(["12345"]))).toBe(false);
    expect(listensOn("", 80, new Set(["1"]))).toBe(false);
  });
});

describe.if(process.platform === "linux")("listener ownership, read from this machine's /proc", () => {
  test("a socket this process listens on is its own; another port is not", async () => {
    const server = createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    try {
      expect(realSupervisorIo.listening(process.pid, port)).toBe(true);
      expect(realSupervisorIo.listening(process.pid, port === 65535 ? 1024 : port + 1)).toBe(false);
    } finally {
      server.close();
    }
  });

  test("a process that is gone (its watchdog reaped it before its exit is seen here) holds nothing; that is not unknown", () => {
    const beyond = Number(readFileSync("/proc/sys/kernel/pid_max", "utf8")) + 1;
    expect(realSupervisorIo.listening(beyond, 80)).toBe(false);
  });
});

describe("stopping: the watchdog owns the rest", () => {
  test("nothing is installed on the process: no exit or signal listener", async () => {
    const events = ["exit", "SIGINT", "SIGTERM", "SIGHUP"] as const;
    const before = events.map((e) => process.listenerCount(e));
    const r = rig({ bootMs: 300 });
    const s = r.make(MIN);
    await settle(r, hostOf(s));
    await r.advance(MIN + 1_000);
    expect(events.map((e) => process.listenerCount(e))).toEqual(before);
  });

  test("a child still shutting down after a failed start is forgotten at once and asked to stop once", async () => {
    const r = rig({ bootMs: null });
    const p = r.make().use(live(), async () => "never");
    await r.advance(100);
    r.children[0].lingers = true;
    const out = await settle(r, p, STARTUP_MS + 1_000);
    expect(out.status).toBe("rejected");
    expect(r.children[0].stops).toBe(1);
    expect(r.state.child).toBeUndefined();
    expect(r.state.starting).toBeUndefined();
  });

  test("a child that exited on its own is not asked to stop", async () => {
    const r = rig({ bootMs: 300 });
    const s = r.make();
    await settle(r, hostOf(s));
    r.children[0].die(1, null);
    s.stop();
    expect(r.children[0].stops).toBe(0);
  });
});

describe("the watchdog", () => {
  test("its source is a script node can compile as it is", () => {
    expect(() => new Script(WATCHDOG)).not.toThrow();
  });
});

describe("a reload, through globalSupervisorState()", () => {
  const KEY = Symbol.for("reflow.ask.ollama-supervisor");
  const wipe = () => delete (globalThis as unknown as Record<symbol, unknown>)[KEY];
  beforeEach(wipe);
  afterEach(wipe);
  const fresh = (r: ReturnType<typeof rig>, env: Record<string, string> = {}) =>
    ollamaSupervisorFor(OLLAMA, { NODE_ENV: "development", ...env }, (l) => r.log.push(l), r.io)!;

  test("a supervisor made after a reload finds the child the first one started", async () => {
    const r = rig({ bootMs: 300 });
    await settle(r, hostOf(fresh(r)));
    expect(globalSupervisorState().child).toBeDefined();
    const reloaded = fresh(r);
    expect(await hostOf(reloaded)).toBe("http://127.0.0.1:41000");
    expect(r.children).toHaveLength(1);
  });

  test("a supervisor made during a cold start joins it", async () => {
    const r = rig({ bootMs: 2_000 });
    const a = hostOf(fresh(r));
    await r.advance(100);
    const b = hostOf(fresh(r));
    expect(await settle(r, Promise.all([a, b]))).toEqual({
      status: "fulfilled",
      value: ["http://127.0.0.1:41000", "http://127.0.0.1:41000"],
    });
    expect(r.children).toHaveLength(1);
  });

  test("a supervisor made after a reload stops the child the first one started", async () => {
    const r = rig({ bootMs: 300 });
    await settle(r, hostOf(fresh(r)));
    fresh(r).stop();
    expect(r.children[0].stops).toBe(1);
    expect(globalSupervisorState().child).toBeUndefined();
  });
});
