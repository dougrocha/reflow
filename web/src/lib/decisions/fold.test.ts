import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fold } from "./fold";
import { parseSnapshot } from "./parse";
import type { Fold, Snapshot } from "./types";

// The fixtures are shared with the converter's fold (tests/test_decisions_store.py), so the Python
// and the TypeScript folds cannot drift: both must produce the same rendering.
const FIXTURES = join(import.meta.dir, "..", "..", "..", "..", "tests", "fixtures", "decisions");
const CASES = readdirSync(FIXTURES).filter((n) => n.endsWith(".jsonl")).map((n) => n.slice(0, -".jsonl".length)).sort();

function render(snapshot: Snapshot, f: Fold) {
  return {
    store: snapshot.store, bytes: snapshot.bytes, lines: snapshot.lines,
    counts: { records: f.records, torn: f.torn, unreadable: f.unreadable, duplicate: f.duplicate },
    active: f.active.map((r) => r.id),
    invalid: f.invalid.map((i) => i.id),
    unsupported: f.unsupported.map((u) => ({ id: u.id, v: u.v, kind: u.kind })),
    reattach: f.reattach.map((r) => ({ from: r.from, to: r.to })),
    error: f.error,
  };
}

describe("the fold of a shared fixture", () => {
  test("there are fixtures", () => expect(CASES.length).toBeGreaterThanOrEqual(20));
  for (const name of CASES) {
    test(name, () => {
      const snapshot = parseSnapshot(readFileSync(join(FIXTURES, `${name}.jsonl`)));
      const expected = JSON.parse(readFileSync(join(FIXTURES, `${name}.expected.json`), "utf8"));
      expect(render(snapshot, fold(snapshot))).toEqual(expected);
    });
  }
});

describe("parse", () => {
  const header = '{"v":1,"kind":"store","id":"00112233445566ff","slug":"demo","created":"2026-10-28T09:00:00Z"}\n';
  const bytes = (s: string) => new TextEncoder().encode(s);

  test("an empty file is an empty store", () => {
    expect(parseSnapshot(bytes(""))).toMatchObject({ store: null, bytes: 0, lines: 0, error: null, entries: [] });
  });

  test("a line over 8 KB, invalid UTF-8 and a BOM are unreadable", () => {
    const long = `{"x":"${"a".repeat(9000)}"}\n`;
    const snap = parseSnapshot(new Uint8Array([...bytes(header), ...bytes(long), 0x7b, 0xff, 0x7d, 0x0a, 0xef, 0xbb, 0xbf, ...bytes("{}\n")]));
    expect(snap.unreadable).toHaveLength(3);
    expect(snap.entries).toHaveLength(0);
  });

  test("only the bytes after the last newline are torn", () => {
    const snap = parseSnapshot(bytes(`${header}{"v":1,"id":"x`));
    expect(snap).toMatchObject({ torn: 1, bytes: header.length, lines: 1, store: "00112233445566ff" });
  });
});
