import { describe, expect, test } from "bun:test";
import {
  anthropicSchema,
  createProvider,
  ProviderFailure,
  ProviderUnavailable,
} from "./providers";

const prompt = {
  system: "SYS",
  user: "USER",
  schema: {
    type: "object",
    properties: {
      evidence: {
        type: "array",
        maxItems: 3,
        items: {
          type: "object",
          properties: { passage: { type: "string", enum: ["P1", "P2"] }, quote: { type: "string", minLength: 1 } },
          required: ["passage", "quote"],
        },
      },
      found: { type: "boolean" },
      answer: { type: "string" },
    },
    required: ["evidence", "found", "answer"],
  },
};

// Parsed request bodies are inspected loosely.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Loose = any;
type Call = { url: string; init: RequestInit; body: Loose };
function fake(respond: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const f = (async (url: string | URL | Request, init?: RequestInit) => {
    const call = { url: String(url), init: init!, body: JSON.parse(String(init!.body)) };
    calls.push(call);
    return respond(call);
  }) as unknown as typeof fetch;
  return { f, calls };
}
const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), { status });
const never = (signal: AbortSignal | null | undefined) =>
  new Promise<Response>((_, reject) => signal!.addEventListener("abort", () => reject(signal!.reason), { once: true }));
const live = () => new AbortController().signal;

const ollama = { provider: "ollama", model: "gemma4:e4b", host: "http://localhost:11434" } as const;
const claude = { provider: "anthropic", model: "claude-haiku-5-5", apiKey: "sk-test" } as const;

describe("ollama", () => {
  test("posts the documented body to /api/chat and returns the message content", async () => {
    const { f, calls } = fake(() => json({ message: { role: "assistant", content: '{"found":false}' } }));
    const p = createProvider(ollama, { fetch: f });
    expect(p.model).toBe("gemma4:e4b");
    expect(await p.complete(prompt, live())).toBe('{"found":false}');
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://localhost:11434/api/chat");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].body).toEqual({
      model: "gemma4:e4b",
      stream: false,
      think: false,
      format: prompt.schema,
      keep_alive: "10m",
      options: { temperature: 0, seed: 0, num_ctx: 8192 },
      messages: [
        { role: "system", content: "SYS" },
        { role: "user", content: "USER" },
      ],
    });
  });

  test("connection refused is model-offline, naming ollama serve", async () => {
    const { f } = fake(() => {
      throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    });
    const err = await createProvider(ollama, { fetch: f }).complete(prompt, live()).catch((e) => e);
    expect(err).toBeInstanceOf(ProviderUnavailable);
    expect(err.why).toBe("model-offline");
    expect(err.operatorMessage).toContain("ollama serve");
  });

  test("404 model not found is model-offline, naming ollama pull", async () => {
    const { f } = fake(() => json({ error: "model 'gemma4:e4b' not found" }, 404));
    const err = await createProvider(ollama, { fetch: f }).complete(prompt, live()).catch((e) => e);
    expect(err).toBeInstanceOf(ProviderUnavailable);
    expect(err.why).toBe("model-offline");
    expect(err.operatorMessage).toContain("ollama pull gemma4:e4b");
  });

  test("the body excerpt in a failure's operator message is escaped and capped", async () => {
    const body = "boom\n[ask] forged\u001b[31m\u009b\u202e" + "z".repeat(500);
    for (const respond of [() => new Response(body, { status: 500 }), () => new Response(body, { status: 200 })]) {
      const err = await createProvider(ollama, { fetch: fake(respond).f }).complete(prompt, live()).catch((e) => e);
      expect(err).toBeInstanceOf(ProviderFailure);
      expect(err.operatorMessage).not.toMatch(/[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/);
      expect(err.operatorMessage.length).toBeLessThan(400);
    }
    const anthropic = await createProvider(
      claude,
      { fetch: fake(() => new Response(body, { status: 500 })).f },
    ).complete(prompt, live()).catch((e) => e);
    expect(anthropic.operatorMessage).not.toMatch(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e]/);
  });

  test("other HTTP errors and bodies without content are failures", async () => {
    for (const r of [() => json({ error: "boom" }, 500), () => json({ message: {} }), () => new Response("not json", { status: 200 })]) {
      const err = await createProvider(ollama, { fetch: fake(r).f }).complete(prompt, live()).catch((e) => e);
      expect(err).toBeInstanceOf(ProviderFailure);
      expect(err.why).toBe("failed");
    }
  });

  test("a reply slower than timeoutMs is a timeout failure and aborts the request", async () => {
    let seen: AbortSignal | null | undefined;
    const { f } = fake((c) => {
      seen = c.init.signal;
      return never(c.init.signal);
    });
    const err = await createProvider(ollama, { fetch: f, timeoutMs: 20 }).complete(prompt, live()).catch((e) => e);
    expect(err).toBeInstanceOf(ProviderFailure);
    expect(err.why).toBe("timeout");
    expect(seen!.aborted).toBe(true);
  });

  test("the caller's abort rejects with its reason and aborts the request", async () => {
    let seen: AbortSignal | null | undefined;
    const { f } = fake((c) => {
      seen = c.init.signal;
      return never(c.init.signal);
    });
    const ac = new AbortController();
    const pending = createProvider(ollama, { fetch: f }).complete(prompt, ac.signal).catch((e) => e);
    await Bun.sleep(5);
    const reason = new Error("client left");
    ac.abort(reason);
    expect(await pending).toBe(reason);
    expect(seen!.aborted).toBe(true);
  });

  test("an already aborted signal never reaches the network", async () => {
    const { f, calls } = fake(() => json({}));
    const ac = new AbortController();
    ac.abort(new Error("gone"));
    const err = await createProvider(ollama, { fetch: f }).complete(prompt, ac.signal).catch((e) => e);
    expect(err.message).toBe("gone");
    expect(calls).toHaveLength(0);
  });
});

describe("anthropic", () => {
  const okBody = { content: [{ type: "text", text: '{"found":false}' }], stop_reason: "end_turn" };

  test("posts the Messages request with the key and version headers; no sampling parameters", async () => {
    const { f, calls } = fake(() => json(okBody));
    const p = createProvider(claude, { fetch: f });
    expect(p.model).toBe("claude-haiku-5-5");
    expect(await p.complete(prompt, live())).toBe('{"found":false}');
    const [c] = calls;
    expect(c.url).toBe("https://api.anthropic.com/v1/messages");
    const h = c.init.headers as Record<string, string>;
    expect(h["x-api-key"]).toBe("sk-test");
    expect(h["anthropic-version"]).toMatch(/^\d{4}-\d\d-\d\d$/);
    expect(h["content-type"]).toBe("application/json");
    expect(c.body.model).toBe("claude-haiku-5-5");
    expect(c.body.system).toBe("SYS");
    expect(c.body.messages).toEqual([{ role: "user", content: "USER" }]);
    expect(c.body.max_tokens).toBeGreaterThan(1000);
    expect(c.body.output_config.format.type).toBe("json_schema");
    for (const k of ["temperature", "top_p", "top_k", "stream", "thinking"]) expect(k in c.body).toBe(false);
  });

  test("the schema sent has the unsupported keywords stripped and closed objects, enum kept", async () => {
    const { f, calls } = fake(() => json(okBody));
    await createProvider(claude, { fetch: f }).complete(prompt, live());
    const sent = calls[0].body.output_config.format.schema;
    expect(sent.additionalProperties).toBe(false);
    expect(sent.properties.evidence.maxItems).toBeUndefined();
    expect(sent.properties.evidence.items.additionalProperties).toBe(false);
    expect(sent.properties.evidence.items.properties.quote.minLength).toBeUndefined();
    expect(sent.properties.evidence.items.properties.passage.enum).toEqual(["P1", "P2"]);
    expect(sent.required).toEqual(["evidence", "found", "answer"]);
    // the prompt's own schema is not mutated
    expect(prompt.schema.properties.evidence.maxItems).toBe(3);
  });

  test("a property named like a dropped keyword is kept", () => {
    const out = anthropicSchema({ type: "object", properties: { minimum: { type: "number" } } }) as Loose;
    expect(out.properties.minimum).toEqual({ type: "number" });
  });

  test("401 and 403 are no-credentials", async () => {
    for (const status of [401, 403]) {
      const { f } = fake(() => json({ error: { type: "authentication_error" } }, status));
      const err = await createProvider(claude, { fetch: f }).complete(prompt, live()).catch((e) => e);
      expect(err).toBeInstanceOf(ProviderUnavailable);
      expect(err.why).toBe("no-credentials");
      expect(err.operatorMessage).toContain("ANTHROPIC_API_KEY");
      expect(err.operatorMessage).not.toContain("sk-test");
    }
  });

  test("429, 500 and a reply without a text block are failures", async () => {
    for (const r of [() => json({}, 429), () => json({}, 500), () => json({ content: [{ type: "thinking" }] })]) {
      const err = await createProvider(claude, { fetch: fake(r).f }).complete(prompt, live()).catch((e) => e);
      expect(err).toBeInstanceOf(ProviderFailure);
      expect(err.why).toBe("failed");
    }
  });

  test("the text block is taken after a thinking block", async () => {
    const { f } = fake(() => json({ content: [{ type: "thinking", thinking: "" }, { type: "text", text: "T" }] }));
    expect(await createProvider(claude, { fetch: f }).complete(prompt, live())).toBe("T");
  });

  test("network failure is a failure, timeout is a timeout, abort passes through", async () => {
    const down = fake(() => {
      throw new TypeError("fetch failed");
    });
    const e1 = await createProvider(claude, { fetch: down.f }).complete(prompt, live()).catch((e) => e);
    expect(e1).toBeInstanceOf(ProviderFailure);
    expect(e1.why).toBe("failed");

    const slow = fake((c) => never(c.init.signal));
    const e2 = await createProvider(claude, { fetch: slow.f, timeoutMs: 20 }).complete(prompt, live()).catch((e) => e);
    expect(e2.why).toBe("timeout");

    const ac = new AbortController();
    const pending = createProvider(claude, { fetch: slow.f }).complete(prompt, ac.signal).catch((e) => e);
    await Bun.sleep(5);
    ac.abort(new Error("x"));
    expect((await pending).message).toBe("x");
  });
});
