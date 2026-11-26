// The model provider, from the server environment only. Used by ask.ts alone.

export type AskConfig =
  | { provider: "ollama"; model: string; host: string }
  | { provider: "anthropic"; model: string; apiKey: string };

type ConfigResult =
  | { ok: true; config: AskConfig }
  | { ok: false; why: "no-credentials"; operatorMessage: string };

const OLLAMA_MODEL = "gemma4:e4b";
const ANTHROPIC_MODEL = "claude-haiku-5-5";

/** The Python assist's normalisation (analyze/vision.py OllamaVision.url), minus the path. */
export function ollamaHost(raw: string | undefined): string {
  let host = (raw ?? "").trim() || "localhost:11434";
  if (!host.includes("://")) host = "http://" + host;
  host = host.replace(/\/+$/, "").replace("//0.0.0.0", "//localhost");
  if ((host.match(/:/g) ?? []).length < 2) host += ":11434";
  return host;
}

/** Pure over `env`: REFLOW_ASK_PROVIDER (ollama | anthropic, default ollama), REFLOW_ASK_MODEL
 *  (default gemma4:e4b / claude-haiku-5-5), OLLAMA_HOST (default localhost:11434, normalised to a
 *  URL as the Python assist does), ANTHROPIC_API_KEY. A missing key or an unknown provider value is
 *  `ok: false` with an operator message naming the variable, never a silent default. */
export function readAskConfig(env: Record<string, string | undefined> = process.env): ConfigResult {
  const provider = (env.REFLOW_ASK_PROVIDER ?? "").trim().toLowerCase() || "ollama";
  const model = (env.REFLOW_ASK_MODEL ?? "").trim();
  if (provider === "ollama") {
    return { ok: true, config: { provider, model: model || OLLAMA_MODEL, host: ollamaHost(env.OLLAMA_HOST) } };
  }
  if (provider === "anthropic") {
    const apiKey = (env.ANTHROPIC_API_KEY ?? "").trim();
    if (!apiKey) {
      return {
        ok: false,
        why: "no-credentials",
        operatorMessage: "REFLOW_ASK_PROVIDER is anthropic but ANTHROPIC_API_KEY is not set in the server environment.",
      };
    }
    return { ok: true, config: { provider, model: model || ANTHROPIC_MODEL, apiKey } };
  }
  return {
    ok: false,
    why: "no-credentials",
    operatorMessage: `REFLOW_ASK_PROVIDER is "${env.REFLOW_ASK_PROVIDER}"; it must be "ollama" or "anthropic".`,
  };
}
