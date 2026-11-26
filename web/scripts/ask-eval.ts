// Golden questions for "Ask this document".
//
//   bun run ask:eval                     retrieval only: no model, no network, seconds
//   bun run ask:eval -- --model          the same questions through the real server path and model
//   bun run ask:eval -- --bench <slug>   time and size of loading one document's corpus
//   --slug <slug>, --set tune|hold|new|confirm   keep only one document's or one set's questions
//   --retriever bm25|embed|hybrid, --embed-model <tag>   the experimental embedding retriever
//          (src/lib/ask/retriever.ts; REFLOW_ASK_RETRIEVER and REFLOW_ASK_EMBED_MODEL otherwise).
//          Retrieval and bench modes only; --model uses the server's own retriever from the env.
//   --json <file>   write the per-question results (retrieval and model modes)
//   --sample <n>    with --bench and an embedding retriever: embed only n passages spread over the
//          document and extrapolate the time, instead of embedding all of it (minutes on a large one)
//
// Both modes read the converted documents in .reflow/<slug>/passages.jsonl (REFLOW_DATA_ROOT
// overrides), so run `bun run docs:build --stale` with the slugs in ask-golden.ts (GOLDEN_SLUGS) first. Run from web/.
// Both refuse (exit 2) while one of those documents is not current against this checkout, because a
// seeded or stale conversion gives a wrong baseline (docs/provenance.md). --allow-stale measures
// anyway, says which documents are behind, and puts their status in the --json file.
//
// Retrieval mode asks the corpus for its top 8 passages and checks that one of them holds the
// expected text (a substring, compared with case, spacing and quote style folded). It prints
// recall@1 and recall@8 and exits 1 when either is under its floor (FLOOR_AT_1, FLOOR_AT_8).
//
// --model mode is manual. It calls ask() from @/lib/ask, the very function the route calls, so the
// provider, model and prompt are exactly the server's: REFLOW_ASK_PROVIDER, REFLOW_ASK_MODEL,
// OLLAMA_HOST (see docs/ask.md). For each question it reports one of:
//   right        validated, and a cited passage holds the expected text
//   mis-cited    validated, but no cited passage holds the expected text (a wrong place, well quoted)
//   unverified   the model said it found an answer, but the quote or a number did not check out
//   declined     the model said the passages do not state the answer
//   no-match     retrieval found nothing (no model call)
//   error        the provider failed, or its reply was unusable
// Five questions have no answer in the document: "refused" when no answer is shown, "invented" when
// a validated answer is.
// "found" is right + mis-cited + unverified; "validated" is right + mis-cited.
// With the anthropic provider it sends passages to the Anthropic API, so it refuses to run
// unless you also pass --send-to-anthropic.
//
// Lint: web/.oxlintrc.json keeps @/lib/ask/* private to the folder. This script needs the
// retrieval top 8 (corpus.retrieve) and the retrievers (retriever.ts), which the public surface
// (ask, askReady, statusOf) does not give, so its imports of @/lib/ask/corpus and
// @/lib/ask/retriever below each carry a narrow eslint-disable. The public
// surface stays as small as it was; the model mode uses only the public ask().

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { ask } from "@/lib/ask";
import { dataRoot } from "@/lib/paths";
import { plural } from "@/lib/format";
import { passagesPath } from "@/lib/published";
// eslint-disable-next-line no-restricted-imports -- the eval needs corpus.retrieve and parseCorpus, which the public surface lacks
import { loadCorpus, parseCorpus } from "@/lib/ask/corpus";
// eslint-disable-next-line no-restricted-imports -- the eval compares retrievers, which the public surface does not expose
import { cacheBytes, cachePaths, embedCorpus, ollamaEmbedder, queryText, readRetrieverConfig, retrieverFor, writeVectors } from "@/lib/ask/retriever";
import type { AskResult } from "@/lib/ask-contract";
import { GOLDEN, type GoldenSet, type TaggedGolden } from "./ask-golden";
import { checkFreshness, formatVerdicts, type Verdict as Freshness } from "./freshness";
import { norm } from "./lib/text";

/** The floors for retrieval mode: a few points under the measured run (recall@1 69/107 = 64.5%, recall@8 103/107 = 96.3%, 2026-11-01, docs/ask.md). */
const FLOOR_AT_1 = 0.58;
const FLOOR_AT_8 = 0.9;

// ---- arguments ------------------------------------------------------------------------------

const argv = process.argv.slice(2);
const wantModel = argv.includes("--model");
const only = argv.includes("--slug") ? argv[argv.indexOf("--slug") + 1] : null;
const jsonOut = argv.includes("--json") ? argv[argv.indexOf("--json") + 1] : null;
const onlySet = argv.includes("--set") ? (argv[argv.indexOf("--set") + 1] as GoldenSet) : null;
const questions = GOLDEN.filter((g) => (!only || g.slug === only) && (!onlySet || g.set === onlySet));
const answerable = questions.filter((g) => g.expect !== null) as (TaggedGolden & { expect: string })[];

const flag = (name: string): string | undefined => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : undefined);
// --model reports what ask() retrieves, and ask() reads only the environment, so the flags apply
// to retrieval and bench modes alone.
const retrieverConfig = readRetrieverConfig(wantModel ? process.env : {
  ...process.env,
  ...(flag("--retriever") ? { REFLOW_ASK_RETRIEVER: flag("--retriever") } : {}),
  ...(flag("--embed-model") ? { REFLOW_ASK_EMBED_MODEL: flag("--embed-model") } : {}),
});
const retrieve = retrieverFor(retrieverConfig, { log: (l) => console.error(`[retriever] ${l}`) });
const retrieverName = retrieverConfig.mode === "bm25" ? "bm25" : `${retrieverConfig.mode} ${retrieverConfig.model}`;

const allowStale = argv.includes("--allow-stale");
const data = resolve(dataRoot());

/** The passage texts of each node of a document, for checking what a citation points at. */
function nodeTexts(slug: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  const lines = readFileSync(passagesPath(data, slug), "utf8").split("\n").filter(Boolean);
  for (const line of lines.slice(1)) {
    const p = JSON.parse(line) as { id: string; t: string };
    const node = p.id.split("#")[0];
    const list = out.get(node) ?? [];
    list.push(norm(p.t));
    out.set(node, list);
  }
  return out;
}

// ---- retrieval mode -------------------------------------------------------------------------

async function retrievalMode(): Promise<number> {
  console.log(`retriever ${retrieverName}\n`);
  let at1 = 0;
  let at8 = 0;
  const rows: { set: GoldenSet; slug: string; question: string; rank: number; ms: number }[] = [];
  const bySet = new Map<GoldenSet, { n: number; at1: number; at8: number }>();
  for (const g of answerable) {
    const corpus = await loadCorpus(g.slug);
    if (!corpus) {
      console.error(`${g.slug}: no passages.jsonl in ${data}; run bun run docs:build ${g.slug}`);
      return 2;
    }
    const q0 = performance.now();
    const hits = await retrieve(corpus, g.question, 8);
    const ms = performance.now() - q0;
    const want = norm(g.expect);
    const rank = hits.findIndex((h) => norm(h.passage.text).includes(want)) + 1;
    if (rank === 1) at1++;
    if (rank >= 1) at8++;
    const tally = bySet.get(g.set) ?? { n: 0, at1: 0, at8: 0 };
    tally.n++;
    if (rank === 1) tally.at1++;
    if (rank >= 1) tally.at8++;
    bySet.set(g.set, tally);
    rows.push({ set: g.set, slug: g.slug, question: g.question, rank, ms });
    const label = rank ? `rank ${rank}` : "MISS";
    console.log(`${label.padEnd(7)} ${g.set.padEnd(4)} ${g.slug.padEnd(10)} ${g.question}`);
  }
  const n = answerable.length;
  const r1 = at1 / n;
  const r8 = at8 / n;
  console.log("");
  for (const set of ["tune", "hold", "new", "confirm"] as const) {
    const t = bySet.get(set);
    if (t) console.log(`${set.padEnd(5)} ${String(t.n).padStart(2)} questions: recall@1 ${t.at1}/${t.n} = ${((t.at1 / t.n) * 100).toFixed(1)}%   recall@8 ${t.at8}/${t.n} = ${((t.at8 / t.n) * 100).toFixed(1)}%`);
  }
  console.log(`\nrecall@1 ${at1}/${n} = ${(r1 * 100).toFixed(1)}%   (floor ${(FLOOR_AT_1 * 100).toFixed(0)}%)`);
  console.log(`recall@8 ${at8}/${n} = ${(r8 * 100).toFixed(1)}%   (floor ${(FLOOR_AT_8 * 100).toFixed(0)}%)`);
  if (jsonOut) {
    await Bun.write(jsonOut, JSON.stringify({ retriever: retrieverName, n, at1, at8, sets: Object.fromEntries(bySet), rows }, null, 2));
    console.log(`wrote ${jsonOut}`);
  }
  if (only || onlySet) return 0; // a floor over one document or one set means little
  if (r1 < FLOOR_AT_1 || r8 < FLOOR_AT_8) {
    console.log("FAIL: recall under its floor");
    return 1;
  }
  console.log("PASS");
  return 0;
}

// ---- model mode -----------------------------------------------------------------------------

type Verdict = "right" | "mis-cited" | "unverified" | "declined" | "no-match" | "error" | "refused" | "invented";

async function askOnce(slug: string, question: string, version: string): Promise<AskResult> {
  for (let attempt = 0; attempt < 6; attempt++) {
    const result = await ask(slug, { question, version }, new AbortController().signal);
    if (result.kind !== "busy") return result;
    await Bun.sleep((result.retryAfterSeconds + 1) * 1000);
  }
  return { kind: "error", why: "provider-failed" };
}

async function modelMode(): Promise<number> {
  const provider = (process.env.REFLOW_ASK_PROVIDER ?? "ollama").toLowerCase();
  if (provider === "anthropic" && !argv.includes("--send-to-anthropic")) {
    console.error("REFLOW_ASK_PROVIDER is anthropic: the passages for each question would be sent to the Anthropic API.\nPass --send-to-anthropic to do that.");
    return 2;
  }
  const model = process.env.REFLOW_ASK_MODEL || "(the provider's default)";
  console.log(`provider ${provider}, model ${model}, ${questions.length} questions (${questions.filter((g) => g.expect === null).length} unanswerable)\n`);
  const counts: Record<Verdict, number> = { right: 0, "mis-cited": 0, unverified: 0, declined: 0, "no-match": 0, error: 0, refused: 0, invented: 0 };
  const texts = new Map<string, Map<string, string[]>>();
  let retrievable = 0;
  const started = Date.now();
  const rows: { slug: string; question: string; verdict: Verdict; detail: string; ms: number }[] = [];
  for (const g of questions) {
    const corpus = await loadCorpus(g.slug);
    if (!corpus) {
      console.error(`${g.slug}: no passages.jsonl in ${data}; run bun run docs:build ${g.slug}`);
      return 2;
    }
    if (!texts.has(g.slug)) texts.set(g.slug, nodeTexts(g.slug));
    const want = g.expect === null ? null : norm(g.expect);
    if (want !== null && (await retrieve(corpus, g.question, 8)).some((h) => norm(h.passage.text).includes(want))) retrievable++;
    const t0 = Date.now();
    const result = await askOnce(g.slug, g.question, corpus.version);
    const ms = Date.now() - t0;
    if (result.kind === "unavailable") {
      console.error(`The provider is unavailable (${result.why}). Check docs/ask.md, "Set up the model". Nothing was measured.`);
      return 2;
    }
    let verdict: Verdict;
    let detail = "";
    if (result.kind === "answer") {
      const byNode = texts.get(g.slug)!;
      const holds = want !== null && result.evidence.some((e) => (byNode.get(e.node) ?? []).some((t) => t.includes(want)) || norm(e.quote).includes(want));
      verdict = want === null ? "invented" : holds ? "right" : "mis-cited";
      detail = result.evidence.map((e) => `p${e.page} "${e.quote.slice(0, 60)}"`).join(" ; ");
    } else if (result.kind === "none") {
      verdict = want === null ? "refused" : result.why === "unverified" ? "unverified" : result.why === "no-match" ? "no-match" : "declined";
    } else {
      verdict = "error";
      detail = result.kind === "error" ? result.why : result.kind;
    }
    counts[verdict]++;
    rows.push({ slug: g.slug, question: g.question, verdict, detail, ms });
    console.log(`${verdict.padEnd(10)} ${(ms / 1000).toFixed(1).padStart(5)}s ${g.slug.padEnd(10)} ${g.question}${detail ? `\n${" ".repeat(29)}${detail}` : ""}`);
  }
  const n = answerable.length;
  const unanswerable = questions.length - n;
  const found = counts.right + counts["mis-cited"] + counts.unverified;
  const validated = counts.right + counts["mis-cited"];
  const seconds = (Date.now() - started) / 1000;
  console.log(`\nmodel ${model}: ${questions.length} questions in ${seconds.toFixed(0)} s (${(seconds / questions.length).toFixed(1)} s each)`);
  console.log(`answerable (${n}):`);
  console.log(`retrieval put the expected passage in the top 8 for ${retrievable} of ${n}`);
  console.log(`found ${found}, validated ${validated}, right ${counts.right}, mis-cited ${counts["mis-cited"]}, unverified ${counts.unverified}, declined ${counts.declined}, no-match ${counts["no-match"]}, error ${counts.error}`);
  console.log(`unanswerable (${unanswerable}): refused ${counts.refused}, invented ${counts.invented} (a validated answer where the document has none)`);
  if (jsonOut) {
    const documents = freshness.map((v) => ({ slug: v.slug, status: v.status }));
    await Bun.write(jsonOut, JSON.stringify({ provider, model, n, retrievable, counts, found, validated, seconds, documents, rows }, null, 2));
    console.log(`wrote ${jsonOut}`);
  }
  return 0;
}

// ---- bench mode -----------------------------------------------------------------------------

const mb = (n: number) => `${(n / 1048576).toFixed(1)} MB`;

/** Build the corpus of one document three times and report size, passages, build time, heap and
 *  the time of a question. Bun's heap, not Node's: the server's own memory is in docs/ask.md. */
async function benchMode(slug: string): Promise<number> {
  const path = passagesPath(data, slug);
  const text = readFileSync(path, "utf8");
  console.log(`${slug}: ${mb(Buffer.byteLength(text))} on disk, ${text.split("\n").length - 2} passages`);
  for (let run = 1; run <= 3; run++) {
    Bun.gc(true);
    const before = process.memoryUsage().heapUsed;
    const rssBefore = process.memoryUsage().rss;
    const t0 = performance.now();
    const corpus = parseCorpus(slug, text);
    const ms = performance.now() - t0;
    Bun.gc(true);
    const held = process.memoryUsage().heapUsed - before;
    const rssGrew = process.memoryUsage().rss - rssBefore;
    const times: number[] = [];
    for (const q of ["What is the phone number of Village Medical in Mesa?", "Which doctors speak Spanish in Peoria?", "Where is Desert Spine and Sports located?", "Chandler Blvd medical office address", "Moher Christian MD"]) {
      const q0 = performance.now();
      corpus.retrieve(q, 8);
      times.push(performance.now() - q0);
    }
    times.sort((a, b) => a - b);
    console.log(`build ${run}: ${ms.toFixed(0)} ms, heap held ${mb(held)}, process memory +${mb(rssGrew)}, question median ${times[2].toFixed(1)} ms, slowest ${times[4].toFixed(1)} ms (${corpus.passageCount} passages)`);
  }
  if (retrieverConfig.mode !== "bm25") await benchEmbeddings(slug, text);
  return 0;
}

/** The embedding side of --bench: embed every passage once (timed, cache rewritten), the cache
 *  size, then the question latency (embedding the question plus the search) warm. */
async function benchEmbeddings(slug: string, text: string): Promise<void> {
  const { model, host } = retrieverConfig;
  const corpus = parseCorpus(slug, text);
  const embed = ollamaEmbedder(host, model);
  await embed([queryText("warm up")]); // load the model, so the time below is embedding only
  const sample = Number(flag("--sample") ?? 0);
  if (sample > 0 && sample < corpus.passageCount) {
    // Every k-th passage, so short headings and long tables are both in the sample.
    const step = corpus.passageCount / sample;
    const picked = Array.from({ length: sample }, (_, i) => corpus.passages[Math.floor(i * step)]);
    const t0 = performance.now();
    const part = await embedCorpus({ ...corpus, passages: picked, passageCount: picked.length }, embed);
    const s = (performance.now() - t0) / 1000;
    const rate = sample / s;
    console.log(`${model}: embedded a sample of ${sample} of ${corpus.passageCount} passages (dim ${part.dim}) in ${s.toFixed(1)} s, ${rate.toFixed(0)} passages/s`);
    console.log(`estimate for all: ${(corpus.passageCount / rate / 60).toFixed(1)} min, cache ${mb(corpus.passageCount * part.dim * 4)}`);
    return;
  }
  const rss0 = process.memoryUsage().rss;
  const t0 = performance.now();
  let last = 0;
  const vectors = await embedCorpus(corpus, embed, (done) => {
    if (performance.now() - last > 15_000) {
      last = performance.now();
      console.error(`  ${done}/${corpus.passageCount} after ${((last - t0) / 1000).toFixed(0)} s`);
    }
  });
  const s = (performance.now() - t0) / 1000;
  await writeVectors(corpus, model, vectors, retrieverConfig.cacheDir);
  console.log(`${model}: embedded ${vectors.count} passages (dim ${vectors.dim}) in ${s.toFixed(1)} s, ${(vectors.count / s).toFixed(0)} passages/s, process memory +${mb(process.memoryUsage().rss - rss0)}`);
  console.log(`cache ${cachePaths(corpus, model, retrieverConfig.cacheDir).vec}: ${mb(await cacheBytes(corpus, model, retrieverConfig.cacheDir))}`);
  const retrieveWith = retrieverFor(retrieverConfig);
  const c0 = performance.now();
  await retrieveWith(corpus, "warm up", 8); // reads the cache it just wrote
  console.log(`first question (cache read + question): ${(performance.now() - c0).toFixed(0)} ms`);
  const times: number[] = [];
  for (const q of ["What is the phone number of Village Medical in Mesa?", "Which doctors speak Spanish in Peoria?", "Where is Desert Spine and Sports located?", "Chandler Blvd medical office address", "Moher Christian MD"]) {
    const q0 = performance.now();
    await retrieveWith(corpus, q, 8);
    times.push(performance.now() - q0);
  }
  times.sort((a, b) => a - b);
  console.log(`${retrieverConfig.mode} question median ${times[2].toFixed(1)} ms, slowest ${times[4].toFixed(1)} ms; process memory ${mb(process.memoryUsage().rss)}`);
}
// ---- freshness ------------------------------------------------------------------------------

/** Whether every document the questions read is current against this checkout (or --allow-stale).
 *  Says which are not, and why. */
function freshEnough(): boolean {
  const slugs = [...new Set(questions.map((q) => q.slug))];
  try {
    freshness = checkFreshness(slugs, { data });
  } catch (e) {
    const why = `the freshness check could not run: ${e instanceof Error ? e.message : String(e)}`;
    freshness = slugs.map((slug) => ({ slug, status: "unverifiable", reasons: [why] }));
  }
  const behind = freshness.filter((v) => v.status !== "current");
  if (behind.length === 0) return true;
  console.error(`${allowStale ? "warning: measuring" : "refusing to measure"} ${plural(behind.length, "document")} that ${behind.length === 1 ? "is" : "are"} not current here:`);
  console.error(formatVerdicts(behind));
  console.error(`Rebuild them here: bun run docs:build --stale ${behind.map((v) => v.slug).join(" ")}${allowStale ? "" : "   (or pass --allow-stale)"}\n`);
  return allowStale;
}
let freshness: Freshness[] = [];

const benchSlug = argv.includes("--bench") ? argv[argv.indexOf("--bench") + 1] : null;
if (!benchSlug && !freshEnough()) process.exit(2);
process.exit(await (benchSlug ? benchMode(benchSlug) : wantModel ? modelMode() : retrievalMode()));
