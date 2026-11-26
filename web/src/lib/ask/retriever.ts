// Experimental retrieval by embeddings (docs/research/2026-11-ask-embeddings.md).
// Opt-in: REFLOW_ASK_RETRIEVER=embed|hybrid. The default, bm25, is Corpus.retrieve unchanged.
//
//   embed    rank every passage by cosine similarity to the question's embedding
//   hybrid   reciprocal rank fusion of the BM25 ranking and the embedding ranking
//
// Embeddings come from a local Ollama (`POST /api/embed`, OLLAMA_HOST) and are cached in
// REFLOW_ASK_EMBED_CACHE (default ~/.cache/reflow/embeddings) as <model>/<slug>-<version>.f32
// (unit-length float32 rows in file order) and .json (what the rows were made from). The cache is
// keyed by the conversion's version, so every worktree that has the same conversion shares it, and
// it stays out of the conversion's folder, which a reconversion replaces whole and refuses to
// replace when it holds files the conversion didn't write. Only passages.jsonl text is embedded,
// so text drafted by `--assist local` stays out, as it stays out of passages.jsonl. Nothing leaves
// the machine.

import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { docSlug } from "@/lib/docs-data";
import { ollamaHost } from "./config";
import type { Corpus, Hit, Passage } from "./corpus";

type RetrieverMode = "bm25" | "embed" | "hybrid";

type RetrieverConfig = {
  mode: RetrieverMode;
  /** The Ollama embedding model, REFLOW_ASK_EMBED_MODEL (default qwen3-embedding:0.6b). */
  model: string;
  /** Ollama base URL, as config.ts normalises OLLAMA_HOST. */
  host: string;
  /** Where vectors are cached, REFLOW_ASK_EMBED_CACHE (default ~/.cache/reflow/embeddings). */
  cacheDir: string;
};

/** Ranks a corpus for a question. Never throws: a failed embedding call falls back to BM25. */
export type Retriever = (corpus: Corpus, question: string, k: number) => Promise<Hit[]>;

const DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b";
/** The model card's query format: a one-line task, no instruction on the document side. */
const QUERY_TASK = "Given a question about a document, retrieve the passage of the document that answers it";
/** Passages are cut to this many characters before embedding, to bound the cost of long tables. */
const EMBED_CHARS = 2000;
const BATCH = 64;
/** The model's context in Ollama. 2,000 characters fit with room to spare, and the vectors match
 *  the default context's, but the default (32K for qwen3-embedding) holds 4.6 GB of video memory
 *  for the 0.6b against 1.0 GB at 2K. */
const EMBED_CTX = 2048;
/** Depth of each ranking that reciprocal rank fusion reads, and its constant. */
const FUSE_DEPTH = 100;
const RRF_K = 60;
/** Bumped when the text sent for a passage changes, so old caches are rebuilt. */
const RECIPE = 1;
const QUERY_TIMEOUT_MS = 30_000;

/** Pure over `env`: REFLOW_ASK_RETRIEVER (bm25 | embed | hybrid, default bm25; anything else is
 *  bm25), REFLOW_ASK_EMBED_MODEL, OLLAMA_HOST, REFLOW_ASK_EMBED_CACHE (else XDG_CACHE_HOME or
 *  ~/.cache, then reflow/embeddings). */
export function readRetrieverConfig(env: Record<string, string | undefined> = process.env): RetrieverConfig {
  const raw = (env.REFLOW_ASK_RETRIEVER ?? "").trim().toLowerCase();
  const mode: RetrieverMode = raw === "embed" || raw === "hybrid" ? raw : "bm25";
  const cacheDir = (env.REFLOW_ASK_EMBED_CACHE ?? "").trim()
    || join((env.XDG_CACHE_HOME ?? "").trim() || join(env.HOME || homedir(), ".cache"), "reflow", "embeddings");
  return { mode, model: (env.REFLOW_ASK_EMBED_MODEL ?? "").trim() || DEFAULT_EMBED_MODEL, host: ollamaHost(env.OLLAMA_HOST), cacheDir };
}

const bm25: Retriever = async (corpus, question, k) => corpus.retrieve(question, k);

/** The text embedded for a passage: its section trail and lead-in for context, then its text. */
export function passageText(p: Passage): string {
  return [p.trail, p.lead, p.text].filter(Boolean).join("\n").slice(0, EMBED_CHARS);
}

export function queryText(question: string): string {
  return `Instruct: ${QUERY_TASK}\nQuery:${question}`;
}

// ---------------------------------------------------------------------------------------------
// Ollama

export type Embedder = (inputs: string[], signal?: AbortSignal) => Promise<Float32Array[]>;

export function ollamaEmbedder(host: string, model: string, fetchImpl: typeof fetch = fetch): Embedder {
  return async (inputs, signal) => {
    const res = await fetchImpl(`${host}/api/embed`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model, input: inputs, truncate: true, options: { num_ctx: EMBED_CTX } }),
      signal,
    });
    if (!res.ok) throw new Error(`ollama embed ${res.status}: ${(await res.text()).slice(0, 200)}`);
    const body = (await res.json()) as { embeddings?: number[][] };
    if (!Array.isArray(body.embeddings) || body.embeddings.length !== inputs.length) {
      throw new Error("ollama embed: wrong number of embeddings");
    }
    return body.embeddings.map((e) => unit(Float32Array.from(e)));
  };
}

function unit(v: Float32Array): Float32Array {
  let s = 0;
  for (let i = 0; i < v.length; i++) s += v[i] * v[i];
  const inv = s > 0 ? 1 / Math.sqrt(s) : 0;
  for (let i = 0; i < v.length; i++) v[i] *= inv;
  return v;
}

// ---------------------------------------------------------------------------------------------
// The vector cache

type Vectors = { dim: number; count: number; data: Float32Array };

type Meta = { recipe: number; model: string; v: string; count: number; dim: number };

const safe = (model: string) => model.replace(/[^A-Za-z0-9._-]+/g, "_");

export function cachePaths(corpus: Pick<Corpus, "slug" | "version">, model: string, dir: string): { vec: string; meta: string } {
  const base = join(dir, safe(model), `${docSlug(corpus.slug)}-${safe(corpus.version)}`);
  return { vec: `${base}.f32`, meta: `${base}.json` };
}

/** Embed every passage of `corpus`, in batches, in file order. */
export async function embedCorpus(corpus: Corpus, embed: Embedder, onBatch?: (done: number) => void): Promise<Vectors> {
  const n = corpus.passages.length;
  let dim = 0;
  let data = new Float32Array(0);
  for (let start = 0; start < n; start += BATCH) {
    const rows = await embed(corpus.passages.slice(start, start + BATCH).map(passageText));
    if (!dim) {
      dim = rows[0].length;
      data = new Float32Array(n * dim);
    }
    rows.forEach((row, i) => data.set(row, (start + i) * dim));
    onBatch?.(Math.min(start + BATCH, n));
  }
  return { dim, count: n, data };
}

/** The cached vectors of `corpus` for `model`, or null when absent or made from another version,
 *  model, passage count or recipe. */
export async function readVectors(corpus: Corpus, model: string, dir: string): Promise<Vectors | null> {
  const { vec, meta } = cachePaths(corpus, model, dir);
  try {
    const m = JSON.parse(await readFile(meta, "utf8")) as Meta;
    if (m.recipe !== RECIPE || m.model !== model || m.v !== corpus.version || m.count !== corpus.passageCount) return null;
    const buf = await readFile(vec);
    if (buf.byteLength !== m.count * m.dim * 4) return null;
    const data = new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
    return { dim: m.dim, count: m.count, data };
  } catch {
    return null;
  }
}

export async function writeVectors(corpus: Corpus, model: string, vectors: Vectors, dir: string): Promise<void> {
  const { vec, meta } = cachePaths(corpus, model, dir);
  await mkdir(dirname(vec), { recursive: true });
  const tmp = `.${process.pid}.${Date.now()}.tmp`;
  await writeFile(vec + tmp, new Uint8Array(vectors.data.buffer, vectors.data.byteOffset, vectors.data.byteLength));
  const m: Meta = { recipe: RECIPE, model, v: corpus.version, count: vectors.count, dim: vectors.dim };
  await writeFile(meta + tmp, JSON.stringify(m));
  // The rows first, so a reader that finds the new meta finds rows of the matching size.
  await rename(vec + tmp, vec);
  await rename(meta + tmp, meta);
}

/** Bytes of the cache for `corpus` and `model` on disk, 0 when absent. */
export async function cacheBytes(corpus: Corpus, model: string, dir: string): Promise<number> {
  const { vec, meta } = cachePaths(corpus, model, dir);
  let total = 0;
  for (const p of [vec, meta]) total += await stat(p).then((s) => s.size, () => 0);
  return total;
}

// ---------------------------------------------------------------------------------------------
// Ranking

/** Indices of the `depth` rows most similar to `q` (unit length), best first, with their scores. */
export function nearest(vectors: Vectors, q: Float32Array, depth: number): { index: number; score: number }[] {
  const { dim, count, data } = vectors;
  const scores = new Float32Array(count);
  for (let r = 0, off = 0; r < count; r++, off += dim) {
    let s = 0;
    for (let i = 0; i < dim; i++) s += data[off + i] * q[i];
    scores[r] = s;
  }
  // A partial selection: keep a sorted top list, which is cheap for small depths.
  const top: { index: number; score: number }[] = [];
  for (let r = 0; r < count; r++) {
    const s = scores[r];
    if (top.length === depth && s <= top[depth - 1].score) continue;
    let at = top.length;
    while (at > 0 && top[at - 1].score < s) at--;
    top.splice(at, 0, { index: r, score: s });
    if (top.length > depth) top.pop();
  }
  return top;
}

/** Reciprocal rank fusion: each list adds 1 / (RRF_K + rank) for a passage, ties by file order. */
export function fuse(lists: readonly (readonly Passage[])[], k: number): Hit[] {
  const score = new Map<Passage, number>();
  for (const list of lists) list.forEach((p, i) => score.set(p, (score.get(p) ?? 0) + 1 / (RRF_K + i + 1)));
  return [...score]
    .map(([passage, s]) => ({ passage, score: s }))
    .sort((a, b) => b.score - a.score || a.passage.order - b.passage.order)
    .slice(0, k);
}

type EmbeddingRetrieverOptions = {
  embed?: Embedder;
  log?: (line: string) => void;
};

/** A retriever for `config.mode` embed or hybrid. The first question about a corpus loads its
 *  vectors from the cache, or embeds every passage and writes the cache (minutes on a large
 *  document). A failed embedding call logs and falls back to BM25 for that question. */
export function embeddingRetriever(config: RetrieverConfig, opts: EmbeddingRetrieverOptions = {}): Retriever {
  const embed = opts.embed ?? ollamaEmbedder(config.host, config.model);
  const dir = config.cacheDir;
  const log = opts.log ?? (() => {});
  const loaded = new WeakMap<Corpus, Promise<Vectors>>();

  const vectorsOf = (corpus: Corpus): Promise<Vectors> => {
    let p = loaded.get(corpus);
    if (!p) {
      p = (async () => {
        const cached = await readVectors(corpus, config.model, dir);
        if (cached) return cached;
        const t0 = Date.now();
        const made = await embedCorpus(corpus, embed);
        await writeVectors(corpus, config.model, made, dir);
        log(`${corpus.slug}: embedded ${made.count} passages with ${config.model} in ${Date.now() - t0} ms`);
        return made;
      })();
      p.catch(() => loaded.delete(corpus));
      loaded.set(corpus, p);
    }
    return p;
  };

  return async (corpus, question, k) => {
    if (k <= 0 || corpus.passageCount === 0) return [];
    try {
      const [vectors, [q]] = await Promise.all([
        vectorsOf(corpus),
        embed([queryText(question)], AbortSignal.timeout(QUERY_TIMEOUT_MS)),
      ]);
      const depth = config.mode === "hybrid" ? FUSE_DEPTH : k;
      const near = nearest(vectors, q, depth).map((x) => ({ passage: corpus.passages[x.index], score: x.score }));
      if (config.mode === "embed") return near;
      const lexical = corpus.rank(question, FUSE_DEPTH).map((h) => h.passage);
      return fuse([lexical, near.map((h) => h.passage)], k);
    } catch (err) {
      log(`${corpus.slug}: embedding retrieval failed (${(err as Error).message}); using BM25. Is \`ollama pull ${config.model}\` done?`);
      return corpus.retrieve(question, k);
    }
  };
}

/** The retriever that `config` names: BM25 unless REFLOW_ASK_RETRIEVER opts in. */
export function retrieverFor(config: RetrieverConfig, opts: EmbeddingRetrieverOptions = {}): Retriever {
  return config.mode === "bm25" ? bm25 : embeddingRetriever(config, opts);
}
