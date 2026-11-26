import { afterEach, beforeEach, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { provenanceSpec, pythonCmd, reflowCmd, reflowEnv, repo, samplePdf, specFile } from "../../../scripts/freshness";
import { append } from "../decisions/store";
import type { Keep } from "../decisions/types";
import { buildsDir } from "../paths";
import { listJobs } from "./record";
import { BuildStore } from "./store";
import { removeDir, tempDir } from "./testkit";

// build-docs.ts against a temporary folder, in the cases that end before any conversion starts.
// (The real builds are checked in the browser run of docs/builds.md.)

const WEB = join(import.meta.dir, "../../..");
let root: string;
beforeEach(async () => (root = await tempDir()));
afterEach(() => removeDir(root));

async function run(args: string[], env: Record<string, string> = {}) {
  const proc = Bun.spawn(["bun", join(WEB, "scripts/build-docs.ts"), "--out", join(root, "docs"), ...args], {
    cwd: WEB, stdout: "pipe", stderr: "pipe", env: { ...process.env, REFLOW_BUILD_JOB: "", ...env },
  });
  const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
  return { out, err, code };
}
const store = () => new BuildStore({ dir: buildsDir(join(root, ".reflow")) });

test("a slug that already has a queued build is skipped, and nothing is converted", async () => {
  const queued = await store().enqueue("fw9", "ui");
  const r = await run(["fw9"]);
  expect(r.code).toBe(0);
  expect(r.out).toContain(`fw9: skipped, already queued as job ${queued.job.id} (ui)`);
  expect(r.out).not.toContain("== ");
  expect(await listJobs(buildsDir(join(root, ".reflow")))).toHaveLength(1);
}, 30_000);

test("a job id that is not for the slug asked for is refused", async () => {
  const other = await store().enqueue("resnet", "ui");
  const r = await run(["fw9"], { REFLOW_BUILD_JOB: other.job.id });
  expect(r.code).toBe(2);
  expect(r.err).toContain("is not a build job for fw9");
}, 30_000);

test("a job that already ended is not run again", async () => {
  const s = store();
  const { job } = await s.enqueue("fw9", "ui");
  await s.finish(job.id, "cancelled");
  const r = await run(["fw9"], { REFLOW_BUILD_JOB: job.id });
  expect(r.code).toBe(2);
  expect(r.out).not.toContain("== ");
}, 30_000);

// --check and --stale, against documents laid out in the temporary folder.

const noOptions = { REFLOW_ENGINE: "", REFLOW_ALT_TEXT: "" };
const roots = () => ({ data: join(root, ".reflow"), site: join(root, "docs"), pdfs: join(root, "pdfs") });

function provenance(command: "snapshot" | "record", slugs: string[], extra: object = {}): unknown {
  const spec = specFile(provenanceSpec(slugs, roots(), {}, extra));
  try {
    const r = spawnSync(pythonCmd[0], [...pythonCmd.slice(1), "-m", "reflow.provenance", command, spec.path],
      { cwd: repo, env: reflowEnv(), encoding: "utf8" });
    expect(r.stderr).toBe("");
    return JSON.parse(r.stdout);
  } finally {
    spec.done();
  }
}

/** Lay fw9 out as a build here leaves it, with its provenance record: a current document. */
function builtHere(slug = "fw9", source = "fw9.pdf") {
  const before = provenance("snapshot", [slug]);
  for (const dir of [join(root, ".reflow", slug), join(root, "docs", slug)]) {
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "manifest.json"), JSON.stringify({ v: "0123456789ab" }));
  }
  mkdirSync(join(root, "pdfs"), { recursive: true });
  copyFileSync(samplePdf(source), join(root, "pdfs", `${slug}.pdf`));
  provenance("record", [slug], { before });
}

test("--check says why each document is or is not current, and takes no claim", async () => {
  builtHere();
  const r = await run(["--check", "fw9", "resnet"], noOptions);
  expect(r.code).toBe(1);
  expect(r.out).toMatch(/fw9\s+current/);
  expect(r.out).toMatch(/resnet\s+missing/);
  expect(existsSync(buildsDir(join(root, ".reflow")))).toBe(false);
}, 30_000);

test("--check sees options that differ from the build's", async () => {
  builtHere();
  const r = await run(["--check", "fw9"], { ...noOptions, REFLOW_ENGINE: "hybrid" });
  expect(r.code).toBe(1);
  expect(r.out).toMatch(/fw9\s+stale/);
  expect(r.out).toContain("engine heuristic -> hybrid");
}, 60_000);

test("--stale with every document current builds nothing", async () => {
  builtHere();
  const r = await run(["--stale", "fw9"], noOptions);
  expect(r.code).toBe(0);
  expect(r.out).toContain("nothing to rebuild");
  expect(r.out).not.toContain("== ");
  expect(await listJobs(buildsDir(join(root, ".reflow")))).toHaveLength(0);
}, 30_000);

test("--stale leaves a document somebody else is building to them", async () => {
  const queued = await store().enqueue("resnet", "ui");
  const r = await run(["--stale", "resnet"], noOptions);
  expect(r.code).toBe(0);
  expect(r.out).toContain("a build of it is queued or running");
  expect(r.out).toContain(`resnet: skipped, already queued as job ${queued.job.id} (ui)`);
  expect(r.out).not.toContain("== ");
}, 30_000);

test("--check and --stale never run a build the web app queued", async () => {
  const { job } = await store().enqueue("fw9", "ui");
  for (const flag of ["--check", "--stale"]) {
    const r = await run([flag, "fw9"], { REFLOW_BUILD_JOB: job.id });
    expect(r.code).toBe(2);
  }
}, 30_000);

// The converter gets --decisions-dir <data>/.decisions, so every build replays the store: a store
// seeded for fw9 shows in the ledger the conversion publishes (decisions.json names the store's id).
test("a build passes --decisions-dir <data>/.decisions and the converter reads the store there", async () => {
  const data = join(root, ".reflow");
  await append("fw9", { kind: "keep", id: "m2k9x4q1-00000001", at: "2026-11-02T10:00:00Z", pdf: "a1".repeat(32), supersedes: null,
    anchor: { page: 1, bbox: [72, 100, 300, 112], text_sha: "0123456789abcdef", crop_sha: "", hint: "n-1" },
    range: { was: "8011O", before: "CO ", after: "", rule: "zip-digit" } }, data);
  const header = JSON.parse(readFileSync(join(data, ".decisions", "fw9.jsonl"), "utf8").split("\n")[0]!);
  const r = await run(["fw9"], noOptions);
  expect(r.code).toBe(0);
  const ledger = JSON.parse(readFileSync(join(root, "docs", "fw9", "decisions.json"), "utf8"));
  expect(ledger.store).toMatchObject({ id: header.id, lines: 2, error: null });
  expect(ledger.orphaned).toHaveLength(1); // made on another PDF: listed, never applied
  expect(ledger.orphaned[0]).toMatchObject({ id: "m2k9x4q1-00000001", reason: "other-pdf" });
}, 240_000);

test("a build with no store still makes the store folder and publishes an empty ledger", async () => {
  const r = await run(["fw9"], noOptions);
  expect(r.code).toBe(0);
  expect(existsSync(join(root, ".reflow", ".decisions"))).toBe(true);
  expect(JSON.parse(readFileSync(join(root, "docs", "fw9", "decisions.json"), "utf8")).store.id).toBeNull();
}, 240_000);

// --rerender: the decisions are replayed on the model the last build saved (reflow rerender), not
// converted again; the audit is carried; a model that cannot be proved sends the job to a full build.

const cached = () => ({ ...noOptions, REFLOW_PAGE_CHECKS_CACHE: join(root, "stage-cache") });
const orphan: Keep = {
  kind: "keep", id: "m2k9x4q1-00000001", at: "2026-11-02T10:00:00Z", pdf: "a1".repeat(32), supersedes: null,
  anchor: { page: 1, bbox: [72, 100, 300, 112], text_sha: "0123456789abcdef", crop_sha: "", hint: "n-1" },
  range: { was: "8011O", before: "CO ", after: "", rule: "zip-digit" },
};
const ledger = () => JSON.parse(readFileSync(join(root, "docs", "fw9", "decisions.json"), "utf8"));
const auditFile = () => join(root, ".reflow", "fw9", "audit.json");
const buildInfo = () => JSON.parse(readFileSync(join(root, "docs", "fw9", "build-info.json"), "utf8"));

test("--rerender replays the store on the saved model, carries the audit and records a rerender job", async () => {
  const first = await run(["--stage-cache", "fw9"], cached());
  expect(first.code).toBe(0);
  expect(ledger().store.id).toBeNull();
  const converted = buildInfo().timings;
  expect(Object.keys(converted.seconds).length).toBeGreaterThan(0);
  writeFileSync(auditFile(), '{"carried": "marker"}'); // a new audit would replace it
  await append("fw9", orphan, join(root, ".reflow"));
  const r = await run(["--stage-cache", "--rerender", "fw9"], cached());
  expect(r.code).toBe(0);
  expect(r.out).toContain("== rerender fw9");
  expect(buildInfo().timings).toEqual(converted); // the conversion's seconds, not the rerender's
  expect(r.out).not.toContain("running a full build");
  expect(ledger().store).toMatchObject({ lines: 2, error: null });
  expect(readFileSync(auditFile(), "utf8")).toBe('{"carried": "marker"}');
  const jobs = await listJobs(buildsDir(join(root, ".reflow")));
  expect(jobs.map((j) => [j.kind, j.state]).sort()).toEqual([["build", "done"], ["rerender", "done"]]);
}, 480_000);

test("--rerender on a conversion the converter changed says so and runs a full build", async () => {
  expect((await run(["--stage-cache", "fw9"], cached())).code).toBe(0);
  const file = join(root, ".reflow", "fw9", "build.json");
  const record = JSON.parse(readFileSync(file, "utf8"));
  record.model.key = "0".repeat(64);
  record.model.inputs.code = "0".repeat(64); // as if the converter's code had changed since
  writeFileSync(file, JSON.stringify(record));
  writeFileSync(auditFile(), '{"carried": "marker"}');
  await append("fw9", orphan, join(root, ".reflow"));
  const r = await run(["--stage-cache", "--rerender", "fw9"], cached());
  expect(r.code).toBe(0);
  expect(r.out).toContain("The converter changed; running a full build");
  expect(ledger().store).toMatchObject({ lines: 2, error: null }); // the full build applied them too
  expect(readFileSync(auditFile(), "utf8")).not.toContain("marker"); // and audited again
  const jobs = await listJobs(buildsDir(join(root, ".reflow")));
  expect(jobs.filter((j) => j.kind === "rerender").map((j) => j.state)).toEqual(["done"]);
}, 480_000);

test("--rerender of a document whose options changed since its build runs a full build", async () => {
  expect((await run(["fw9"], noOptions)).code).toBe(0);
  rmSync(join(root, ".reflow", ".provenance"), { recursive: true }); // the build.json decides, not the record
  const file = join(root, ".reflow", "fw9", "build.json");
  const record = JSON.parse(readFileSync(file, "utf8"));
  record.options.ocr = "none"; // as if built with an option the sample no longer has
  writeFileSync(file, JSON.stringify(record));
  const r = await run(["--rerender", "fw9"], noOptions);
  expect(r.code).toBe(0);
  expect(r.out).toContain("The options changed since the build; running a full build");
  expect(r.out).not.toContain("== rerender fw9");
}, 480_000);

test("--rerender on a conversion with no saved model runs a full build", async () => {
  expect((await run(["fw9"], noOptions)).code).toBe(0);
  rmSync(join(root, ".reflow", "fw9", "build.json")); // as if built before builds recorded one
  await append("fw9", orphan, join(root, ".reflow"));
  const r = await run(["--rerender", "fw9"], noOptions);
  expect(r.code).toBe(0);
  expect(r.out).toContain("running a full build");
  expect(ledger().store).toMatchObject({ lines: 2, error: null });
}, 480_000);

test("--rerender of a conversion whose sample PDF changed since runs a full build", async () => {
  // The conversion in .reflow/fw9 is of a PDF that is no longer the sample (one byte more after
  // %%EOF), with a build.json true to it: only the sample itself can say the model is stale.
  const old = join(root, "old", "fw9.pdf");
  mkdirSync(join(root, "old"));
  writeFileSync(old, Buffer.concat([readFileSync(samplePdf("fw9.pdf")), Buffer.from("\n")]));
  mkdirSync(join(root, ".decisions-old"));
  const made = spawnSync(reflowCmd[0], [...reflowCmd.slice(1), "convert", old, "-o", join(root, ".reflow", "fw9"),
    "--decisions", join(root, ".decisions-old", "fw9.jsonl")], { cwd: repo, env: reflowEnv(), encoding: "utf8" });
  expect(made.status).toBe(0);
  expect(existsSync(join(root, ".reflow", "fw9", "build.json"))).toBe(true);
  await append("fw9", orphan, join(root, ".reflow"));
  const r = await run(["--rerender", "fw9"], noOptions);
  expect(r.code).toBe(0);
  expect(r.out).toContain("CannotRerender: pdf:");
  expect(r.out).toContain("The converter changed; running a full build");
  expect(ledger().store).toMatchObject({ lines: 2, error: null });
}, 480_000);

test("--rerender takes one slug, and not --check or --stale", async () => {
  for (const args of [["--rerender"], ["--rerender", "fw9", "resnet"], ["--rerender", "--stale", "fw9"]]) {
    expect((await run(args, noOptions)).code).toBe(2);
  }
}, 30_000);

test("a build job id is refused by a --rerender run, and a rerender job id by a build", async () => {
  const s = store();
  const { job } = await s.enqueue("fw9", "ui", { kind: "rerender" });
  const r = await run(["fw9"], { REFLOW_BUILD_JOB: job.id });
  expect(r.code).toBe(2);
  const { job: other } = await s.enqueue("resnet", "ui");
  expect((await run(["--rerender", "resnet"], { REFLOW_BUILD_JOB: other.id })).code).toBe(2);
}, 30_000);
