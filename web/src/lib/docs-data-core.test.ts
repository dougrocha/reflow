import { describe, expect, test } from "bun:test";
import { documentFrom, pageFrom, type RawSidecar } from "./docs-data-core";

// The pure part of the stamped reads (docs-data.ts): plain objects in, plain objects out. The
// stamping itself (the generation read before the files) is checked in docs-data.test.ts.

/** A manifest as reflow writes it (schema/manifest.schema.json), with `n` pages. */
const manifest = (n: number, fields: Record<string, unknown> = {}) => ({
  v: `v${n}`, title: `Title ${n}`, lang: "en", dir: "ltr", pageCount: n, pageSize: [612, 792], labelOffset: 0,
  imageWidths: [800, 1600], pages: {}, chunks: [{ href: "chunks/0001.html", first: 1, last: n }],
  toc: [], textBase: 1.1, text: Array.from({ length: n }, () => [0, 0, 0]), tocComplete: true, ...fields,
});

const item = (fields: Record<string, unknown> = {}) => ({ id: "r", page: 1, severity: "warning", ...fields });

const files = (fields: Record<string, unknown> = {}) => ({
  slug: "fw9", generation: 3, manifest: manifest(2), review: [], audit: null, build: null, ...fields,
});

describe("documentFrom", () => {
  test("a manifest gives the page count, title, labels and image set, under the stamp", () => {
    const audit = { filename: "fw9.pdf", passed: true, findings: [] };
    const doc = documentFrom(files({ audit }));
    expect(doc).toEqual({
      slug: "fw9", generation: 3, pages: 2, title: "Title 2", labels: ["1", "2"],
      images: { widths: [800, 1600], version: "v2" }, review: [], audit, build: null, outdated: null,
    });
  });

  test("no manifest reads as not converted", () => {
    const doc = documentFrom(files({ manifest: null, review: null }));
    expect(doc).toMatchObject({ pages: null, title: null, labels: [], images: {}, review: null, outdated: null });
  });

  test("an empty title reads as none, and a missing audit as null", () => {
    const doc = documentFrom(files({ manifest: manifest(2, { title: "" }), audit: undefined }));
    expect(doc.title).toBeNull();
    expect(doc.audit).toBeNull();
    expect(doc.pages).toBe(2);
  });

  test("a manifest that is not one makes the document outdated, with no pages, title or review", () => {
    const doc = documentFrom(files({ manifest: { pageCount: 9, v: "v9" }, review: [item()] }));
    expect(doc.outdated).toContain('manifest.json does not match its schema: (root): missing "title"');
    expect({ pages: doc.pages, title: doc.title, labels: doc.labels, images: doc.images, review: doc.review })
      .toEqual({ pages: null, title: null, labels: [], images: {}, review: null });
  });

  test("a review that is not a list reads as none", () => {
    expect(documentFrom(files({ review: { id: "r" } })).review).toBeNull();
    expect(documentFrom(files({ review: null })).review).toBeNull();
  });

  test("review items get a box, page size and targets by default, and keep the ones they have", () => {
    const targets = [{ code: "ocr-reading-lost", box: [1, 2, 3, 4] }, { code: "ocr-band-missing", box: [5, 6, 7, 8] }];
    const doc = documentFrom(files({
      review: [
        item({ id: "a" }),
        item({ id: "b", targets, bbox: [9, 9, 9, 9], pageSize: [612, 792] }),
      ],
    }));
    const [a, b] = doc.review!;
    expect({ bbox: a.bbox, pageSize: a.pageSize, targets: a.targets }).toEqual({ bbox: null, pageSize: null, targets: [] });
    expect({ bbox: b.bbox, pageSize: b.pageSize, targets: b.targets }).toEqual({ bbox: [9, 9, 9, 9], pageSize: [612, 792], targets });
  });

  test("an item's page label is the manifest's label for its page, and null without a page or beyond the document", () => {
    const doc = documentFrom(files({
      manifest: manifest(3, { labelOffset: 4, pages: { "2": { label: "iii" } } }),
      review: [item({ id: "a", page: 1 }), item({ id: "b", page: 2 }), item({ id: "c", page: null }), item({ id: "d", page: 7 })],
    }));
    expect(doc.labels).toEqual(["5", "iii", "7"]);
    expect(doc.review?.map((i) => i.pageLabel)).toEqual(["5", "iii", null, null]);
  });

  test("the build record is kept when this app can show it, and null otherwise", () => {
    const build = { schema: 1, engine: "heuristic", ai: [], built: "2026-01-01", ocr: { engine: "none" } };
    expect(documentFrom(files({ build })).build).toEqual(build as never);
    expect(documentFrom(files({ build: { schema: 2 } })).build).toBeNull();
    expect(documentFrom(files({ build: "x" })).build).toBeNull();
  });
});

describe("pageFrom", () => {
  const trace = { publication: 1 } as never;

  test("no sidecar gives null, with the trace and stamp as they are", () => {
    expect(pageFrom({ slug: "fw9", generation: 3, sidecar: null, trace })).toEqual({ slug: "fw9", generation: 3, sidecar: null, trace });
  });

  test("a sidecar gets its defaults, and each fact is labelled by the sidecar's labels, else its code", () => {
    const raw = {
      w: 612, h: 792,
      labels: { "ocr-reading-lost": "Reading lost" },
      nodes: [
        { id: "n1", text: null },
        {
          id: "n2", text: "hello", attrs: { role: "p" },
          facts: [{ code: "ocr-reading-lost", source: "ocr" }, { code: "other", source: "x", detail: "d" }],
        },
      ],
    } as unknown as RawSidecar;
    const { sidecar } = pageFrom({ slug: "fw9", generation: 0, sidecar: raw, trace: null });
    expect(sidecar).toMatchObject({ width: 612, height: 792, sections: [] });
    expect(sidecar?.nodes[0]).toMatchObject({ id: "n1", text: "", attrs: {}, facts: [] });
    expect(sidecar?.nodes[1]).toMatchObject({ text: "hello", attrs: { role: "p" } });
    expect(sidecar?.nodes[1].facts).toEqual([
      { code: "ocr-reading-lost", source: "ocr", label: "Reading lost" },
      { code: "other", source: "x", detail: "d", label: "other" },
    ]);
  });
});
