import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HELPER = join("scripts", "build-process.py");

/** This file's folder when it runs as source: Bun sets `import.meta.dir`; Node has the module URL.
 *  Inside the Next server bundle neither points at the source tree reliably, so it may be absent. */
function sourceDir(): string | null {
  if (typeof import.meta.dir === "string") return import.meta.dir;
  try {
    return import.meta.url?.startsWith("file:") ? dirname(fileURLToPath(import.meta.url)) : null;
  } catch {
    return null;
  }
}

/** scripts/build-process.py, kept outside the Next bundle so Node and Bun use the same Linux
 *  operations. Found from this module first (so `bun /abs/web/scripts/build-docs.ts` works from any
 *  folder), then from the Next server's working directory (web/, or the repository root). */
export function processHelper(): string {
  const here = sourceDir();
  const candidates = [
    ...(here ? [join(here, "..", "..", "..", HELPER)] : []),
    join(process.cwd(), HELPER),
    join(process.cwd(), "web", HELPER),
  ];
  const found = candidates.find((path) => existsSync(path));
  if (!found) throw new Error(`Cannot find ${HELPER} (looked in ${candidates.join(", ")}).`);
  return found;
}

/** A kernel flock, shared by all servers and CLI writers. No lease or stale-lock deletion.
 * The helper holds it until this pipe closes, including when the calling process dies. */
export async function transaction<T>(dir: string, work: () => Promise<T>): Promise<T> {
  await mkdir(dir, { recursive: true });
  const child = spawn("python3", [processHelper(), "lock", join(dir, "coordination.lock")], { stdio: ["pipe", "pipe", "pipe"] });
  let error = "";
  child.stderr.on("data", (chunk) => { error += String(chunk); });
  const exited = new Promise<void>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve() : reject(new Error(error || `Build lock helper exited ${code}.`)));
  });
  // Install the rejection handler before awaiting readiness.
  void exited.catch(() => {});
  try {
    await new Promise<void>((resolve, reject) => {
      child.stdout.once("data", () => resolve());
      child.once("error", reject);
      child.once("close", () => reject(new Error(error || "Build lock helper closed before acquiring the lock.")));
    });
    return await work();
  } finally {
    child.stdin.end();
    await exited;
  }
}
