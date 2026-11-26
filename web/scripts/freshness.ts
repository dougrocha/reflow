// Whether a published document is what a rebuild in this checkout would publish: current, stale,
// missing or unverifiable, and why. reflow.provenance makes the comparison (python -m
// reflow.provenance check); this file says where each document lives and which Python and
// options a rebuild here would use. build-docs.ts (--check, --stale) and ask-eval.ts use it.
// See docs/provenance.md.

import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import type { ConvertOptions } from "../src/docs";
import { mainCheckout, samplePath } from "./lib/checkout";
import { buildsDir, claimsDir } from "../src/lib/paths";
import { variantOf, variantOptions } from "../src/pipelines";

export const web = resolve(import.meta.dir, "..");
export const repo = resolve(web, "..");

// The venv lives in the main checkout, which every worktree shares. Use it when it is there;
// `uv run --frozen` otherwise, because uv.lock can be stale and a plain `uv run` would sync.
const mainRepo = mainCheckout(repo);
const venvBin = join(mainRepo, ".venv/bin");
export const reflowCmd = existsSync(join(venvBin, "reflow"))
  ? [join(venvBin, "reflow")]
  : ["uv", "run", "--frozen", "--project", repo, "reflow"];
export const pythonCmd = existsSync(join(venvBin, "python"))
  ? [join(venvBin, "python")]
  : ["uv", "run", "--frozen", "--project", repo, "python"];
/** The environment every reflow process of this checkout runs with: its own src first, so a
 *  worktree converts (and checks against) its own converter, not the venv's install of main's.
 *  Never ANTHROPIC_API_KEY, which Bun and Next load from .env: only the converter of a document
 *  that asks the Anthropic API gets it (anthropicKey, in build-docs.ts). */
export function reflowEnv(env: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const { ANTHROPIC_API_KEY: _, ...rest } = env;
  return { ...rest, PYTHONPATH: [join(repo, "src"), env.PYTHONPATH].filter(Boolean).join(delimiter) } as NodeJS.ProcessEnv;
}

/** The reflow.run.Options keywords docs:build converts every document with: REFLOW_ENGINE and
 *  REFLOW_ALT_TEXT when set, else the defaults of `reflow convert-many`, which are Options' own. */
export function buildOptions(env: NodeJS.ProcessEnv = process.env): ConvertOptions {
  const options: ConvertOptions = {};
  if (env.REFLOW_ENGINE) options.engine = env.REFLOW_ENGINE;
  if (env.REFLOW_ALT_TEXT) options.alt_text = env.REFLOW_ALT_TEXT;
  return options;
}

/** One build docs:build can make: a variant of a sample (src/pipelines.ts). `slug` is its storage
 *  key (the sample's slug for the default variant), `base` the sample's slug, whose PDF it reads
 *  and whose PDF copy it shares, and `options` its own (the sample's, then its pipeline's). */
type BuildDoc = { slug: string; base: string; source: string; options: ConvertOptions };

/** The build of a variant key, or null when it names no variant of a sample. */
export function buildDocOf(key: string): BuildDoc | null {
  const v = variantOf(key);
  return v ? { slug: key, base: v.doc.slug, source: v.doc.source, options: variantOptions(v) } : null;
}

/** The options one build converts with: docs:build's, then its own. */
export function docOptions(doc: { options?: ConvertOptions }, shared: ConvertOptions = buildOptions()): ConvertOptions {
  return { ...shared, ...doc.options };
}

const VALUE_FLAGS = [
  ["engine", "--engine"], ["ocr", "--ocr"], ["alt_text", "--alt-text"], ["alt_text_model", "--alt-text-model"],
  ["assist_provider", "--assist-provider"], ["assist_model", "--assist-model"],
  ["direct_model", "--direct-model"], ["direct_prompt", "--direct-prompt"], ["polish_model", "--polish-model"],
] as const;
const SWITCHES = [
  ["assist_labels", "--assist-labels"], ["assist_structure", "--assist-structure"],
  ["assist_scan_fields", "--assist-scan-fields"], ["assist_page_vision", "--assist-page-vision"],
  ["polish", "--polish"],
] as const;

/** Options as `reflow convert-many` flags. */
export function convertArgs(options: ConvertOptions): string[] {
  const args: string[] = [];
  for (const [key, flag] of VALUE_FLAGS) if (options[key]) args.push(flag, options[key]);
  for (const [key, flag] of SWITCHES) if (options[key]) args.push(flag);
  return args;
}

/** Whether a conversion with these options asks the Anthropic API, so needs ANTHROPIC_API_KEY. */
export function usesAnthropic(options: ConvertOptions): boolean {
  const assists = options.assist_labels || options.assist_structure || options.assist_scan_fields;
  const direct = options.engine === "direct" && (options.direct_model ?? "claude-haiku-5-5").startsWith("claude-");
  return direct || !!options.polish || options.alt_text === "anthropic" || (options.assist_provider === "anthropic" && !!assists);
}

/** ANTHROPIC_API_KEY from this checkout's .env, else the main checkout's (both gitignored), for
 *  the convert process of a document that uses anthropic, and nothing else. Undefined when none. */
export function anthropicKey(): string | undefined {
  for (const dir of [repo, mainRepo]) {
    const path = join(dir, ".env");
    if (!existsSync(path)) continue;
    for (const line of readFileSync(path, "utf8").split("\n")) {
      const m = line.match(/^\s*(?:export\s+)?ANTHROPIC_API_KEY\s*=\s*(.*?)\s*$/);
      if (m) return m[1].replace(/^(['"])(.*)\1$/, "$2") || undefined;
    }
  }
  return undefined;
}

/** The page-check harness's stage cache (STAGE_ROOT in tests/pages/corpus.py), in the main
 *  checkout, which every worktree shares: `docs:build --stage-cache` reads and adds to it. */
export function stageRoot(env: NodeJS.ProcessEnv = process.env): string {
  const shared = env.REFLOW_PAGE_CHECKS_CACHE || join(mainRepo, ".pytest_cache");
  return join(shared, "reflow-page-checks-stages");
}

/** A sample's PDF: in this checkout, else in the main checkout (some samples are gitignored). */
export function samplePdf(source: string): string {
  return samplePath(repo, join("samples", source));
}

type Status = "current" | "stale" | "missing" | "unverifiable";
export type Verdict = { slug: string; status: Status; reasons: string[] };
/** Where the documents are: DATA_ROOT, and the public copies when they are to be checked too. */
export type Roots = { data: string; site?: string; pdfs?: string };

function recordPath(data: string, slug: string): string {
  return join(data, ".provenance", `${slug}.json`);
}

/** The SPEC of `python -m reflow.provenance` for these slugs. */
export function provenanceSpec(slugs: string[], roots: Roots, options: ConvertOptions, extra: object = {}) {
  const docs = slugs.map((slug) => {
    const doc = buildDocOf(slug);
    if (!doc) throw new Error(`unknown slug: ${slug}`);
    return {
      slug, pdf: samplePdf(doc.source), data: join(roots.data, slug), record: recordPath(roots.data, slug),
      ...(roots.site ? { site: join(roots.site, slug) } : {}),
      ...(roots.pdfs ? { pdfCopy: join(roots.pdfs, `${doc.base}.pdf`) } : {}),
      ...(Object.keys(doc.options).length ? { options: doc.options } : {}),
    };
  });
  return { src: join(repo, "src", "reflow"), options, docs, ...extra };
}

/** Write a SPEC to a file of its own; `done` removes it. */
export function specFile(spec: object): { path: string; done: () => void } {
  const dir = mkdtempSync(join(tmpdir(), "reflow-provenance-"));
  const path = join(dir, "spec.json");
  writeFileSync(path, JSON.stringify(spec));
  return { path, done: () => rmSync(dir, { recursive: true, force: true }) };
}

/** The verdict of each slug, read only: it takes no claim and writes nothing. A slug with a build
 *  queued or running says so too. Throws when the check cannot run. */
export function checkFreshness(slugs: string[], roots: Roots, options: ConvertOptions = buildOptions()): Verdict[] {
  if (slugs.length === 0) return [];
  const spec = specFile(provenanceSpec(slugs, roots, options));
  try {
    const r = spawnSync(pythonCmd[0], [...pythonCmd.slice(1), "-m", "reflow.provenance", "check", spec.path],
      { cwd: repo, env: reflowEnv(), encoding: "utf8", maxBuffer: 64 << 20 });
    if (r.status !== 0) throw new Error(`the freshness check failed: ${(r.stderr || r.error?.message || "").trim()}`);
    const verdicts = JSON.parse(r.stdout) as Verdict[];
    for (const v of verdicts) {
      if (existsSync(join(claimsDir(buildsDir(roots.data)), `${v.slug}.json`))) v.reasons.push("a build of it is queued or running");
    }
    return verdicts;
  } finally {
    spec.done();
  }
}

/** One line per document, then its reasons, indented. */
export function formatVerdicts(verdicts: Verdict[]): string {
  const width = Math.max(0, ...verdicts.map((v) => v.slug.length));
  return verdicts
    .map((v) => [`${v.slug.padEnd(width)}  ${v.status}`, ...v.reasons.map((r) => `${" ".repeat(width + 2)}- ${r}`)].join("\n"))
    .join("\n");
}

/** A few lines for many documents: the count of each status and, for each that is not current,
 *  its slugs and its most common reason. */
export function summarizeVerdicts(verdicts: Verdict[], most = 8): string {
  const lines: string[] = [];
  for (const status of ["current", "stale", "unverifiable", "missing"] as Status[]) {
    const these = verdicts.filter((v) => v.status === status);
    if (these.length === 0) continue;
    if (status === "current") {
      lines.push(`  ${these.length} current`);
      continue;
    }
    const slugs = these.map((v) => v.slug);
    const shown = slugs.slice(0, most).join(" ") + (slugs.length > most ? ` and ${slugs.length - most} more` : "");
    lines.push(`  ${these.length} ${status}: ${shown}`);
    const reasons = new Map<string, number>();
    for (const v of these) {
      const first = (v.reasons[0] ?? "").split(":")[0];
      reasons.set(first, (reasons.get(first) ?? 0) + 1);
    }
    const [top, n] = [...reasons].sort((a, b) => b[1] - a[1])[0];
    lines.push(`    ${n === these.length ? "all" : `${n} of ${these.length}`}: ${top}`);
  }
  return lines.join("\n");
}
