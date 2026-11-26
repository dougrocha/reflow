// Model providers over plain fetch: local Ollama, or Anthropic with the operator's key. No SDK.

import type { AskConfig } from "./config";
import type { Prompt } from "./prompt";

/** The provider cannot be used at all (not running, model not pulled, key missing or rejected). */
export class ProviderUnavailable extends Error {
  constructor(readonly why: "model-offline" | "no-credentials", readonly operatorMessage: string) {
    super(operatorMessage);
  }
}

/** The provider was reached but this call failed: "timeout" past the time limit, "failed" for an
 *  HTTP error or a body without the reply text. */
export class ProviderFailure extends Error {
  constructor(readonly why: "timeout" | "failed", readonly operatorMessage: string) {
    super(operatorMessage);
  }
}

export interface ModelProvider {
  /** "gemma4:e4b" etc.: shown to the reader as the author of the summary. */
  readonly model: string;
  /** The model's raw reply text for `prompt`. Throws ProviderUnavailable or ProviderFailure;
   *  rejects with the abort reason when `signal` aborts, and aborts the HTTP request with it. */
  complete(prompt: Pick<Prompt, "system" | "user" | "schema">, signal: AbortSignal): Promise<string>;
}

/** The first `max` characters of `text` as a JSON string literal, for a log line: newlines, escapes
 *  and the C1, line-separator and bidi controls are written as \uXXXX, so text from a model or a
 *  document cannot forge or hide a line. */
export function excerpt(text: string, max = 200): string {
  return JSON.stringify(text.slice(0, max)).replace(
    /[\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g,
    (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

const ANTHROPIC_URL = "https://api.anthropic.com/v1/messages";
/** The `anthropic-version` header value. 2026-10-21 is the current version (it removes the sampling
 *  parameters, which this module never sends). */
const ANTHROPIC_VERSION = "2026-10-21";
/** Room for adaptive thinking tokens plus a reply of a few hundred tokens. */
const ANTHROPIC_MAX_TOKENS = 4096;

type Io = {
  fetch?: typeof fetch;
  timeoutMs?: number;
  /** Ollama only: runs the call with the host of a server that is up, which may start one first
   *  (ollama-supervisor.ts). Absent: the configured host, as it is. */
  withHost?: <T>(signal: AbortSignal, run: (host: string) => Promise<T>) => Promise<T>;
};
type Prompted = Pick<Prompt, "system" | "user" | "schema">;

/** The structured-output subset of JSON Schema: no length or count or range keywords, every object
 *  closed. The validator enforces what is dropped. */
export function anthropicSchema(schema: unknown): unknown {
  const DROP = new Set(["minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "multipleOf", "pattern"]);
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk);
    if (node === null || typeof node !== "object") return node;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(node)) {
      if (DROP.has(k)) continue;
      out[k] = k === "properties" && v && typeof v === "object"
        ? Object.fromEntries(Object.entries(v).map(([name, sub]) => [name, walk(sub)]))
        : walk(v);
    }
    if (out.type === "object") out.additionalProperties = false;
    return out;
  };
  return walk(schema);
}

type Sent = { res: Response; text: string };

/** One POST with a time limit and the caller's abort. Throws the abort reason on caller abort,
 *  ProviderFailure("timeout") on time out, and `onNetworkError` for any other failure to connect. */
async function post(
  io: Io,
  url: string,
  headers: Record<string, string>,
  body: unknown,
  signal: AbortSignal,
  onNetworkError: (e: unknown) => Error,
): Promise<Sent> {
  if (signal.aborted) throw signal.reason;
  const doFetch = io.fetch ?? fetch;
  const timeoutMs = io.timeoutMs ?? 120_000;
  const controller = new AbortController();
  let timedOut = false;
  const onAbort = () => controller.abort(signal.reason);
  signal.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort(new Error("timeout"));
  }, timeoutMs);
  try {
    const res = await doFetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await res.text();
    return { res, text };
  } catch (e) {
    if (signal.aborted) throw signal.reason;
    if (timedOut) throw new ProviderFailure("timeout", `no reply from ${url} within ${timeoutMs} ms`);
    throw onNetworkError(e);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", onAbort);
  }
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

const field = (o: unknown, k: string): unknown =>
  o && typeof o === "object" ? (o as Record<string, unknown>)[k] : undefined;

function ollama(config: Extract<AskConfig, { provider: "ollama" }>, io: Io): ModelProvider {
  const attempt = async (host: string, prompt: Prompted, signal: AbortSignal): Promise<string> => {
    const body = {
      model: config.model,
      stream: false,
      think: false,
      format: prompt.schema,
      keep_alive: "10m",
      options: { temperature: 0, seed: 0, num_ctx: 8192 },
      messages: [
        { role: "system", content: prompt.system },
        { role: "user", content: prompt.user },
      ],
    };
    const { res, text } = await post(io, `${host}/api/chat`, {}, body, signal, (e) =>
      new ProviderUnavailable(
        "model-offline",
        `Ollama is not reachable at ${host}: run \`ollama serve\` (${e instanceof Error ? e.message : String(e)})`,
      ),
    );
    const json = parseJson(text);
    if (!res.ok && /not found/i.test(String(field(json, "error") ?? text))) {
      throw new ProviderUnavailable("model-offline", `Ollama has no model ${config.model}: run \`ollama pull ${config.model}\``);
    }
    if (!res.ok) throw new ProviderFailure("failed", `Ollama answered HTTP ${res.status}: ${excerpt(text)}`);
    const content = field(field(json, "message"), "content");
    if (typeof content !== "string" || content === "") {
      throw new ProviderFailure("failed", `Ollama's reply had no message content: ${excerpt(text)}`);
    }
    return content;
  };
  return {
    model: config.model,
    complete: (prompt, signal) =>
      io.withHost ? io.withHost(signal, (host) => attempt(host, prompt, signal)) : attempt(config.host, prompt, signal),
  };
}

function anthropic(config: Extract<AskConfig, { provider: "anthropic" }>, io: Io): ModelProvider {
  return {
    model: config.model,
    async complete(prompt: Prompted, signal: AbortSignal): Promise<string> {
      const body = {
        model: config.model,
        max_tokens: ANTHROPIC_MAX_TOKENS,
        system: prompt.system,
        messages: [{ role: "user", content: prompt.user }],
        output_config: { format: { type: "json_schema", schema: anthropicSchema(prompt.schema) } },
      };
      const headers = { "x-api-key": config.apiKey, "anthropic-version": ANTHROPIC_VERSION };
      const { res, text } = await post(io, ANTHROPIC_URL, headers, body, signal, (e) =>
        new ProviderFailure("failed", `could not reach the Anthropic API: ${e instanceof Error ? e.message : String(e)}`),
      );
      if (res.status === 401 || res.status === 403) {
        throw new ProviderUnavailable("no-credentials", `Anthropic rejected ANTHROPIC_API_KEY (HTTP ${res.status})`);
      }
      if (!res.ok) throw new ProviderFailure("failed", `Anthropic answered HTTP ${res.status}: ${excerpt(text)}`);
      const content = field(parseJson(text), "content");
      const block = Array.isArray(content) ? content.find((b) => field(b, "type") === "text") : undefined;
      const reply = field(block, "text");
      if (typeof reply !== "string" || reply === "") {
        throw new ProviderFailure("failed", `Anthropic's reply had no text block: ${excerpt(text)}`);
      }
      return reply;
    },
  };
}

/** Ollama: POST {host}/api/chat, stream false, think false, format = schema, temperature 0, seed 0,
 *  keep_alive 10m, num_ctx 8192. ECONNREFUSED and 404 "not found" are ProviderUnavailable("model-offline").
 *  Anthropic: POST https://api.anthropic.com/v1/messages with x-api-key and anthropic-version,
 *  output_config.format json_schema (keywords outside its subset stripped); 401/403 are
 *  ProviderUnavailable("no-credentials"). `io.fetch` and `io.timeoutMs` (default 120 000) are for tests. */
export function createProvider(config: AskConfig, io: Io = {}): ModelProvider {
  return config.provider === "ollama" ? ollama(config, io) : anthropic(config, io);
}
