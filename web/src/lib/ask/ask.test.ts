import { describe, expect, test } from "bun:test";
import type { AskResult, Evidence } from "@/lib/ask-contract";
import { ask, clientOf, statusOf, type AskDeps } from "./ask";
import { ClientLimiter } from "./client-limit";
import type { Corpus, Hit, Passage } from "./corpus";
import { Gate } from "./gate";
import { ProviderFailure, ProviderUnavailable, type ModelProvider } from "./providers";
import type { Verdict } from "./validate";

const SLUG = "resnet";
const REQ = { question: "What is a residual block?", version: "v1" };
const live = () => new AbortController().signal;

const passage = (order: number, page: number, text = `text ${order}`): Passage => ({
  id: `n${order}`, node: `n${order}`, order, page, bbox: [0, 0, 10, 10], kind: "p", text, trail: "", lead: "",
});
const evidenceOf = (p: Passage, quote: string): Evidence => ({
  page: p.page, node: p.node, quote, section: "",
  region: { page: p.page, width: 100, height: 100, bbox: [0, 0, 10, 10] } as unknown as Evidence["region"],
});

const P = [passage(0, 5), passage(1, 2), passage(2, 2)];

type Setup = {
  reply?: () => Promise<string>;
  verdict?: Verdict;
  hits?: Hit[];
  corpus?: Corpus | null;
  provider?: ReturnType<AskDeps["provider"]>;
  gate?: Gate;
  clients?: ClientLimiter;
};

function setup(o: Setup = {}) {
  const calls = { complete: 0, retrieve: 0, log: [] as string[] };
  const corpus: Corpus | null =
    o.corpus === undefined
      ? {
          slug: SLUG, version: "v1", passageCount: 3, passages: P, rank: () => [],
          retrieve: () => (calls.retrieve++, o.hits ?? P.map((passage) => ({ passage, score: 1 }))),
          evidence: evidenceOf,
        }
      : o.corpus;
  const model: ModelProvider = {
    model: "fake:1b",
    complete: async () => (calls.complete++, o.reply ? o.reply() : "{}"),
  };
  const deps: AskDeps = {
    corpus: async () => corpus,
    provider: () => o.provider ?? model,
    gate: o.gate ?? new Gate({ concurrent: 1, waiting: 2, perMinute: 10_000 }),
    clients: o.clients ?? new ClientLimiter({ perMinute: 10_000 }),
    cache: new Map(),
    inflight: new Map(),
    log: (l) => calls.log.push(l),
    buildPrompt: (_q, hits) => ({
      system: "", user: "", schema: {},
      offered: new Map(hits.map((h, i) => [`P${i + 1}` as const, h.passage])),
    }),
    validateReply: () =>
      o.verdict ?? { kind: "answer", answer: "It is x.", picks: [{ passage: P[0], quote: "text 0" }] },
  };
  return { deps, calls };
}

describe("ask: decided before the model", () => {
  test("unknown slug", async () => {
    const { deps } = setup();
    expect(await ask("nope", REQ, live(), deps)).toEqual({ kind: "error", why: "unknown-document" });
  });

  test("empty, blank and over-long questions are bad requests", async () => {
    const { deps, calls } = setup();
    for (const question of ["", "   \n ", "x".repeat(301)]) {
      expect(await ask(SLUG, { ...REQ, question }, live(), deps)).toEqual({ kind: "error", why: "bad-request" });
    }
    expect(calls.complete).toBe(0);
  });

  test("missing passages file is not-indexed and the operator is told", async () => {
    const { deps, calls } = setup({ corpus: null });
    expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "unavailable", why: "not-indexed" });
    expect(calls.log.join()).toContain("docs:build");
  });

  test("a different version is stale", async () => {
    const { deps, calls } = setup();
    expect(await ask(SLUG, { ...REQ, version: "old" }, live(), deps)).toEqual({ kind: "unavailable", why: "stale" });
    expect(calls.retrieve).toBe(0);
  });

  test("no hits is none/no-match without a provider call, even with the provider offline", async () => {
    const { deps, calls } = setup({
      hits: [],
      provider: { unavailable: "model-offline", operatorMessage: "run ollama serve" },
    });
    expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "none", why: "no-match", model: null });
    expect(calls.complete).toBe(0);
  });

  test("deps.retrieve, when given, ranks the passages instead of corpus.retrieve", async () => {
    const { deps, calls } = setup();
    const seen: string[] = [];
    deps.retrieve = async (_c, question, k) => (seen.push(`${question}|${k}`), []);
    expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "none", why: "no-match", model: null });
    expect(seen).toEqual([`${REQ.question}|8`]);
    expect(calls.retrieve).toBe(0);
  });

  test("an unavailable provider is reported and logged", async () => {
    const { deps, calls } = setup({
      provider: { unavailable: "no-credentials", operatorMessage: "set ANTHROPIC_API_KEY" },
    });
    expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "unavailable", why: "no-credentials" });
    expect(calls.log.join()).toContain("ANTHROPIC_API_KEY");
  });
});

describe("ask: the model call", () => {
  test("the validator is given the cleaned question, so the answer may repeat its numbers", async () => {
    const { deps } = setup();
    const seen: string[] = [];
    deps.validateReply = (_raw, _offered, question) => {
      seen.push(question ?? "");
      return { kind: "none", why: "not-stated" };
    };
    await ask(SLUG, { ...REQ, question: "  What is it\n in 2025? " }, live(), deps);
    expect(seen).toEqual(["What is it in 2025?"]);
  });

  test("an answer carries evidence in document order and the model name", async () => {
    const { deps } = setup({
      verdict: {
        kind: "answer", answer: "It is x.",
        picks: [
          { passage: P[0], quote: "a" }, // page 5
          { passage: P[2], quote: "b" }, // page 2, later
          { passage: P[1], quote: "c" }, // page 2, earlier
        ],
      },
    });
    const result = await ask(SLUG, REQ, live(), deps);
    if (result.kind !== "answer") throw new Error(result.kind);
    expect(result.model).toBe("fake:1b");
    expect(result.evidence.map((e) => e.quote)).toEqual(["c", "b", "a"]);
  });

  test("the model declining and failed citations are none, with the model", async () => {
    for (const why of ["not-stated", "unverified"] as const) {
      const { deps } = setup({ verdict: { kind: "none", why } });
      expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "none", why, model: "fake:1b" });
    }
  });

  test("a malformed reply is error/unusable-reply and the start of it is logged", async () => {
    const { deps, calls } = setup({ verdict: { kind: "malformed" }, reply: async () => "not json at all" });
    expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "error", why: "unusable-reply" });
    expect(calls.log.join()).toContain("not json at all");
  });

  test("the logged excerpt of a reply is escaped and capped: no newline, escape or bidi control gets through", async () => {
    const nasty = `{"x":\n"[ask] forged line"\u001b[31m\u009b\u202e\u2028}` + "a".repeat(1000);
    for (const verdict of [{ kind: "malformed" }, { kind: "none", why: "unverified" }] as const) {
      const { deps, calls } = setup({ verdict, reply: async () => nasty });
      await ask(SLUG, REQ, live(), deps);
      expect(calls.log).toHaveLength(1);
      const line = calls.log[0];
      expect(line).not.toMatch(/[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/);
      expect(line).toContain("forged line");
      expect(line.length).toBeLessThan(400);
    }
  });

  test("provider failures map to timeout and provider-failed", async () => {
    const timeout = setup({ reply: async () => { throw new ProviderFailure("timeout", "120 s"); } });
    expect(await ask(SLUG, REQ, live(), timeout.deps)).toEqual({ kind: "error", why: "timeout" });
    const failed = setup({ reply: async () => { throw new ProviderFailure("failed", "HTTP 500"); } });
    expect(await ask(SLUG, REQ, live(), failed.deps)).toEqual({ kind: "error", why: "provider-failed" });
  });

  test("a provider that turns out to be offline is unavailable, with the operator detail logged", async () => {
    const { deps, calls } = setup({
      reply: async () => { throw new ProviderUnavailable("model-offline", "run `ollama pull fake:1b`"); },
    });
    expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "unavailable", why: "model-offline" });
    expect(calls.log.join()).toContain("ollama pull");
  });

  test("an unexpected throw becomes an error result, never an exception", async () => {
    const { deps } = setup({ reply: async () => { throw new TypeError("oops"); } });
    expect(await ask(SLUG, REQ, live(), deps)).toEqual({ kind: "error", why: "provider-failed" });
    const broken = setup();
    broken.deps.corpus = async () => { throw new Error("disk"); };
    expect(await ask(SLUG, REQ, live(), broken.deps)).toEqual({ kind: "error", why: "provider-failed" });
  });

  test("a refused gate is busy with the retry hint", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 0 });
    const { deps, calls } = setup({ gate });
    const result = await ask(SLUG, REQ, live(), deps);
    expect(result.kind).toBe("busy");
    expect(calls.complete).toBe(0);
    expect(result.kind === "busy" && result.retryAfterSeconds).toBeGreaterThan(0);
  });
});

describe("ask: the minute's budget covers retrieval", () => {
  test("requests that end in no-match are charged, and the budget refuses the rest before the corpus is touched", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 2 });
    const { deps, calls } = setup({ gate, hits: [] });
    let loads = 0;
    const load = deps.corpus;
    deps.corpus = async (slug) => (loads++, load(slug));
    const results: AskResult[] = [];
    for (let i = 0; i < 4; i++) results.push(await ask(SLUG, { ...REQ, question: `question ${i}` }, live(), deps));
    expect(results.map((r) => r.kind)).toEqual(["none", "none", "busy", "busy"]);
    expect(calls.retrieve).toBe(2);
    expect(loads).toBe(2);
    expect(calls.complete).toBe(0);
  });

  test("a request that reaches the model spends one call, not two; a cached repeat spends none", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 2 });
    const { deps, calls } = setup({ gate });
    const other = (question: string) => ask(SLUG, { ...REQ, question }, live(), deps);
    expect((await ask(SLUG, REQ, live(), deps)).kind).toBe("answer");
    expect((await ask(SLUG, REQ, live(), deps)).kind).toBe("answer");
    expect(calls.complete).toBe(1);
    expect((await other("another question")).kind).toBe("answer");
    expect((await other("a third question")).kind).toBe("busy");
    expect(calls.complete).toBe(2);
  });

  test("a cached question is answered after the minute's budget is spent", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 1 });
    const { deps, calls } = setup({ gate });
    expect((await ask(SLUG, REQ, live(), deps)).kind).toBe("answer");
    expect((await ask(SLUG, { ...REQ, question: "another question" }, live(), deps)).kind).toBe("busy");
    expect((await ask(SLUG, REQ, live(), deps)).kind).toBe("answer");
    expect(calls.complete).toBe(1);
  });

  test("malformed requests are not charged", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 1 });
    const { deps } = setup({ gate });
    await ask(SLUG, { ...REQ, question: "" }, live(), deps);
    await ask("nope", REQ, live(), deps);
    expect((await ask(SLUG, REQ, live(), deps)).kind).toBe("answer");
  });
});

describe("ask: a budget for each client", () => {
  const q = (i: number) => ({ ...REQ, question: `question ${i}` });

  test("a client over its limit is refused as busy while another client still gets through", async () => {
    let t = 0;
    const clients = new ClientLimiter({ perMinute: 2, now: () => t });
    const { deps, calls } = setup({ clients });
    const as = (client: string, i: number) => ask(SLUG, q(i), live(), deps, client);
    expect((await as("a", 1)).kind).toBe("answer");
    expect((await as("a", 2)).kind).toBe("answer");
    expect(await as("a", 3)).toEqual({ kind: "busy", retryAfterSeconds: 60 });
    expect(statusOf(await as("a", 3))).toBe(429);
    expect((await as("b", 4)).kind).toBe("answer");
    expect(calls.complete).toBe(3);
    t = 61_000;
    expect((await as("a", 5)).kind).toBe("answer");
  });

  test("a refused client takes nothing from the gate's budget", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 3 });
    const { deps } = setup({ gate, clients: new ClientLimiter({ perMinute: 1 }) });
    const as = (client: string, i: number) => ask(SLUG, q(i), live(), deps, client);
    expect((await as("a", 1)).kind).toBe("answer");
    for (let i = 2; i < 8; i++) expect((await as("a", i)).kind).toBe("busy");
    expect((await as("b", 8)).kind).toBe("answer");
    expect((await as("c", 9)).kind).toBe("answer");
  });

  test("the process-wide gate still applies to every client together", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 2 });
    const { deps } = setup({ gate, clients: new ClientLimiter({ perMinute: 5 }) });
    const as = (client: string, i: number) => ask(SLUG, q(i), live(), deps, client);
    expect((await as("a", 1)).kind).toBe("answer");
    expect((await as("b", 2)).kind).toBe("answer");
    expect((await as("c", 3)).kind).toBe("busy");
  });

  test("a client the gate refused keeps its own call, and a cached answer spends none", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 1 });
    const clients = new ClientLimiter({ perMinute: 2 });
    const { deps } = setup({ gate, clients });
    const as = (client: string, i: number) => ask(SLUG, q(i), live(), deps, client);
    expect((await as("b", 1)).kind).toBe("answer");
    expect((await as("a", 2)).kind).toBe("busy");
    expect((await as("a", 3)).kind).toBe("busy");
    expect((await as("a", 1)).kind).toBe("answer");
    expect((await as("a", 1)).kind).toBe("answer");
    expect((await as("a", 1)).kind).toBe("answer");
  });

  test("a call abandoned after the model was called keeps the client's charge", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 100 });
    const { deps, calls } = setup({ gate, clients: new ClientLimiter({ perMinute: 1 }) });
    const controller = new AbortController();
    deps.provider = () => ({
      model: "fake:1b",
      complete: async () => (calls.complete++, controller.abort(), "{}"),
    });
    await ask(SLUG, q(1), controller.signal, deps, "a");
    expect(calls.complete).toBe(1);
    expect((await ask(SLUG, q(2), live(), deps, "a")).kind).toBe("busy");
    expect(calls.complete).toBe(1);
  });

  test("a caller that names no client has no per-client budget", async () => {
    const { deps } = setup({ clients: new ClientLimiter({ perMinute: 1 }) });
    expect((await ask(SLUG, q(1), live(), deps)).kind).toBe("answer");
    expect((await ask(SLUG, q(2), live(), deps)).kind).toBe("answer");
    expect(deps.clients.size).toBe(0);
  });

  test("clientOf trusts no header unless REFLOW_ASK_CLIENT_HEADER names it", () => {
    const headers = new Headers({ "x-forwarded-for": "6.6.6.6", "x-real-ip": "1.2.3.4" });
    expect(clientOf(headers, {})).toBeUndefined();
    expect(clientOf(new Headers(), { REFLOW_ASK_CLIENT_HEADER: "X-Real-IP" })).toBe("-");
    expect(clientOf(headers, { REFLOW_ASK_CLIENT_HEADER: "X-Real-IP" })).toBe("1.2.3.4");
  });
});

describe("ask: cache and sharing", () => {
  test("answers and nones are cached; the question is folded", async () => {
    const { deps, calls } = setup();
    const first = await ask(SLUG, REQ, live(), deps);
    const second = await ask(SLUG, { ...REQ, question: "  WHAT is a   Residual block? " }, live(), deps);
    expect(second).toEqual(first);
    expect(calls.complete).toBe(1);

    const nones = setup({ verdict: { kind: "none", why: "not-stated" } });
    await ask(SLUG, REQ, live(), nones.deps);
    await ask(SLUG, REQ, live(), nones.deps);
    expect(nones.calls.complete).toBe(1);
  });

  test("errors, busy and unavailable are not cached", async () => {
    let fail = true;
    const { deps, calls } = setup({
      reply: async () => {
        if (fail) throw new ProviderFailure("failed", "HTTP 500");
        return "{}";
      },
    });
    expect((await ask(SLUG, REQ, live(), deps)).kind).toBe("error");
    fail = false;
    expect((await ask(SLUG, REQ, live(), deps)).kind).toBe("answer");
    expect(calls.complete).toBe(2);
    expect(deps.cache.size).toBe(1);
  });

  test("a different model is a different cache entry", async () => {
    const a = setup();
    await ask(SLUG, REQ, live(), a.deps);
    a.deps.provider = () => ({ model: "other:2b", complete: async () => "{}" });
    const result = await ask(SLUG, REQ, live(), a.deps);
    expect(a.deps.cache.size).toBe(2);
    expect(result.kind === "answer" && result.model).toBe("other:2b");
  });

  test("the cache keeps 200 entries", async () => {
    const { deps } = setup();
    for (let i = 0; i < 205; i++) await ask(SLUG, { ...REQ, question: `question ${i}` }, live(), deps);
    expect(deps.cache.size).toBe(200);
  });

  test("identical concurrent questions share one model call", async () => {
    let release!: () => void;
    const gateOpen = new Promise<void>((r) => (release = r));
    const { deps, calls } = setup({ reply: async () => (await gateOpen, "{}") });
    const a = ask(SLUG, REQ, live(), deps);
    const b = ask(SLUG, REQ, live(), deps);
    await new Promise((r) => setTimeout(r, 0));
    expect(deps.inflight.size).toBe(1);
    release();
    const [ra, rb] = await Promise.all([a, b]);
    expect(rb).toEqual(ra);
    expect(calls.complete).toBe(1);
    expect(deps.inflight.size).toBe(0);
  });

  test("if the first caller leaves, a sharing caller still gets an answer", async () => {
    let n = 0;
    const { deps, calls } = setup({
      reply: async () => {
        n++;
        await new Promise((r) => setTimeout(r, 5));
        return "{}";
      },
    });
    const leader = new AbortController();
    const a = ask(SLUG, REQ, leader.signal, deps);
    const b = ask(SLUG, REQ, live(), deps);
    await new Promise((r) => setTimeout(r, 0));
    leader.abort();
    const [, rb] = await Promise.all([a, b]);
    expect(rb.kind).toBe("answer");
    expect(calls.complete).toBe(n);
  });
});

describe("ask: abort", () => {
  test("an aborted request caches nothing and frees the gate", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 0, perMinute: 100 });
    const controller = new AbortController();
    const { deps } = setup({
      gate,
      reply: async () => {
        controller.abort();
        throw controller.signal.reason;
      },
    });
    await ask(SLUG, REQ, controller.signal, deps);
    expect(deps.cache.size).toBe(0);
    expect(deps.inflight.size).toBe(0);
    expect(await gate.run(async () => "free")).toEqual({ ran: "free" });
  });

  test("a reply that arrives after the abort is not cached", async () => {
    const controller = new AbortController();
    const { deps } = setup({ reply: async () => (controller.abort(), "{}") });
    await ask(SLUG, REQ, controller.signal, deps);
    expect(deps.cache.size).toBe(0);
  });

  test("an abort while queued leaves the queue", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 100 });
    let release!: () => void;
    const hold = gate.run(() => new Promise<void>((r) => (release = r)));
    const controller = new AbortController();
    const { deps, calls } = setup({ gate });
    const queued = ask(SLUG, REQ, controller.signal, deps);
    await new Promise((r) => setTimeout(r, 0));
    controller.abort();
    await queued;
    expect(calls.complete).toBe(0);
    release();
    await hold;
  });
});

describe("statusOf", () => {
  const cases: [AskResult, number][] = [
    [{ kind: "none", why: "no-match", model: null }, 200],
    [{ kind: "unavailable", why: "stale" }, 503],
    [{ kind: "busy", retryAfterSeconds: 3 }, 429],
    [{ kind: "error", why: "bad-request" }, 400],
    [{ kind: "error", why: "unknown-document" }, 404],
    [{ kind: "error", why: "timeout" }, 502],
    [{ kind: "error", why: "unusable-reply" }, 502],
    [{ kind: "error", why: "provider-failed" }, 502],
    [{ kind: "error", why: "network" }, 502],
  ];
  test.each(cases)("%j", (result, status) => expect(statusOf(result)).toBe(status));
  test("answer is 200", () => {
    const e = evidenceOf(P[0], "q");
    expect(statusOf({ kind: "answer", answer: "a", evidence: [e], model: "m" })).toBe(200);
  });
});
