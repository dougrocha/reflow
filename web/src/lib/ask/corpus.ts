// Retrieval over a converted document's passages.jsonl. The BM25 index is private to this file.

import { stat as fsStat, readFile } from "node:fs/promises";
import { ASK_QUOTE_CHARS, type Evidence } from "@/lib/ask-contract";
import { isFiniteNumber } from "@/lib/guards";
import { DATA_ROOT, docSlug } from "@/lib/docs-data";
import { passagesPath } from "@/lib/published";

type PassageKind = "p" | "li" | "cap" | "tbl" | "fig" | "h";

export type Passage = {
  /** As in the file: the node id, plus "#r<k>" for a table row group. */
  readonly id: string;
  /** The transcript's data-node: `id` without "#r<k>". */
  readonly node: string;
  /** Position in the file (reading order), 0-based: sorts evidence into document order. */
  readonly order: number;
  readonly page: number;
  /** [x0, top, x1, bottom] in PDF points, top-left origin. */
  readonly bbox: readonly [number, number, number, number];
  readonly kind: PassageKind;
  /** The full text, the only quotable field. */
  readonly text: string;
  /** Enclosing headings joined by " > ", innermost last. Indexed and shown; never quotable. */
  readonly trail: string;
  /** Lead-in sentence or table caption, "" if none. Indexed and shown; never quotable. */
  readonly lead: string;
  /** [start, end) UTF-16 ranges of `text` that OCR is unsure of (the file's "u"), if any. */
  readonly doubt?: readonly (readonly [number, number])[];
};

export type Hit = { passage: Passage; score: number };

export type Corpus = {
  readonly slug: string;
  /** The conversion this corpus belongs to (header "v", the manifest's "v"). */
  readonly version: string;
  readonly passageCount: number;
  /** Every passage in file order (index = `order`). */
  readonly passages: readonly Passage[];
  /** BM25 ranking of every passage that shares a token with the question, best first, cut to
   *  `depth`, with no score floor. For fusion with another retriever (retriever.ts). */
  rank(question: string, depth: number): Hit[];
  /** Top passages for the question by BM25 (design section 7): at most `k` (default 8), none below
   *  0.3 of the best score, at least 3 when available. Empty when no question token occurs in the
   *  corpus. Never throws on odd input. */
  retrieve(question: string, k?: number): Hit[];
  /** The client-facing highlight for a passage and a quote already located in it: region from the
   *  passage bbox and its page size in the header, section from the trail. */
  evidence(passage: Passage, quote: string, at?: number): Evidence;
};

/** File access for the loader, injectable for tests. `stat` resolves null when the file is missing. */
export type CorpusIO = {
  stat(path: string): Promise<{ size: number; mtimeMs: number } | null>;
  readText(path: string): Promise<string>;
};

const DEFAULT_K = 8;
const MIN_HITS = 3;
const SCORE_FLOOR = 0.3;
const BM25_K1 = 1.2;
// b is low on purpose: with the textbook 0.75, one-line fragments (a list item, a callout, a table
// cell) that repeat the question's words outrank the sentence that answers it. Measured with
// `bun run ask:eval` (docs/ask.md): recall@1 61/107 with 0.75, 68/107 with 0.3.
const BM25_B = 0.3;
/** Weight of the trail and lead tokens against the passage's own text. */
const CONTEXT_WEIGHT = 0.6;

// ---------------------------------------------------------------------------------------------
// Tokenizer

// Short lists for the document languages in the samples (en, es). Accents are already dropped when
// these are compared, so the Spanish entries are written without them. Everything else is kept.
const STOPWORDS: ReadonlySet<string> = new Set(
  (
    // en
    "a an the and or but if of to in on at by for with from as is are was were be been being am it its " +
    "this that these those i you he she we they me my your our their not no do does did have has had " +
    "will would can could should may might what which who whom whose when where why how there here " +
    "than then so also about into over under up out " +
    // es
    "el la los las un una unos unas y o pero si de del al en por para con sin sobre es son era eran " +
    "ser fue ha han hay que se su sus lo le les mi tu te nos como cual cuales quien quienes cuando " +
    "donde cuanto cuanta esto esta este estos estas eso esa ese mas muy ya"
  ).split(" "),
);

const CJK_RUN = /([\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]+)/u;
const NON_WORD = /[^\p{L}\p{N}]+/u;
const MARKS = /\p{M}+/gu;

function fold(word: string): string {
  if (word.length > 4 && word.endsWith("ies")) return `${word.slice(0, -3)}y`;
  if (word.length > 3 && word.endsWith("s") && !word.endsWith("ss")) return word.slice(0, -1);
  return word;
}

/** Tokens of a string, the same function for passages and questions: NFKC, lower case, marks
 *  dropped, split on non-letter non-digit, CJK runs as bigrams, plural s/ies folded, the short en/es
 *  stopword list dropped. Exported for tests. */
export function tokenize(text: string): string[] {
  const out: string[] = [];
  const parts = String(text ?? "").normalize("NFKC").toLowerCase().split(CJK_RUN);
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (!part) continue;
    if (i % 2 === 1) {
      // A CJK run: overlapping bigrams, or the lone character.
      const chars = Array.from(part);
      if (chars.length === 1) out.push(chars[0]);
      else for (let j = 0; j + 1 < chars.length; j++) out.push(chars[j] + chars[j + 1]);
      continue;
    }
    for (const word of part.normalize("NFD").replace(MARKS, "").split(NON_WORD)) {
      if (!word || STOPWORDS.has(word)) continue;
      out.push(fold(word));
    }
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Parsing

type Size = readonly [number, number];

function fail(line: number, why: string): never {
  throw new Error(`passages.jsonl line ${line}: ${why}`);
}

const KINDS: ReadonlySet<string> = new Set(["p", "li", "cap", "tbl", "fig", "h"]);


function parseHeader(line: string): { version: string; pages: Size[] } {
  let h: unknown;
  try {
    h = JSON.parse(line);
  } catch {
    return fail(1, "not JSON");
  }
  const o = h as { schema?: unknown; v?: unknown; pages?: unknown };
  if (!o || typeof o !== "object" || o.schema !== 1) return fail(1, "schema is not 1");
  if (typeof o.v !== "string") return fail(1, "missing v");
  if (!Array.isArray(o.pages)) return fail(1, "missing pages");
  const pages = o.pages.map((p): Size => {
    if (!Array.isArray(p) || p.length < 2 || !isFiniteNumber(p[0]) || !isFiniteNumber(p[1])) return fail(1, "bad page size");
    return [p[0], p[1]];
  });
  return { version: o.v, pages };
}

function parsePassage(line: string, no: number, order: number, pageCount: number): Passage {
  let r: Record<string, unknown>;
  try {
    r = JSON.parse(line);
  } catch {
    return fail(no, "not JSON");
  }
  if (!r || typeof r !== "object") return fail(no, "not an object");
  const { id, n, b, k, t, h, c, u } = r;
  if (typeof id !== "string" || !id) return fail(no, "bad id");
  if (typeof n !== "number" || !Number.isInteger(n) || n < 1 || n > pageCount) return fail(no, "bad page");
  if (!Array.isArray(b) || b.length !== 4 || !b.every(isFiniteNumber)) return fail(no, "bad bbox");
  if (typeof k !== "string" || !KINDS.has(k)) return fail(no, "bad kind");
  if (typeof t !== "string") return fail(no, "bad text");
  const doubt = u === undefined ? undefined : doubtRanges(u, t);
  if (doubt === null) return fail(no, "bad doubt ranges");
  return {
    id,
    node: id.split("#")[0],
    order,
    page: n,
    bbox: [b[0], b[1], b[2], b[3]],
    kind: k as PassageKind,
    text: t,
    trail: typeof h === "string" ? h : "",
    lead: typeof c === "string" ? c : "",
    ...(doubt?.length ? { doubt } : {}),
  };
}

/** The file's "u": [start, end) ranges in code points (Python's offsets), as UTF-16 ranges of
 *  `text`; null when one is not a pair of integers inside the text in order. */
function doubtRanges(u: unknown, text: string): [number, number][] | null {
  if (!Array.isArray(u)) return null;
  const units: number[] = [0]; // code point index -> UTF-16 offset
  for (const ch of text) units.push(units[units.length - 1] + ch.length);
  const out: [number, number][] = [];
  for (const r of u) {
    if (!Array.isArray(r) || r.length !== 2 || !r.every((v) => Number.isInteger(v))) return null;
    const [a, z] = r as number[];
    if (a < 0 || z <= a || z >= units.length) return null;
    out.push([units[a], units[z]]);
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Index

type Posting = { docs: Int32Array; tf: Float32Array };

function clipQuote(quote: string): string {
  if (quote.length <= ASK_QUOTE_CHARS) return quote;
  const cut = quote.slice(0, ASK_QUOTE_CHARS);
  const space = cut.search(/\s\S*$/);
  return (space > 0 ? cut.slice(0, space) : cut).trimEnd();
}

/** Parse a whole passages.jsonl (header line, then passages) into an indexed Corpus. Throws on a
 *  header whose `schema` is not 1 or on a malformed line. Pure. */
export function parseCorpus(slug: string, jsonl: string): Corpus {
  const lines = jsonl.split("\n");
  if (lines.length && lines[0].charCodeAt(0) === 0xfeff) lines[0] = lines[0].slice(1);
  const { version, pages } = parseHeader(lines[0] ?? "");

  const passages: Passage[] = [];
  const building = new Map<string, number[]>(); // term -> [doc, tf, doc, tf, ...]
  const lengths: number[] = [];
  let totalLength = 0;

  for (let i = 1; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    const passage = parsePassage(line, i + 1, passages.length, pages.length);
    const idx = passages.length;
    passages.push(passage);

    const tf = new Map<string, number>();
    const own = tokenize(passage.text);
    for (const t of own) tf.set(t, (tf.get(t) ?? 0) + 1);
    for (const t of tokenize(`${passage.trail} ${passage.lead}`)) {
      tf.set(t, (tf.get(t) ?? 0) + CONTEXT_WEIGHT);
    }
    for (const [term, f] of tf) {
      let list = building.get(term);
      if (!list) building.set(term, (list = []));
      list.push(idx, f);
    }
    lengths.push(own.length);
    totalLength += own.length;
  }

  const n = passages.length;
  const avgLength = n ? Math.max(totalLength / n, 1) : 1;
  const index = new Map<string, Posting>();
  for (const [term, list] of building) {
    const df = list.length / 2;
    const docs = new Int32Array(df);
    const tf = new Float32Array(df);
    for (let j = 0; j < df; j++) {
      docs[j] = list[2 * j];
      tf[j] = list[2 * j + 1];
    }
    index.set(term, { docs, tf });
  }
  building.clear();
  const norm = Float64Array.from(lengths, (len) => BM25_K1 * (1 - BM25_B + (BM25_B * len) / avgLength));

  function rankAll(question: string): Hit[] {
    const scores = new Map<number, number>();
    for (const term of new Set(tokenize(question))) {
      const posting = index.get(term);
      if (!posting) continue;
      const df = posting.docs.length;
      const idf = Math.log(1 + (n - df + 0.5) / (df + 0.5));
      for (let j = 0; j < df; j++) {
        const d = posting.docs[j];
        const f = posting.tf[j];
        scores.set(d, (scores.get(d) ?? 0) + (idf * f * (BM25_K1 + 1)) / (f + norm[d]));
      }
    }
    const ranked: Hit[] = [];
    for (const [d, score] of scores) ranked.push({ passage: passages[d], score });
    ranked.sort((a, b) => b.score - a.score || a.passage.order - b.passage.order);
    return ranked;
  }

  function rank(question: string, depth: number): Hit[] {
    try {
      return depth > 0 && n > 0 ? rankAll(question).slice(0, Math.floor(depth)) : [];
    } catch {
      return [];
    }
  }

  function retrieve(question: string, k: number = DEFAULT_K): Hit[] {
    try {
      const limit = Number.isFinite(k) ? Math.floor(k) : DEFAULT_K;
      if (limit <= 0 || n === 0) return [];
      const ranked = rankAll(question);
      if (ranked.length === 0) return [];
      const best = ranked[0].score;
      let keep = 0;
      while (keep < ranked.length && ranked[keep].score >= SCORE_FLOOR * best) keep++;
      return ranked.slice(0, Math.min(limit, Math.max(keep, MIN_HITS)));
    } catch {
      return [];
    }
  }

  function evidence(passage: Passage, quote: string, at = passage.text.indexOf(quote)): Evidence {
    const size = pages[passage.page - 1];
    const shown = clipQuote(quote);
    // The passage's doubtful words that the quote shows, as ranges of the quote.
    const doubt = at < 0 ? [] : (passage.doubt ?? [])
      .map(([a, z]): [number, number] => [Math.max(a - at, 0), Math.min(z - at, shown.length)])
      .filter(([a, z]) => a < z);
    return {
      page: passage.page,
      node: passage.node,
      quote: shown,
      section: passage.trail,
      region: { bbox: [...passage.bbox], width: size[0], height: size[1] },
      ...(doubt.length ? { doubt } : {}),
    };
  }

  return { slug, version, passageCount: n, passages, rank, retrieve, evidence };
}

// ---------------------------------------------------------------------------------------------
// Loading

type Entry = { size: number; mtimeMs: number; corpus: Corpus };
type Building = { size: number; mtimeMs: number; promise: Promise<Corpus | null> };

/** A loader holding at most `capacity` corpora (LRU), rebuilding one when its file's size or mtime
 *  changes and sharing one build between concurrent first calls. Resolves null when the file is
 *  missing or does not parse. Reads DATA_ROOT/<docSlug(slug)>/passages.jsonl. */
export function corpusLoader(io: CorpusIO, capacity = 2): (slug: string) => Promise<Corpus | null> {
  const cache = new Map<string, Entry>(); // insertion order = recency, oldest first
  const building = new Map<string, Building>();

  return async (slug) => {
    const path = passagesPath(DATA_ROOT, docSlug(slug));
    let st: { size: number; mtimeMs: number } | null;
    try {
      st = await io.stat(path);
    } catch {
      st = null;
    }
    if (!st) {
      cache.delete(slug);
      return null;
    }
    const cached = cache.get(slug);
    if (cached && cached.size === st.size && cached.mtimeMs === st.mtimeMs) {
      cache.delete(slug);
      cache.set(slug, cached);
      return cached.corpus;
    }
    const pending = building.get(slug);
    if (pending && pending.size === st.size && pending.mtimeMs === st.mtimeMs) return pending.promise;

    const { size, mtimeMs } = st;
    const promise = (async () => {
      try {
        return parseCorpus(slug, await io.readText(path));
      } catch {
        return null;
      }
    })();
    const mine: Building = { size, mtimeMs, promise };
    building.set(slug, mine);
    try {
      const corpus = await promise;
      if (corpus && building.get(slug) === mine) {
        cache.delete(slug);
        cache.set(slug, { size, mtimeMs, corpus });
        while (cache.size > capacity) cache.delete(cache.keys().next().value as string);
      }
      return corpus;
    } finally {
      if (building.get(slug) === mine) building.delete(slug);
    }
  };
}

const fsIO: CorpusIO = {
  async stat(path) {
    try {
      const s = await fsStat(path);
      return s.isFile() ? { size: s.size, mtimeMs: s.mtimeMs } : null;
    } catch {
      return null;
    }
  },
  readText: (path) => readFile(path, "utf8"),
};

const defaultLoader = corpusLoader(fsIO, 2);

/** The process-wide loader over node:fs (corpusLoader with capacity 2). Not "use cache": the index
 *  is not serializable. Throws only when docSlug(slug) does. */
export async function loadCorpus(slug: string): Promise<Corpus | null> {
  return defaultLoader(slug);
}

/** Whether the viewer should offer Ask for `slug`: it is a sample document and its passages.jsonl
 *  exists (stat only, no parse). Never throws. */
export async function askReady(slug: string): Promise<boolean> {
  try {
    return (await fsIO.stat(passagesPath(DATA_ROOT, docSlug(slug)))) !== null;
  } catch {
    return false;
  }
}
