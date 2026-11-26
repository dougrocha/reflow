import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { repo } from "../../../scripts/freshness";
import { fold } from "./fold";
import { NOW_MAX_CHARS, NOW_MAX_GROWTH } from "./limits";
import { parseSnapshot } from "./parse";
import { readPublished, type Published } from "./snapshot";
import {
  alt, correct, emptyLedger, figureItem, FINDING, keep, OTHER_PDF, ocrItem, PDF, publications, removeRoot, REV, SUGGESTION_ID,
  tempRoot, textAnchor, warningItem, writePublished,
} from "./testkit";
import { buildRecords, invalidAlt, invalidNow, mintId, parseRequest, type Request } from "./validate";
import { toWire, type StoreRecord, type Snapshot } from "./types";

let root: string;
beforeEach(async () => { root = await tempRoot(); });
afterEach(() => removeRoot(root));

async function published(items: unknown[], ledger = emptyLedger()): Promise<Published> {
  await writePublished(join(root, "docs"), "fw9", { items, ledger });
  const r = await readPublished("fw9", { docsRoot: join(root, "docs"), publications: publications({}) });
  if (!r.ok) throw new Error(r.error);
  return r.published;
}

const HEADER = '{"v":1,"kind":"store","id":"00112233445566ff","slug":"fw9","created":"2026-10-28T09:00:00Z"}\n';
const store = (...records: StoreRecord[]): Snapshot =>
  parseSnapshot(new TextEncoder().encode(HEADER + records.map((r) => `${JSON.stringify(toWire(r))}\n`).join("")));

const req = (body: Record<string, unknown>): Request => {
  const r = parseRequest(body);
  if (!r.ok) throw new Error(r.error);
  return r.request;
};
const ctx = (p: Published | null, s: Snapshot = store()) => ({ published: p, store: s, id: () => "m2k9x4q9-deadbeef", at: "2026-11-02T10:00:00Z" });
const build = (body: Record<string, unknown>, p: Published | null, s?: Snapshot) => buildRecords(req(body), ctx(p, s));
const records = (r: ReturnType<typeof buildRecords>): StoreRecord[] => {
  if (!r.ok) throw new Error(`${r.status} ${r.error}`);
  return r.records;
};

const ITEM = "5:1a2b3c4d5e6f";
const accept = { rev: REV, item: ITEM, choice: "accept", suggestion: SUGGESTION_ID };

describe("parseRequest", () => {
  test("each choice, with exactly its fields", () => {
    expect(req(accept)).toEqual({ choice: "accept", target: { type: "suggestion", rev: REV, item: ITEM, suggestion: SUGGESTION_ID, supersedes: null } });
    expect(req({ ...accept, choice: "keep", supersedes: "m2k9x4q1-00000001" })).toMatchObject({ choice: "keep", target: { supersedes: "m2k9x4q1-00000001" } });
    expect(req({ ...accept, choice: "other", now: "80110" })).toMatchObject({ choice: "other", now: "80110" });
    expect(req({ rev: REV, item: "2:x", choice: "alt", alt: { text: "A map", decorative: false } })).toMatchObject({ choice: "alt", alt: { text: "A map", decorative: false } });
    expect(req({ rev: REV, item: ITEM, choice: "dismiss", finding: FINDING })).toMatchObject({ choice: "dismiss", findings: [FINDING] });
    expect(req({ rev: REV, item: ITEM, choice: "dismiss", findings: [FINDING, "1111222233334444"] })).toMatchObject({ findings: [FINDING, "1111222233334444"] });
    expect(req({ choice: "undo", of: "m2k9x4q1-00000001" })).toEqual({ choice: "undo", of: "m2k9x4q1-00000001" });
    expect(req({ rev: REV, item: "x", choice: "reattach" })).toEqual({ choice: "reattach", rev: REV, item: "x" });
    expect(req({ choice: "other", supersedes: "m2k9x4q1-00000001", now: "80111" })).toEqual({ choice: "other", target: { type: "applied", supersedes: "m2k9x4q1-00000001" }, now: "80111" });
  });

  test("anything else is a 400: unknown choices and keys, a name, wrong types, missing fields", () => {
    const bad: Record<string, unknown>[] = [
      {}, { choice: "nope" }, { choice: 5 }, { ...accept, by: "Sam" }, { ...accept, name: "Sam" }, { ...accept, extra: 1 },
      { ...accept, rev: 5 }, { ...accept, rev: "" }, { ...accept, suggestion: undefined }, { ...accept, item: "" }, { ...accept, supersedes: 5 },
      { ...accept, choice: "keep", now: "x" }, { ...accept, choice: "other" }, { ...accept, choice: "other", now: 5 },
      { rev: REV, item: ITEM, choice: "alt" }, { rev: REV, item: ITEM, choice: "alt", alt: { text: "x" } }, { rev: REV, item: ITEM, choice: "alt", alt: { text: 1, decorative: true } },
      { rev: REV, item: ITEM, choice: "alt", alt: { text: "x", decorative: false, by: 1 } },
      { rev: REV, item: ITEM, choice: "dismiss" }, { rev: REV, item: ITEM, choice: "dismiss", finding: 5 }, { rev: REV, item: ITEM, choice: "dismiss", findings: [] },
      { rev: REV, item: ITEM, choice: "dismiss", findings: [5] }, { rev: REV, item: ITEM, choice: "dismiss", finding: "a", findings: ["b"] },
      { choice: "undo" }, { choice: "undo", of: "" }, { choice: "undo", of: "x", rev: REV }, { choice: "reattach" }, { choice: "reattach", rev: REV },
      { choice: "alt", alt: { text: "x", decorative: false } }, { choice: "accept", supersedes: "m2k9x4q1-00000001" },
      { choice: "accept", item: ITEM, suggestion: SUGGESTION_ID }, { ...accept, item: undefined, supersedes: "m2k9x4q1-00000001" },
    ];
    for (const body of bad) {
      const r = parseRequest(JSON.parse(JSON.stringify(body)) as Record<string, unknown>);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.status).toBe(400);
    }
  });
});

describe("invalidNow and invalidAlt", () => {
  test("now: one non-empty line, no control or bidi, at most 200 characters and 4 times was", () => {
    expect(invalidNow("80110", "8011O")).toBeNull();
    expect(invalidNow("", "x")).toBe("empty");
    expect(invalidNow("a".repeat(201), "a".repeat(100))).toContain("200");
    expect(invalidNow("aaaaaaaaa", "ab")).toContain("4 times");
    expect(invalidNow("aaaaaaaa", "ab")).toBeNull();
    expect(invalidNow("80\n10", "8011O")).toContain("control");
    expect(invalidNow("80 ", "8011O")).toContain("control");
    expect(invalidNow("80\t1", "8011O")).toContain("control");
    for (const c of ["؜", "‎", "‏", "‪", "‮", "⁦", "⁩"]) expect(invalidNow(`80${c}1`, "8011O")).toContain("bidirectional");
    expect(invalidNow("\u{1F600}\u{1F600}\u{1F600}\u{1F600}", "\u{1F600}")).toBeNull(); // counted in characters, not UTF-16 units
  });

  test("alt: at most 1,000 characters, no control or bidi", () => {
    expect(invalidAlt("A map of Carmel")).toBeNull();
    expect(invalidAlt("a".repeat(1000))).toBeNull();
    expect(invalidAlt("a".repeat(1001))).toContain("1000");
    expect(invalidAlt("a\nb")).toContain("control");
    expect(invalidAlt("a‮b")).toContain("bidirectional");
  });
});

describe("mintId", () => {
  test("<ms base 36>-<8 hex>", () => {
    expect(mintId(0)).toMatch(/^0-[0-9a-f]{8}$/);
    expect(mintId(1761642843000)).toMatch(/^[0-9a-z]{8}-[0-9a-f]{8}$/);
    expect(mintId(1761642843000).split("-")[0]).toBe((1761642843000).toString(36));
    expect(mintId()).not.toBe(mintId());
  });
});

describe("buildRecords: a text decision comes from the published suggestion, not the client", () => {
  test("accept: target and pdf from the snapshot, now from the suggestion, no name", async () => {
    const [rec] = records(build(accept, await published([ocrItem()])));
    expect(rec).toEqual({
      kind: "correct", id: "m2k9x4q9-deadbeef", at: "2026-11-02T10:00:00Z", pdf: PDF, anchor: textAnchor(),
      range: { was: "8011O", before: "CO ", after: "", rule: "zip-digit" }, now: "80110", supersedes: null,
    });
    expect(Object.keys(toWire(rec!))).not.toContain("by");
    expect(JSON.stringify(toWire(rec!))).not.toContain('"by"');
  });

  test("keep and other", async () => {
    const p = await published([ocrItem()]);
    expect(records(build({ ...accept, choice: "keep" }, p))[0]).toMatchObject({ kind: "keep", pdf: PDF, range: { was: "8011O" } });
    expect(records(build({ ...accept, choice: "other", now: "8O110" }, p))[0]).toMatchObject({ kind: "correct", now: "8O110", range: { was: "8011O" } });
  });

  test("a stale rev, a missing item and a missing suggestion are the same 409 and write nothing", async () => {
    const p = await published([ocrItem()]);
    for (const body of [{ ...accept, rev: "old" }, { ...accept, item: "9:nope" }, { ...accept, suggestion: "0000000000000000" }]) {
      expect(build(body, p)).toMatchObject({ ok: false, status: 409, reload: true });
    }
    expect(build({ ...accept, rev: "old" }, p)).toMatchObject({ error: "This page shows an older build. Reload to decide on the current one." });
  });

  test("no decisions.json is a 409 with the rebuild sentence", async () => {
    await writePublished(join(root, "docs"), "fw9", { ledger: null });
    const r = await readPublished("fw9", { docsRoot: join(root, "docs"), publications: publications({}) });
    if (!r.ok) throw new Error();
    expect(build(accept, r.published)).toEqual({ ok: false, status: 409, error: "Rebuild this document once before recording decisions." });
  });

  test("a correction's limits are the converter's (analyze/decisions.py)", () => {
    const python = readFileSync(`${repo}/src/reflow/analyze/decisions.py`, "utf8");
    const limit = (name: string) => Number(new RegExp(`^${name} = (\\d+)`, "m").exec(python)?.[1]);
    expect([limit("NOW_MAX_CHARS"), limit("NOW_MAX_GROWTH")]).toEqual([NOW_MAX_CHARS, NOW_MAX_GROWTH]);
  });

  test("other: the value's limits are 400", async () => {
    const p = await published([ocrItem()]);
    for (const now of ["", "x".repeat(41), "80\n10", "80‮10"]) {
      expect(build({ ...accept, choice: "other", now }, p)).toMatchObject({ ok: false, status: 400 });
    }
  });

  test("a suggestion whose reading breaks the limits is not accepted", async () => {
    const p = await published([ocrItem({ suggestions: [{ ...ocrItem().suggestions[0], now: "" }] })]);
    expect(build(accept, p)).toMatchObject({ ok: false, status: 400 });
  });
});

describe("buildRecords: alt text", () => {
  const body = (a: Record<string, unknown>) => ({ rev: REV, item: "2:ffeeddccbbaa", choice: "alt", alt: a });

  test("the target is the item's anchor, the pdf the ledger's", async () => {
    const p = await published([figureItem()]);
    expect(records(build(body({ text: "A map of Carmel", decorative: false }), p))[0]).toMatchObject({
      kind: "alt", pdf: PDF, text: "A map of Carmel", decorative: false, anchor: { page: 2, crop_sha: "fedcba9876543210" },
    });
    expect(records(build(body({ text: "", decorative: true }), p))[0]).toMatchObject({ kind: "alt", text: "", decorative: true });
  });

  test("a figure other than the one the item names (a group member) is a 409 and writes nothing", async () => {
    const p = await published([figureItem({ group: { nodeIds: ["2:ffeeddccbbaa", "2:000000000002"] } })]);
    const a = { text: "A map", decorative: false };
    expect(records(build({ ...body(a), node: "2:ffeeddccbbaa" }, p))[0]).toMatchObject({ kind: "alt" });
    expect(records(build(body(a), p))[0]).toMatchObject({ kind: "alt" });
    expect(build({ ...body(a), node: "2:000000000002" }, p)).toMatchObject({ ok: false, status: 409 });
    expect(parseRequest({ ...body(a), node: 5 })).toMatchObject({ ok: false, status: 400 });
  });

  test("limits are 400; an item that is not a figure is a 409", async () => {
    const p = await published([figureItem(), ocrItem()]);
    for (const a of [{ text: "a".repeat(1001), decorative: false }, { text: "a\nb", decorative: false }, { text: "", decorative: false }, { text: "   ", decorative: false }, { text: "x", decorative: true }]) {
      expect(build(body(a), p)).toMatchObject({ ok: false, status: 400 });
    }
    expect(build({ ...body({ text: "x", decorative: false }), item: ITEM }, p)).toMatchObject({ ok: false, status: 409 });
  });
});

describe("buildRecords: dismiss", () => {
  const dismiss = (item: string, finding: string) => ({ rev: REV, item, choice: "dismiss", finding });

  test("a warning finding of the item", async () => {
    const p = await published([warningItem()]);
    expect(records(build(dismiss("3:aabbccddeeff", "5555666677778888"), p))[0]).toMatchObject({ kind: "dismiss", pdf: PDF, findings: ["5555666677778888"] });
  });

  test("an error finding, and the decision warnings, are 400; a finding not on the item is 409", async () => {
    const p = await published([
      figureItem(), warningItem(),
      warningItem({ id: "decision:m2k9x4q1-00000001", role: "decision", findings: [{ code: "decision-orphaned", severity: "warning", id: "aaaaaaaaaaaaaaaa" }] }),
      warningItem({ id: "doc", role: "doc", findings: [{ code: "decisions-other-pdf", severity: "warning", id: "bbbbbbbbbbbbbbbb" }] }),
    ]);
    expect(build(dismiss("2:ffeeddccbbaa", "1111222233334444"), p)).toMatchObject({ ok: false, status: 400 });
    expect(build(dismiss("decision:m2k9x4q1-00000001", "aaaaaaaaaaaaaaaa"), p)).toMatchObject({ ok: false, status: 400 });
    expect(build(dismiss("doc", "bbbbbbbbbbbbbbbb"), p)).toMatchObject({ ok: false, status: 400 });
    expect(build(dismiss("3:aabbccddeeff", "1111222233334444"), p)).toMatchObject({ ok: false, status: 409 });
    expect(build({ ...dismiss("3:aabbccddeeff", "5555666677778888"), rev: "old" }, p)).toMatchObject({ ok: false, status: 409 });
  });
});

describe("buildRecords: supersedes and changing a decision", () => {
  test("re-deciding an orphan: supersedes must be an active correct, keep or alt of this store", async () => {
    const orphan = correct({ id: "m2k9x4q1-00000001" });
    const p = await published([ocrItem()]);
    const ok = records(build({ ...accept, supersedes: orphan.id }, p, store(orphan)));
    expect(ok[0]).toMatchObject({ kind: "correct", supersedes: orphan.id });
    expect(build({ ...accept, supersedes: "m2k9x4q1-00000002" }, p, store(orphan))).toMatchObject({ ok: false, status: 404 });
    const undone = { kind: "undo", id: "m2k9x4q2-00000002", at: "x", pdf: PDF, of: orphan.id } as StoreRecord;
    expect(build({ ...accept, supersedes: orphan.id }, p, store(orphan, undone))).toMatchObject({ ok: false, status: 409 });
    const dismissed = { kind: "dismiss", id: "m2k9x4q3-00000003", at: "x", pdf: PDF, findings: [FINDING] } as StoreRecord;
    expect(build({ ...accept, supersedes: dismissed.id }, p, store(dismissed))).toMatchObject({ ok: false, status: 409 });
    expect(build({ ...accept, supersedes: orphan.id }, await published([figureItem()], emptyLedger()), store(alt({ id: orphan.id })))).toMatchObject({ ok: false });
  });

  test("an alt supersedes only an alt, a text decision only a correct or keep", async () => {
    const a = alt({ id: "m2k9x4q1-0000000a" });
    const p = await published([figureItem(), ocrItem()]);
    const body = { rev: REV, item: "2:ffeeddccbbaa", choice: "alt", alt: { text: "x", decorative: false }, supersedes: a.id };
    expect(records(build(body, p, store(a)))[0]).toMatchObject({ kind: "alt", supersedes: a.id });
    expect(build({ ...accept, supersedes: a.id }, p, store(a))).toMatchObject({ ok: false, status: 409 });
  });

  test("changing an applied decision copies the old record's target and pdf from the store, with no item", () => {
    const old = correct({ id: "m2k9x4q1-00000001", pdf: OTHER_PDF });
    const r = records(build({ choice: "other", supersedes: old.id, now: "80111" }, null, store(old)));
    expect(r[0]).toEqual({ kind: "correct", id: "m2k9x4q9-deadbeef", at: "2026-11-02T10:00:00Z", pdf: OTHER_PDF, anchor: old.anchor, range: old.range, now: "80111", supersedes: old.id });
    expect(records(build({ choice: "keep", supersedes: old.id }, null, store(old)))[0]).toMatchObject({ kind: "keep", anchor: old.anchor, range: old.range, pdf: OTHER_PDF });
    // the value limits use the old record's `was`
    expect(build({ choice: "other", supersedes: old.id, now: "x".repeat(41) }, null, store(old))).toMatchObject({ ok: false, status: 400 });
    const a = alt({ id: "m2k9x4q1-0000000a" });
    expect(records(build({ choice: "alt", supersedes: a.id, alt: { text: "New", decorative: false } }, null, store(a)))[0]).toMatchObject({ kind: "alt", anchor: a.anchor, text: "New", supersedes: a.id });
    expect(build({ choice: "keep", supersedes: a.id }, null, store(a))).toMatchObject({ ok: false, status: 409 });
  });
});

describe("buildRecords: undo", () => {
  const u = (of: string) => ({ choice: "undo", of });

  test("a correct, keep, alt or dismiss in this store; pdf is the decision's; needs no snapshot", () => {
    const c = correct({ id: "m2k9x4q1-00000001" });
    expect(records(build(u(c.id), null, store(c)))[0]).toEqual({ kind: "undo", id: "m2k9x4q9-deadbeef", at: "2026-11-02T10:00:00Z", pdf: c.pdf, of: c.id });
    for (const r of [keep({ id: "m2k9x4q1-00000002" }), alt({ id: "m2k9x4q1-00000003" }), { kind: "dismiss", id: "m2k9x4q1-00000004", at: "x", pdf: PDF, findings: [FINDING] } as StoreRecord]) {
      expect(records(build(u(r.id), null, store(r)))[0]).toMatchObject({ kind: "undo", of: r.id });
    }
  });

  test("an id that is not in the store is 404; an undo, a reattach and an undone decision are 409", () => {
    const c = correct({ id: "m2k9x4q1-00000001" });
    const undo = { kind: "undo", id: "m2k9x4q2-00000002", at: "x", pdf: PDF, of: c.id } as StoreRecord;
    const re = { kind: "reattach", id: "m2k9x4q3-00000003", at: "x", pdf: PDF, from: OTHER_PDF, to: PDF } as StoreRecord;
    expect(build(u("m2k9x4q1-ffffffff"), null, store(c))).toMatchObject({ ok: false, status: 404 });
    expect(build(u(c.id), null, store())).toMatchObject({ ok: false, status: 404 });
    expect(build(u(undo.id), null, store(c, undo))).toMatchObject({ ok: false, status: 409 });
    expect(build(u(re.id), null, store(re))).toMatchObject({ ok: false, status: 409 });
    expect(build(u(c.id), null, store(c, undo))).toMatchObject({ ok: false, status: 409 });
  });

  test("an undo that came before its target did not undo it", () => {
    const early = { kind: "undo", id: "m2k9x4q2-00000002", at: "x", pdf: PDF, of: "m2k9x4q1-00000001" } as StoreRecord;
    const c = correct({ id: "m2k9x4q1-00000001" });
    expect(fold(store(early, c)).undone).toEqual([]);
    expect(records(build(u(c.id), null, store(early, c)))[0]).toMatchObject({ kind: "undo" });
  });
});

describe("buildRecords: reattach", () => {
  const ledger = (orphaned: { id: string; reason: string }[]) =>
    emptyLedger({ orphaned: orphaned.map((o) => ({ ...o, kind: "correct", page: 1, bbox: null, was: "a", now: "b", text_now: "" })) });
  const docItem = warningItem({ id: "doc", role: "doc", findings: [{ code: "decisions-other-pdf", severity: "warning", id: "bbbbbbbbbbbbbbbb" }] });
  const body = { rev: REV, item: "doc", choice: "reattach" };

  test("one reattach per old PDF hash among the ledger's other-pdf orphans, to the snapshot's hash", async () => {
    const a = correct({ id: "m2k9x4q1-00000001", pdf: OTHER_PDF });
    const b = correct({ id: "m2k9x4q1-00000002", pdf: OTHER_PDF });
    const c = correct({ id: "m2k9x4q1-00000003", pdf: "c3".repeat(32) });
    const d = correct({ id: "m2k9x4q1-00000004" }); // on this PDF: not an other-pdf orphan
    const p = await published([docItem], ledger([a, b, c, d].map((r) => ({ id: r.id, reason: r.pdf === PDF ? "no-node" : "other-pdf" }))));
    const ids = ["m2k9x4q9-00000001", "m2k9x4q9-00000002"];
    let n = 0;
    const r = buildRecords(req(body), { ...ctx(p, store(a, b, c, d)), id: () => ids[n++]! });
    expect(records(r)).toEqual([
      { kind: "reattach", id: ids[0], at: "2026-11-02T10:00:00Z", pdf: PDF, from: OTHER_PDF, to: PDF },
      { kind: "reattach", id: ids[1], at: "2026-11-02T10:00:00Z", pdf: PDF, from: "c3".repeat(32), to: PDF },
    ]);
  });

  test("nothing to reattach, or a stale rev, or no such item, writes nothing", async () => {
    const a = correct({ id: "m2k9x4q1-00000001", pdf: OTHER_PDF });
    const p = await published([docItem], ledger([{ id: a.id, reason: "other-pdf" }]));
    expect(build(body, await published([docItem], ledger([])), store(a))).toMatchObject({ ok: false, status: 409 });
    expect(build({ ...body, rev: "old" }, p, store(a))).toMatchObject({ ok: false, status: 409 });
    expect(build({ ...body, item: "9:nope" }, p, store(a))).toMatchObject({ ok: false, status: 409 });
    expect(build({ ...body, item: "3:aabbccddeeff" }, await published([warningItem(), docItem], ledger([{ id: a.id, reason: "other-pdf" }])), store(a))).toMatchObject({ ok: false, status: 409 });
  });

  test("another document warning listed first under the same id does not hide the other-pdf item", async () => {
    const a = correct({ id: "m2k9x4q1-00000001", pdf: OTHER_PDF });
    const title = warningItem({ id: "doc", role: "doc", findings: [{ code: "title-suspect", severity: "warning", id: "cccccccccccccccc" }] });
    const p = await published([title, docItem], ledger([{ id: a.id, reason: "other-pdf" }]));
    expect(records(build(body, p, store(a)))).toHaveLength(1);
  });

  test("a hash already reattached to this PDF is not written again", async () => {
    const a = correct({ id: "m2k9x4q1-00000001", pdf: OTHER_PDF });
    const done = { kind: "reattach", id: "m2k9x4q2-00000002", at: "x", pdf: PDF, from: OTHER_PDF, to: PDF } as StoreRecord;
    const p = await published([docItem], ledger([{ id: a.id, reason: "other-pdf" }]));
    expect(build(body, p, store(a, done))).toMatchObject({ ok: false, status: 409 });
  });
});
