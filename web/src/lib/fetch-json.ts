// One request from client code to this app's own API, answered as an object. A body that is empty
// or not JSON is {}: the status says enough. A request that cannot be made at all throws.

export type Answer = { ok: boolean; status: number; body: Record<string, unknown> };

/** A GET, or with `post` a POST that sends `post.body` as JSON (none for a bare POST). */
export async function fetchJson(path: string, post?: { body?: unknown }, doFetch: typeof fetch = fetch): Promise<Answer> {
  const res = await doFetch(path, !post ? { cache: "no-store" } : {
    method: "POST",
    headers: post.body === undefined ? undefined : { "content-type": "application/json" },
    body: post.body === undefined ? undefined : JSON.stringify(post.body),
    cache: "no-store",
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, body: typeof body === "object" && body !== null ? body : {} };
}
