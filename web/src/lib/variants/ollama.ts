import { ollamaHost } from "@/lib/ask";

/** The local Ollama server's models that can see images (`/api/tags`, then each one's capabilities
 *  from `/api/show`): the assist steps and the alt text send page crops. [] when it does not answer
 *  quickly. */
export async function ollamaModels(env: NodeJS.ProcessEnv = process.env, timeoutMs = 1500): Promise<string[]> {
  const host = ollamaHost(env.OLLAMA_HOST);
  try {
    const r = await fetch(`${host}/api/tags`, { signal: AbortSignal.timeout(timeoutMs) });
    if (!r.ok) return [];
    const body = (await r.json()) as { models?: { name?: unknown }[] };
    const names = (body.models ?? []).map((m) => m.name).filter((n): n is string => typeof n === "string");
    const seeing = await Promise.all(names.map(async (model) => {
      const shown = await fetch(`${host}/api/show`, { method: "POST", body: JSON.stringify({ model }), signal: AbortSignal.timeout(timeoutMs) });
      const caps = shown.ok ? ((await shown.json()) as { capabilities?: unknown }).capabilities : null;
      return Array.isArray(caps) && caps.includes("vision") ? model : null;
    }));
    return seeing.filter((m): m is string => m !== null).sort();
  } catch {
    return [];
  }
}

/** `load`, remembered for `ttlMs` and shared by callers that arrive while it runs: the variants
 *  list and the builds' enqueue check both ask which models are installed, and each answer is
 *  about a second of requests to Ollama. */
export function remembered(load: () => Promise<string[]>, ttlMs = 15_000, now: () => number = Date.now): () => Promise<string[]> {
  let at = -Infinity;
  let value: string[] = [];
  let loading: Promise<string[]> | undefined;
  return () => {
    if (now() - at < ttlMs) return Promise.resolve(value);
    loading ??= load().then((models) => {
      value = models;
      at = now();
      return models;
    }).finally(() => { loading = undefined; });
    return loading;
  };
}

/** The vision models installed, as of the last 15 seconds. */
export const installedModels = remembered(() => ollamaModels());
