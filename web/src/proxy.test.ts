import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { removeDir, tempDir } from "@/lib/builds/testkit";
import { NextRequest, type NextResponse } from "next/server";
import { SAMPLE_DOCS } from "@/docs";
import { lookupPath, proxy } from "./proxy";

const SAVED = { ...process.env };
afterEach(() => {
  process.env = { ...SAVED };
});
/** The proxy's answer for a path it answers at once (every path but /embed/<id>). */
const call = (path: string): NextResponse => {
  const r = proxy(new NextRequest(`http://localhost:3447${path}`));
  if (r instanceof Promise) throw new Error(`${path} was answered later`);
  return r;
};
const rewritten = (path: string) => call(path).headers.get("x-middleware-rewrite") === "http://localhost:3447/not-found-by-proxy";
const passes = (path: string) => call(path).headers.get("x-middleware-next") === "1";

describe("the /builds gate", () => {
  test("off: the request is rewritten to a path that does not exist, so it is a 404", () => {
    delete process.env.REFLOW_BUILDS;
    expect(rewritten("/builds")).toBe(true);
    expect(rewritten("/builds/x")).toBe(true);
  });

  test("on with REFLOW_BUILDS=1 or in development: the request goes on", () => {
    process.env.REFLOW_BUILDS = "1";
    expect(passes("/builds")).toBe(true);
    delete process.env.REFLOW_BUILDS;
    (process.env as Record<string, string>).NODE_ENV = "development";
    expect(passes("/builds")).toBe(true);
  });
});

describe("the loader script, /embed/loader.js", () => {
  test("is a static file, so it passes through and is not taken for a document id", () => {
    expect(passes("/embed/loader.js")).toBe(true);
  });
});

describe("document pages", () => {
  test("every sample slug passes on all three routes, with or without a trailing slash", () => {
    for (const { slug } of SAMPLE_DOCS) {
      for (const route of ["docs", "view", "inspect"]) {
        expect(passes(`/${route}/${slug}`)).toBe(true);
      }
    }
    expect(passes("/docs/fw9/")).toBe(true);
  });

  test("an unknown slug is a 404 on all three routes", () => {
    for (const route of ["docs", "view", "inspect"]) {
      for (const slug of ["nope", "NOPE", "fw9.json", "fw9%20", "%E0%A4%A", "fw9%2Fx"]) {
        expect(rewritten(`/${route}/${slug}`)).toBe(true);
      }
      // A dot segment is resolved by the URL itself: /docs/.. is the home page, which exists.
      for (const slug of ["..", "%2e%2e"]) expect(passes(`/${route}/${slug}`)).toBe(true);
    }
  });

  test("an encoded known slug is still known", () => {
    expect(passes("/docs/%66w9")).toBe(true);
  });

  test("a known page passes as it came, whatever the gate: nothing is added to the request", () => {
    for (const env of ["1", undefined]) {
      if (env) process.env.REFLOW_BUILDS = env;
      else delete process.env.REFLOW_BUILDS;
      const r = call("/docs/fw9");
      expect(r.headers.get("x-middleware-next")).toBe("1");
      expect(r.headers.get("x-middleware-override-headers")).toBeNull();
      expect([...r.headers.keys()].filter((k) => k.startsWith("x-middleware-request-"))).toEqual([]);
    }
  });
});

describe("a document's published files", () => {
  test("pass at their own names, at both paths", () => {
    for (const path of ["/docs/fw9/index.html", "/docs/fw9/chunks/0001.html", "/docs/fw9/manifest.json", "/docs/fw9/compare",
      "/api/files/docs/fw9/index.html"]) expect(passes(path)).toBe(true);
    // The URL parser resolves a dot segment, "%2e%2e" too, before Next or its header rules see the path.
    for (const path of ["/x/../docs/fw9/index.html", "/x/%2e%2e/docs/fw9/index.html", "/docs/fw9/./index.html"]) expect(passes(path)).toBe(true);
  });

  test("an escaped name is a 404: it would be served decoded but miss the sandbox CSP", () => {
    for (const path of ["/docs/fw9/index.%68tml", "/docs/fw9/chunks/0001.%68tml", "/docs/fw9/figures/a.%73vg",
      "/api/files/docs/fw9/index.%68tml", "/docs/fw9/%69ndex.html"]) expect(rewritten(path)).toBe(true);
  });

  test("so is an escaped or doubled prefix or separator, which Next decodes before it looks the file up", () => {
    for (const path of ["/%64ocs/fw9/index.%68tml", "/%64ocs/fw9/index.html", "/docs%2ffw9/index.html", "/docs%2Ffw9/index.html",
      "/docs/fw9%2findex.html", "/api%2ffiles/docs/fw9/index.html", "/%61pi/files/docs/fw9/index.html", "//docs/fw9/index.html",
      "/docs//fw9/index.html", "/docs/fw9//index.html", "/docs/fw9/%E0%A4%A.html"]) expect(rewritten(path)).toBe(true);
  });

  test("so is a dot segment, a route of Next's own, a backslash, another case or a double escape on the way", () => {
    for (const path of ["/foo%2f..%2fdocs/fw9/index.%68tml", "/foo%2F..%2Fdocs/fw9/index.html",
      "/_next/static/..%2f..%2fdocs/fw9/index.%68tml", "/_next/image%2f..%2f..%2fdocs/fw9/index.%68tml",
      "/docs%5cfw9%5cindex.html", "/DOCS/fw9/index.%68tml", "/DOCS/fw9/index.html", "/Docs/fw9/index.html",
      "/API/files/docs/fw9/index.html", "/%2564ocs/fw9/index.html"]) expect(rewritten(path)).toBe(true);
  });

  test("lookupPath decodes, then resolves separators and dot segments", () => {
    expect(lookupPath("/foo%2f..%2fdocs/fw9/index.%68tml")).toBe("/docs/fw9/index.html");
    expect(lookupPath("/_next/static/..%2f..%2fdocs/x")).toBe("/docs/x");
    expect(lookupPath("/a%5cb//c/./d/")).toBe("/a/b/c/d/");
    expect(lookupPath("/%2564ocs")).toBe("/docs");
    expect(lookupPath("/%E0%A4%A")).toBe("/%E0%A4%A");
    expect(lookupPath("/")).toBe("/");
  });

  test("other paths with an escape are not refused for it", () => {
    for (const path of ["/docs/%66w9", "/view/fw9%7Ehaiku", "/", "/api/source/fw9/1", "/api/ask/fw9%7Ehaiku", "/_next/static/chunks/a.js",
      "/DOCS/fw9", "/docs/fw9/", "/favicon.ico"]) expect(passes(path)).toBe(true);
  });
});

describe("variant pages", () => {
  test("the viewer and the inspector take a variant's key; the overview is one per document", () => {
    expect(passes("/view/fw9~haiku")).toBe(true);
    expect(passes("/inspect/fw9~ollama-gemma4_e4b")).toBe(true);
    expect(passes("/view/fw9%7Ehaiku")).toBe(true);
    expect(rewritten("/docs/fw9~haiku")).toBe(true);
    expect(rewritten("/view/fw9~nope")).toBe(true);
    expect(rewritten("/view/fw9~default")).toBe(true);
    expect(rewritten("/inspect/nope~haiku")).toBe(true);
  });

  test("the compare screen is one per document", () => {
    expect(passes("/docs/fw9/compare")).toBe(true);
    expect(rewritten("/docs/nope/compare")).toBe(true);
    expect(rewritten("/docs/fw9~haiku/compare")).toBe(true);
  });
});

describe("the embeddable viewer, /embed/<id>", () => {
  const PARENT = "https://example.org";
  const INSTANCE = "0123456789abcdef0123456789abcdef";
  let dataRoot: string;
  beforeAll(async () => {
    dataRoot = await tempDir();
    await writeFile(join(dataRoot, "resolver.json"), JSON.stringify({
      sites: { acme: { origins: [PARENT], documents: { [`${PARENT}/w9.pdf`]: "fw9" } } },
    }));
  });
  afterAll(() => removeDir(dataRoot));

  const embed = async (path: string) => {
    process.env.REFLOW_DATA_ROOT = dataRoot;
    const r = proxy(new NextRequest(`http://localhost:3447${path}`));
    if (!(r instanceof Promise)) throw new Error("an /embed answer reads the index");
    return r;
  };
  const query = (q: Record<string, string>) => `?${new URLSearchParams(q)}`;

  test("framed for a parent the site lists: frame-ancestors is that one origin, and no X-Frame-Options", async () => {
    const r = await embed(`/embed/fw9${query({ site: "acme", parent: PARENT, instance: INSTANCE })}`);
    expect(r.headers.get("x-middleware-next")).toBe("1");
    const csp = r.headers.get("content-security-policy")!;
    expect(csp).toEndWith(`; frame-ancestors ${PARENT}`);
    expect(csp).toContain("default-src 'self'");
    expect(r.headers.get("x-frame-options")).toBeNull();
    expect(r.headers.get("x-content-type-options")).toBe("nosniff");
    expect(r.headers.get("cache-control")).toBe("no-store");
  });

  test("for any other parent, or none, nobody may frame it", async () => {
    const others: Record<string, string>[] = [
      {}, { site: "acme" }, { site: "acme", parent: "https://evil.test", instance: INSTANCE },
      { site: "nobody", parent: PARENT, instance: INSTANCE }, { site: "acme", parent: PARENT },
    ];
    for (const q of others) {
      const r = await embed(`/embed/fw9${query(q)}`);
      expect(r.headers.get("x-middleware-next")).toBe("1"); // the shareable page still opens
      expect(r.headers.get("content-security-policy")).toEndWith("; frame-ancestors 'none'");
    }
    // A registered document of the catalogue that this site did not register.
    const other = await embed(`/embed/resnet${query({ site: "acme", parent: PARENT, instance: INSTANCE })}`);
    expect(other.headers.get("content-security-policy")).toEndWith("; frame-ancestors 'none'");
  });

  test("the demo site (REFLOW_DEMO_ORIGIN) may frame its documents from its origin and no other", async () => {
    const DEMO = "http://127.0.0.1:3499";
    process.env.REFLOW_DEMO_ORIGIN = DEMO;
    const ok = await embed(`/embed/fw9${query({ site: "demo", parent: DEMO, instance: INSTANCE })}`);
    expect(ok.headers.get("content-security-policy")).toEndWith(`; frame-ancestors ${DEMO}`);
    for (const q of [
      { site: "demo", parent: "http://localhost:3499", instance: INSTANCE },
      { site: "demo", parent: PARENT, instance: INSTANCE },
    ]) {
      const r = await embed(`/embed/fw9${query(q)}`);
      expect(r.headers.get("content-security-policy")).toEndWith("; frame-ancestors 'none'");
    }
    // A catalogue document the demo did not register.
    const other = await embed(`/embed/resnet${query({ site: "demo", parent: DEMO, instance: INSTANCE })}`);
    expect(other.headers.get("content-security-policy")).toEndWith("; frame-ancestors 'none'");
    delete process.env.REFLOW_DEMO_ORIGIN;
    const off = await embed(`/embed/fw9${query({ site: "demo", parent: DEMO, instance: INSTANCE })}`);
    expect(off.headers.get("content-security-policy")).toEndWith("; frame-ancestors 'none'");
  });

  test("only a catalogue document is an id: a variant, a path or an unknown name is a 404 no one may frame", async () => {
    for (const id of ["nope", "fw9~haiku", "fw9%2F..%2Fx", "%E0%A4%A", "..%2Fview%2Ffw9"]) {
      const r = await embed(`/embed/${id}${query({ site: "acme", parent: PARENT, instance: INSTANCE })}`);
      expect(r.headers.get("x-middleware-rewrite")).toBe("http://localhost:3447/not-found-by-proxy");
      expect(r.headers.get("content-security-policy")).toEndWith("; frame-ancestors 'none'");
    }
  });
});
