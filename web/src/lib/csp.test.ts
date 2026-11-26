import { describe, expect, test } from "bun:test";
// Next's own matcher for a rule's `source`, so the test reads the rules as the server does.
import { getPathMatch } from "next/dist/shared/lib/router/utils/path-match";
import nextConfig from "../../next.config";
import { RAW_SANDBOX } from "./csp";

// Which headers next.config.ts gives a path (rules with a `has` or `missing` condition, the caching
// ones, are left out). Every matching rule applies in order, and a later one wins for the same key.
async function headersFor(path: string): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const rule of (await nextConfig.headers!())) {
    if (rule.has || rule.missing) continue;
    if (!getPathMatch(rule.source, { strict: true, removeUnnamedParams: true })(path)) continue;
    for (const { key, value } of rule.headers) out[key] = value;
  }
  return out;
}

describe("next.config.ts's security headers", () => {
  test("the app's pages, the document pages and the compare screen: the app's CSP, framed by no other site", async () => {
    for (const path of ["/", "/about", "/view/fw9", "/inspect/fw9", "/docs/fw9", "/docs/fw9/compare", "/v1/resolve", "/api/builds", "/embed"]) {
      const h = await headersFor(path);
      expect(h["X-Frame-Options"]).toBe("SAMEORIGIN");
      expect(h["Content-Security-Policy"]).toStartWith("default-src 'self'");
      expect(h["Content-Security-Policy"]).not.toContain("sandbox");
    }
  });

  test("/embed/<id> gets none of them: src/proxy.ts sets its headers per request", async () => {
    expect(await headersFor("/embed/fw9")).toEqual({});
    expect(await headersFor("/embed/fw9/")).toEqual({});
    // Any deeper path is not the page: it keeps the app's headers.
    expect((await headersFor("/embed/fw9/x"))["X-Frame-Options"]).toBe("SAMEORIGIN");
  });

  test("a document's own HTML and SVG run sandboxed, at /docs and at the files route; the PDF and the JSON do not", async () => {
    for (const path of ["/docs/fw9/index.html", "/docs/fw9/chunks/0001.html", "/docs/fw9~haiku/index.html", "/docs/fw9/figures/f1.svg",
      "/api/files/docs/fw9/index.html", "/api/files/docs/fw9/chunks/0002.html"]) {
      const h = await headersFor(path);
      expect(h["Content-Security-Policy"]).toEndWith(`; ${RAW_SANDBOX}`);
      expect(h["X-Frame-Options"]).toBe("SAMEORIGIN");
    }
    expect(RAW_SANDBOX).not.toContain("allow-same-origin");
    for (const path of ["/docs/fw9/original.pdf", "/docs/fw9/manifest.json", "/pdfs/fw9.pdf"]) {
      expect((await headersFor(path))["Content-Security-Policy"]).not.toContain("sandbox");
    }
  });

  test("only the chunk pages and toc.json are readable across origins", async () => {
    expect((await headersFor("/docs/fw9/chunks/0001.html"))["Access-Control-Allow-Origin"]).toBe("*");
    expect((await headersFor("/docs/fw9/toc.json"))["Access-Control-Allow-Origin"]).toBe("*");
    for (const path of ["/docs/fw9/index.html", "/docs/fw9/manifest.json", "/docs/fw9/review.json", "/docs/fw9", "/embed/fw9", "/api/builds"]) {
      expect((await headersFor(path))["Access-Control-Allow-Origin"]).toBeUndefined();
    }
  });
});
