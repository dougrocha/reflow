import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { ASK_QUOTE_CHARS } from "@/lib/ask-contract";
import { askReady, type CorpusIO, corpusLoader, parseCorpus, tokenize } from "./corpus";

const FIXTURES = join(import.meta.dir, "fixtures");
const hand = readFileSync(join(FIXTURES, "hand-passages.jsonl"), "utf8");
const corpus = parseCorpus("resnet", hand);

const ids = (q: string, k?: number) => corpus.retrieve(q, k).map((h) => h.passage.id);

describe("tokenize", () => {
  test("lower-cases and splits on non-letters and non-digits", () => {
    expect(tokenize("Residual-Learning, 3x3!")).toEqual(["residual", "learning", "3x3"]);
  });
  test("drops accents", () => {
    expect(tokenize("Café déjà Résumé")).toEqual(["cafe", "deja", "resume"]);
  });
  test("drops Arabic marks", () => {
    expect(tokenize("الشَّبَكَةُ")).toEqual(tokenize("الشبكة"));
    expect(tokenize("الشَّبَكَةُ")).toEqual(["الشبكة"]);
  });
  test("NFKC folds compatibility forms", () => {
    expect(tokenize("ｆｕｌｌ－width ﬁne")).toEqual(["full", "width", "fine"]);
  });
  test("CJK runs become overlapping bigrams, a lone character stays", () => {
    expect(tokenize("深層学習")).toEqual(["深層", "層学", "学習"]);
    expect(tokenize("한국어")).toEqual(["한국", "국어"]);
    expect(tokenize("本")).toEqual(["本"]);
    expect(tokenize("ResNet深層")).toEqual(["resnet", "深層"]);
  });
  test("folds plural s and ies", () => {
    expect(tokenize("networks")).toEqual(tokenize("network"));
    expect(tokenize("studies")).toEqual(tokenize("study"));
    expect(tokenize("class")).toEqual(["class"]);
    expect(tokenize("gas")).toEqual(["gas"]);
  });
  test("drops english stopwords", () => {
    expect(tokenize("What is the depth of the network?")).toEqual(["depth", "network"]);
    expect(tokenize("this these those")).toEqual([]);
  });
  test("drops spanish stopwords, with or without accents", () => {
    expect(tokenize("¿Cuál es el error de las redes?")).toEqual(["error", "rede"]);
    expect(tokenize("está")).toEqual([]);
    expect(tokenize("Más")).toEqual([]);
  });
  test("keeps other languages whole", () => {
    expect(tokenize("Die Netze sind tief")).toEqual(["die", "netze", "sind", "tief"]);
  });
  test("odd input", () => {
    expect(tokenize("")).toEqual([]);
    expect(tokenize("   \n\t")).toEqual([]);
    expect(tokenize("?!…")).toEqual([]);
    expect(tokenize(undefined as unknown as string)).toEqual([]);
  });
});

describe("parseCorpus", () => {
  test("reads header and passages", () => {
    expect(corpus.slug).toBe("resnet");
    expect(corpus.version).toBe("hand0001");
    expect(corpus.passageCount).toBe(13);
  });
  test("passage fields", () => {
    const p = corpus.retrieve("Figure building block", 1)[0].passage;
    expect(p.id).toBe("2:b3");
    expect(p.kind).toBe("cap");
    expect(p.page).toBe(2);
    expect(p.bbox).toEqual([50.1, 270, 561.9, 300]);
  });
  test("row group node drops the suffix; order follows the file", () => {
    const rows = corpus.retrieve("conv3_x fc softmax", 3).map((h) => h.passage);
    const row = rows.find((p) => p.id === "2:b4#r1");
    expect(row?.node).toBe("2:b4");
    expect(row?.lead).toBe("Table 1. Architectures for ImageNet.");
    expect(row?.order).toBe(8);
  });
  test("rejects a schema other than 1", () => {
    expect(() => parseCorpus("resnet", '{"schema":2,"v":"x","pages":[]}\n')).toThrow();
  });
  test("rejects malformed lines, empty input, bad pages and kinds", () => {
    const head = '{"schema":1,"v":"x","pages":[[10,10]]}\n';
    expect(() => parseCorpus("resnet", "")).toThrow();
    expect(() => parseCorpus("resnet", `${head}not json\n`)).toThrow();
    expect(() => parseCorpus("resnet", `${head}{"id":"a","n":2,"b":[0,0,1,1],"k":"p","t":"x"}\n`)).toThrow();
    expect(() => parseCorpus("resnet", `${head}{"id":"a","n":1,"b":[0,0,1],"k":"p","t":"x"}\n`)).toThrow();
    expect(() => parseCorpus("resnet", `${head}{"id":"a","n":1,"b":[0,0,1,1],"k":"zz","t":"x"}\n`)).toThrow();
  });
  test("tolerates blank lines and a missing final newline", () => {
    const text = `${hand.trimEnd()}\n\n`;
    expect(parseCorpus("resnet", text).passageCount).toBe(13);
  });
  test("an empty document retrieves nothing", () => {
    const empty = parseCorpus("resnet", '{"schema":1,"v":"x","pages":[]}\n');
    expect(empty.passageCount).toBe(0);
    expect(empty.retrieve("anything")).toEqual([]);
  });
});

describe("retrieve", () => {
  const questions: [string, string][] = [
    ["How deep are the residual nets evaluated on ImageNet?", "1:a2"],
    ["What are the shortcut connections?", "2:b2"],
    ["What is the mini-batch size?", "3:c4"],
    ["what weight decay is used", "3:c4"],
    ["Which layer has a 1000-d fc softmax?", "2:b4#r1"],
    ["What do we hypothesize about the residual mapping?", "2:b1"],
    ["¿Qué redes son más fáciles de entrenar?", "3:c2"],
    ["error de clasificación", "3:c1"],
    ["Our main contributions optimize", "1:a3"],
    ["Why are deeper networks difficult to train?", "1:a1"],
  ];
  for (const [q, want] of questions) {
    test(`ranks ${want} first for "${q}"`, () => {
      expect(ids(q, 8)[0]).toBe(want);
    });
  }
  test("a pure stopword question has no hits", () => {
    expect(ids("What is the?")).toEqual([]);
  });
  test("no-hit search returns []", () => {
    expect(corpus.retrieve("zebra quasar")).toEqual([]);
    expect(corpus.retrieve("")).toEqual([]);
    expect(corpus.retrieve("???")).toEqual([]);
  });
  test("a token that occurs nowhere adds nothing", () => {
    const base = corpus.retrieve("shortcut", 3).map((h) => h.score);
    const withMiss = corpus.retrieve("shortcut zebra", 3).map((h) => h.score);
    expect(withMiss).toEqual(base);
  });
  test("odd k and odd input never throw", () => {
    expect(corpus.retrieve("residual", 0)).toEqual([]);
    expect(corpus.retrieve("residual", -2)).toEqual([]);
    expect(corpus.retrieve("residual", Number.NaN).length).toBeGreaterThan(0);
    expect(corpus.retrieve("residual", 1)).toHaveLength(1);
    expect(() => corpus.retrieve(null as unknown as string)).not.toThrow();
    expect(() => corpus.retrieve("x".repeat(100_000))).not.toThrow();
  });
  test("scores descend, are positive, and k caps the count", () => {
    const hits = corpus.retrieve("residual learning networks layers", 4);
    expect(hits.length).toBeLessThanOrEqual(4);
    for (let i = 1; i < hits.length; i++) expect(hits[i - 1].score).toBeGreaterThanOrEqual(hits[i].score);
    expect(hits.every((h) => h.score > 0)).toBe(true);
  });
  test("the default k is 8", () => {
    const text = [
      '{"schema":1,"v":"x","pages":[[10,10]]}',
      ...Array.from({ length: 20 }, (_, i) => JSON.stringify({ id: `p${i}`, n: 1, b: [0, 0, 1, 1], k: "p", t: "alpha beta", h: "", c: "" })),
    ].join("\n");
    expect(parseCorpus("resnet", text).retrieve("alpha")).toHaveLength(8);
  });

  describe("score floor", () => {
    // One passage matches strongly; many weakly. The floor (0.3 of best) must cut the weak ones,
    // but three are always kept when available.
    const mk = (strong: number, weak: number) => {
      const rows = [
        { id: "s", t: Array(strong).fill("needle").join(" ") + " filler" },
        ...Array.from({ length: weak }, (_, i) => ({ id: `w${i}`, t: `needle ${"pad ".repeat(60)}` })),
        ...Array.from({ length: 40 }, (_, i) => ({ id: `f${i}`, t: "filler words" })),
      ];
      return parseCorpus(
        "resnet",
        [
          '{"schema":1,"v":"x","pages":[[10,10]]}',
          ...rows.map((r) => JSON.stringify({ id: r.id, n: 1, b: [0, 0, 1, 1], k: "p", t: r.t, h: "", c: "" })),
        ].join("\n"),
      );
    };
    test("drops passages below 0.3 of the best", () => {
      const c = mk(6, 6);
      const hits = c.retrieve("needle", 8);
      const best = hits[0].score;
      expect(hits[0].passage.id).toBe("s");
      // The six weak ones fall below the floor, so only the minimum of three remain.
      expect(hits).toHaveLength(3);
      expect(hits[2].score).toBeLessThan(0.3 * best);
      expect(hits[1].score).toBeGreaterThan(0);
    });
    test("keeps at least 3 when available", () => {
      const c = mk(6, 6);
      expect(c.retrieve("needle", 8).length).toBeGreaterThanOrEqual(3);
      expect(mk(6, 1).retrieve("needle", 8)).toHaveLength(2);
      expect(mk(6, 2).retrieve("needle", 8)).toHaveLength(3);
    });
    test("k below 3 still caps", () => {
      expect(mk(6, 6).retrieve("needle", 2)).toHaveLength(2);
    });
  });

  describe("length normalisation", () => {
    // A three-word fragment that repeats three of the question's four words must not outrank the
    // sentence that answers it (BM25 b is 0.3, not 0.75; the retrieval numbers are in
    // docs/ask.md).
    const filler = (i: number) => `item ${i} ${"general words about taxes and forms ".repeat(5)}`;
    const c = parseCorpus(
      "resnet",
      [
        '{"schema":1,"v":"x","pages":[[10,10]]}',
        ...[
          { id: "frag", t: "Student loan interest deduction." },
          {
            id: "answer",
            t: "No tax on car loan interest. If you paid qualified passenger vehicle loan interest on a vehicle you purchased for personal use, you may be eligible to deduct up to $10,000 of that interest, with a phase-out at higher incomes.",
          },
          ...Array.from({ length: 30 }, (_, i) => ({ id: `f${i}`, t: filler(i) })),
        ].map((r) => JSON.stringify({ id: r.id, n: 1, b: [0, 0, 1, 1], k: "p", t: r.t, h: "", c: "" })),
      ].join("\n"),
    );
    test("the answering sentence outranks a short fragment with the same words", () => {
      expect(c.retrieve("car loan interest deduction", 3)[0].passage.id).toBe("answer");
    });
  });

  describe("trail and lead weight", () => {
    const mk = (a: object, b: object) =>
      parseCorpus(
        "resnet",
        [
          '{"schema":1,"v":"x","pages":[[10,10]]}',
          ...[a, b].map((r, i) => JSON.stringify({ id: `p${i}`, n: 1, b: [0, 0, 1, 1], k: "p", ...r })),
        ].join("\n"),
      );
    test("a trail match is found", () => {
      const c = mk({ t: "plain words here", h: "Quantum Gravity", c: "" }, { t: "other words there", h: "", c: "" });
      expect(c.retrieve("quantum").map((h) => h.passage.id)).toEqual(["p0"]);
    });
    test("a lead match is found", () => {
      const c = mk({ t: "plain words here", h: "", c: "Eligible members include:" }, { t: "other words", h: "", c: "" });
      expect(c.retrieve("eligible").map((h) => h.passage.id)).toEqual(["p0"]);
    });
    test("a body match outranks the same word in the trail alone", () => {
      const c = mk({ t: "alpha beta gamma", h: "", c: "" }, { t: "beta gamma delta", h: "alpha", c: "" });
      const hits = c.retrieve("alpha");
      expect(hits.map((h) => h.passage.id)).toEqual(["p0", "p1"]);
      expect(hits[1].score).toBeLessThan(hits[0].score);
    });
    test("trail tokens count 0.6 of body tokens", () => {
      // Same length bodies; passage 0 has the term in the text, passage 1 in the trail twice
      // (1.2 > 1) so it should win, and once (0.6 < 1) so it should lose.
      const body = { t: "alpha x y", h: "", c: "" };
      const once = mk(body, { t: "q x y", h: "alpha", c: "" });
      const twice = mk(body, { t: "q x y", h: "alpha", c: "alpha" });
      expect(once.retrieve("alpha")[0].passage.id).toBe("p0");
      expect(twice.retrieve("alpha")[0].passage.id).toBe("p1");
    });
  });
});

describe("Corpus.rank", () => {
  test("is the unfloored ranking that retrieve cuts", () => {
    const q = "residual learning networks layers";
    const ranked = corpus.rank(q, 1000);
    const top = corpus.retrieve(q, 8);
    expect(ranked.slice(0, top.length)).toEqual(top);
    expect(ranked.length).toBeGreaterThanOrEqual(top.length);
    expect(corpus.rank(q, 2)).toHaveLength(2);
    expect(corpus.rank(q, 0)).toEqual([]);
    expect(corpus.rank("zebra quasar", 8)).toEqual([]);
  });
});

describe("heading passages (Carmel shape)", () => {
  const carmel = parseCorpus("carmel", readFileSync(join(FIXTURES, "heading-passages.jsonl"), "utf8"));
  const question = "What is the total revenue and financing for FY 2024-25?";

  test("parseCorpus accepts kind h and keeps its own box", () => {
    const heading = carmel.retrieve(question, 8).find((h) => h.passage.kind === "h")?.passage;
    expect(heading?.id).toBe("8:95f0f97b9c2b");
    expect(heading?.text).toBe("FY 2024-25 revenue and financing $18.6 million");
    expect(heading?.trail).toBe("Budget-in-Brief");
    expect(heading?.bbox).toEqual([72, 440, 420, 458]);
  });

  test("the heading is among the top 3 hits for the question", () => {
    const top = carmel.retrieve(question, 3).map((h) => h.passage.id);
    expect(top).toContain("8:95f0f97b9c2b");
  });
});

describe("evidence", () => {
  const passage = corpus.retrieve("Table architectures conv1", 1)[0].passage;
  test("builds the region from the header page size and the section from the trail", () => {
    const ev = corpus.evidence(passage, "112x112");
    expect(ev).toEqual({
      page: 2,
      node: "2:b4",
      quote: "112x112",
      section: "3. Deep Residual Learning > 3.3. Network Architectures",
      region: { bbox: [125.9, 320, 469.2, 520], width: 612, height: 792 },
    });
  });
  test("uses the page's own size", () => {
    const p = corpus.retrieve("clasificación", 1)[0].passage;
    const ev = corpus.evidence(p, "error de clasificación");
    expect(ev.page).toBe(3);
    expect(ev.region.width).toBe(595);
    expect(ev.region.height).toBe(842);
  });
  test("the region does not alias the passage bbox", () => {
    const ev = corpus.evidence(passage, "x");
    ev.region.bbox[0] = -1;
    expect(passage.bbox[0]).toBe(125.9);
  });
  test("clips a long quote at a word boundary", () => {
    const quote = "word ".repeat(100).trim();
    const ev = corpus.evidence(passage, quote);
    expect(ev.quote.length).toBeLessThanOrEqual(ASK_QUOTE_CHARS);
    expect(quote.startsWith(ev.quote)).toBe(true);
    expect(ev.quote.endsWith("word")).toBe(true);
  });
  test("leaves a short quote alone", () => {
    expect(corpus.evidence(passage, "short quote").quote).toBe("short quote");
  });
});

describe("corpusLoader", () => {
  function fakeIO() {
    const files = new Map<string, { text: string; size: number; mtimeMs: number }>();
    const calls = { stat: 0, read: 0 };
    const io: CorpusIO = {
      async stat(path) {
        calls.stat++;
        const f = files.get(path.split("/").slice(-2).join("/"));
        return f ? { size: f.size, mtimeMs: f.mtimeMs } : null;
      },
      async readText(path) {
        calls.read++;
        await Promise.resolve();
        const f = files.get(path.split("/").slice(-2).join("/"));
        if (!f) throw new Error("ENOENT");
        return f.text;
      },
    };
    const put = (slug: string, text = hand, mtimeMs = 1) => {
      files.set(`${slug}/passages.jsonl`, { text, size: text.length, mtimeMs });
    };
    return { io, calls, put, files };
  }

  test("null when the file is missing", async () => {
    const { io } = fakeIO();
    expect(await corpusLoader(io)("resnet")).toBeNull();
  });
  test("null when the file does not parse, and not cached", async () => {
    const { io, put, calls } = fakeIO();
    put("resnet", "garbage");
    const load = corpusLoader(io);
    expect(await load("resnet")).toBeNull();
    expect(await load("resnet")).toBeNull();
    expect(calls.read).toBe(2);
  });
  test("throws for a slug that is not a sample document", async () => {
    const { io } = fakeIO();
    expect(corpusLoader(io)("../etc")).rejects.toThrow();
  });
  test("a warm call returns the same corpus without reading", async () => {
    const { io, put, calls } = fakeIO();
    put("resnet");
    const load = corpusLoader(io);
    const a = await load("resnet");
    const b = await load("resnet");
    expect(a).not.toBeNull();
    expect(b).toBe(a);
    expect(calls.read).toBe(1);
  });
  test("concurrent first calls share one build", async () => {
    const { io, put, calls } = fakeIO();
    put("resnet");
    const load = corpusLoader(io);
    const [a, b, c] = await Promise.all([load("resnet"), load("resnet"), load("resnet")]);
    expect(a).toBe(b);
    expect(b).toBe(c);
    expect(calls.read).toBe(1);
  });
  test("rebuilds when the mtime changes", async () => {
    const { io, put, calls } = fakeIO();
    put("resnet", hand, 1);
    const load = corpusLoader(io);
    const a = await load("resnet");
    put("resnet", hand, 2);
    const b = await load("resnet");
    expect(b).not.toBe(a);
    expect(calls.read).toBe(2);
    expect(await load("resnet")).toBe(b);
  });
  test("rebuilds when the size changes and picks up the new version", async () => {
    const { io, put } = fakeIO();
    put("resnet", hand, 1);
    const load = corpusLoader(io);
    expect((await load("resnet"))?.version).toBe("hand0001");
    put("resnet", hand.replace("hand0001", "hand0002 "), 1);
    expect((await load("resnet"))?.version).toBe("hand0002 ");
  });
  test("a file that disappears resolves null", async () => {
    const { io, put, files } = fakeIO();
    put("resnet");
    const load = corpusLoader(io);
    await load("resnet");
    files.clear();
    expect(await load("resnet")).toBeNull();
  });
  test("keeps two corpora by default and evicts the least recently used", async () => {
    const { io, put, calls } = fakeIO();
    for (const s of ["resnet", "fw9", "p17"]) put(s);
    const load = corpusLoader(io);
    await load("resnet");
    await load("fw9");
    await load("resnet"); // resnet is now most recent
    await load("p17"); // evicts fw9
    expect(calls.read).toBe(3);
    await load("resnet");
    expect(calls.read).toBe(3);
    await load("fw9");
    expect(calls.read).toBe(4);
  });
  test("capacity is configurable", async () => {
    const { io, put, calls } = fakeIO();
    for (const s of ["resnet", "fw9", "p17"]) put(s);
    const load = corpusLoader(io, 3);
    for (const s of ["resnet", "fw9", "p17", "resnet", "fw9", "p17"]) await load(s);
    expect(calls.read).toBe(3);
  });
});

describe("askReady", () => {
  test("false for an unknown slug, and never throws", async () => {
    expect(await askReady("not-a-document")).toBe(false);
    expect(await askReady("../..")).toBe(false);
  });
});

describe("resnet fixture from the converter", () => {
  const path = join(FIXTURES, "resnet-passages.jsonl");
  test.skipIf(!existsSync(path))("parses, and every passage is found by its own words", () => {
    const c = parseCorpus("resnet", readFileSync(path, "utf8"));
    expect(c.passageCount).toBeGreaterThan(0);
    const sample = [...new Set(c.retrieve("residual learning", 8))];
    expect(sample.length).toBeGreaterThan(0);
    for (const h of sample) {
      const words = h.passage.text.split(/\s+/).slice(0, 12).join(" ");
      expect(c.retrieve(words, 8).some((x) => x.passage.id === h.passage.id)).toBe(true);
    }
  });
});

describe("synthetic 60k-passage corpus", () => {
  const vocab = Array.from({ length: 8000 }, (_, i) => `w${(i * 7919).toString(36)}`);
  function build(): string {
    let seed = 12345;
    const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
    const lines = ['{"schema":1,"v":"syn","pages":[' + Array(600).fill("[612,792]").join(",") + "]}"];
    for (let i = 0; i < 60_000; i++) {
      const words: string[] = [];
      const len = 30 + Math.floor(rnd() * 50);
      for (let j = 0; j < len; j++) words.push(vocab[Math.floor(rnd() ** 2 * vocab.length)]);
      if (i === 31_337) words.push("zyzzyva", "needlepoint");
      lines.push(
        JSON.stringify({
          id: `${(i % 600) + 1}:n${i}`,
          n: (i % 600) + 1,
          b: [50, 60, 500, 120],
          k: "p",
          t: words.join(" "),
          h: `Section ${i % 40} > Part ${i % 7}`,
          c: "",
        }),
      );
    }
    return lines.join("\n");
  }
  test("builds in under 5 s and answers in under 50 ms", () => {
    const text = build();
    const t0 = performance.now();
    const c = parseCorpus("resnet", text);
    const buildMs = performance.now() - t0;
    expect(c.passageCount).toBe(60_000);

    // Warm up, then take the slowest of several questions, including a very common word.
    c.retrieve("zyzzyva needlepoint");
    const qs = ["zyzzyva needlepoint", `${vocab[0]} ${vocab[1]} ${vocab[2]}`, `${vocab[3]} ${vocab[10]} section part`, "section"];
    let worst = 0;
    for (const q of qs) {
      const t = performance.now();
      const hits = c.retrieve(q);
      worst = Math.max(worst, performance.now() - t);
      expect(hits.length).toBeGreaterThan(0);
    }
    expect(c.retrieve("zyzzyva needlepoint")[0].passage.id).toBe("138:n31337");
    console.log(`synthetic 60k: build ${buildMs.toFixed(0)} ms, slowest question ${worst.toFixed(1)} ms`);
    expect(buildMs).toBeLessThan(5000);
    expect(worst).toBeLessThan(50);
  });
});

describe("doubtful words (u)", () => {
  const header = '{"schema":1,"v":"x","pages":[[10,10]]}';
  const line = (o: object) => JSON.stringify({ id: "p0", n: 1, b: [0, 0, 1, 1], k: "p", t: "Dear 😀 Sxra, the fee", h: "", c: "", ...o });
  test("a passage with u parses, its code point ranges as UTF-16 ranges of the text", () => {
    const c = parseCorpus("d", [header, line({ u: [[7, 11]] })].join("\n"));
    const passage = c.retrieve("fee", 1)[0].passage;
    expect(passage.doubt).toEqual([[8, 12]]);
    expect(passage.text.slice(8, 12)).toBe("Sxra");
    expect(c.evidence(passage, "Sxra, the fee", 8).doubt).toEqual([[0, 4]]);
    expect(c.evidence(passage, "the fee", 14).doubt).toBeUndefined();
  });
  test("a passage without u still parses, with no doubt", () => {
    const c = parseCorpus("d", [header, line({})].join("\n"));
    expect(c.retrieve("fee", 1)[0].passage.doubt).toBeUndefined();
  });
  test("a bad range is a malformed line", () => {
    for (const u of [[[3, 2]], [[0, 99]], "x", [[1]]]) {
      expect(() => parseCorpus("d", [header, line({ u })].join("\n"))).toThrow();
    }
  });
});
