// The one orchestration function: question in, AskResult out. The only file that calls the others.

import {
  ASK_MAX_QUESTION_CHARS,
  type AskRequest,
  type AskResult,
  type ErrorReason,
  type Evidence,
  type UnavailableReason,
} from "@/lib/ask-contract";
import { docSlug } from "@/lib/docs-data";
import { ClientLimiter, clientKey, readClientLimitConfig } from "./client-limit";
import { readAskConfig } from "./config";
import { loadCorpus, type Corpus } from "./corpus";
import { Gate } from "./gate";
import { buildPrompt, PROMPT_VERSION } from "./prompt";
import { createProvider, excerpt, ProviderFailure, ProviderUnavailable, type ModelProvider } from "./providers";
import { ollamaSupervisorFor } from "./ollama-supervisor";
import { readRetrieverConfig, retrieverFor, type Retriever } from "./retriever";
import { validateReply } from "./validate";

export type AskDeps = {
  corpus: (slug: string) => Promise<Corpus | null>;
  /** How passages are ranked for a question; Corpus.retrieve (BM25) when absent. */
  retrieve?: Retriever;
  provider: () => ModelProvider | { unavailable: UnavailableReason; operatorMessage: string };
  gate: Gate;
  /** Each client's own budget of questions a minute, charged before the gate's. */
  clients: ClientLimiter;
  /** LRU of 200; only answer and none results are stored. */
  cache: Map<string, AskResult>;
  /** Identical concurrent questions share one call. */
  inflight: Map<string, Promise<AskResult>>;
  /** Operator-facing detail (ollama serve, ollama pull, the variable to set). */
  log: (line: string) => void;
  /** Test seams for the pure S3 functions; the real ones when absent. */
  buildPrompt?: typeof buildPrompt;
  validateReply?: typeof validateReply;
};

const CACHE_SIZE = 200;
const RETRIEVE_K = 8;
/** A queued call gives up after this long. With the model's own 120 s limit that bounds a
 *  request at about 250 s, which the route's maxDuration (270) covers. */
const QUEUE_WAIT_MS = 130_000;

let shared: AskDeps | undefined;

const log = (line: string) => console.error(`[ask] ${line}`);

/** Called once when the server boots (src/instrumentation.ts). In development, or with
 *  REFLOW_ASK_OLLAMA=auto, starts Ollama (or finds the one that is running) without waiting for it,
 *  so it is up before the first question. Does nothing otherwise. */
export function startAskOllama(): void {
  const config = readAskConfig();
  if (config.ok) ollamaSupervisorFor(config.config, process.env, log)?.begin();
}

/** Who sent the request, as `ask` wants it: the address in the header REFLOW_ASK_CLIENT_HEADER names
 *  (see client-limit.ts). Undefined when the variable is unset: no header can be trusted to tell
 *  clients apart, so there is no per-client budget and the gate's alone applies. */
export function clientOf(headers: Headers, env: Record<string, string | undefined> = process.env): string | undefined {
  const { header } = readClientLimitConfig(env);
  return header ? clientKey(headers, header) : undefined;
}

/** The process-wide deps: loadCorpus, createProvider(readAskConfig()), Gate {concurrent 1,
 *  waiting 2, perMinute 20}, a ClientLimiter (REFLOW_ASK_PER_CLIENT, default 5), module-level cache and inflight maps, console.error. Built once.
 *  In development, an ollama provider is wrapped by the supervisor (ollama-supervisor.ts), which starts it again if it has gone. */
function defaultDeps(): AskDeps {
  if (shared) return shared;
  let provider: ReturnType<AskDeps["provider"]> | undefined;
  shared = {
    corpus: loadCorpus,
    retrieve: retrieverFor(readRetrieverConfig(), { log }),
    provider: () => {
      if (!provider) {
        const config = readAskConfig();
        if (config.ok) {
          const supervisor = ollamaSupervisorFor(config.config, process.env, log);
          provider = createProvider(config.config, supervisor ? { withHost: (s, run) => supervisor.use(s, run) } : {});
        } else {
          provider = { unavailable: config.why, operatorMessage: config.operatorMessage };
        }
      }
      return provider;
    },
    gate: new Gate({ concurrent: 1, waiting: 2, perMinute: 20, maxWaitMs: QUEUE_WAIT_MS }),
    clients: new ClientLimiter({ perMinute: readClientLimitConfig().perMinute }),
    cache: new Map(),
    inflight: new Map(),
    log,
  };
  return shared;
}

/** Results produced because the caller went away: never cached, never handed to another caller. */
const abandoned = new WeakSet<AskResult>();

const fail = (why: ErrorReason): AskResult => ({ kind: "error", why });

/** The question as asked, with each run of whitespace as one space and trimmed; "" for a non-string. */
export const cleanQuestion = (q: unknown): string =>
  typeof q === "string" ? q.replace(/\s+/g, " ").trim() : "";

const foldQuestion = (q: string): string => q.normalize("NFKC").toLowerCase();

function remember(cache: Map<string, AskResult>, key: string, result: AskResult): void {
  cache.delete(key);
  cache.set(key, result);
  while (cache.size > CACHE_SIZE) {
    const oldest = cache.keys().next();
    if (oldest.done) break;
    cache.delete(oldest.value);
  }
}

/**
 * The whole feature server side. Never throws: every failure is an AskResult.
 *  1. unknown slug (docSlug throws) -> error/unknown-document
 *  2. clean the question (trim, collapse whitespace, at most ASK_MAX_QUESTION_CHARS)
 *  3. cache lookup (key: slug, version, folded question, provider, model, PROMPT_VERSION): a hit is
 *     served without spending the budget
 *  4. charge the client's per-minute budget (when the caller names a client), then the gate's (either refused -> busy; a refused
 *     gate gives the client's back); an in-flight hit refunds both.
 *     corpus null -> unavailable/not-indexed; corpus.version !== request.version -> unavailable/stale
 *  5. deps.retrieve (default corpus.retrieve, BM25); no hits -> none/no-match WITHOUT a model call
 *  6. provider unavailable -> unavailable; gate.run(buildPrompt -> provider.complete) -> busy if refused;
 *     ProviderFailure -> error/timeout or error/provider-failed
 *  7. validateReply: malformed -> error/unusable-reply; none -> none; answer -> corpus.evidence() per
 *     pick, sorted by (page, passage order)
 *  8. store answer/none in the cache. An aborted `signal` caches nothing.
 */
export async function ask(
  slug: string,
  request: AskRequest,
  signal: AbortSignal,
  deps: AskDeps = defaultDeps(),
  /** Who is asking, for the per-client budget; see clientOf. Undefined: only the gate's applies. */
  client?: string,
): Promise<AskResult> {
  try {
    try {
      docSlug(slug);
    } catch {
      return fail("unknown-document");
    }
    const question = cleanQuestion(request?.question);
    if (question === "" || question.length > ASK_MAX_QUESTION_CHARS) return fail("bad-request");
    if (typeof request.version !== "string") return fail("bad-request");

    const provider = deps.provider();
    const who = "unavailable" in provider ? "-" : provider.model;
    const key = JSON.stringify([slug, request.version, foldQuestion(question), who, PROMPT_VERSION]);
    // A cached answer costs nothing, so it is served even when the minute's budget is spent. It is
    // keyed by the version the reader has open, so it matches their page.
    const cached = deps.cache.get(key);
    if (cached) {
      remember(deps.cache, key, cached);
      return cached;
    }

    // Every other well-formed request spends one of the minute's calls, before the corpus is read
    // or searched; one served from the cache or another caller's work gives it back below.
    // The client's own budget first, so that a client over it takes nothing from the gate's.
    const own = client === undefined ? undefined : deps.clients.charge(client);
    if (own && "busy" in own) return { kind: "busy", retryAfterSeconds: own.busy };
    const refundClient = () => {
      if (client !== undefined && own) deps.clients.refund(client, own.charged);
    };
    const spent = deps.gate.charge();
    if ("busy" in spent) {
      refundClient();
      return { kind: "busy", retryAfterSeconds: spent.busy };
    }
    const charged = spent.charged;

    const corpus = await deps.corpus(slug);
    if (!corpus) {
      deps.log(`${slug}: no passages file; convert the document again (bun run docs:build ${slug})`);
      return { kind: "unavailable", why: "not-indexed" };
    }
    if (corpus.version !== request.version) return { kind: "unavailable", why: "stale" };

    for (;;) {
      const hit = deps.cache.get(key);
      if (hit) {
        remember(deps.cache, key, hit);
        deps.gate.refund(charged);
        refundClient();
        return hit;
      }
      const running = deps.inflight.get(key);
      if (!running) break;
      const result = await running;
      // The leader's caller went away; take over unless ours has too.
      if (!abandoned.has(result) || signal.aborted) {
        deps.gate.refund(charged);
        refundClient();
        return result;
      }
    }

    const work = answer(slug, corpus, question, provider, signal, deps, charged);
    deps.inflight.set(key, work);
    try {
      const result = await work;
      // Busy: the gate refused the call and gave its own back. An abandoned call keeps both charges,
      // since the model may already have been called.
      if (result.kind === "busy") refundClient();
      if (!signal.aborted && (result.kind === "answer" || result.kind === "none")) {
        remember(deps.cache, key, result);
      }
      return result;
    } finally {
      if (deps.inflight.get(key) === work) deps.inflight.delete(key);
    }
  } catch (err) {
    deps.log(`${slug}: unexpected failure: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`);
    return fail("provider-failed");
  }
}

async function answer(
  slug: string,
  corpus: Corpus,
  question: string,
  provider: ReturnType<AskDeps["provider"]>,
  signal: AbortSignal,
  deps: AskDeps,
  charged: number,
): Promise<AskResult> {
  const gone = (): AskResult => {
    const result = fail("provider-failed");
    abandoned.add(result);
    return result;
  };
  try {
    if (signal.aborted) {
      deps.gate.refund(charged);
      return gone();
    }
    const hits = deps.retrieve ? await deps.retrieve(corpus, question, RETRIEVE_K) : corpus.retrieve(question, RETRIEVE_K);
    if (hits.length === 0) return { kind: "none", why: "no-match", model: null };

    if ("unavailable" in provider) {
      deps.log(`${slug}: ${provider.operatorMessage}`);
      return { kind: "unavailable", why: provider.unavailable };
    }

    const prompt = (deps.buildPrompt ?? buildPrompt)(question, hits);
    const started = Date.now();
    let raw: string;
    try {
      const outcome = await deps.gate.run(() => provider.complete(prompt, signal), signal, charged);
      if ("busy" in outcome) return { kind: "busy", retryAfterSeconds: outcome.busy };
      raw = outcome.ran;
    } catch (err) {
      if (signal.aborted) return gone();
      if (err instanceof ProviderUnavailable) {
        deps.log(`${slug}: ${err.operatorMessage}`);
        return { kind: "unavailable", why: err.why };
      }
      if (err instanceof ProviderFailure) {
        deps.log(`${slug}: ${provider.model} ${err.why} after ${Date.now() - started} ms: ${err.operatorMessage}`);
        return fail(err.why === "timeout" ? "timeout" : "provider-failed");
      }
      throw err;
    }
    if (signal.aborted) return gone();

    const verdict = (deps.validateReply ?? validateReply)(raw, prompt.offered, question);
    switch (verdict.kind) {
      case "malformed":
        deps.log(`${slug}: ${provider.model} gave an unusable reply: ${excerpt(raw)}`);
        return fail("unusable-reply");
      case "none":
        if (verdict.why === "unverified") {
          deps.log(`${slug}: ${provider.model} reply failed the citation check (debug): ${excerpt(raw)}`);
        }
        return { kind: "none", why: verdict.why, model: provider.model };
      case "answer": {
        const picks = [...verdict.picks].sort(
          (a, b) => a.passage.page - b.passage.page || a.passage.order - b.passage.order,
        );
        const evidence = picks.map((p) => corpus.evidence(p.passage, p.quote, p.at)) as [Evidence, ...Evidence[]];
        return { kind: "answer", answer: verdict.answer, evidence, model: provider.model };
      }
    }
  } catch (err) {
    if (signal.aborted) return gone();
    throw err;
  }
}

/** HTTP status for a result: answer/none 200; unavailable 503; busy 429; error: bad-request 400,
 *  unknown-document 404, anything else 502. */
export function statusOf(result: AskResult): number {
  switch (result.kind) {
    case "answer":
    case "none":
      return 200;
    case "unavailable":
      return 503;
    case "busy":
      return 429;
    case "error":
      return result.why === "bad-request" ? 400 : result.why === "unknown-document" ? 404 : 502;
  }
}
