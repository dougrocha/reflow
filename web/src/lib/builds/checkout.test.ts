import { afterAll, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { mainCheckout, samplePath } from "../../../scripts/lib/checkout";

const temps: string[] = [];
const temp = () => { const dir = realpathSync(mkdtempSync(join(tmpdir(), "checkout-test-"))); temps.push(dir); return dir; };
afterAll(() => { for (const dir of temps) rmSync(dir, { recursive: true, force: true }); });

const git = (cwd: string, ...args: string[]) => {
  const r = spawnSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", ...args], { cwd, encoding: "utf8" });
  if (r.status !== 0) throw new Error(r.stderr);
};

function repoWithWorktree() {
  const root = temp();
  const main = join(root, "main");
  mkdirSync(join(main, "samples"), { recursive: true });
  writeFileSync(join(main, "samples/a.pdf"), "");
  git(main, "init", "-q");
  git(main, "commit", "-q", "--allow-empty", "-m", "x");
  const worktree = join(root, "wt");
  git(main, "worktree", "add", "-q", "--detach", worktree);
  return { main, worktree };
}

test("a worktree finds the main checkout, and a folder outside git is its own", () => {
  const { main, worktree } = repoWithWorktree();
  expect(mainCheckout(worktree)).toBe(main);
  expect(mainCheckout(main)).toBe(main);
  const outside = temp();
  expect(mainCheckout(outside, { PATH: process.env.PATH, GIT_CEILING_DIRECTORIES: tmpdir() })).toBe(outside);
});

test("a sample the worktree lacks is read from the main checkout", () => {
  const { main, worktree } = repoWithWorktree();
  expect(samplePath(worktree, "samples/a.pdf")).toBe(join(main, "samples/a.pdf"));
  mkdirSync(join(worktree, "samples"));
  writeFileSync(join(worktree, "samples/a.pdf"), "");
  expect(samplePath(worktree, "samples/a.pdf")).toBe(join(worktree, "samples/a.pdf"));
});
