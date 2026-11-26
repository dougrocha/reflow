import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { pythonCmd, reflowEnv, repo } from "../../../scripts/freshness";
import { CANNOT_RERENDER, parseLine, type Line } from "./lines";

type Status = Extract<Line, { kind: "status" }>["status"];

/** What src/reflow/progress_lines.py defines and prints, read from the module itself. */
function protocol() {
  const script = "import json\nfrom reflow import progress_lines as p\n"
    + "print(json.dumps({'start': p.START, 'ok': p.OK, 'failed': p.FAILED, 'cannot': p.CANNOT_RERENDER,\n"
    + "  'lines': [p.status_line(p.START, '/out/a', 'a.pdf'), p.status_line(p.OK, '/out/a', '3 pages, 1 figures'),\n"
    + "            p.status_line(p.FAILED, '/out/b', 'ValueError: bad'), p.progress_line('/out/a', 'Extracting', 2, 5),\n"
    + "            p.progress_line('/out/a', 'Reading', 0, None),\n"
    + "            p.timings_line('/out/a', {'extract': 0.85, 'publish': 1.03}, {'extract': 0.2})]}))";
  const r = spawnSync(pythonCmd[0], [...pythonCmd.slice(1), "-c", script], { cwd: repo, env: reflowEnv(), encoding: "utf8" });
  if (r.status !== 0) throw new Error(r.stderr);
  return JSON.parse(r.stdout) as { start: Status; ok: Status; failed: Status; cannot: string; lines: string[] };
}

test("the reader parses the lines the converter's protocol module prints", () => {
  const p = protocol();
  expect(p.cannot).toBe(CANNOT_RERENDER);
  expect(p.lines.map(parseLine)).toEqual([
    { kind: "status", status: p.start, dir: "/out/a", details: "a.pdf" },
    { kind: "status", status: p.ok, dir: "/out/a", details: "3 pages, 1 figures" },
    { kind: "status", status: p.failed, dir: "/out/b", details: "ValueError: bad" },
    { kind: "progress", out: "/out/a", stage: "Extracting", done: 2, total: 5 },
    { kind: "progress", out: "/out/a", stage: "Reading", done: 0, total: null },
    { kind: "timings", out: "/out/a", timings: { seconds: { extract: 0.85, publish: 1.03 }, gpuWait: { extract: 0.2 } } },
  ]);
});

test("anything else is an ordinary line", () => {
  for (const text of ["", "Warning: slow", "{not json", '{"event":"other"}', "done\t/out/a\tx"]) {
    expect(parseLine(text)).toEqual({ kind: "other", text });
  }
});
