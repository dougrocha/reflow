import { afterAll, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { classify, controlVerdict, disableRemark, recordIfPassed, REMARK, type Result } from "./verdict";

const P = ["P1", "P2", "P3", "P4", "P5", "P6", "P7"];
const A = ["A1", "A2", "A3", "A4", "A5", "A6", "A7"];
/** Every check passing, except those named in `failed`. */
function results(failed: string[] = []): Result[] {
  return [
    ...P.map((id) => ({ id, kind: "premise" as const, ok: !failed.includes(id), what: id })),
    ...A.map((id) => ({ id, kind: "product" as const, ok: !failed.includes(id), what: id })),
  ];
}
/** The shape a control run must have: A4 and A7 fail, the tab and a plain load stay on A. */
const control = (failed = ["A4", "A7"]) => [
  ...results(failed),
  { id: "C1", kind: "control" as const, ok: true, what: "C1" },
  { id: "C2", kind: "control" as const, ok: true, what: "C2" },
];

describe("classify", () => {
  test("0 when everything passed", () => expect(classify(results(), false)).toBe(0));
  test("1 when a product check failed", () => expect(classify(results(["A4"]), false)).toBe(1));
  test("2 when a premise failed, even with product failures", () => {
    expect(classify(results(["P6"]), false)).toBe(2);
    expect(classify(results(["P6", "A4", "A7"]), false)).toBe(2);
  });
  test("2 when a reader threw, even with product failures", () => {
    expect(classify(results(), true)).toBe(2);
    expect(classify(results(["A4"]), true)).toBe(2);
  });
});

describe("controlVerdict", () => {
  test("accepts the control shape, with A6 either way", () => {
    expect(controlVerdict(control(), false, 1)).toEqual([]);
    expect(controlVerdict(control(["A4", "A6", "A7"]), false, 1)).toEqual([]);
  });
  test("rejects a premise failure", () => expect(controlVerdict(control(["P6", "A4", "A7"]), false, 1)).toEqual(["P6 failed: P6"]));
  test("rejects a failed A1, A2 or A5", () => {
    for (const id of ["A1", "A2", "A5"]) expect(controlVerdict(control([id, "A4", "A7"]), false, 1)).toEqual([`${id} failed: ${id}`]);
  });
  test("rejects a passing A4: the check cannot fail", () => expect(controlVerdict(control(["A7"]), false, 1)).toEqual(["A4 passed: A4"]));
  test("rejects a tab or a plain load that left A", () => {
    const moved = control().map((r) => (r.id === "C1" ? { ...r, ok: false } : r));
    expect(controlVerdict(moved, false, 1)).toEqual(["C1 failed: C1"]);
  });
  test("rejects a mutation that did not match exactly once", () => {
    expect(controlVerdict(control(), false, 0)).toEqual(["the mutation matched 0 times, not once"]);
    expect(controlVerdict(control(), false, 2)).toEqual(["the mutation matched 2 times, not once"]);
  });
  test("rejects a run the harness could not read, or one that stopped early", () => {
    expect(controlVerdict(control(), true, 1)).toEqual(["the harness could not read what it needed"]);
    expect(controlVerdict(control().filter((r) => r.id !== "A7"), false, 1)).toEqual(["A7 did not run"]);
  });
});

describe("disableRemark", () => {
  test("matches system.ts exactly once, and leaves no re-mark", () => {
    const { source, matches } = disableRemark(readFileSync(join(import.meta.dir, "../../src/lib/builds/system.ts"), "utf8"));
    expect(matches).toBe(1);
    expect(source).not.toContain(REMARK);
    expect(source).toContain("\n      false,\n");
  });
  test("leaves a text with no match or two matches unchanged", () => {
    expect(disableRemark("x")).toEqual({ source: "x", matches: 0 });
    expect(disableRemark(REMARK + REMARK)).toEqual({ source: REMARK + REMARK, matches: 2 });
  });
});

describe("recordIfPassed", () => {
  const versions = { declared: "16.4.3", installed: "16.4.3", ran: "16.4.3" };
  const pass = { exit: 0, control: false, interrupted: false, healedMs: 6330, versions };
  const dirs: string[] = [];
  afterAll(() => { for (const dir of dirs) rmSync(dir, { recursive: true, force: true }); });
  const file = () => {
    const f = join(mkdtempSync(join(tmpdir(), "crossing-verdict-")), "verified.json");
    dirs.push(dirname(f));
    writeFileSync(f, '{"next": "16.4.2"}\n');
    return f;
  };
  test("writes the version that ran, the date and healedMs after a full pass", async () => {
    const f = file();
    expect(await recordIfPassed(f, { ...pass, now: new Date("2026-10-28T12:00:00Z") })).toBe(true);
    expect(JSON.parse(readFileSync(f, "utf8"))).toEqual({ next: "16.4.3", checkedAt: "2026-10-28T12:00:00.000Z", healedMs: 6330 });
  });
  for (const [why, run] of [
    ["exit 1", { ...pass, exit: 1 }],
    ["exit 2", { ...pass, exit: 2 }],
    ["--control", { ...pass, control: true }],
    ["an interruption", { ...pass, interrupted: true }],
    ["a declared version that is not installed", { ...pass, versions: { ...versions, declared: "16.5.0" } }],
    ["a scratch tree on another version", { ...pass, versions: { ...versions, ran: "16.5.0" } }],
  ] as const) {
    test(`leaves the file byte-identical on ${why}`, async () => {
      const f = file();
      expect(await recordIfPassed(f, run)).toBe(false);
      expect(readFileSync(f, "utf8")).toBe('{"next": "16.4.2"}\n');
    });
  }
});
