// The crossing check's verdicts, as pure functions (crossing-mark.ts runs the check). Tested by
// verdict.test.ts without a build; the version rules are also the fast suite's tripwire
// (src/lib/builds/crossing-verified.test.ts).
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { writeAtomic } from "../../src/lib/builds/record";

/** The harness could not read what it needed (a stamp, a marker, a record): no valid experiment. */
export class HarnessError extends Error {}

/** A premise says the experiment really happened; a product check says the product behaved. A
 *  control check is only judged by --control (controlVerdict). */
export type Result = { id: string; kind: "premise" | "product" | "control"; ok: boolean; what: string };

/** The exit code: 2 when there was no valid experiment (a premise failed, or a reader threw), 1 when a
 *  valid experiment did not heal, 0 when everything passed. */
export function classify(results: Result[], harnessError: boolean): 0 | 1 | 2 {
  if (harnessError || results.some((r) => r.kind === "premise" && !r.ok)) return 2;
  return results.some((r) => r.kind === "product" && !r.ok) ? 1 : 0;
}

/** --control: the re-mark is disabled, so the check must fail, and for the right reason: the crossing
 *  happened, the tab reported and kept refreshing without a reload, and it never healed. A6 (no more
 *  refreshes) may go either way. Anything else is listed as unexpected. */
export function controlVerdict(results: Result[], harnessError: boolean, mutations: number): string[] {
  const unexpected: string[] = [];
  if (mutations !== 1) unexpected.push(`the mutation matched ${mutations} times, not once`);
  if (harnessError) unexpected.push("the harness could not read what it needed");
  const want: Record<string, boolean> = { A1: true, A2: true, A3: true, A4: false, A5: true, A7: false };
  for (const r of results) {
    const ok = r.kind === "premise" || r.kind === "control" ? true : want[r.id];
    if (ok !== undefined && r.ok !== ok) unexpected.push(`${r.id} ${r.ok ? "passed" : "failed"}: ${r.what}`);
  }
  for (const id of Object.keys(want)) if (!results.some((r) => r.id === id)) unexpected.push(`${id} did not run`);
  return unexpected;
}

/** The text in system.ts that marks a document again when a page reports it is behind. */
export const REMARK = "!changed.includes(slug) && rendered < (current.get(slug) ?? 0) && at - (markedAt.get(slug) ?? -Infinity) >= RENEW_AFTER_MS,";

/** system.ts with the re-mark disabled (a report never marks again), and how often the text matched. */
export function disableRemark(source: string): { source: string; matches: number } {
  const matches = source.split(REMARK).length - 1;
  return { source: matches === 1 ? source.replace(REMARK, "false,") : source, matches };
}

/** Next as declared in web/package.json and as installed in its node_modules. */
export function nextVersions(web: string): { declared: string; installed: string } {
  const declared = JSON.parse(readFileSync(join(web, "package.json"), "utf8")).dependencies.next;
  const installed = JSON.parse(readFileSync(createRequire(join(web, "package.json")).resolve("next/package.json"), "utf8")).version;
  return { declared, installed };
}

/** Why the declared and installed Next disagree, or null. */
export function pinProblem(declared: string, installed: string): string | null {
  if (!/^\d+\.\d+\.\d+(-[\w.]+)?$/.test(declared)) return `web/package.json declares next "${declared}": pin next to an exact version.`;
  if (declared !== installed) {
    return `web/package.json declares next ${declared} but node_modules has ${installed}. Run bun install (in a worktree, in the main checkout, whose node_modules is linked) before the crossing check.`;
  }
  return null;
}

/** Why the crossing check must be rerun (the installed Next is not the one it passed on), or null. */
export function recheckProblem(installed: string, recorded: string): string | null {
  return installed === recorded ? null
    : `Next is now ${installed}; the crossing check last passed on ${recorded}. Run \`cd web && bun run check:crossing -- --record\` and commit scripts/crossing/verified.json.`;
}

type Verified = { next: string; checkedAt: string; healedMs: number };

/** --record: writes `file` only after a full pass of a normal run, not interrupted, on the one Next
 *  that package.json declares, node_modules holds and the scratch tree ran. Whether it wrote. */
export async function recordIfPassed(file: string, run: {
  exit: number; control: boolean; interrupted: boolean; healedMs: number | null;
  versions: { declared: string; installed: string; ran: string }; now?: Date;
}): Promise<boolean> {
  const { declared, installed, ran } = run.versions;
  if (run.exit !== 0 || run.control || run.interrupted || run.healedMs === null || declared !== installed || installed !== ran) return false;
  const record: Verified = { next: ran, checkedAt: (run.now ?? new Date()).toISOString(), healedMs: run.healedMs };
  await writeAtomic(file, `${JSON.stringify(record, null, 2)}\n`);
  return true;
}

export function readVerified(file: string): Verified | null {
  return existsSync(file) ? JSON.parse(readFileSync(file, "utf8")) : null;
}
