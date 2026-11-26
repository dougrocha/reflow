import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

// Where the main checkout is, for the scripts that run in any git worktree of the repository.
// scripts/checkout.py is the same helper for the Python tools.

/** git's common dir as seen from `cwd`, or null outside git. */
export function gitCommonDir(cwd: string, env: Record<string, string | undefined> = process.env): string | null {
  const r = spawnSync("git", ["rev-parse", "--path-format=absolute", "--git-common-dir"], { cwd, env: env as NodeJS.ProcessEnv, encoding: "utf8" });
  return r.status === 0 ? r.stdout.trim() : null;
}

/** The main checkout of the repository that `repo` is a checkout of: `repo` itself outside git. */
export function mainCheckout(repo: string, env: Record<string, string | undefined> = process.env): string {
  const common = gitCommonDir(repo, env);
  return common ? dirname(common) : repo;
}

/** A file of `repo`, else the main checkout's: some samples are gitignored and live only there. */
export function samplePath(repo: string, relative: string): string {
  const here = join(repo, relative);
  return existsSync(here) ? here : join(mainCheckout(repo), relative);
}
