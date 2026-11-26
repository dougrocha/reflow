import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { generated } from "../../scripts/schema-gen";
import { normalizeManifest } from "./core";
import { contract } from "./contract-fixture";
import { MANIFEST_SCHEMA } from "./manifest.gen";
import { transcriptAnchor } from "./react/transcript-style";
import { generateTypes, validate, type Schema } from "./schema";

// The other half of tests/test_viewer_contract.py: schema/viewer-contract.json holds what Python
// publishes for a small document, and the viewer's own code must read the same pages and anchors.
// A copy to break: loosely typed, so a test can put the wrong thing anywhere.
type Loose = { chunks: Record<string, unknown>[]; pages: Record<string, unknown>; [key: string]: unknown };
const copy = () => structuredClone(contract.manifest) as Loose;

describe("the viewer reads what Python publishes", () => {
  test("each page as Python laid it out: label, size, image, chunk and text size", () => {
    expect(normalizeManifest(contract.manifest).pages).toEqual(contract.pages);
  });

  test("the version digest, the form and the image widths", () => {
    const m = normalizeManifest(contract.manifest);
    expect(m.version).toBe(contract.manifest.v as string);
    expect(m.fillable).toBe(true);
    expect(m.imageWidths).toEqual(contract.manifest.imageWidths as number[]);
  });

  test("a node's anchor is the id Python gave its element, and each contents entry points at one", () => {
    for (const [node, id] of contract.anchors) expect(transcriptAnchor(node)).toBe(id);
    const ids = new Set(contract.anchors.map(([, id]) => id));
    for (const entry of normalizeManifest(contract.manifest).toc) expect(ids.has(entry.id)).toBe(true);
  });
});

describe("a manifest that does not match the schema", () => {
  test("names the wrong field instead of reading it as NaN", () => {
    const m = copy();
    m.chunks[1].first = "two";
    expect(() => normalizeManifest(m)).toThrow('chunks[1].first: expected integer, got "two"');
  });

  test("names a missing field and a field the schema doesn't have", () => {
    const m = copy();
    delete m.textBase;
    m.page_count = 4;
    expect(() => normalizeManifest(m)).toThrow(/missing "textBase".*unexpected "page_count"/);
    // A key every object inherits is still not one of the schema's.
    expect(validate(MANIFEST_SCHEMA, { ...copy(), constructor: 1 })).toEqual(['(root): unexpected "constructor"']);
  });

  test("rejects a chunk href that leaves the document", () => {
    for (const href of ["/api/builds", "../other/chunks/0001.html", "https://evil.example/c.html", "javascript:alert(1)"]) {
      const m = copy();
      m.chunks[0].href = href;
      expect(() => normalizeManifest(m)).toThrow(`chunks[0].href: not a path inside the document: ${href}`);
    }
  });

  test("rejects a page key that is not a page number and a size that is not a pair", () => {
    const m = copy();
    m.pages.first = {};
    m.pageSize = [612];
    expect(validate(MANIFEST_SCHEMA, m)).toEqual(["pageSize: has 1 items, needs 2", 'pages.first (name): "first" does not match ^[1-9][0-9]*$']);
  });
});

describe("the generated types", () => {
  test("are what bun run schema:gen writes from the schema", () => {
    for (const { out, path, text } of generated()) {
      expect(readFileSync(path, "utf8"), `${out} is stale: cd web && bun run schema:gen`).toBe(text);
    }
  });

  test("a keyword outside the supported subset is an error, not ignored", () => {
    const schema = { title: "X", type: "string", format: "date" } as unknown as Schema;
    expect(() => validate(schema, "x")).toThrow('"format" is outside the supported subset');
    expect(() => generateTypes(schema, "x.json", "X_SCHEMA")).toThrow('"format" is outside the supported subset');
  });
});
