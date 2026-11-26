import { spawn } from "node:child_process";

/** Checks running now, by what they check: a second request for the same keys joins the first
 *  instead of starting another process. */
const running = new Map<string, Promise<Set<string>>>();

/** A check that has not answered by then is killed, and nothing is called stale. */
const TIMEOUT_MS = 30_000;

/** The keys among `keys` that `docs:build --check` calls stale (their converter, PDF, options or
 *  dependencies changed since their build). Its other verdicts are not "stale": a variant a model
 *  answered for is never provably current, and that is not a reason to rebuild it. Concurrent calls
 *  for the same keys share one process. */
export function staleKeys(
  keys: string[], webDir = process.cwd(), bun = process.env.REFLOW_BUILDS_BUN || "bun", timeoutMs = TIMEOUT_MS,
): Promise<Set<string>> {
  if (keys.length === 0) return Promise.resolve(new Set());
  const id = JSON.stringify([webDir, bun, [...keys].sort()]);
  const joined = running.get(id);
  if (joined) return joined;
  const check = new Promise<Set<string>>((resolve) => {
    const proc = spawn(bun, ["scripts/build-docs.ts", "--check", "--json", ...keys], { cwd: webDir, stdio: ["ignore", "pipe", "ignore"], timeout: timeoutMs });
    let out = "";
    proc.stdout.on("data", (chunk) => (out += chunk));
    proc.on("error", () => resolve(new Set()));
    proc.on("close", () => {
      try {
        const verdicts = JSON.parse(out) as { slug: string; status: string }[];
        resolve(new Set(verdicts.filter((v) => v.status === "stale" && keys.includes(v.slug)).map((v) => v.slug)));
      } catch {
        resolve(new Set());
      }
    });
  }).finally(() => running.delete(id));
  running.set(id, check);
  return check;
}
