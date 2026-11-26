import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { appendFile, mkdir, readFile, stat, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fold, pending } from "./fold";
import { storePath } from "../paths";
import { append, readStore } from "./store";
import { correct, keep, removeRoot, tempRoot } from "./testkit";

let root: string;
beforeEach(async () => { root = await tempRoot(); });
afterEach(() => removeRoot(root));

const lines = async (slug = "fw9") => (await readFile(storePath(root, slug), "utf8")).split("\n");

describe("append", () => {
  test("the first write creates the header, then the record", async () => {
    const rec = correct();
    expect(await append("fw9", rec, root)).toEqual([rec]);
    const [header, record, end] = await lines();
    expect(JSON.parse(header!)).toMatchObject({ v: 1, kind: "store", slug: "fw9" });
    expect(JSON.parse(header!).id).toMatch(/^[0-9a-f]{16}$/);
    expect(JSON.parse(header!).created).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    expect(JSON.parse(record!)).toMatchObject({ v: 1, id: rec.id, kind: "correct", now: "80110", pdf: rec.pdf });
    expect(end).toBe("");
    const second = keep();
    await append("fw9", second, root);
    const snap = await readStore("fw9", root);
    expect(snap.entries.map((e) => (e.parsed.type === "record" ? e.parsed.record.id : null))).toEqual([rec.id, second.id]);
    expect((await lines()).filter(Boolean)).toHaveLength(3); // one header only
  });

  test("a torn tail gets a newline, then the record", async () => {
    await append("fw9", correct(), root);
    const path = storePath(root, "fw9");
    await appendFile(path, '{"v":1,"id":"m2k9x4q2-0000');
    const rec = keep();
    await append("fw9", rec, root);
    const text = await readFile(path, "utf8");
    expect(text).toContain('{"v":1,"id":"m2k9x4q2-0000\n{');
    const snap = await readStore("fw9", root);
    expect(snap.unreadable).toHaveLength(1);
    expect(snap.torn).toBe(0);
    expect(fold(snap).active.map((r) => r.id)).toContain(rec.id);
  });

  test("20 parallel appends give 20 lines that all parse", async () => {
    const recs = Array.from({ length: 20 }, (_, i) => correct({ range: { was: `8011O`, before: `${i} `, after: "", rule: "zip-digit" } }));
    await Promise.all(recs.map((r) => append("fw9", r, root)));
    const snap = await readStore("fw9", root);
    expect(snap.lines).toBe(21);
    expect(snap.unreadable).toHaveLength(0);
    expect(new Set(snap.entries.map((e) => e.parsed.type === "record" ? e.parsed.record.id : "")).size).toBe(20);
    expect(snap.entries.every((e) => e.parsed.type === "record")).toBe(true);
  }, 60_000);

  test("a symlink in place of the store file is refused and its target untouched", async () => {
    const dir = join(root, ".decisions");
    await mkdir(dir, { recursive: true });
    const target = join(root, "elsewhere.jsonl");
    await writeFile(target, "keep me\n");
    await symlink(target, storePath(root, "fw9"));
    await expect(append("fw9", correct(), root)).rejects.toThrow();
    expect(await readFile(target, "utf8")).toBe("keep me\n");
    await expect(readStore("fw9", root)).rejects.toThrow();
  });

  test("a store with no valid header is refused, an empty file gets its header", async () => {
    const dir = join(root, ".decisions");
    await mkdir(dir, { recursive: true });
    await writeFile(storePath(root, "fw9"), "not a header\n");
    await expect(append("fw9", correct(), root)).rejects.toThrow(/header/);
    await writeFile(storePath(root, "p17"), "");
    await append("p17", correct(), root);
    expect((await readStore("p17", root)).store).toMatch(/^[0-9a-f]{16}$/);
  });

  test("a line over 8 KB is refused and nothing is written", async () => {
    await expect(append("fw9", correct({ now: "x".repeat(9000) }), root)).rejects.toThrow(/8192/);
    await expect(stat(storePath(root, "fw9"))).rejects.toThrow();
  });

  test("the decide callback sees the store under the lock and may refuse", async () => {
    const first = correct();
    await append("fw9", first, root);
    const refused = await append("fw9", (snap) => (snap.entries.length === 1 ? { refused: "already", status: 409 } : correct()), root);
    expect(refused).toEqual({ refused: "already", status: 409 });
    expect((await readStore("fw9", root)).entries).toHaveLength(1);
  });

  test("a slug that is not a document name never becomes a path", async () => {
    await expect(append("../x", correct(), root)).rejects.toThrow();
    expect(() => storePath(root, "A-Upper")).toThrow();
    await expect(readStore("a/b", root)).rejects.toThrow();
  });
});

describe("readStore", () => {
  test("a missing file is an empty store", async () => {
    expect(await readStore("fw9", root)).toMatchObject({ store: null, bytes: 0, lines: 0, entries: [], error: null });
  });
});

describe("pending", () => {
  test("a record after the ledger's bytes is pending, one before is not", async () => {
    const a = correct(), b = keep();
    await append("fw9", a, root);
    const mid = await readStore("fw9", root);
    await append("fw9", b, root);
    const snap = await readStore("fw9", root);
    const ids = (p: ReturnType<typeof pending>) => p.map((e) => (e.parsed.type === "record" ? e.parsed.record.id : ""));
    expect(ids(pending(snap, { store: mid.store, bytes: mid.bytes }))).toEqual([b.id]);
    expect(ids(pending(snap, { store: snap.store, bytes: snap.bytes }))).toEqual([]);
  });

  test("a recreated store (another id) makes every record pending", async () => {
    await append("fw9", correct(), root);
    await append("fw9", keep(), root);
    const snap = await readStore("fw9", root);
    expect(pending(snap, { store: "ffffffffffffffff", bytes: snap.bytes })).toHaveLength(2);
    expect(pending(snap, { store: null, bytes: 0 })).toHaveLength(2);
  });
});
