import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { nextVersions, pinProblem, readVerified, recheckProblem } from "../../../scripts/crossing/verdict";

// The re-mark (system.ts) depends on how Next compares a mark with an entry's save time, which no unit
// test can see. The crossing check (web/scripts/crossing/crossing-mark.ts) sees it, on the production
// server; this test fails until it has passed on the installed Next (docs/builds.md, Known limits).
const web = join(import.meta.dir, "../../..");

test("the crossing check has passed on the Next that web/package.json declares and node_modules holds", () => {
  const { declared, installed } = nextVersions(web);
  const recorded = readVerified(join(web, "scripts/crossing/verified.json"))?.next ?? "none";
  expect(pinProblem(declared, installed) ?? recheckProblem(installed, recorded)).toBeNull();
});

describe("the version rules", () => {
  test("a range in package.json fails", () => {
    expect(pinProblem("^16.4.3", "16.4.3")).toContain("pin next to an exact version");
    expect(pinProblem("16.5.0-canary.3", "16.5.0-canary.3")).toBeNull();
  });
  test("a declared version that is not installed fails", () => {
    expect(pinProblem("16.5.0", "16.4.3")).toBe(
      "web/package.json declares next 16.5.0 but node_modules has 16.4.3. Run bun install (in a worktree, in the main checkout, whose node_modules is linked) before the crossing check.",
    );
  });
  test("an installed version the check has not passed on fails", () => {
    expect(recheckProblem("16.5.0", "16.4.3")).toBe(
      "Next is now 16.5.0; the crossing check last passed on 16.4.3. Run `cd web && bun run check:crossing -- --record` and commit scripts/crossing/verified.json.",
    );
    expect(recheckProblem("16.4.3", "16.4.3")).toBeNull();
  });
});
