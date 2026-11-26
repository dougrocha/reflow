import { describe, expect, test } from "bun:test";
import { readAskConfig } from "./config";

const ok = (env: Record<string, string | undefined>) => {
  const r = readAskConfig(env);
  if (!r.ok) throw new Error(r.operatorMessage);
  return r.config;
};

describe("readAskConfig", () => {
  test("defaults to local Ollama with gemma4:e4b", () => {
    expect(ok({})).toEqual({ provider: "ollama", model: "gemma4:e4b", host: "http://localhost:11434" });
  });

  test("OLLAMA_HOST is normalised as the Python assist does", () => {
    const host = (h: string) => (ok({ OLLAMA_HOST: h }) as { host: string }).host;
    expect(host("gpu-box")).toBe("http://gpu-box:11434");
    expect(host("gpu-box:9000")).toBe("http://gpu-box:9000");
    expect(host("http://gpu-box:9000/")).toBe("http://gpu-box:9000");
    expect(host("https://gpu-box")).toBe("https://gpu-box:11434");
    expect(host("0.0.0.0:11434")).toBe("http://localhost:11434");
    expect(host("  ")).toBe("http://localhost:11434");
  });

  test("REFLOW_ASK_MODEL overrides the model", () => {
    expect(ok({ REFLOW_ASK_MODEL: "qwen3:8b" }).model).toBe("qwen3:8b");
    expect(ok({ REFLOW_ASK_MODEL: " " }).model).toBe("gemma4:e4b");
  });

  test("anthropic with a key defaults to claude-haiku-5-5", () => {
    expect(ok({ REFLOW_ASK_PROVIDER: "anthropic", ANTHROPIC_API_KEY: "sk-test" })).toEqual({
      provider: "anthropic",
      model: "claude-haiku-5-5",
      apiKey: "sk-test",
    });
    expect(ok({ REFLOW_ASK_PROVIDER: "Anthropic", ANTHROPIC_API_KEY: "k", REFLOW_ASK_MODEL: "claude-sonnet-5-5" }).model).toBe(
      "claude-sonnet-5-5",
    );
  });

  test("anthropic without a key is no-credentials and names the variable", () => {
    for (const key of [undefined, "", "  "]) {
      const r = readAskConfig({ REFLOW_ASK_PROVIDER: "anthropic", ANTHROPIC_API_KEY: key });
      expect(r.ok).toBe(false);
      if (!r.ok) {
        expect(r.why).toBe("no-credentials");
        expect(r.operatorMessage).toContain("ANTHROPIC_API_KEY");
      }
    }
  });

  test("an unknown provider is an error naming the variable, not a default", () => {
    const r = readAskConfig({ REFLOW_ASK_PROVIDER: "openai", ANTHROPIC_API_KEY: "k" });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.operatorMessage).toContain("REFLOW_ASK_PROVIDER");
  });

  test("the key is not read for ollama", () => {
    expect(ok({ ANTHROPIC_API_KEY: "sk-test" }).provider).toBe("ollama");
  });
});
