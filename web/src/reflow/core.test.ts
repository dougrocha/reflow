import { afterEach, describe, expect, test } from "bun:test";
import { loadChunkPages, pageImageSet, resolve } from "./core";
import { BASE, published, serveContract } from "./contract-fixture";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const load = async (name: string) => {
  globalThis.fetch = serveContract;
  return loadChunkPages(BASE, `chunks/${name}.html`);
};

describe("loadChunkPages, on the chunk pages Python publishes", () => {
  test("splits a chunk into its pages and drops the page label", async () => {
    const pages = await load("0002");
    expect([...pages.keys()]).toEqual([2, 3]);
    for (const html of pages.values()) expect(html).not.toContain("pg-label");
    expect(published("chunks/0002.html")).toContain("pg-label");
  });

  test("keeps the OCR note and data-source", async () => {
    const html = (await load("0003")).get(4) ?? "";
    expect(html).toContain('class="ocr-note"');
    expect(html).toContain("data-generated");
    expect(html).toContain('data-source="ocr"');
  });

  test("keeps page, note and backlink fragments in the viewer", async () => {
    const first = (await load("0001")).get(1) ?? "";
    expect(first).toMatch(/role="doc-noteref" href="#n-1:\w+" id="r-1:\w+"/);
    expect(first).toMatch(/role="doc-backlink" href="#r-1:\w+"/);
    expect((await load("0002")).get(2) ?? "").toContain('href="#p4"');
  });

  test("points figures at the document and other links at the site", async () => {
    const html = (await load("0002")).get(2) ?? "";
    expect(html).toContain('src="/doc/figures/p0002-1.webp?v=0123456789ab"');
    globalThis.fetch = (async () => new Response(
      published("chunks/0002.html").replace("</div></div>", '<a href="other.html#intro">x</a></div></div>'))) as unknown as typeof fetch;
    expect((await loadChunkPages(BASE, "chunks/0002.html")).get(3) ?? "").toContain('href="/doc/other.html"');
  });
});

const CDN = "https://cdn.example/docs/resnet/";

describe("resolve, with a path-only base", () => {
  test("stays a root-relative path, with the version added once", () => {
    expect(resolve("docs/resnet/", "manifest.json")).toBe("/docs/resnet/manifest.json");
    expect(resolve("/my docs/%C3%A9/", "a b.json")).toBe("/my%20docs/%C3%A9/a%20b.json");
    expect(resolve("/docs/resnet/", "manifest.json")).toBe("/docs/resnet/manifest.json");
    expect(resolve("/docs/resnet/", "pages/0001.webp", "abc")).toBe("/docs/resnet/pages/0001.webp?v=abc");
    expect(resolve("/docs/resnet/", "pages/0001.webp?v=old", "abc")).toBe("/docs/resnet/pages/0001.webp?v=old");
  });

  test("keeps dropping a fragment of the path", () => {
    expect(resolve("/docs/resnet/", "chunks/0002.html#p5")).toBe("/docs/resnet/chunks/0002.html");
  });
});

describe("resolve, with a base on another origin", () => {
  test("keeps the origin of an absolute base", () => {
    expect(resolve(CDN, "manifest.json")).toBe("https://cdn.example/docs/resnet/manifest.json");
    expect(resolve("http://localhost:9000/d/", "chunks/0001.html", "abc"))
      .toBe("http://localhost:9000/d/chunks/0001.html?v=abc");
  });

  test("adds the version once", () => {
    const url = resolve(CDN, "pages/0001.webp", "abc");
    expect(url).toBe("https://cdn.example/docs/resnet/pages/0001.webp?v=abc");
    expect(resolve(CDN, "pages/0001.webp?v=old", "abc")).toBe("https://cdn.example/docs/resnet/pages/0001.webp?v=old");
  });

  test("gives a protocol-relative base the scheme of the page, or https", () => {
    const real = globalThis.location;
    try {
      expect(resolve("//cdn.example/docs/resnet/", "manifest.json")).toMatch(/^https?:\/\/cdn\.example\/docs\/resnet\/manifest\.json$/);
      Object.defineProperty(globalThis, "location", { value: { protocol: "http:" }, configurable: true });
      expect(resolve("//cdn.example/d/", "a.json")).toBe("http://cdn.example/d/a.json");
      Object.defineProperty(globalThis, "location", { value: { protocol: "about:" }, configurable: true });
      expect(resolve("//cdn.example/d/", "a.json")).toBe("https://cdn.example/d/a.json");
      Object.defineProperty(globalThis, "location", { value: { protocol: "https:" }, configurable: true });
      expect(resolve("//cdn.example:80/d/", "a.json")).toBe("https://cdn.example:80/d/a.json");
    } finally {
      Object.defineProperty(globalThis, "location", { value: real, configurable: true });
    }
  });

  test("keeps a path that names another host on the base's origin", () => {
    expect(resolve(CDN, "//evil.example/x.json")).toBe("https://cdn.example/x.json");
    expect(resolve(CDN, "https://evil.example/x.json")).toBe("https://cdn.example/x.json");
    expect(resolve("/docs/resnet/", "//evil.example/x.json")).toBe("/x.json");
    expect(resolve("/docs/resnet/", "/.//evil.example/x.json")).toBe("/evil.example/x.json");
    expect(() => resolve("/docs/resnet/", "data:/\\evil.example/x.json")).toThrow();
    expect(() => resolve(CDN, "javascript:alert(1)")).toThrow();
  });

  test("refuses a base that is not a path or an http(s) URL", () => {
    for (const base of ["javascript:alert(1)", "data:text/html,x", "file:///etc/", "ftp://h/d/", " javascript:alert(1)",
      "https:/h/d/", "https:/x/d/", " https:/x/d/", "///x/d/", "http:/x/d/", "/\\x/d/", "/\\evil.example/d/", "\\\\evil.example/d/", "http://"]) {
      expect(() => resolve(base, "manifest.json")).toThrow();
    }
  });
});

describe("pageImageSet, with a base on another origin", () => {
  test("every candidate of the srcset is on the origin, versioned once", () => {
    expect(pageImageSet(CDN, "pages/0001.webp", [400, 800, 1200], "abc")).toEqual({
      src: "https://cdn.example/docs/resnet/pages/0001.webp?v=abc",
      srcSet: "https://cdn.example/docs/resnet/pages/0001.webp?v=abc 400w, "
        + "https://cdn.example/docs/resnet/pages/0001-800.webp?v=abc 800w, "
        + "https://cdn.example/docs/resnet/pages/0001-1200.webp?v=abc 1200w",
    });
  });
});

describe("loadChunkPages, with a base on another origin", () => {
  const chunk = `<div class="chunk-content"><div class="pg" data-page="1">
    <img src="figures/p0001-1.webp"><a href="0002.html#p2">next</a><a href="#top">top</a>
    <a href="/site/page">site</a><img src="//other.example/x.webp"><a href="https://ext.example/">ext</a>
  </div></div>`;

  test("rewrites relative figures and links onto the origin and leaves the rest", async () => {
    const urls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response(chunk);
    }) as typeof fetch;
    const html = (await loadChunkPages(CDN, "chunks/0001.html", undefined, "abc")).get(1) ?? "";
    expect(urls).toEqual(["https://cdn.example/docs/resnet/chunks/0001.html?v=abc"]);
    expect(html).toContain('src="https://cdn.example/docs/resnet/figures/p0001-1.webp?v=abc"');
    expect(html).toContain('href="#p2"');
    expect(html).toContain('href="#top"');
    expect(html).toContain('href="https://ext.example/"');
  });

  test("drops a link to a path of the site and an image on another host, which the renderer never writes", async () => {
    globalThis.fetch = (async () => new Response(chunk)) as unknown as typeof fetch;
    const html = (await loadChunkPages(CDN, "chunks/0001.html", undefined, "abc")).get(1) ?? "";
    expect(html).toContain("<a>site</a>");
    expect(html).not.toContain("/site/page");
    expect(html).not.toContain("other.example");
  });
});
