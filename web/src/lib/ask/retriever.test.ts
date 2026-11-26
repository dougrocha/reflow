import { afterAll, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseCorpus } from "./corpus";
import {
  type Embedder,
  cachePaths,
  embeddingRetriever,
  fuse,
  nearest,
  passageText,
  queryText,
  readRetrieverConfig,
  readVectors,
} from "./retriever";

const hand = readFileSync(join(import.meta.dir, "fixtures", "hand-passages.jsonl"), "utf8");
const corpus = parseCorpus("resnet", hand);
const dir = mkdtempSync(join(tmpdir(), "reflow-retriever-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));
const cfg = (mode: "embed" | "hybrid", model: string) => ({ mode, model, host: "http://unused", cacheDir: dir });

/** A fake embedder over three topics: a passage's vector counts its words about each, and a query
 *  matches a topic by a synonym that BM25 cannot see. */
const TOPICS = [/residual|shortcut|identity|skip/i, /imagenet|classification|top-5|error/i, /layer|depth|deep/i];
const SYNONYMS = [/bypass/i, /benchmark/i, /stack/i];
function fakeEmbedder(calls: { inputs: number }): Embedder {
  return async (inputs) => {
    calls.inputs += inputs.length;
    return inputs.map((text) => {
      const v = Float32Array.from(TOPICS, (re, i) => (text.match(new RegExp(re, "gi"))?.length ?? 0) + (SYNONYMS[i].test(text) ? 5 : 0) + 0.01);
      const n = Math.hypot(...v);
      return v.map((x) => x / n);
    });
  };
}

describe("readRetrieverConfig", () => {
  test("bm25 unless opted in", () => {
    expect(readRetrieverConfig({}).mode).toBe("bm25");
    expect(readRetrieverConfig({ REFLOW_ASK_RETRIEVER: "nonsense" }).mode).toBe("bm25");
    expect(readRetrieverConfig({ REFLOW_ASK_RETRIEVER: " Hybrid " }).mode).toBe("hybrid");
    expect(readRetrieverConfig({ REFLOW_ASK_RETRIEVER: "embed" }).mode).toBe("embed");
  });
  test("model and host", () => {
    expect(readRetrieverConfig({ HOME: "/h" })).toEqual({
      mode: "bm25", model: "qwen3-embedding:0.6b", host: "http://localhost:11434", cacheDir: "/h/.cache/reflow/embeddings",
    });
    expect(readRetrieverConfig({ XDG_CACHE_HOME: "/x" }).cacheDir).toBe("/x/reflow/embeddings");
    expect(readRetrieverConfig({ XDG_CACHE_HOME: "/x", REFLOW_ASK_EMBED_CACHE: "/e" }).cacheDir).toBe("/e");
    expect(readRetrieverConfig({ REFLOW_ASK_EMBED_MODEL: "qwen3-embedding:8b", OLLAMA_HOST: "box:1" }).model).toBe("qwen3-embedding:8b");
  });
});

describe("nearest and fuse", () => {
  const vectors = { dim: 2, count: 4, data: Float32Array.from([1, 0, 0, 1, 0.6, 0.8, -1, 0]) };
  test("nearest is best first and cut to depth", () => {
    expect(nearest(vectors, Float32Array.from([0, 1]), 2).map((x) => x.index)).toEqual([1, 2]);
    expect(nearest(vectors, Float32Array.from([1, 0]), 10).map((x) => x.index)).toEqual([0, 2, 1, 3]);
  });
  test("fuse rewards agreement, then rank, then file order", () => {
    const [a, b, c] = corpus.passages;
    expect(fuse([[a, b], [b, c]], 3).map((h) => h.passage)).toEqual([b, a, c]);
    expect(fuse([[a], [c]], 1).map((h) => h.passage)).toEqual([a]);
  });
});

describe("embeddingRetriever", () => {
  test("query and document texts follow the model card", () => {
    expect(queryText("Why?")).toMatch(/^Instruct: .+\nQuery:Why\?$/);
    const p = corpus.passages[0];
    expect(passageText(p)).toContain(p.text.slice(0, 40));
    expect(passageText({ ...p, text: "x".repeat(10_000) }).length).toBe(2000);
  });

  test("embeds once, caches outside the conversion by version, and finds a synonym BM25 cannot", async () => {
    const calls = { inputs: 0 };
    const config = cfg("embed", "fake:1");
    const retrieve = embeddingRetriever(config, { embed: fakeEmbedder(calls) });
    expect(corpus.retrieve("bypass", 8)).toEqual([]);
    const hits = await retrieve(corpus, "bypass", 3);
    expect(hits).toHaveLength(3);
    expect(hits[0].passage.text).toMatch(TOPICS[0]);
    expect(calls.inputs).toBe(corpus.passageCount + 1);
    await retrieve(corpus, "bypass", 3);
    expect(calls.inputs).toBe(corpus.passageCount + 2);

    expect(cachePaths(corpus, "fake:1", dir).vec).toBe(join(dir, "fake_1", `resnet-${corpus.version}.f32`));
    const cached = await readVectors(corpus, "fake:1", dir);
    expect(cached?.count).toBe(corpus.passageCount);
    // A new process (a fresh retriever) reads the cache instead of embedding again.
    const again = { inputs: 0 };
    await embeddingRetriever(config, { embed: fakeEmbedder(again) })(corpus, "bypass", 3);
    expect(again.inputs).toBe(1);
    // Another conversion of the document has its own file and does not use the old vectors.
    const reconverted = parseCorpus("resnet", hand.replace(/"v":"[^"]*"/, '"v":"other"'));
    expect(cachePaths(reconverted, "fake:1", dir).vec).not.toBe(cachePaths(corpus, "fake:1", dir).vec);
    expect(await readVectors(reconverted, "fake:1", dir)).toBeNull();
  });

  test("hybrid keeps BM25's hit and adds the semantic one", async () => {
    const retrieve = embeddingRetriever(cfg("hybrid", "fake:2"), { embed: fakeEmbedder({ inputs: 0 }) });
    const lexical = corpus.retrieve("ImageNet bypass", 8);
    const hits = await retrieve(corpus, "ImageNet bypass", 8);
    expect(hits).toHaveLength(8);
    expect(hits.map((h) => h.passage)).toContain(lexical[0].passage);
  });

  test("a failed embedding call falls back to BM25 and is logged", async () => {
    const log: string[] = [];
    const broken: Embedder = async () => {
      throw new Error("connection refused");
    };
    const retrieve = embeddingRetriever(cfg("embed", "fake:3"), { embed: broken, log: (l) => log.push(l) });
    expect(await retrieve(corpus, "residual shortcut", 8)).toEqual(corpus.retrieve("residual shortcut", 8));
    expect(log.join()).toContain("ollama pull fake:3");
  });
});
