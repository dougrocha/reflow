// The crossing check: a publication overtakes a render, on the production server (docs/builds.md,
// "A stuck page reports itself"). Run it after every Next upgrade:
//
//   bun run check:crossing [-- --record] [--control] [--port N] [--out DIR] [--keep]
//
// Next compares a mark with the time a cache entry was SAVED. A render that read publication A and is
// still running when B is published and marked saves A's page after the mark, and Next takes it as
// fresh. Only a page's report (?behind=) and the server's re-mark heal it. The steps:
//
//   1. the page settles on fw9 (generation 0);
//   2. a CLI build publishes A, and a poll marks the document stale;
//   3. a load is answered from the stale entry and starts the regeneration; trace.cjs holds its read of
//      the manifest (it has read A's stamp);
//   4. a second build publishes B, and a poll marks again;
//   5. the held read is released: the regeneration saves A's page after B's mark;
//   6. four plain loads and polls stay on A (the stuck state: premise P6);
//   7. a real Chromium tab polls, reports ?behind=fw9:<A>, and shows B by itself, with no full page
//      load and no more refreshes after; a plain load then carries B.
//
// It runs on a scratch copy of web/ in the temp folder (scratch.ts), never on the checkout:
//
//   <scratch>/web/            the copy (node_modules linked), built with `next build --webpack` and
//                             served with `next start` on 127.0.0.1, port 3481 to 3499 (never 3000)
//     public/docs/fw9/        the fixture document (fixture/fw9), published into by every build
//     public/crossing-run.txt this run's nonce: the server it reads it from is its own
//   <scratch>/.venv/bin/      the converter (fixture-reflow.py: audit n writes "crossing fixture
//                             audit n") and a shim to the main checkout's python
//   <scratch>/samples/fw9.pdf a stub, <scratch>/fixture/ the template, counters and calls.log
//   <scratch>/data/           REFLOW_DATA_ROOT: the seed fw9 (audit 0) and the build record
//   <scratch>/logs/           every child's output, copied to --out (default /tmp/reflow-crossing-logs/<time>)
//
// Exit 0: all passed. Exit 1: a page stayed behind a publication (the re-mark no longer works on this
// Next). Exit 2: the check could not test the case: a premise failed (P1 to P7), including the case
// where Next no longer serves the overtaking render as fresh, or a reader could not read a stamp.
// --record writes verified.json after a pass; src/lib/builds/crossing-verified.test.ts fails until the
// installed Next is the recorded one. --control disables the re-mark in the copy and exits 0 only when
// the check then fails for the right reason.
import type { Browser } from "playwright";
import { appendFileSync, existsSync, mkdirSync, readFileSync, realpathSync } from "node:fs";
import { join, resolve } from "node:path";
import { launchChromium } from "../lib/browser";
import { at, check, domContent, generation, load, poll, release, settle, stall, truth, until, watch } from "./lib";
import { sleep } from "../lib/net";
import { claimServer, childEnv, makeScratch, mainPython, Owned, parsePort, PORTS, removeScratch, saveLogs, type Scratch } from "./scratch";
import { classify, controlVerdict, HarnessError, nextVersions, pinProblem, recordIfPassed, type Result } from "./verdict";

const here = import.meta.dir;
const checkout = resolve(here, "../../..");
const VERIFIED = join(here, "verified.json");

const opts = { keep: false, record: false, control: false, port: null as number | null, out: `/tmp/reflow-crossing-logs/${new Date().toISOString().replace(/[:.]/g, "-")}` };
const argv = process.argv.slice(2);
try {
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--keep") opts.keep = true;
    else if (a === "--record") opts.record = true;
    else if (a === "--control") opts.control = true;
    else if (a === "--port") opts.port = parsePort(argv[++i] ?? "");
    else if (a === "--out") opts.out = resolve(argv[++i] ?? "");
    else throw new HarnessError(`unknown option ${a}`);
  }
} catch (e) {
  console.log(`crossing-mark: could not test (${(e as Error).message})`);
  process.exit(2);
}

mkdirSync(opts.out, { recursive: true });
const print = (line: string) => {
  console.log(line);
  appendFileSync(join(opts.out, "crossing.log"), `${line}\n`);
};
const log = (o: object) => print(JSON.stringify({ t: at(), ...o }));

const results: Result[] = [];
let harness: string | null = null;
let interrupted = false;
let healedMs: number | null = null;
let scratch: Scratch | null = null;
let owned: Owned | null = null;
let mutations = 0;
let ran = "";

/** Stops everything, saves the logs and removes the scratch tree (kept with --keep, or when a process
 *  of the run survived). Memoized: a signal during the end of the run waits for the same cleanup. */
let finishing: Promise<boolean> | undefined;
const finish = () => (finishing ??= (async () => {
  if (!scratch || !owned) return true;
  const survivors = await owned.cleanup();
  saveLogs(scratch, opts.out);
  if (survivors.length) {
    print(`crossing-mark: processes of this run survived: ${JSON.stringify(survivors)}; the scratch tree ${scratch.root} is kept`);
    return false;
  }
  if (opts.keep) print(`scratch tree kept: ${scratch.root}`);
  else removeScratch(scratch);
  print(`logs: ${opts.out}`);
  return true;
})());

const forever = () => new Promise<never>(() => {});
for (const [signal, code] of [["SIGINT", 130], ["SIGTERM", 143]] as const) {
  process.on(signal, async () => {
    if (interrupted) return;
    interrupted = true;
    print(`crossing-mark: interrupted by ${signal}`);
    const clean = await finish().catch(() => false);
    process.exit(clean ? code : 2);
  });
}

async function run(): Promise<void> {
  // P1: preflight. Anything wrong here throws: there is nothing to test yet.
  const { declared, installed } = nextVersions(join(checkout, "web"));
  const pin = pinProblem(declared, installed);
  if (pin) throw new HarnessError(pin);
  const python = mainPython(checkout);
  if (!existsSync(python)) throw new HarnessError(`the main checkout's python ${python} does not exist`);
  const made = makeScratch({ checkout, python, control: opts.control });
  scratch = made.scratch;
  mutations = made.mutations;
  const s = scratch;
  owned = new Owned(s.nonce, s.logs);
  if (opts.control && mutations !== 1) throw new HarnessError(`the control mutation matched ${mutations} times; update it (verdict.ts REMARK)`);
  ran = JSON.parse(readFileSync(join(realpathSync(join(s.web, "node_modules")), "next/package.json"), "utf8")).version;
  if (ran !== installed) throw new HarnessError(`the scratch tree runs next ${ran}, not the installed ${installed}`);
  const browser: Browser = await launchChromium({ env: childEnv(s, "browser"), handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false });
  owned.before(() => release(s.gate));
  owned.before(() => Promise.race([browser.close(), sleep(5000)]));
  check(results, "P1", "premise", true, `preflight: next ${ran} declared, installed and in the scratch tree; python, temp folder and Chromium ready`, print);

  const next = join(s.web, "node_modules/next/dist/bin/next");
  let t = at();
  const build = await owned.own("next-build", "nice", ["-n", "19", "node", next, "build", "--webpack"], { cwd: s.web, env: childEnv(s, "build"), timeoutMs: 600_000 });
  if ((await build.exited) !== 0) throw new HarnessError(`next build failed; see ${join(opts.out, "logs/next-build.log")}`);
  log({ step: "next build", ms: at() - t });
  t = at();
  const { base, port } = await claimServer(owned, s, opts.port ? [opts.port] : PORTS, (p) => ({
    cmd: "node", args: [next, "start", "-p", String(p), "-H", "127.0.0.1"], env: childEnv(s, "serve"),
  }));
  log({ step: "server ready", port, ms: at() - t });
  t = at();

  // The calls the fixture converter logged: each build must add some, or another converter ran.
  const calls = () => existsSync(join(s.fixture, "calls.log")) ? readFileSync(join(s.fixture, "calls.log"), "utf8").split("\n").filter(Boolean).length : 0;
  let builds = 0;
  const cliBuild = async () => {
    const before = calls();
    const b = await owned!.own(`cli-build-${++builds}`, process.execPath, ["scripts/build-docs.ts", "fw9"], { cwd: s.web, env: childEnv(s, "cli"), timeoutMs: 120_000 });
    return { code: await b.exited, converted: calls() > before };
  };

  check(results, "P2", "premise", await settle(base), "the page is current before the first build (a readable stamp equal to the published generation)", print);
  const g0 = 0; // a fresh data root: no publication yet

  const a = await cliBuild();
  const gA = generation(s), tA = truth(s);
  check(results, "P3", "premise", a.code === 0 && gA > g0 && a.converted, `build A exits 0 (${a.code}), publishes (${g0} -> ${gA}) through the fixture converter (${a.converted}); audit ${tA}`, print);
  await poll(base); // marks A stale
  await sleep(100);
  stall(s.gate);
  const trigger = await load(base); // the stale entry, and the regeneration starts
  for (let i = 0; i < 100 && !existsSync(`${s.gate}.ready`); i++) await sleep(50);
  check(results, "P4", "premise", existsSync(`${s.gate}.ready`), "the regeneration's read of the manifest reached the gate", print);
  log({ step: "read held", trigger: trigger.stamp, publishedA: gA, auditA: tA });

  const b = await cliBuild();
  const gB = generation(s), tB = truth(s);
  check(results, "P5", "premise", b.code === 0 && gB > gA && b.converted && tB !== tA, `build B exits 0 (${b.code}), publishes (${gA} -> ${gB}) through the fixture converter (${b.converted}), and changes the audit (${tA} -> ${tB})`, print);
  const marked = await poll(base); // marks B stale; Next applies it when this request has ended
  check(results, "A1", "product", marked.fw9 === gB, `the poll reports the second publication (${marked.fw9})`, print);
  await sleep(100);
  release(s.gate);
  await sleep(300); // the regeneration saves A's page now, after the mark
  log({ step: "read released after the second publication", publishedB: gB, auditB: tB });

  // Plain loads and polls, as a program that does not report would make them.
  const plain: string[] = [];
  for (let i = 0; i < 4; i++) {
    const l = await load(base);
    plain.push(`${l.stamp}:${l.audit}`);
    await poll(base);
    await sleep(300);
  }
  log({ step: "plain loads, plain polls", loads: plain });
  if (!check(results, "P6", "premise", plain.every((l) => l === `${gA}:${tA}`), `the crossing happened: four plain loads stay on A (${gA}:${tA})`, print)) {
    print("the crossing did not happen: Next may now compare marks with read time; see docs/builds.md, 'A stuck page reports itself', before retiring the re-mark");
  }

  // A tab: the product's own client, polling and refreshing, reporting when it is stuck.
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const w = watch(page);
  await page.goto(`${base}/docs/fw9`, { waitUntil: "load" });
  const tabAt = at();
  const first = await domContent(page);
  check(results, "P7", "premise", first === tA, `the new tab first shows A (audit ${first}, A is ${tA})`, print);
  healedMs = await until(page, tB, 30_000);
  const shown = await domContent(page).catch(() => null);
  const tally = (items: string[]) => [...new Set(items)].map((i) => `${i} x ${items.filter((x) => x === i).length}`);
  log({ step: "the tab after the wait", healedMs, shown, refreshes: w.refreshes.length, answers: tally(w.refreshes.map((r) => `${r.cache} ${JSON.stringify(r.stamps)}`)), reloads: w.reloads });
  log({ step: "timeline of the tab (ms from its load)", polls: w.polls.map((p) => `${p.t - tabAt}${p.query ? ` ${p.query}` : ""}`), refreshes: w.refreshes.map((r) => `${r.t - tabAt} ${r.cache} ${JSON.stringify(r.stamps)}`) });
  const reports = w.polls.filter((p) => p.query === `?behind=fw9:${gA}`).length;
  check(results, "A2", "product", reports > 0, `the tab reported ?behind=fw9:${gA} (${reports} polls)`, print);
  const behindRefreshes = w.refreshes.filter((r) => r.stamps?.fw9 !== gB).length;
  check(results, "A3", "product", behindRefreshes > 0, `the tab refreshed while behind (${behindRefreshes} refreshes)`, print);
  const healing = w.refreshes.some((r) => r.stamps?.fw9 === gB);
  if (healedMs !== null && !w.refreshes.some((r) => r.stamps)) throw new HarnessError("the tab shows B, but no refresh had a readable generations stamp");
  check(results, "A4", "product", healedMs !== null && healing, `the tab shows B by itself within 30 s (healedMs ${healedMs}, a refresh stamped {"fw9":${gB}}: ${healing})`, print);
  check(results, "A5", "product", w.reloads === 0 && w.refreshes.length > 0 && w.refreshes.every((r) => r.status === 200), `without a full page load (${w.reloads}), every refresh answered 200`, print);
  const n = w.refreshes.length;
  await sleep(6000);
  check(results, "A6", "product", w.refreshes.length === n, `then no more refreshes (${w.refreshes.length - n} in 6 s)`, print);
  const after = await load(base);
  check(results, "A7", "product", after.stamp === gB && after.audit === tB, `a plain load now carries B (${after.stamp}:${after.audit}, B is ${gB}:${tB})`, print);
  if (opts.control) {
    check(results, "C1", "control", healedMs === null && shown === tA, `the tab is still on A (audit ${shown})`, print);
    check(results, "C2", "control", after.stamp === gA && after.audit === tA, `a plain load still carries A (${after.stamp}:${after.audit})`, print);
  }
  log({ step: "info", behindPolls: reports, publicationsPerBuild: [gA - g0, gB - gA], scenarioMs: at() - t });
  await context.close();
}

try {
  await run();
} catch (e) {
  harness = e instanceof HarnessError ? e.message : String((e as Error)?.stack ?? e);
}
if (interrupted) await forever(); // the signal handler finishes and exits
const exit = classify(results, harness !== null);
let code: number = exit;
if (harness) print(`crossing-mark: could not test (${harness})`);
else if (exit === 0) print("crossing-mark: all passed");
else print(`crossing-mark: ${results.filter((r) => r.kind !== "control" && !r.ok).length} failed`);
if (opts.control) {
  const unexpected = controlVerdict(results, harness !== null, mutations);
  if (unexpected.length) print(`control: unexpected results:\n  ${unexpected.join("\n  ")}`);
  else print("control: the check fails without the re-mark, as it must");
  code = unexpected.length ? 2 : 0;
}
const clean = await finish();
if (interrupted) await forever();
if (!clean) code = 2;
if (opts.record) {
  const versions = { ...nextVersions(join(checkout, "web")), ran };
  const wrote = await recordIfPassed(VERIFIED, { exit: clean ? exit : 2, control: opts.control, interrupted, healedMs, versions });
  print(wrote ? `recorded next ${ran} in ${VERIFIED}` : "not recorded: --record writes only after a full pass of a normal run");
}
print(`exit ${code}`);
process.exit(code);
