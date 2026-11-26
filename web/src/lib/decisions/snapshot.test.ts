import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { rm } from "node:fs/promises";
import { join } from "node:path";
import { readPublished } from "./snapshot";
import { emptyLedger, figureItem, ocrItem, PDF, publications, removeRoot, REV, tempRoot, writePublished } from "./testkit";

let root: string;
let docsRoot: string;
beforeEach(async () => { root = await tempRoot(); docsRoot = join(root, "docs"); });
afterEach(() => removeRoot(root));

const read = (pubs = publications({})) => readPublished("fw9", { docsRoot, publications: pubs });

describe("readPublished", () => {
  test("a coherent snapshot: the build's rev, items, ledger and generation", async () => {
    await writePublished(docsRoot, "fw9");
    const r = await read();
    if (!r.ok) throw new Error(r.error);
    expect(r.published.rev).toBe(REV);
    expect(r.published.generation).toBe(3);
    expect(r.published.items.map((i) => i.id)).toEqual(["5:1a2b3c4d5e6f", "2:ffeeddccbbaa", "3:aabbccddeeff"]);
    expect(r.published.items[0]!.suggestions[0]).toMatchObject({ was: "8011O", now: "80110", id: "9f8e7d6c5b4a3210", before: "CO " });
    expect(r.published.items[0]!.findings).toEqual([{ code: "ocr-suggestion", severity: "warning", id: "0a1b2c3d4e5f6789" }]);
    expect(r.published.items[1]!.anchor?.crop_sha).toBe("fedcba9876543210");
    expect(r.published.ledger?.source.sha256).toBe(PDF);
  });

  test("a publication in progress is a 409 that says to reload, before or after the read", async () => {
    await writePublished(docsRoot, "fw9");
    for (const pubs of [publications({ publishing: "20260101-000000-abcdef" }), publications({}, { publishing: "20260101-000000-abcdef" })]) {
      expect(await read(pubs)).toEqual({ ok: false, status: 409, error: "The document is being published. Reload the page.", reload: true });
    }
  });

  test("a generation that changes between the two reads is a 409", async () => {
    await writePublished(docsRoot, "fw9");
    const r = await read(publications({ generation: 3 }, { generation: 4 }));
    expect(r).toMatchObject({ ok: false, status: 409, reload: true });
  });

  test("a document built without the build record has generation 0 throughout", async () => {
    await writePublished(docsRoot, "fw9");
    const r = await readPublished("fw9", { docsRoot, publications: async () => ({}) });
    expect(r.ok && r.published.generation).toBe(0);
  });

  test("no decisions.json is a ledger of null, so the caller can say to rebuild", async () => {
    await writePublished(docsRoot, "fw9", { ledger: null });
    const r = await read();
    expect(r.ok && r.published.ledger).toBeNull();
  });

  test("a ledger without a usable PDF hash is no ledger", async () => {
    await writePublished(docsRoot, "fw9", { ledger: emptyLedger({ source: { sha256: "" } }) });
    const r = await read();
    expect(r.ok && r.published.ledger).toBeNull();
  });

  test("missing or unreadable manifest or review.json is a 409", async () => {
    expect(await read()).toMatchObject({ ok: false, status: 409 });
    await writePublished(docsRoot, "fw9");
    await rm(join(docsRoot, "fw9", "review.json"));
    expect(await read()).toMatchObject({ ok: false, status: 409 });
    await writePublished(docsRoot, "fw9", { manifest: { v: 7, other: true } });
    expect(await read()).toMatchObject({ ok: true });
    await writePublished(docsRoot, "fw9", { manifest: {} });
    expect(await read()).toMatchObject({ ok: false, status: 409 });
  });

  test("items and suggestions that are not well-formed are left out, never half-read", async () => {
    const bad = ocrItem({ id: "9:bad", suggestions: [{ was: "x" }, { ...ocrItem().suggestions[0], anchor: null }], findings: [{ code: 1 }] });
    await writePublished(docsRoot, "fw9", { items: [bad, 5, null, figureItem({ anchor: { page: 0 } }), { nope: true }] });
    const r = await read();
    if (!r.ok) throw new Error(r.error);
    expect(r.published.items.map((i) => i.id)).toEqual(["9:bad", "2:ffeeddccbbaa"]);
    expect(r.published.items[0]).toMatchObject({ suggestions: [], findings: [] });
    expect(r.published.items[1]!.anchor).toBeNull();
  });

  test("reads the files again each time (uncached)", async () => {
    await writePublished(docsRoot, "fw9");
    expect((await read() as { published: { rev: string } }).published.rev).toBe(REV);
    await writePublished(docsRoot, "fw9", { rev: "c0ffee000000" });
    expect((await read() as { published: { rev: string } }).published.rev).toBe("c0ffee000000");
  });

  test("a slug that is not a document name reads nothing", async () => {
    await expect(readPublished("../x", { docsRoot, publications: publications({}) })).rejects.toThrow();
  });
});
