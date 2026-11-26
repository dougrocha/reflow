import { describe, expect, test } from "bun:test";
import { parseSnapshot } from "./parse";
import { decisionViews } from "./status";
import { toWire } from "./types";
import { correct, emptyLedger, rid } from "./testkit";

const header = '{"v":1,"kind":"store","id":"00112233445566ff","slug":"demo","created":"2026-10-28T09:00:00Z"}\n';
const line = (o: unknown) => `${JSON.stringify(o)}\n`;
const undo = (id: string, of: string) => ({ v: 1, id, at: "2026-10-28T09:20:00Z", pdf: correct().pdf, kind: "undo", of });

describe("decisionViews: what the next build changes", () => {
  test("a decision undone before any build changes nothing", () => {
    const c = correct();
    const snap = parseSnapshot(new TextEncoder().encode(header + line(toWire(c)) + line(undo(rid(), c.id))));
    const { decisions } = decisionViews(snap, null);
    expect(decisions.map((d) => [d.status, d.pending])).toEqual([["undone", false]]);
  });
  test("an undo of a decision the build holds does", () => {
    const c = correct();
    const first = header + line(toWire(c));
    const bytes = new TextEncoder().encode(first).length;
    const ledger = emptyLedger({ store: { ...emptyLedger().store, id: "00112233445566ff", bytes, lines: 2 }, applied: [{ id: c.id, kind: "correct", page: 1, bbox: [], node: "n", was: "a", now: "b", tier: "exact", title_unchanged: true }] });
    const snap = parseSnapshot(new TextEncoder().encode(first + line(undo(rid(), c.id))));
    const { decisions } = decisionViews(snap, ledger);
    expect(decisions.map((d) => [d.status, d.pending])).toEqual([["undone", true]]);
  });
  test("a decision the build has not seen is pending and carries its anchor", () => {
    const c = correct();
    const snap = parseSnapshot(new TextEncoder().encode(header + line(toWire(c))));
    const { decisions } = decisionViews(snap, null);
    expect(decisions[0]).toMatchObject({ status: "pending", pending: true, kind: "correct", anchor: c.anchor, range: c.range });
  });
});
