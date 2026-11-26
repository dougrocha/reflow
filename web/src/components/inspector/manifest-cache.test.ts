import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { manifestFor } from "./manifest-cache";

const real = globalThis.fetch;
let reads: string[];

// A manifest.json as reflow writes it (schema/manifest.schema.json), with `n` pages.
const manifestJson = (n: number) => ({
  v: `v${n}`, title: "T", lang: "en", dir: "ltr", pageCount: n, pageSize: [612, 792], labelOffset: 0,
  pages: {}, chunks: [{ href: "chunks/0001.html", first: 1, last: n }],
  toc: [], textBase: 1.1, text: Array.from({ length: n }, () => [0, 0, 0]), tocComplete: true,
});

beforeEach(() => {
  reads = [];
  let pages = 1;
  globalThis.fetch = (async (url: string) => {
    reads.push(String(url));
    return Response.json(manifestJson(pages++));
  }) as unknown as typeof fetch;
});
afterEach(() => { globalThis.fetch = real; });

describe("manifestFor", () => {
  test("one read per build, shared by every page", async () => {
    const a = await manifestFor("/docs/cache-a/", "v1");
    const b = await manifestFor("/docs/cache-a/", "v1");
    expect(b).toBe(a);
    expect(reads).toHaveLength(1);
  });

  test("a rebuild (a new version) reads the manifest again", async () => {
    const before = await manifestFor("/docs/cache-b/", "v1");
    const after = await manifestFor("/docs/cache-b/", "v2");
    expect(after.pageCount).not.toBe(before.pageCount);
    expect(reads).toHaveLength(2);
  });

  test("without a version nothing is kept", async () => {
    await manifestFor("/docs/cache-c/", null);
    await manifestFor("/docs/cache-c/", null);
    expect(reads).toHaveLength(2);
  });
});
