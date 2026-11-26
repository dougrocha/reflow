import { afterEach, beforeEach, expect, test } from "bun:test";
import { chmod, copyFile, cp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GET } from "@/app/api/builds/route";
import { BuildStore } from "./store";
import { realProcs } from "./proc";
import { Runner } from "./runner";
import { makeBuilds, setBuildsForTests } from "./system";
import { removeDir, tempDir } from "./testkit";

const WEB = join(import.meta.dir, "../../..");
let root: string;
let web: string;
let store: BuildStore;
const owned: { pid: number; start: number }[] = [];
beforeEach(async () => {
  root = await tempDir();
  web = join(root, "web");
  await mkdir(join(web, "scripts"), { recursive: true });
  await mkdir(join(web, "src/lib"), { recursive: true });
  await cp(join(WEB, "src/lib/builds"), join(web, "src/lib/builds"), { recursive: true });
  await cp(join(WEB, "src/lib/decisions"), join(web, "src/lib/decisions"), { recursive: true });
  for (const name of ["paths.ts", "guards.ts", "format.ts", "build-info.ts"]) await copyFile(join(WEB, "src/lib", name), join(web, "src/lib", name));
  for (const name of ["docs.ts", "pipelines.ts"]) await copyFile(join(WEB, "src", name), join(web, "src", name));
  for (const name of ["build-docs.ts", "build-process.py", "freshness.ts"]) await copyFile(join(WEB, "scripts", name), join(web, "scripts", name));
  await cp(join(WEB, "scripts/lib"), join(web, "scripts/lib"), { recursive: true });
  // The converter fixture prints its lines through the real protocol module.
  await mkdir(join(root, "src/reflow"), { recursive: true });
  for (const name of ["__init__.py", "progress_lines.py"]) await copyFile(join(WEB, "../src/reflow", name), join(root, "src/reflow", name));
  await mkdir(join(root, "samples"));
  for (const slug of ["fw9", "resnet"]) await writeFile(join(root, "samples", `${slug}.pdf`), "fixture");
  await mkdir(join(root, ".venv/bin"), { recursive: true });
  // The venv python prints PUBLIC_NAMES. FIXTURE_PY_SLEEP makes it slow, FIXTURE_PY_PIDS records its PID.
  // As `-m reflow.provenance snapshot|record SPEC` it answers for every document of the spec at once,
  // and FIXTURE_SPECS keeps a line per call: the command, then the spec.
  await writeFile(join(root, ".venv/bin/python"), `#!/bin/sh
if [ "$2" = reflow.provenance ]; then
  if [ -n "$FIXTURE_SPECS" ]; then printf '%s\\t%s\\n' "$3" "$(cat "$4")" >> "$FIXTURE_SPECS"; fi
  exec /usr/bin/python3 -c 'import json, sys; docs = json.load(open(sys.argv[2]))["docs"]; print(json.dumps({d["slug"]: {"written": True, "changed": []} if sys.argv[1] == "record" else {} for d in docs}))' "$3" "$4"
fi
if [ -n "$FIXTURE_PY_PIDS" ]; then echo $$ >> "$FIXTURE_PY_PIDS"; fi
sleep "\${FIXTURE_PY_SLEEP:-0}"
echo manifest.json
`);
  await chmod(join(root, ".venv/bin/python"), 0o755);
  // The converter fixture. FIXTURE_PLAN, when set, is [[slug, "ok" | "FAILED", seconds], ...] played
  // in order (an ok adds a page to the document), then FIXTURE_THEN=hang waits. Otherwise it hangs
  // ignoring TERM, or with FIXTURE_SUCCESS=1 reports every document ok at once.
  await writeFile(join(root, ".venv/bin/reflow"), `#!/usr/bin/python3
import json, os, signal, sys, time
from reflow.progress_lines import FAILED, OK, START, status_line
if sys.argv[1] == "audit":
    if os.environ.get("FIXTURE_AUDIT_CHILD") == "1":
        # A descendant that ignores TERM and holds none of the audit's output open.
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.environ["FIXTURE_AUDIT_PIDS"], "w") as f:
            f.write(str(child.pid))
    time.sleep(float(os.environ.get("FIXTURE_AUDIT_S", "0")))
    print("{}", flush=True)
    sys.exit(0)
if os.environ.get("FIXTURE_IGNORE_INT") == "1":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
with open(os.environ["FIXTURE_WORKERS"], "a") as f:
    f.write(str(os.getpid()) + "\\n")
outs = {os.path.basename(sys.argv[i + 1]): sys.argv[i + 1] for i in range(2, sys.argv.index("--max-docs"), 2)}
if os.environ.get("FIXTURE_ARGS"):
    with open(os.environ["FIXTURE_ARGS"], "w") as f:
        json.dump(sys.argv[1:], f)
def publish(out):
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "manifest.json")
    pages = json.load(open(path))["pageCount"] + 1 if os.path.exists(path) else 1
    with open(path, "w") as f:
        json.dump({"pageCount": pages, "v": "fixture-%d" % pages}, f)
if os.environ.get("FIXTURE_PLAN"):
    for out in outs.values():
        print(status_line(START, out, ""), flush=True)
    began = time.monotonic()
    for slug, status, at in json.loads(os.environ["FIXTURE_PLAN"]):
        time.sleep(max(0, began + at - time.monotonic()))
        if status == "ok":
            publish(outs[slug])
            print(status_line(OK, outs[slug], "1 page"), flush=True)
        else:
            print(status_line(FAILED, outs[slug], "fixture failure for " + slug), flush=True)
    if os.environ.get("FIXTURE_THEN") == "hang":
        while True: time.sleep(0.05)
    sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get("FIXTURE_SUCCESS") == "1":
    for out in outs.values():
        publish(out)
        print(status_line(OK, out, "1 page"), flush=True)
    sys.exit(0)
while True: time.sleep(0.05)
`);
  await chmod(join(root, ".venv/bin/reflow"), 0o755);
  store = new BuildStore({ dir: join(root, "data/.builds") });
});
const SAVED_ENV = { ...process.env };
afterEach(async () => {
  for (const ref of owned.splice(0)) if (await realProcs.isAlive(ref)) await realProcs.killTree(ref, 100);
  setBuildsForTests(undefined);
  process.env = { ...SAVED_ENV };
  await removeDir(root);
});
const wait = (ms = 40) => new Promise((r) => setTimeout(r, ms));
async function until(check: () => Promise<boolean>) {
  for (let i = 0; i < 150; i++) { if (await check()) return; await wait(); }
  throw new Error("Timed out waiting for the fixture process.");
}
async function remember(pid: number) {
  const start = await realProcs.start(pid);
  if (start !== null) owned.push({ pid, start });
}
async function cli() {
  const proc = Bun.spawn([process.execPath, join(web, "scripts/build-docs.ts"), "fw9", "resnet"], {
    cwd: web, env: { ...process.env, REFLOW_DATA_ROOT: join(root, "data"), FIXTURE_WORKERS: join(root, "workers"), REFLOW_BUILD_JOB: "" },
    stdout: "ignore", stderr: "pipe",
  });
  await remember(proc.pid);
  await until(async () => (await Bun.file(join(root, "workers")).text().catch(() => "")).trim().length > 0);
  const pid = Number((await readFile(join(root, "workers"), "utf8")).trim());
  await remember(pid);
  return { proc, pid };
}

test("real CLI cancellation retains all claims until the TERM-resistant converter exits", async () => {
  const { proc, pid } = await cli();
  const jobs = (await store.snapshot()).active;
  expect(jobs.map((j) => j.slug).sort()).toEqual(["fw9", "resnet"]);
  const job = jobs.find((j) => j.slug === "fw9")!;
  const cancellation = store.cancel(job.id);
  await until(() => store.cancelRequested(job.id));
  await wait(200);
  expect(await realProcs.start(pid)).not.toBeNull();
  for (const slug of ["fw9", "resnet"]) expect((await store.enqueue(slug, "ui")).created).toBe(false);
  await cancellation;
  await proc.exited;
  expect(await realProcs.start(pid)).toBeNull();
  await store.recover();
  for (const record of jobs) expect((await store.get(record.id))?.state).toBe("cancelled");
  expect((await store.enqueue("fw9", "ui")).created).toBe(true);
}, 20_000);

test("SIGKILL of a script preserves the orphan converter's exclusion and allows targeted cleanup", async () => {
  const { proc, pid } = await cli();
  const job = (await store.snapshot()).active.find((j) => j.slug === "fw9")!;
  proc.kill("SIGKILL");
  await proc.exited;
  await store.recover();
  expect(await realProcs.start(pid)).not.toBeNull();
  expect((await store.enqueue("fw9", "ui")).created).toBe(false);
  await store.cancel(job.id);
  expect(await realProcs.start(pid)).toBeNull();
  expect((await store.enqueue("fw9", "ui")).created).toBe(true);
}, 20_000);

test("kernel coordination lock releases when the owning OS process is killed", async () => {
  const code = `import { transaction } from ${JSON.stringify(join(WEB, "src/lib/builds/lock.ts"))};
import { writeFile } from "node:fs/promises";
await transaction(${JSON.stringify(store.dir)}, async () => {
  await writeFile(${JSON.stringify(join(root, "locked"))}, "ready");
  await new Promise(() => {});
});`;
  const proc = Bun.spawn([process.execPath, "--eval", code], { cwd: WEB, stdout: "ignore", stderr: "inherit" });
  await remember(proc.pid);
  await until(() => Bun.file(join(root, "locked")).exists());
  const enqueued = store.enqueue("fw9", "ui");
  proc.kill("SIGKILL");
  await proc.exited;
  expect((await enqueued).created).toBe(true);
}, 10_000);

test("two independent server processes share one global dispatch slot", async () => {
  await store.enqueue("fw9", "ui");
  await store.enqueue("resnet", "ui");
  const code = `import { Runner, nodeSpawnBuild } from ${JSON.stringify(join(web, "src/lib/builds/runner.ts"))};
import { BuildStore } from ${JSON.stringify(join(web, "src/lib/builds/store.ts"))};
const store = new BuildStore({dir: ${JSON.stringify(store.dir)}});
const runner = new Runner(store, {concurrency: 1, spawn: nodeSpawnBuild({webDir: ${JSON.stringify(web)}, buildsDir: store.dir})});
await runner.start();
await Bun.sleep(800);
runner.stop();
process.exit(0);`;
  const servers = Array.from({ length: 2 }, () => Bun.spawn([process.execPath, "--eval", code], {
    cwd: web, env: { ...process.env, REFLOW_BUILDS_BUN: process.execPath, REFLOW_DATA_ROOT: join(root, "data"), FIXTURE_WORKERS: join(root, "workers") },
    stdout: "ignore", stderr: "inherit",
  }));
  for (const server of servers) await remember(server.pid);
  await until(() => Bun.file(join(root, "workers")).exists());
  for (const job of (await store.snapshot()).active) if (job.pid) await remember(job.pid);
  const workerPids = (await readFile(join(root, "workers"), "utf8")).trim().split("\n").map(Number);
  for (const pid of workerPids) await remember(pid);
  await Promise.all(servers.map((s) => s.exited));
  expect(workerPids).toHaveLength(1);
  const { active } = await store.snapshot();
  expect(active.filter((j) => j.state === "running")).toHaveLength(1);
  expect(active.filter((j) => j.state === "queued")).toHaveLength(1);
}, 10_000);


test("the real script publishes successful conversions and records done without an error", async () => {
  const proc = Bun.spawn([process.execPath, join(web, "scripts/build-docs.ts"), "fw9", "resnet"], {
    cwd: web, env: { ...process.env, REFLOW_DATA_ROOT: join(root, "data"), FIXTURE_WORKERS: join(root, "workers"), FIXTURE_SUCCESS: "1", REFLOW_BUILD_JOB: "" },
    stdout: "pipe", stderr: "pipe",
  });
  await remember(proc.pid);
  const [code, stdout, stderr] = await Promise.all([proc.exited, new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
  expect({ code, stderr }).toEqual({ code: 0, stderr: "" });
  expect(stdout).toContain("1 page");
  const { active, recent } = await store.snapshot();
  expect(active).toEqual([]);
  expect(recent).toHaveLength(2);
  for (const job of recent) {
    expect(job).toMatchObject({ state: "done", error: null, childPid: null, childStart: null });
    expect(await Bun.file(join(web, "public/docs", job.slug, "manifest.json")).json()).toEqual({ pageCount: 1, v: "fixture-1" });
  }
}, 10_000);

test("--stage-cache converts through the main checkout's stage cache and records that it did", async () => {
  const cache = join(root, "shared-cache");
  for (const flags of [["--stage-cache"], []]) {
    await rm(join(root, "specs"), { force: true });
    const proc = Bun.spawn([process.execPath, join(web, "scripts/build-docs.ts"), ...flags, "fw9"], {
      cwd: web, stdout: "pipe", stderr: "pipe",
      env: { ...process.env, REFLOW_DATA_ROOT: join(root, "data"), FIXTURE_WORKERS: join(root, "workers"), FIXTURE_SUCCESS: "1",
             REFLOW_BUILD_JOB: "", REFLOW_PAGE_CHECKS_CACHE: cache, FIXTURE_ARGS: join(root, "args"), FIXTURE_SPECS: join(root, "specs") },
    });
    await remember(proc.pid);
    const [code, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text()]);
    expect({ code, stderr }).toEqual({ code: 0, stderr: "" });
    const args: string[] = await Bun.file(join(root, "args")).json();
    const at = args.indexOf("--stage-cache");
    const record = (await readFile(join(root, "specs"), "utf8")).split("\n").find((l) => l.startsWith("record\t"));
    const spec = JSON.parse(record!.slice("record\t".length));
    if (flags.length) {
      expect(args[at + 1]).toBe(join(cache, "reflow-page-checks-stages"));
      expect(spec.stageCache).toBe(true);
    } else {
      expect(at).toBe(-1);
      expect(spec.stageCache).toBeUndefined();
    }
  }
}, 10_000);

/** The real script on fw9 and resnet (or `slugs`), with the converter fixture's `env`. In a session
 *  of its own when `session`, like a terminal's foreground job, so a "Ctrl-C" reaches only its group. */
async function script(env: Record<string, string>, opts: { slugs?: string[]; session?: boolean } = {}) {
  const cmd = [process.execPath, join(web, "scripts/build-docs.ts"), ...(opts.slugs ?? ["fw9", "resnet"])];
  const proc = Bun.spawn(opts.session ? ["setsid", ...cmd] : cmd, {
    cwd: web, env: { ...process.env, REFLOW_DATA_ROOT: join(root, "data"), FIXTURE_WORKERS: join(root, "workers"), REFLOW_BUILD_JOB: "", ...env },
    stdout: "pipe", stderr: "pipe",
  });
  await remember(proc.pid);
  return proc;
}
/** This test's server: GET /api/builds over the fixture's store, recording what it expires. */
function server() {
  process.env.REFLOW_BUILDS = "1";
  const revalidated: string[] = [];
  setBuildsForTests(makeBuilds({
    store, runner: new Runner(store, { concurrency: 1, spawn: () => { throw new Error("no page jobs in these tests"); } }),
    docsRoot: join(web, "public/docs"), slugs: () => ["fw9", "resnet"], revalidate: async (slug) => void revalidated.push(slug),
  }));
  const poll = async () => {
    const body = await (await GET(new Request("http://localhost/api/builds", { headers: { host: "localhost" } }))).json();
    const jobs: { slug: string; state: string; error: string | null }[] = [...body.active, ...body.recent];
    return { published: body.published as Record<string, number>, states: Object.fromEntries(jobs.map((j) => [j.slug, j.state])) as Record<string, string> };
  };
  return { revalidated, poll };
}
const stateOf = async (pid: number) => {
  const text = await readFile(`/proc/${pid}/stat`, "utf8").catch(() => null);
  return text === null ? null : text.slice(text.lastIndexOf(")") + 2).split(" ")[0];
};

test("each publication is expired while its build is still auditing, document by document", async () => {
  const { revalidated, poll } = server();
  const proc = await script({ FIXTURE_PLAN: JSON.stringify([["fw9", "ok", 0.2], ["resnet", "ok", 1.4]]), FIXTURE_AUDIT_S: "1.5" });
  let exited = false;
  void proc.exited.then(() => { exited = true; });
  const polls: Awaited<ReturnType<typeof poll>>[] = [];
  while (!exited) {
    polls.push(await poll());
    await wait(100);
  }
  polls.push(await poll());
  expect(await proc.exited).toBe(0);
  // fw9's new files were expired while fw9 was still running (its audit next), before resnet published.
  const fw9 = polls.find((p) => p.published.fw9 === 1 && p.states.fw9 === "running");
  expect(fw9?.published.resnet).toBeUndefined();
  expect(polls.find((p) => p.published.resnet === 1 && p.states.resnet === "running")).toBeDefined();
  // The audit is a second publication of each; everything ends expired and done.
  expect(polls.at(-1)).toEqual({ published: { fw9: 2, resnet: 2 }, states: { fw9: "done", resnet: "done" } });
  expect(revalidated.filter((s) => s === "fw9")).toHaveLength(2);
  expect(revalidated.filter((s) => s === "resnet")).toHaveLength(2);
}, 30_000);

test("a build cancelled after it published still has that output expired", async () => {
  const { revalidated, poll } = server();
  const proc = await script({ FIXTURE_PLAN: JSON.stringify([["fw9", "ok", 0.1]]), FIXTURE_THEN: "hang" });
  await until(async () => (await store.publications()).fw9?.generation === 1);
  const fw9 = (await store.snapshot()).active.find((j) => j.slug === "fw9")!;
  await store.cancel(fw9.id);
  expect(await proc.exited).toBe(143);
  expect(await poll()).toEqual({ published: { fw9: 1 }, states: { fw9: "cancelled", resnet: "cancelled" } });
  expect(revalidated).toEqual(["fw9"]);
}, 30_000);

test("SIGTERM during an audit after another document failed keeps that failure and records the publication", async () => {
  const proc = await script({ FIXTURE_PLAN: JSON.stringify([["fw9", "FAILED", 0.1], ["resnet", "ok", 0.2]]), FIXTURE_AUDIT_S: "20" });
  await until(async () => {
    const resnet = (await store.snapshot()).active.find((j) => j.slug === "resnet");
    return resnet?.stage === "Auditing the source PDF" && resnet.childPid !== null && (await store.publications()).resnet?.publishing !== null;
  });
  await wait(200);
  proc.kill("SIGTERM");
  expect(await proc.exited).toBe(143);
  const jobs = Object.fromEntries((await store.snapshot()).recent.map((j) => [j.slug, j]));
  expect(jobs.fw9).toMatchObject({ state: "failed", error: "fixture failure for fw9" });
  expect(jobs.resnet).toMatchObject({ state: "interrupted", error: "Stopped by SIGTERM.", childPid: null });
  // The public swap (1) and the audit the stop interrupted (2) are both recorded, nothing left half-way.
  expect((await store.publications()).resnet).toMatchObject({ generation: 2, publishing: null });
  expect(await readdir(join(store.dir, "claims"))).toEqual([]);
}, 30_000);

test("a second Ctrl-C while the stop has the converter frozen leaves nothing stopped or open", async () => {
  const proc = await script({ FIXTURE_IGNORE_INT: "1" }, { slugs: ["fw9"], session: true });
  await until(async () => (await Bun.file(join(root, "workers")).text().catch(() => "")).trim().length > 0);
  const converter = Number((await readFile(join(root, "workers"), "utf8")).trim());
  await remember(converter);
  process.kill(-proc.pid, "SIGINT");
  const deadline = Date.now() + 10_000;
  while (await stateOf(converter) !== "T" && Date.now() < deadline) await Bun.sleep(0.5);
  expect(await stateOf(converter)).toBe("T");
  process.kill(-proc.pid, "SIGINT"); // the terminal's whole group: the script, its helpers, the converter
  expect(await proc.exited).toBe(130);
  expect(await stateOf(converter)).toBeNull();
  expect((await store.snapshot()).recent).toMatchObject([{ slug: "fw9", state: "interrupted", error: "Stopped by SIGINT." }]);
  expect(await readdir(join(store.dir, "claims"))).toEqual([]);
}, 30_000);

test("the command line works by absolute path from any folder, finding its helper and data from its own file", async () => {
  const env: Record<string, string | undefined> = { ...process.env, FIXTURE_WORKERS: join(root, "workers"), FIXTURE_SUCCESS: "1", REFLOW_BUILD_JOB: "" };
  for (const name of ["REFLOW_DATA_ROOT", "REFLOW_DOCS_OUT", "REFLOW_PDFS_OUT"]) delete env[name];
  for (const cwd of [root, tmpdir()]) {
    const proc = Bun.spawn([process.execPath, join(web, "scripts/build-docs.ts"), "fw9"], { cwd, env, stdout: "pipe", stderr: "pipe" });
    await remember(proc.pid);
    const [code, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text(), new Response(proc.stdout).text()]);
    expect({ cwd, code, stderr }).toEqual({ cwd, code: 0, stderr: "" });
  }
  const local = new BuildStore({ dir: join(web, ".reflow/.builds") });
  expect((await local.snapshot()).recent.map((j) => [j.slug, j.state, j.error])).toEqual([["fw9", "done", null], ["fw9", "done", null]]);
  expect(await Bun.file(join(web, "public/docs/fw9/manifest.json")).json()).toEqual({ pageCount: 2, v: "fixture-2" });
  // Each run: its public swap and its audit; the second also removed the first one's audit first.
  expect((await local.publications()).fw9).toMatchObject({ generation: 5, publishing: null });
}, 30_000);

test("a worker runs only after its identity is in the record: the go byte follows the save", async () => {
  // python3 on PATH is a shim: a worker wrapper becomes a checker that, at the moment it is told to
  // go, looks for its own PID and start time in the job record, then runs the command as usual.
  const real = (await new Response(Bun.spawn(["python3", "-c", "import sys; print(sys.executable)"], { stdout: "pipe" }).stdout).text()).trim();
  const shim = join(root, "shim");
  await mkdir(shim);
  await writeFile(join(shim, "check.py"), `import glob, json, os, sys
command = json.loads(sys.argv[3])
if sys.stdin.buffer.read(1) != b"g":
    sys.exit(0)
start = int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19])
def mine(path):
    try:
        job = json.load(open(path))
    except (OSError, ValueError):
        return False
    return job.get("childPid") == os.getpid() and job.get("childStart") == start
saved = any(mine(p) for p in glob.glob(os.path.join(os.environ["SHIM_JOBS"], "*.json")))
with open(os.environ["SHIM_LOG"], "a") as log:
    log.write(("saved" if saved else "UNSAVED") + "\\n")
os.execvp(command[0], command)
`);
  await writeFile(join(shim, "python3"), `#!/bin/sh\nif [ "$2" = worker ]; then exec ${real} ${join(shim, "check.py")} "$@"; fi\nexec ${real} "$@"\n`);
  await chmod(join(shim, "python3"), 0o755);
  const proc = await script({
    FIXTURE_SUCCESS: "1", PATH: `${shim}:${process.env.PATH}`, SHIM_JOBS: join(store.dir, "jobs"), SHIM_LOG: join(root, "shim.log"),
  }, { slugs: ["fw9"] });
  expect({ code: await proc.exited, stderr: await new Response(proc.stderr).text() }).toEqual({ code: 0, stderr: "" });
  // The public names, the provenance snapshot, the converter, the audit and the provenance record:
  // each found its identity saved before it ran.
  expect((await readFile(join(root, "shim.log"), "utf8")).trim().split("\n")).toEqual(["saved", "saved", "saved", "saved", "saved"]);
  expect((await store.snapshot()).recent).toMatchObject([{ slug: "fw9", state: "done" }]);
}, 30_000);

test("a stop helper killed while it has the tree frozen is run again, so nothing is left stopped", async () => {
  const { readdirSync, readFileSync } = await import("node:fs");
  const tree = Bun.spawn(["sh", "-c", "trap '' TERM; sleep 100 & sleep 100 & wait"], { stdout: "ignore", stderr: "ignore" });
  await remember(tree.pid);
  await wait(200);
  const start = (await realProcs.start(tree.pid))!;
  const kids = (await readdir("/proc")).filter((n) => /^\d+$/.test(n)).map(Number).filter((pid) => {
    try { return readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1].split(" ")[1] === String(tree.pid); } catch { return false; }
  });
  expect(kids).toHaveLength(2);
  for (const pid of kids) await remember(pid);
  const state = (pid: number) => { try { return readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1][0]; } catch { return null; } };
  const root_ = JSON.stringify({ pid: tree.pid, start });
  const helpers = () => readdirSync("/proc").filter((n) => /^\d+$/.test(n)).filter((n) => {
    try { return readFileSync(`/proc/${n}/cmdline`, "utf8").split("\0").includes(root_); } catch { return false; }
  }).map(Number);
  const stopping = realProcs.killTree({ pid: tree.pid, start }, 1500);
  let helper: number | undefined;
  const deadline = Date.now() + 5000;
  while (helper === undefined && Date.now() < deadline) helper = helpers()[0];
  expect(helper).toBeDefined();
  // Spin (synchronously, so nothing waits on the event loop) until the helper has the tree frozen.
  while (state(tree.pid) !== "T" && Date.now() < deadline) { /* spin */ }
  expect(state(tree.pid)).toBe("T");
  process.kill(helper!, "SIGKILL");
  await stopping;
  for (const pid of [tree.pid, ...kids]) expect([null, "Z"]).toContain(state(pid));
  expect(helpers()).toEqual([]);
}, 30_000);

test("starting the server does not wait for the build lock, and the runner retries until it gets it", async () => {
  // Another process holds coordination.lock; a page job is queued, so the first pass needs the lock.
  const code = `import { transaction } from ${JSON.stringify(join(WEB, "src/lib/builds/lock.ts"))};
import { writeFile } from "node:fs/promises";
await transaction(${JSON.stringify(store.dir)}, async () => {
  await writeFile(${JSON.stringify(join(root, "locked"))}, "ready");
  await new Promise(() => {});
});`;
  await store.enqueue("fw9", "ui");
  const holder = Bun.spawn([process.execPath, "--eval", code], { cwd: WEB, stdout: "ignore", stderr: "inherit" });
  await remember(holder.pid);
  await until(() => Bun.file(join(root, "locked")).exists());
  process.env.REFLOW_BUILDS = "1";
  (process.env as Record<string, string>).NEXT_RUNTIME = "nodejs";
  const spawned: string[] = [];
  const runner = new Runner(store, { concurrency: 1, spawn: (job) => { spawned.push(job.slug); return { pid: 4_000_000, exited: new Promise(() => {}) }; } });
  setBuildsForTests(makeBuilds({ store, runner, docsRoot: join(web, "public/docs"), slugs: () => ["fw9"], revalidate: async () => {} }));
  const { register } = await import("@/instrumentation");
  const began = performance.now();
  await register();
  expect(performance.now() - began).toBeLessThan(500);
  await wait(300);
  expect(spawned).toEqual([]); // still waiting for the lock
  holder.kill("SIGKILL");
  await holder.exited;
  await until(async () => spawned.length > 0);
  expect(spawned).toEqual(["fw9"]);
  await runner.stop();
}, 30_000);

test("a stop during an audit keeps the document's claim until the audit's whole tree has exited", async () => {
  const pids = join(root, "audit-pids");
  const proc = await script({ FIXTURE_PLAN: JSON.stringify([["fw9", "ok", 0.1]]), FIXTURE_AUDIT_S: "20", FIXTURE_AUDIT_CHILD: "1", FIXTURE_AUDIT_PIDS: pids }, { slugs: ["fw9"] });
  await until(() => Bun.file(pids).exists());
  const grandchild = Number(await readFile(pids, "utf8"));
  await remember(grandchild);
  const job = (await store.snapshot()).active[0];
  proc.kill("SIGTERM");
  // The audit dies at once and its output ends, but its descendant ignores TERM until the KILL.
  let aliveAtEnd: boolean | undefined;
  const deadline = Date.now() + 15_000;
  while (aliveAtEnd === undefined && Date.now() < deadline) {
    const now = await store.get(job.id);
    if (now && now.state !== "running" && now.state !== "queued") aliveAtEnd = (await realProcs.start(grandchild)) !== null;
    else await wait(10);
  }
  expect(aliveAtEnd).toBe(false);
  expect(await proc.exited).toBe(143);
  expect(await store.get(job.id)).toMatchObject({ state: "interrupted", error: "Stopped by SIGTERM." });
}, 30_000);

/** Pids of the processes running `sleep <seconds>`: the fixture python's child, which a stop must not leave. */
async function sleepers(seconds: string) {
  const found: number[] = [];
  for (const name of await readdir("/proc")) {
    if (!/^\d+$/.test(name)) continue;
    const argv = (await readFile(`/proc/${name}/cmdline`, "utf8").catch(() => "")).split("\0");
    if (argv[0].endsWith("sleep") && argv[1] === seconds) found.push(Number(name));
  }
  return found;
}

for (const [signal, code] of [["SIGINT", 130], ["SIGTERM", 143]] as const) {
  test(`${signal} while PUBLIC_NAMES is read ends the records as stopped, exits ${code} and leaves nothing behind`, async () => {
    const pids = join(root, "py-pids");
    const seconds = signal === "SIGINT" ? "29.1" : "29.2";
    const env = { FIXTURE_PY_SLEEP: seconds, FIXTURE_PY_PIDS: pids };
    const proc = await script(env, { session: true });
    await until(async () => (await Bun.file(pids).text().catch(() => "")).trim().length > 0);
    await until(async () => (await sleepers(seconds)).length > 0);
    const python = Number((await readFile(pids, "utf8")).trim());
    for (const pid of [python, ...await sleepers(seconds)]) await remember(pid);
    expect((await store.snapshot()).active.map((j) => j.state)).toEqual(["queued", "queued"]);
    if (signal === "SIGINT") process.kill(-proc.pid, "SIGINT"); // Ctrl-C reaches the whole group
    else proc.kill("SIGTERM");
    const [exit, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text(), new Response(proc.stdout).text()]);
    expect(exit).toBe(code);
    expect(stderr).not.toContain("PUBLIC_NAMES"); // no stack trace
    expect(stderr).not.toContain("is stopping");
    const { active, recent } = await store.snapshot();
    expect(active).toEqual([]);
    expect(recent.map((j) => [j.slug, j.state, j.error, j.childPid]).sort()).toEqual([
      ["fw9", "interrupted", `Stopped by ${signal}.`, null], ["resnet", "interrupted", `Stopped by ${signal}.`, null],
    ]);
    expect(await readdir(join(store.dir, "claims"))).toEqual([]);
    expect(await Bun.file(join(root, "workers")).exists()).toBe(false); // the converter never started
    expect(await realProcs.start(python)).toBeNull();
    expect(await sleepers(seconds)).toEqual([]);
    // The same command again runs: nothing is stuck, so a server starting after it has nothing to settle.
    const again = await script({ FIXTURE_SUCCESS: "1" });
    expect(await again.exited).toBe(0);
    let transactions = 0;
    const runner = new Runner(store, { concurrency: 1, spawn: () => { throw new Error("no page jobs here"); } });
    const dispatch = store.dispatch.bind(store);
    store.dispatch = (...args) => { transactions++; return dispatch(...args); };
    await runner.start();
    await runner.stop();
    expect(transactions).toBe(0);
    expect((await store.snapshot()).active).toEqual([]);
  }, 30_000);
}

test("a server that starts after a dead command-line build and a later successful one settles the dead one", async () => {
  const dead = await script({}, { slugs: ["fw9"], session: true });
  await until(async () => (await Bun.file(join(root, "workers")).text().catch(() => "")).trim().length > 0);
  const converter = Number((await readFile(join(root, "workers"), "utf8")).trim());
  await remember(converter);
  const job = (await store.snapshot()).active.find((j) => j.slug === "fw9")!;
  process.kill(-dead.pid, "SIGKILL"); // a tool timeout: the script, its helpers and the converter
  await dead.exited;
  await until(async () => (await realProcs.start(converter)) === null);
  // Another build takes the dead build's claim, finishes, and the claim goes with it.
  const later = await script({ FIXTURE_SUCCESS: "1" }, { slugs: ["fw9"] });
  expect(await later.exited).toBe(0);
  expect(await readdir(join(store.dir, "claims"))).toEqual([]);
  expect((await store.snapshot()).active.map((j) => j.id)).toEqual([job.id]); // dead, yet still listed as building
  let transactions = 0;
  const dispatch = store.dispatch.bind(store);
  store.dispatch = (...args) => { transactions++; return dispatch(...args); };
  const runner = new Runner(store, { concurrency: 1, spawn: () => { throw new Error("no page jobs here"); } });
  await runner.start();
  await runner.stop();
  expect(transactions).toBe(1);
  expect(await store.get(job.id)).toMatchObject({ state: "interrupted" });
  expect((await store.snapshot()).active).toEqual([]);
}, 30_000);
