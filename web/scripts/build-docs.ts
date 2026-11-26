// Convert the sample PDFs with the reflow CLI into .reflow/<slug>/ (DATA_ROOT, gitignored: the
// whole conversion, sidecars and audit included), copy only the public files into
// public/docs/<slug>/, and copy each PDF
// to public/pdfs/<slug>.pdf (what the test page links to).
//
//   bun scripts/build-docs.ts [--parallel N] [--out DIR] [--data DIR] [--pdfs DIR] [slug ...]
//   bun scripts/build-docs.ts --check [--summary | --json] [--present | slug ...]   (bun run docs:check)
//   bun scripts/build-docs.ts --stale [slug ...]
//   bun scripts/build-docs.ts --stage-cache [--stale] [slug ...]
//   bun scripts/build-docs.ts --rerender <slug>
//   bun scripts/build-docs.ts --variant <pipeline> <slug> ...
//   bun scripts/build-docs.ts --model <ollama model> <slug> ...
//
// A slug names a document's default build. A document's other variants (src/pipelines.ts: docling,
// hybrid, haiku, sonnet, opus, ollama-<model>) are named <slug>~<pipeline>, or with --variant, or
// for an Ollama model with --model and the model's id as `ollama list` prints it (gemma4:e4b), and
// stored under that key (.reflow/<slug>~<pipeline>/, public/docs/<slug>~<pipeline>/); they share
// the document's PDF copy. Without slugs, every document's default build is meant.
//
// All documents go through ONE `reflow convert-many` process: up to N (default 4) are converted
// at once on a single shared worker pool, so the machine is used without oversubscribing it.
//
//   --parallel N   documents converted at the same time (default 4)
//   --out DIR      where the public files go (default web/public/docs; env REFLOW_DOCS_OUT)
//   --data DIR     DATA_ROOT, where the whole conversion goes (default web/.reflow; env
//                  REFLOW_DATA_ROOT; with a custom --out, default <parent of --out>/.reflow)
//   --pdfs DIR     where the PDF copies go (default web/public/pdfs; env REFLOW_PDFS_OUT;
//                  with a custom --out, default <parent of --out>/pdfs)
//   --check        build nothing: say whether each document is current, stale, missing or
//                  unverifiable against this checkout, and why. Exits 1 unless all are current.
//                  It takes no claim and writes nothing. --summary prints counts, not every reason.
//                  --json prints the verdicts as a JSON array of { slug, status, reasons }, which
//                  the web app reads (src/lib/variants/stale.ts).
//                  --present checks the documents of the corpus that have a conversion here, and
//                  prints nothing when there are none.
//   --stale        build only the documents that are not current (all of them, or the slugs).
//                  It prints why, or the counts (--summary, or more than 12 documents).
//   --stage-cache  reuse the reading of each PDF (working copy, OCR, extraction, page reads) from
//                  the page-check harness's stage cache in the main checkout, and add to it
//                  (reflow.stagecache, docs/provenance.md). Off by default.
//   --rerender     apply the slug's recorded decisions to its built document without converting it
//                  again (`reflow rerender`, docs/review-decisions.md), as a job of kind "rerender".
//                  The source audit is carried over. When the saved model cannot be proved (none
//                  saved, or the converter, tools, options, OCR data or sample PDF changed), a full
//                  build runs.
//
// Provenance (docs/provenance.md): before a conversion starts, the inputs a rebuild reads (this
// checkout's converter source, the PDF, the options, the dependency versions) are read; once the
// documents are published and audited, each converted one gets DATA_ROOT/.provenance/<slug>.json,
// tied to its conversion by the manifest's "v". --check and --stale compare those records. Its
// public part (build_info: engine, OCR, AI steps and models, decisions, commit, time) is published
// as public/docs/<slug>/build-info.json, which the viewer and the inspector show.
//
// Env: REFLOW_ENGINE (heuristic | docling | hybrid) and REFLOW_ALT_TEXT are passed to
// `reflow convert-many` when set; otherwise the CLI defaults apply. A document's own options
// (SampleDoc.options in src/docs.ts) go over them: the documents that share options convert in one
// `reflow convert-many`, one set after another. ANTHROPIC_API_KEY is read from the gitignored .env
// (this checkout's, else the main checkout's) and given only to the converter of a set that asks
// the Anthropic API.
//
// Every build is also written to the shared build record (DATA_ROOT/.builds/, docs/builds.md), so
// the web app shows it: one job per slug, with the stage and its progress. A slug that already has
// a queued or running build (from the web app or another run of this script) is skipped, so two
// builds of one document never run at once. The web app starts this script with REFLOW_BUILD_JOB=<id>
// for a build it queued; that job is then updated instead of a new one made.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { copyFile, cp, mkdir, rm } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { SAMPLE_DOCS } from "../src/docs";
import { recover, swapIn } from "./lib/swap";
import { PIPELINES, ollamaPipeline, pipelineOf, variantKey } from "../src/pipelines";
import { parseBuildInfo } from "../src/lib/build-info";
import { CANNOT_RERENDER, parseLine, type StageTimings } from "../src/lib/builds/lines";
import { processHelper } from "../src/lib/builds/lock";
import { buildsDir, dataRoot, decisionsDir, docsRoot, pdfsRoot } from "../src/lib/paths";
import { realProcs } from "../src/lib/builds/proc";
import { writeAtomic } from "../src/lib/builds/record";
import type { JobKind, JobRecord } from "../src/lib/builds/view";
import { BuildStore } from "../src/lib/builds/store";
import {
  anthropicKey, buildDocOf, buildOptions, checkFreshness, convertArgs, docOptions, formatVerdicts, usesAnthropic, provenanceSpec, pythonCmd, reflowCmd, reflowEnv, repo,
  samplePdf, specFile, stageRoot, summarizeVerdicts, web, type Roots,
} from "./freshness";

let parallel = 4; // measured: 4 builds about 6% faster than 2, for about 0.5 GB more memory
let outRoot = process.env.REFLOW_DOCS_OUT ?? docsRoot(web);
let dataFlag: string | undefined;
let pdfsFlag: string | undefined = process.env.REFLOW_PDFS_OUT;
const slugs: string[] = [];
let checkOnly = false;
let staleOnly = false;
let summary = false;
let json = false;
let present = false;
let stageCache = false;
let rerendering = false;
let variant: string | undefined;
let model: string | undefined;
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === "--parallel") parallel = Number(argv[++i]);
  else if (a === "--check") checkOnly = true;
  else if (a === "--stale") staleOnly = true;
  else if (a === "--summary") summary = true;
  else if (a === "--json") json = true;
  else if (a === "--present") present = true;
  else if (a === "--stage-cache") stageCache = true;
  else if (a === "--rerender") rerendering = true;
  else if (a === "--variant") variant = argv[++i];
  else if (a.startsWith("--variant=")) variant = a.slice("--variant=".length);
  else if (a === "--model") model = argv[++i];
  else if (a.startsWith("--model=")) model = a.slice("--model=".length);
  else if (a === "--out") outRoot = argv[++i];
  else if (a === "--data") dataFlag = argv[++i];
  else if (a === "--pdfs") pdfsFlag = argv[++i];
  else if (a.startsWith("--")) {
    console.error(`unknown option ${a}`);
    process.exit(2);
  } else slugs.push(a);
}
if (!Number.isInteger(parallel) || parallel < 1) {
  console.error("--parallel needs a whole number of at least 1");
  process.exit(2);
}
if (json && (!checkOnly || summary)) {
  console.error("--json goes with --check, not --summary");
  process.exit(2);
}
if (present && (!checkOnly || slugs.length)) {
  console.error("--present goes with --check and takes no slugs");
  process.exit(2);
}
if (rerendering && (slugs.length !== 1 || checkOnly || staleOnly)) {
  console.error("--rerender takes one document, and not --check or --stale");
  process.exit(2);
}
const kind: JobKind = rerendering ? "rerender" : "build";
outRoot = resolve(outRoot);
// With a custom output root the PDF copies go next to it, never into the real public/pdfs.
const customOut = outRoot !== resolve(docsRoot(web));
const pdfsDir = resolve(pdfsFlag ?? (customOut ? join(dirname(outRoot), "pdfs") : pdfsRoot(web)));
// DATA_ROOT holds what is not served; a custom --out gets its own, never the real web/.reflow.
const siblingData = customOut && !process.env.REFLOW_DATA_ROOT ? join(dirname(outRoot), ".reflow") : undefined;
const data = resolve(dataFlag ?? siblingData ?? dataRoot(process.env, web));

const roots: Roots = { data, site: outRoot, pdfs: pdfsDir };
const options = buildOptions();

// One list of public names, owned by reflow.names.
async function publicNames(): Promise<string[]> {
  const proc = await worker([...pythonCmd, "-c", "from reflow.names import PUBLIC_NAMES; print(*sorted(PUBLIC_NAMES))"], [...records.keys()]);
  const text = await new Response(proc.stdout).text();
  if ((await proc.exited) !== 0) throw new Error("could not read PUBLIC_NAMES from reflow.names");
  return text.trim().split(/\s+/).filter(Boolean);
}

// Replace public/docs/<slug> with only the public names of the conversion, swapping the folder
// in at the end so the site never sees a half-copied document, nor a missing one (scripts/lib/swap.ts). Recorded as a publication of the
// document (store.publish), so the web app expires its cached reads as soon as the swap is done,
// whatever happens to the rest of the build.
async function publish(slug: string, from: string, to: string, names: string[]) {
  await published(slug, async () => {
    const staging = `${to}.new`;
    await recover(to);
    await rm(staging, { recursive: true, force: true });
    await mkdir(staging, { recursive: true });
    for (const name of names) {
      if (existsSync(join(from, name))) await cp(join(from, name), join(staging, name), { recursive: true });
    }
    await swapIn(staging, to);
  });
}

/** Run `write` as a publication of the slug's document, when this run has a record for it. */
async function published(slug: string, write: () => Promise<void>) {
  const job = records.get(slug);
  if (job) await store.publish(job.id, write);
  else await write();
}

// The source-PDF audit, for every sample, converted or not. A failed check exits 1 with the
// report on stdout, which is still the report; only an empty stdout is an error.
async function audit(pdf: string, dest: string, slug: string) {
  const proc = await worker([...reflowCmd, "audit", pdf, "--json"], [slug]);
  const out = await new Response(proc.stdout).text();
  await proc.exited;
  if (!out.trim().startsWith("{")) throw new Error("No audit report was returned.");
  await mkdir(dirname(dest), { recursive: true });
  await writeAtomic(dest, out); // a page that reads it meanwhile gets the old report or the new one
}

// A slug names a sample's default build, or (`<slug>~<pipeline>`, src/pipelines.ts) another
// variant of it; --variant <pipeline> names that variant of every slug given, and --model <id> the
// variant that runs on that Ollama model.
if (variant !== undefined && model !== undefined) {
  console.error("give --variant or --model, not both");
  process.exit(2);
}
if (model !== undefined) {
  const made = ollamaPipeline(model);
  if (!made) {
    console.error(`--model takes an Ollama model id as \`ollama list\` prints it (such as gemma4:e4b), not ${JSON.stringify(model)}`);
    process.exit(2);
  }
  variant = made.name;
}
if (variant !== undefined && !pipelineOf(variant)) {
  console.error(`unknown variant ${JSON.stringify(variant)}: one of ${PIPELINES.map((p) => p.name).join(", ")}, or --model <ollama model>`);
  process.exit(2);
}
if (variant !== undefined && slugs.length === 0) {
  console.error(`${model !== undefined ? "--model" : "--variant"} needs the slugs of the documents to build it for`);
  process.exit(2);
}
const keys = variant === undefined ? slugs : slugs.map((s) => variantKey(s, variant));
const only = new Set(keys);
const unknown = keys.filter((s) => buildDocOf(s) === null);
if (unknown.length) {
  console.error(`unknown slug: ${unknown.join(", ")}`);
  process.exit(2);
}
let wanted = (only.size ? [...only] : SAMPLE_DOCS.map((d) => d.slug)).map((k) => buildDocOf(k)!);
if (present) {
  wanted = wanted.filter((d) => existsSync(join(data, d.slug)));
  if (wanted.length === 0) process.exit(0);
}
// Without slugs, a sample whose source PDF is absent (most are, in a public clone) is skipped.
if (!checkOnly && only.size === 0) {
  const absent = wanted.filter((d) => !existsSync(samplePdf(d.source)));
  for (const d of absent) console.log(`-- ${d.slug}: skipped, source PDF absent (samples/${d.source})`);
  wanted = wanted.filter((d) => !absent.includes(d));
  if (wanted.length === 0) {
    console.error("!! no sample's source PDF is present; nothing to build");
    process.exit(1);
  }
}

// --check and --stale: compare each document with what a rebuild here would use. Read only.
const MANY = 12;
if (checkOnly || staleOnly) {
  if (process.env.REFLOW_BUILD_JOB) {
    console.error("!! --check and --stale do not run a build the web app queued");
    process.exit(2);
  }
  let verdicts;
  try {
    verdicts = checkFreshness(wanted.map((d) => d.slug), roots, options);
  } catch (e) {
    console.error(`!! ${String(e instanceof Error ? e.message : e)}`);
    process.exit(2);
  }
  const behind = verdicts.filter((v) => v.status !== "current");
  if (checkOnly) {
    console.log(json ? JSON.stringify(verdicts) : summary ? summarizeVerdicts(verdicts) : formatVerdicts(verdicts));
    process.exit(behind.length ? 1 : 0);
  }
  // Every reason for a few documents; the counts for many, such as the whole corpus.
  console.log(summary || verdicts.length > MANY ? summarizeVerdicts(verdicts) : formatVerdicts(verdicts));
  if (behind.length === 0) {
    console.log("-- every document asked for is current; nothing to rebuild");
    process.exit(0);
  }
  const rebuild = new Set(behind.map((v) => v.slug));
  wanted = wanted.filter((d) => rebuild.has(d.slug));
}

await mkdir(outRoot, { recursive: true });
await mkdir(pdfsDir, { recursive: true });
await mkdir(data, { recursive: true });

// The build record. A build the web app queued is adopted; otherwise one job per slug is made,
// which takes the slug's claim, and a slug somebody else is building is left to them.
const store = new BuildStore({ dir: buildsDir(data) });
const adopted = process.env.REFLOW_BUILD_JOB;
const records = new Map<string, JobRecord>(); // slug -> its job
const self = { pid: process.pid, pidStart: await realProcs.start(process.pid) };
if (adopted) {
  const job = self.pidStart === null ? null : await store.adopt(adopted, wanted[0]?.slug ?? "", { pid: self.pid, start: self.pidStart }, kind);
  if (!job || wanted.length !== 1 || job.slug !== wanted[0].slug) {
    console.error(`!! REFLOW_BUILD_JOB=${adopted} is not a ${kind} job for ${wanted.map((d) => d.slug).join(", ")}`);
    process.exit(2);
  }
  records.set(job.slug, job);
} else {
  for (const d of wanted) {
    const { job, created } = await store.enqueue(d.slug, "cli", { ...self, kind });
    if (created) records.set(d.slug, job);
    else console.log(`-- ${d.slug}: skipped, already ${job.state} as job ${job.id} (${job.who})`);
  }
}
const docs = wanted.filter((d) => records.has(d.slug));
if (docs.length === 0 && wanted.length > 0) process.exit(0);

// Writes to one job go one after another, so an old progress line cannot land after a newer one.
const chains = new Map<string, Promise<unknown>>();
function record(slug: string, patch: Partial<JobRecord>): Promise<unknown> {
  const job = records.get(slug);
  if (!job) return Promise.resolve();
  const next = (chains.get(slug) ?? Promise.resolve()).then(() => store.update(job.id, patch)).catch((e) => {
    console.error(`!! could not write the build record of ${slug}: ${String(e)}`);
  });
  chains.set(slug, next);
  return next;
}
const settledRecords = new Set<string>();
const endings = new Map<string, { state: "done" | "failed"; error: string | null }>();
/** How a document ended, kept for finalize(). Once the build is stopping, the signal handler decides
 *  for every document that has not ended yet, so a worker it killed does not read as a failure. */
function endRecord(slug: string, state: "done" | "failed", error: string | null = null) {
  if (stopping || settledRecords.has(slug)) return;
  settledRecords.add(slug);
  endings.set(slug, { state, error });
}

// `reflow convert` builds each output beside its folder and swaps it in only on success, so
// the data folder never holds a half-built document and a failed one keeps its previous output.
// Each finished document is then copied, public names only, into public/docs.
const jobs = docs.map((d) => ({
  doc: d,
  src: samplePdf(d.source),
  out: join(data, d.slug),
  site: join(outRoot, d.slug),
}));
const byOut = new Map(jobs.map((j) => [j.out, j]));


let failed = 0;
const settled = new Set<string>();
const timings = new Map<string, StageTimings>(); // by slug: the converter's seconds per stage, for build-info.json
const publishing: Promise<void>[] = [];

function fail(j: (typeof jobs)[number], why = "The conversion failed; the previous output is kept.") {
  if (stopping) return;
  failed++;
  console.error(`!! ${j.doc.slug} failed; the previous output is kept`);
  endRecord(j.doc.slug, "failed", why);
}

// A worker waits for a byte before exec. Its PID is saved first; if this script dies
// before publication, EOF makes the waiting wrapper exit without doing any work.
let stopping = false;
const workers = new Map<number, { start: number; exited: Promise<number> }>();
const launches = new Set<Promise<unknown>>();
function worker(args: string[], slugs: string[], extraEnv: Record<string, string> = {}) {
  const launch = launchWorker(args, slugs, extraEnv);
  launches.add(launch);
  void launch.finally(() => launches.delete(launch)).catch(() => {});
  return launch;
}
async function launchWorker(args: string[], slugs: string[], extraEnv: Record<string, string>) {
  if (stopping) throw new Error("The build is stopping.");
  const env = { ...reflowEnv(), ...extraEnv };
  const proc = Bun.spawn(["python3", processHelper(), "worker", JSON.stringify(args)], { cwd: repo, env, stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  const start = await realProcs.start(proc.pid);
  if (start === null) { proc.stdin.end(); throw new Error("Could not identify the build worker."); }
  workers.set(proc.pid, { start, exited: proc.exited });
  try {
    for (const slug of slugs) {
      await chains.get(slug);
      const job = records.get(slug);
      if (!job) throw new Error(`No build record for ${slug}.`);
      const saved = await store.update(job.id, { childPid: proc.pid, childStart: start });
      if (saved?.childPid !== proc.pid || saved.childStart !== start) throw new Error("The worker identity was not saved.");
    }
    if (!stopping) proc.stdin.write("g");
  } finally { proc.stdin.end(); }
  void new Response(proc.stderr).text().then((text) => { if (text) process.stderr.write(text); });
  void proc.exited.then(() => workers.delete(proc.pid));
  return proc;
}

// Every record ends exactly once, through finalize(): the end of the main flow and the signal
// handler both await the same promise, and once stopping only the handler exits the process.
// A document keeps the ending it reached (done, or failed with its reason); the rest become
// cancelled when a cancel was asked for, interrupted on a signal, failed otherwise.
async function finishRecords(signal?: "SIGTERM" | "SIGINT") {
  await Promise.all(publishing);
  await Promise.all(chains.values());
  for (const [slug, job] of records) {
    const ending = endings.get(slug);
    const asked = !ending && await store.cancelRequested(job.id);
    await store.update(job.id, { childPid: null, childStart: null });
    await store.finish(job.id, ending?.state ?? (asked ? "cancelled" : signal ? "interrupted" : "failed"),
      ending ? ending.error : asked ? null : signal ? `Stopped by ${signal}.` : "The build ended without reporting this document.");
  }
}
let finalizing: Promise<void> | undefined;
const finalize = (signal?: "SIGTERM" | "SIGINT") => (finalizing ??= finishRecords(signal));
const forever = () => new Promise<never>(() => {});

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, async () => {
    if (stopping) return;
    stopping = true;
    try {
      await Promise.allSettled(launches);
      await Promise.all(chains.values());
      for (const [pid, ref] of workers) await realProcs.killTree({ pid, start: ref.start });
      await Promise.all([...workers.values()].map((w) => w.exited));
      await finalize(signal);
      process.exit(signal === "SIGINT" ? 130 : 143);
    } catch (error) {
      console.error(error);
      process.exit(1); // recovery retains claims if a worker could not be stopped
    }
  });
}

// The startup awaits (the PUBLIC_NAMES read, the audit removal, the converter's launch) fail once a
// stop begins: worker() refuses to start, or the stop killed the worker. That failure is the stop,
// not a crash, so it waits for the signal handler, which finishes the records and exits.
let names: string[] = [];
async function convert() {
  for (const j of jobs) await copyFile(j.src, join(pdfsDir, `${j.doc.base}.pdf`)); // a variant shares its sample's copy
  names = await publicNames();
  // A conversion refuses an output folder that holds files it did not write; audit.json is
  // written after it, so clear the old one first. The document page shows it, so its removal is a
  // publication too: the page stops showing an audit that is gone, even if this build then fails.
  // A rerender keeps the report in memory: the PDF is the one it was made from, so it is still true.
  const carried = new Map<string, string>();
  for (const j of jobs) {
    const audit = join(j.out, "audit.json");
    if (!existsSync(audit)) continue;
    if (rerendering) carried.set(j.doc.slug, await Bun.file(audit).text());
    if (stopping) throw new Error("The build is stopping."); // the handler may already have counted the publications
    const removed = published(j.doc.slug, () => rm(audit, { force: true }));
    publishing.push(removed.catch(() => {})); // a stop waits for its publication to be recorded
    await removed;
  }
  // What the conversions read, from before they start: the record of each one is written from it.
  const before = await provenance("snapshot", jobs.map((j) => j.doc.slug));
  // Every build replays the reviewer's decisions: the store is <data>/.decisions/<slug>.jsonl, which
  // convert-many reads as <dir>/<output folder name>.jsonl. A missing file is an empty store. The
  // web app writes the stores (src/lib/decisions); the converter only reads them.
  const decisions = decisionsDir(data);
  await mkdir(decisions, { recursive: true });
  let rerendered = false;
  if (rerendering) {
    const j = jobs[0];
    const own = docOptions(j.doc, options);
    if (!existsSync(join(j.out, "build.json"))) {
      // A conversion from before builds recorded their model (docs/review-decisions.md).
      await say(j, "No saved model; running a full build");
    } else if (optionsMoved(j)) {
      await say(j, "The options changed since the build; running a full build");
    } else {
      // --source and the build's options: a sample PDF or an option that changed since the
      // conversion makes it exit 4, and the full build below runs.
      const args = [...reflowCmd, "rerender", j.out, "--decisions", join(decisions, `${j.doc.slug}.jsonl`),
        "--source", j.src, ...convertArgs({ engine: own.engine, alt_text: own.alt_text })]; // all it takes; optionsMoved checked the rest
      if (stageCache) args.push("--stage-cache", stageRoot());
      args.push("--progress-json");
      console.log(`\n== rerender ${j.doc.slug}`);
      const kept = await recordedTimings(j.site);
      const refusal = await runConverter(args, true);
      if (refusal === null) {
        rerendered = true;
        // The rerender's own seconds say nothing about the conversion: the record keeps the
        // conversion's (none, for a build that recorded none) rather than a rerender's.
        if (kept) timings.set(j.doc.slug, kept);
        else timings.delete(j.doc.slug);
      }
      else await say(j, "The converter changed; running a full build", refusal);
    }
  }
  if (!rerendered) {
    // One converter per set of options, one set after another.
    const sets = new Map<string, typeof jobs>();
    for (const j of jobs) {
      const key = JSON.stringify(convertArgs(docOptions(j.doc, options)));
      sets.set(key, [...(sets.get(key) ?? []), j]);
    }
    for (const set of sets.values()) {
      if (stopping) break;
      const own = docOptions(set[0].doc, options);
      const args = [...reflowCmd, "convert-many"];
      for (const j of set) args.push(j.src, j.out);
      args.push("--max-docs", String(parallel), ...convertArgs(own), "--decisions-dir", decisions);
      if (stageCache) args.push("--stage-cache", stageRoot());
      args.push("--progress-json"); // the web app's progress; the lines are not printed
      const extraEnv: Record<string, string> = {};
      if (usesAnthropic(own)) {
        const key = anthropicKey();
        if (key) extraEnv.ANTHROPIC_API_KEY = key;
        else console.error("!! no ANTHROPIC_API_KEY in .env; the documents that ask the Anthropic API will fail");
      }
      const flags = convertArgs(own).join(" ");
      console.log(`\n== ${set.map((j) => j.doc.slug).join(", ")} (${parallel} at a time${flags ? `; ${flags}` : ""})`);
      await runConverter(args, false, set, extraEnv);
    }
  }

  if (stopping) await forever();

  // The source audit of every requested sample, converted or not (a few at a time). A rerender
  // writes back the report it set aside; with none to carry, it audits like a build.
  const queue = [...jobs];
  await Promise.all(
    Array.from({ length: Math.min(parallel, queue.length) }, async () => {
      for (let j = queue.shift(); j && !stopping; j = queue.shift()) {
        if (settledRecords.has(j.doc.slug)) continue; // a failed conversion
        const saved = rerendered ? carried.get(j.doc.slug) : undefined;
        void record(j.doc.slug, { stage: saved === undefined ? "Auditing the source PDF" : "Publishing", done: null, total: null });
        const audited = published(j.doc.slug, saved === undefined
          ? () => audit(j.src, join(j.out, "audit.json"), j.doc.slug)
          : () => writeAtomic(join(j.out, "audit.json"), saved));
        publishing.push(audited.catch(() => {})); // a stop waits for its publication to be recorded
        try {
          await audited;
        } catch (e) {
          if (!stopping) console.error(`!! ${j.doc.slug}: audit failed: ${String(e)}`);
        }
        // The document is published; a failed audit does not undo that. It is the same as before: logged.
        // A stop that killed the audit is the signal handler's to record.
        endRecord(j.doc.slug, "done");
      }
    }),
  );

  // The provenance of every document this run converted, from the snapshot and the inputs now.
  // Without one, a document reads as unverifiable, never as current.
  const converted = jobs.filter((j) => endings.get(j.doc.slug)?.state === "done").map((j) => j.doc.slug);
  if (stopping || !before || converted.length === 0) return;
  const written = await provenance("record", converted, {
    before, ...(stageCache ? { stageCache: true } : {}), timings: Object.fromEntries(timings),
  });
  for (const slug of converted) {
    const r = (written as Record<string, { changed?: string[]; error?: string; info?: object }> | null)?.[slug];
    if (!r || r.error) console.error(`!! ${slug}: no provenance recorded${r?.error ? `: ${r.error}` : ""}`);
    else if (r.changed?.length) console.error(`!! ${slug}: its ${r.changed.join(", ")} changed during the build; it reads as unverifiable`);
    // "How this transcript was made" (reflow.provenance.build_info): public facts only, in the
    // site's copy, published like the rest of the document so the pages read it at once.
    const info = r?.info;
    const site = jobs.find((j) => j.doc.slug === slug)?.site;
    if (info && site) await published(slug, () => writeAtomic(join(site, "build-info.json"), JSON.stringify(info, null, 1) + "\n"));
  }
}

/** Whether the conversion's build.json records other options than a build here would use. `reflow
 *  rerender --source` checks only the engine and the alt text, and replays the rest as recorded. */
function optionsMoved(j: (typeof jobs)[number]): boolean {
  const script = "import json, sys\n"
    + "from reflow.steps import options_record\n"
    + "from reflow.run import Options\n"
    + "want = options_record(Options(**json.loads(sys.argv[1])))\n"
    + "got = json.load(open(sys.argv[2], encoding='utf-8'))['options']\n"
    + "print(json.dumps(want != got))";
  const r = spawnSync(pythonCmd[0], [...pythonCmd.slice(1), "-c", script, JSON.stringify(docOptions(j.doc, options)), join(j.out, "build.json")],
    { cwd: repo, env: reflowEnv(), encoding: "utf8" });
  return r.status !== 0 || r.stdout.trim() !== "false"; // cannot tell: the full build is the safe one
}

/** Say what the job is doing in its stage, and in the log. */
async function say(j: (typeof jobs)[number], stage: string, why?: string) {
  console.log(`-- ${j.doc.slug}: ${stage}${why ? ` (${why})` : ""}`);
  await record(j.doc.slug, { stage, done: null, total: null });
}

/** The timings the document's published build-info.json holds (those of the conversion that made
 *  it), or undefined when it has none or no readable record. */
async function recordedTimings(site: string): Promise<StageTimings | undefined> {
  try {
    const info = parseBuildInfo(JSON.parse(await Bun.file(join(site, "build-info.json")).text()));
    return info?.timings ? { seconds: info.timings.seconds, gpuWait: info.timings.gpuWait ?? {} } : undefined;
  } catch {
    return undefined;
  }
}

/** Run a converter process (`reflow convert-many`, or `reflow rerender` when `rerender`) and publish
 *  each document it reports done. It prints "start|ok|FAILED<TAB>output dir<TAB>details" as each
 *  document begins or ends, and (with --progress-json) JSON progress lines, which go to the build
 *  record only. Returns null, or, for a rerender that answers `CannotRerender` (exit 4: the saved
 *  model is not the one a full build would make), the reason: nothing is failed or published then,
 *  and the caller runs the full build. */
async function runConverter(args: string[], rerender: boolean, set = jobs, extraEnv: Record<string, string> = {}): Promise<string | null> {
  const proc = await worker(args, set.map((j) => j.doc.slug), extraEnv);
  let refusal: string | null = null;
  const decoder = new TextDecoder();
  let buffer = "";
  const lastProgress = new Map<string, { stage: string; at: number }>();
  function handle(text: string) {
    if (stopping) return;
    const line = parseLine(text);
    if (line.kind === "other") {
      console.log(text);
      return;
    }
    if (line.kind === "timings") {   // kept for the build record; not printed
      const j = byOut.get(line.out);
      if (j) timings.set(j.doc.slug, line.timings);
      return;
    }
    if (line.kind === "progress") {
      const j = byOut.get(line.out);
      if (!j || settled.has(j.out)) return;
      const now = Date.now();
      const last = lastProgress.get(j.out);
      const finishedStage = line.total !== null && line.done === line.total;
      if (last && last.stage === line.stage && !finishedStage && now - last.at < 250) return;
      lastProgress.set(j.out, { stage: line.stage, at: now });
      void record(j.doc.slug, { state: "running", stage: line.stage, done: line.done, total: line.total });
      return;
    }
    console.log(text);
    const j = byOut.get(line.dir);
    if (!j || settled.has(j.out)) return;
    if (line.status === "start") {
      void record(j.doc.slug, { state: "running", startedAt: Date.now(), stage: "Starting", done: null, total: null });
      return;
    }
    if (line.status === "FAILED" && rerender && line.details.startsWith(`${CANNOT_RERENDER}:`)) {
      refusal = line.details.slice(CANNOT_RERENDER.length + 1).trim() || "the saved model cannot be proved";
      return;
    }
    settled.add(j.out);
    if (line.status === "FAILED") fail(j, line.details || "The conversion failed.");
    else {
      void record(j.doc.slug, { stage: "Publishing", done: null, total: null });
      publishing.push(
        publish(j.doc.slug, j.out, j.site, names).catch((e) => {
          fail(j, `Publishing failed: ${String(e)}`);
          console.error(String(e));
        }),
      );
    }
  }
  const reader = proc.stdout.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      handle(buffer.slice(0, nl));
      buffer = buffer.slice(nl + 1);
    }
  }
  if (buffer) handle(buffer);
  await proc.exited;
  await Promise.all(publishing);
  if (refusal !== null) return refusal;
  // A document that never reported (the process died) counts as failed.
  for (const j of set) if (!settled.has(j.out)) fail(j, "The converter ended without reporting this document.");
  return null;
}

/** Run `python -m reflow.provenance <command>` for the slugs on a worker, and its JSON answer.
 *  A failure is logged, not thrown: the documents then have no record, so they read as
 *  unverifiable, and the build goes on. */
async function provenance(command: "snapshot" | "record", slugs: string[], extra: object = {}): Promise<unknown> {
  const spec = specFile(provenanceSpec(slugs, roots, options, extra));
  try {
    const proc = await worker([...pythonCmd, "-m", "reflow.provenance", command, spec.path], slugs);
    const [out, code] = await Promise.all([new Response(proc.stdout).text(), proc.exited]);
    if (code !== 0) throw new Error(`exit ${code}`);
    return JSON.parse(out);
  } catch (e) {
    if (!stopping) console.error(`!! could not ${command} the provenance of ${slugs.join(", ")}: ${String(e)}`);
    return null;
  } finally {
    spec.done();
  }
}
try {
  if (jobs.length) await convert();
} catch (error) {
  if (stopping) await forever();
  throw error;
}

if (stopping) await forever();
await finalize();
if (stopping) await forever(); // a signal during finalize(): the handler exits once it is done
if (failed) {
  console.error(`\n${failed} of ${docs.length} documents failed`);
  process.exit(1);
}
