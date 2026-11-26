import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { childEnv, claimServer, copyWeb, mainPython, makeScratch, Owned, parsePort, removeScratch, withNonce, type Scratch } from "./scratch";
import { HarnessError } from "./verdict";

const checkout = resolve(import.meta.dir, "../../..");
const python = mainPython(checkout);
const temps: string[] = [];
const temp = () => { const dir = mkdtempSync(join(tmpdir(), "crossing-test-")); temps.push(dir); return dir; };

let s: Scratch;
beforeAll(() => { s = makeScratch({ checkout, python }).scratch; });
afterAll(() => {
  removeScratch(s);
  for (const dir of temps) rmSync(dir, { recursive: true, force: true });
});

describe("parsePort", () => {
  test("refuses the shared ports and anything that is not a user port", () => {
    for (const p of ["3000", "11434", "11999", "0", "70000", "abc", ""]) expect(() => parsePort(p)).toThrow(HarnessError);
    expect(parsePort("3482")).toBe(3482);
  });
});

describe("childEnv", () => {
  const parent = Object.fromEntries([
    "REFLOW_BUILD_JOB", "REFLOW_BUILDS_BUN", "REFLOW_BUILDS_CONCURRENCY", "REFLOW_ENGINE", "REFLOW_ALT_TEXT",
    "REFLOW_DEV_ORIGINS", "REFLOW_ASK_MODEL", "REFLOW_ASK_OLLAMA_IDLE_MINUTES", "ANTHROPIC_API_KEY",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH",
    "NODE_OPTIONS", "NODE_ENV", "REFLOW_DATA_ROOT", "REFLOW_DOCS_OUT", "REFLOW_PDFS_OUT", "REFLOW_BUILDS",
  ].map((name) => [name, "/elsewhere"]).concat([["PATH", "/usr/bin"], ["HOME", "/home/x"]]));

  test("keeps only the allowlist, and the output roots are inside the scratch tree", () => {
    for (const phase of ["build", "serve", "cli", "browser"] as const) {
      const env = childEnv(s, phase, parent);
      expect(Object.values(env)).not.toContain("/elsewhere");
      expect(env.PATH).toBe("/usr/bin");
      expect(env.CROSSING_RUN).toBe(s.nonce);
      if (phase !== "browser") {
        for (const root of [env.REFLOW_DATA_ROOT, env.REFLOW_DOCS_OUT, env.REFLOW_PDFS_OUT]) expect(root.startsWith(`${s.root}/`)).toBe(true);
      }
    }
    expect(childEnv(s, "cli", parent).PYTHONPATH).toBe(join(checkout, "src"));
    expect(childEnv(s, "serve", parent).NODE_OPTIONS).toBe(`--require=${join(s.web, "scripts/crossing/trace.cjs")}`);
  });

  test("refuses an output root that resolves outside the scratch tree", () => {
    const outside = temp();
    const tricked = { ...s, data: join(s.root, "data-link") };
    symlinkSync(outside, tricked.data);
    expect(() => childEnv(tricked, "cli")).toThrow(HarnessError);
    rmSync(tricked.data);
  });
});

describe("the copy of web/", () => {
  test("skips outputs, dependencies and every .env file", () => {
    const from = temp();
    for (const f of ["src/a.ts", "src/.env", ".env.local", "public/docs/x", ".next/x", ".reflow/x", "node_modules/x", "src/node_modules/x", "tsconfig.tsbuildinfo", ".dev-bg.log"]) {
      mkdirSync(join(from, f, ".."), { recursive: true });
      writeFileSync(join(from, f), "x");
    }
    const to = join(temp(), "web");
    copyWeb(from, to);
    expect(readdirSync(to, { recursive: true }).sort()).toEqual(["src", "src/a.ts"]);
  });

  test("refuses a symlink in what it copies", () => {
    const from = temp();
    mkdirSync(join(from, "src"));
    symlinkSync("/etc/hostname", join(from, "src/link"));
    expect(() => copyWeb(from, join(temp(), "web"))).toThrow(HarnessError);
  });

  test("leaves out the checkout's .env files and links node_modules", () => {
    expect(readdirSync(s.web, { recursive: true }).filter((f) => /(^|\/)\.env/.test(String(f)))).toEqual([]);
    expect(existsSync(join(s.web, "node_modules/next/package.json"))).toBe(true);
    expect(readFileSync(join(s.web, "public/crossing-run.txt"), "utf8")).toBe(s.nonce);
  });
});

describe("the scratch tree", () => {
  test("is refused under a git checkout, before its converter could run", () => {
    const repo = temp();
    spawnSync("git", ["init", "-q", repo]);
    mkdirSync(join(repo, ".venv/bin"), { recursive: true });
    writeFileSync(join(repo, ".venv/bin/reflow"), `#!/bin/sh\ntouch ${repo}/marker\nexit 1\n`);
    chmodSync(join(repo, ".venv/bin/reflow"), 0o755);
    expect(() => makeScratch({ checkout, python, tmp: repo })).toThrow("is inside a git checkout");
    expect(readdirSync(repo).filter((f) => f.startsWith("reflow-crossing-") || f === "marker")).toEqual([]);
  });

  test("a CLI build publishes into it through the fixture converter, whatever the parent's environment says", async () => {
    const sentinels = ["docs", "pdfs", "data"].map((name) => {
      const dir = temp();
      writeFileSync(join(dir, "keep"), name);
      return dir;
    });
    const parent = { ...process.env, REFLOW_DOCS_OUT: sentinels[0], REFLOW_PDFS_OUT: sentinels[1], REFLOW_DATA_ROOT: sentinels[2], REFLOW_BUILD_JOB: "bogus" };
    const owned = new Owned(s.nonce, s.logs);
    const build = await owned.own("cli-build-test", process.execPath, ["scripts/build-docs.ts", "fw9"], { cwd: s.web, env: childEnv(s, "cli", parent), timeoutMs: 60_000 });
    expect(await build.exited).toBe(0);
    expect(await owned.cleanup()).toEqual([]);
    expect(JSON.parse(readFileSync(join(s.data, ".builds/publications.json"), "utf8")).fw9.generation).toBe(3);
    expect(readFileSync(join(s.fixture, "calls.log"), "utf8").trim().split("\n").length).toBe(2);
    for (const [i, dir] of sentinels.entries()) {
      expect(readdirSync(dir)).toEqual(["keep"]);
      expect(readFileSync(join(dir, "keep"), "utf8")).toBe(["docs", "pdfs", "data"][i]);
    }
  }, 60_000);
});

describe("claimServer", () => {
  // A stand-in for `next start`: serves public/crossing-run.txt, and crashes with EADDRINUSE on a taken port.
  const stub = join(temp(), "stub-server.cjs");
  writeFileSync(stub, `require("node:http").createServer((q, r) => r.end(require("node:fs").readFileSync("public/crossing-run.txt"))).listen(Number(process.argv[2]), "127.0.0.1");\n`);
  const command = (port: number) => ({ cmd: "node", args: [stub, String(port)], env: childEnv(s, "serve") });
  const listen = (port: number, body: string) => new Promise<Server>((ok) => {
    const server = createServer((socket) => socket.end(`HTTP/1.1 200 OK\r\ncontent-length: ${body.length}\r\n\r\n${body}`));
    server.listen(port, "127.0.0.1", () => ok(server));
  });
  const ports = () => { const p = 40000 + Math.floor(Math.random() * 10000); return [p, p + 1, p + 2]; };

  test("skips a port that is visibly busy", async () => {
    const [busy, free] = ports();
    const other = await listen(busy, "another run");
    const owned = new Owned(s.nonce, s.logs);
    expect((await claimServer(owned, s, [busy, free], command)).port).toBe(free);
    expect(await owned.cleanup()).toEqual([]);
    other.close();
  });

  test("moves on when a foreign server binds the port first, and never takes its answer", async () => {
    const [first, second] = ports();
    let foreign: Server | undefined;
    const owned = new Owned(s.nonce, s.logs);
    const claimed = await claimServer(owned, s, [first, second], (port) => {
      if (port === first) void listen(first, "not this run").then((f) => { foreign = f; });
      return command(port);
    });
    expect(claimed.port).toBe(second);
    expect(readFileSync(join(s.logs, `server-${first}.log`), "utf8")).toContain("EADDRINUSE");
    expect(await owned.cleanup()).toEqual([]);
    foreign?.close();
  });

  test("fails, after cleanup, for a server that never answers or cannot start", async () => {
    const [port] = ports();
    const owned = new Owned(s.nonce, s.logs);
    await expect(claimServer(owned, s, [port], () => ({ cmd: "sleep", args: ["30"], env: childEnv(s, "serve") }), 1000)).rejects.toThrow("did not answer");
    await expect(claimServer(owned, s, [port + 1], () => ({ cmd: "/nonexistent/next", args: [], env: childEnv(s, "serve") }))).rejects.toThrow("could not start");
    expect(await owned.cleanup()).toEqual([]);
    expect(await withNonce(s.nonce)).toEqual([]);
  });
});

describe("cleanup", () => {
  test("reaps a child that ignores SIGTERM and a grandchild that left in a session of its own, once", async () => {
    const owned = new Owned(s.nonce, s.logs);
    let hooks = 0;
    owned.before(() => hooks++);
    const script = `trap '' TERM; sh -c 'setsid sleep 300 &'; sleep 300; sleep 300`;
    await owned.own("stubborn", "sh", ["-c", script], { cwd: s.root, env: childEnv(s, "cli") });
    await new Promise((r) => setTimeout(r, 300));
    expect((await withNonce(s.nonce)).length).toBeGreaterThanOrEqual(3);
    const [a, b] = [owned.cleanup(), owned.cleanup()];
    expect(a).toBe(b);
    expect(await a).toEqual([]);
    expect(hooks).toBe(1);
    expect(await withNonce(s.nonce)).toEqual([]);
  }, 30_000);
});
