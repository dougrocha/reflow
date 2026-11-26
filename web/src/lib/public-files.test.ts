import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdir, symlink, utimes, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { cacheControlFor, rawFileHeaders, resolvePublicFile, servePublicFile } from "./public-files";
import { removeDir, tempDir } from "./builds/testkit";

const SLUGS = ["fw9", "resnet"];
let root: string;
let outside: string;
beforeEach(async () => {
  root = await tempDir();
  outside = await tempDir();
  await mkdir(join(root, "docs/fw9/pages"), { recursive: true });
  await mkdir(join(root, "docs/resnet"), { recursive: true });
  await mkdir(join(root, "pdfs"), { recursive: true });
  await writeFile(join(root, "docs/fw9/manifest.json"), '{"pageCount":6}');
  await writeFile(join(root, "docs/fw9/pages/0001.webp"), "webp-bytes");
  await writeFile(join(root, "docs/fw9/secret"), "no extension");
  await writeFile(join(root, "docs/resnet/manifest.json"), "{}");
  await writeFile(join(root, "pdfs/fw9.pdf"), "%PDF-");
  await writeFile(join(outside, "passwd.json"), "outside");
});
afterEach(async () => {
  await removeDir(root);
  await removeDir(outside);
});

const get = (kind: "docs" | "pdfs", segs: string[], url = "http://x/y", headers: Record<string, string> = {}, method = "GET") =>
  servePublicFile(new Request(url, { method, headers }), root, kind, segs, SLUGS);

describe("files of a build, read from disk on each request", () => {
  test("a file is served with its type, length and validators", async () => {
    const r = await get("docs", ["fw9", "manifest.json"]);
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toBe("application/json; charset=utf-8");
    expect(r.headers.get("content-length")).toBe("15");
    expect(r.headers.get("etag")).toMatch(/^W\//);
    expect(r.headers.get("last-modified")).not.toBeNull();
    expect(await r.text()).toBe('{"pageCount":6}');
  });

  test("a file added after the first request is served (no restart, no cache of the listing)", async () => {
    expect((await get("docs", ["fw9", "pages", "0002.webp"])).status).toBe(404);
    await writeFile(join(root, "docs/fw9/pages/0002.webp"), "new");
    const r = await get("docs", ["fw9", "pages", "0002.webp"]);
    expect([r.status, await r.text()]).toEqual([200, "new"]);
  });

  test("a changed file changes its ETag, and the old one gets 304 only while it is current", async () => {
    const first = await get("docs", ["fw9", "manifest.json"]);
    const etag = first.headers.get("etag")!;
    expect((await get("docs", ["fw9", "manifest.json"], "http://x/y", { "if-none-match": etag })).status).toBe(304);
    await writeFile(join(root, "docs/fw9/manifest.json"), '{"pageCount":7, "more":1}');
    await utimes(join(root, "docs/fw9/manifest.json"), new Date(), new Date(Date.now() + 5000));
    const again = await get("docs", ["fw9", "manifest.json"], "http://x/y", { "if-none-match": etag });
    expect(again.status).toBe(200);
    expect(await again.text()).toContain("more");
  });

  test("HEAD has the headers and no body", async () => {
    const r = await get("docs", ["fw9", "pages", "0001.webp"], "http://x/y", {}, "HEAD");
    expect(r.status).toBe(200);
    expect(r.headers.get("content-length")).toBe("10");
    expect(await r.text()).toBe("");
  });

  test("a pdf is served from pdfs/", async () => {
    const r = await get("pdfs", ["fw9.pdf"]);
    expect([r.status, r.headers.get("content-type")]).toEqual([200, "application/pdf"]);
    expect((await get("pdfs", ["resnet.pdf"])).status).toBe(404); // known slug, no file
    expect((await get("pdfs", ["nope.pdf"])).status).toBe(404);
  });
});

describe("caching headers match next.config.ts", () => {
  test("immutable only for a versioned URL of pages, figures, chunks and assets", async () => {
    expect((await get("docs", ["fw9", "pages", "0001.webp"], "http://x/y?v=abc")).headers.get("cache-control")).toBe("public, max-age=31536000, immutable");
    expect((await get("docs", ["fw9", "pages", "0001.webp"])).headers.get("cache-control")).toBe("public, max-age=0, must-revalidate");
    expect((await get("docs", ["fw9", "manifest.json"], "http://x/y?v=abc")).headers.get("cache-control")).toBe("public, max-age=0, must-revalidate");
    expect(cacheControlFor(["assets"], true)).toBe("public, max-age=0");
    expect(cacheControlFor(["index.html"], true)).toBe("public, max-age=0");
    expect((await get("pdfs", ["fw9.pdf"])).headers.get("cache-control")).toBe("public, max-age=0");
    expect(cacheControlFor(["chunks", "0001.html"], true)).toBe("public, max-age=31536000, immutable");
  });
});

describe("a document's own pages run sandboxed, as next.config.ts serves them", () => {
  test("index.html, a chunk page and an SVG get the sandbox CSP; JSON, images and PDFs do not", async () => {
    await mkdir(join(root, "docs/fw9/chunks"), { recursive: true });
    await mkdir(join(root, "docs/fw9/figures"), { recursive: true });
    await writeFile(join(root, "docs/fw9/index.html"), "<!doctype html><p>x");
    await writeFile(join(root, "docs/fw9/chunks/0001.html"), "<!doctype html><p>x");
    await writeFile(join(root, "docs/fw9/figures/f1.svg"), "<svg/>");
    for (const segs of [["fw9", "index.html"], ["fw9", "chunks", "0001.html"], ["fw9", "figures", "f1.svg"]]) {
      const csp = (await get("docs", segs)).headers.get("content-security-policy") ?? "";
      expect(csp).toContain("sandbox allow-scripts");
      expect(csp).not.toContain("allow-same-origin");
    }
    for (const [kind, segs] of [["docs", ["fw9", "manifest.json"]], ["docs", ["fw9", "pages", "0001.webp"]], ["pdfs", ["fw9.pdf"]]] as const) {
      expect((await get(kind, [...segs])).headers.get("content-security-policy")).toBeNull();
    }
  });

  test("the chunk pages and toc.json, which the sandboxed viewer fetches, are readable across origins; nothing else is", () => {
    expect(rawFileHeaders("docs", ["chunks", "0001.html"])["Access-Control-Allow-Origin"]).toBe("*");
    expect(rawFileHeaders("docs", ["toc.json"])["Access-Control-Allow-Origin"]).toBe("*");
    for (const rest of [["index.html"], ["manifest.json"], ["review.json"], ["pages", "0001.webp"], ["original.pdf"]]) {
      expect(rawFileHeaders("docs", rest)["Access-Control-Allow-Origin"]).toBeUndefined();
    }
    expect(rawFileHeaders("pdfs", ["fw9.pdf"])).toEqual({});
  });
});

describe("it serves only the public files of a known document", () => {
  test("unknown slugs, bare folders, other types and short paths are 404", async () => {
    for (const segs of [["nope", "manifest.json"], ["fw9"], ["fw9", "pages"], ["fw9", "secret"], ["fw9", "missing.json"], []]) {
      expect((await get("docs", segs)).status).toBe(404);
    }
    expect((await get("pdfs", ["fw9.pdf", "x"])).status).toBe(404);
  });

  test("path traversal in any form is refused", async () => {
    const bad = [
      ["fw9", "..", "resnet", "manifest.json"], ["fw9", "pages", "..", "..", "..", "pdfs", "fw9.pdf"],
      ["fw9", "../resnet/manifest.json"], ["fw9", "..\\resnet\\manifest.json"], ["..", "x.json"],
      ["fw9", "pages/../manifest.json"], ["fw9", ".", "manifest.json"], ["fw9", "", "manifest.json"],
      ["fw9", "manifest.json\0.png"], ["fw9", ".hidden.json"], ["fw9", "%2e%2e", "x.json"],
      [join(root, "docs/fw9/manifest.json")],
    ];
    for (const segs of bad) expect((await get("docs", segs)).status).toBe(404);
    for (const segs of [["..", "docs", "fw9", "manifest.json"], ["../docs/fw9/manifest.json"], ["fw9.pdf", ".."]]) {
      expect((await get("pdfs", segs)).status).toBe(404);
    }
  });

  test("a symlink out of the document's folder is not followed", async () => {
    await symlink(join(outside, "passwd.json"), join(root, "docs/fw9/link.json"));
    await symlink(outside, join(root, "docs/fw9/dir"));
    expect((await get("docs", ["fw9", "link.json"])).status).toBe(404);
    expect((await get("docs", ["fw9", "dir", "passwd.json"])).status).toBe(404);
    expect(await resolvePublicFile(root, "docs", ["fw9", "link.json"], SLUGS)).toBeNull();
  });

  test("a symlink to a sibling document is not followed either", async () => {
    await symlink(join(root, "docs/resnet/manifest.json"), join(root, "docs/fw9/sibling.json"));
    expect((await get("docs", ["fw9", "sibling.json"])).status).toBe(404);
  });
});
