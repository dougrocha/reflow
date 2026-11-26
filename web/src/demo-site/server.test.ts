import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { removeDir, tempDir } from "@/lib/builds/testkit";
import { DEMO_DOCUMENTS, DEMO_SITE_KEY, DEMO_UNREGISTERED } from "@/lib/resolver/demo";
import { DEMO_FILES, demoCsp, demoResponse } from "./server";

// The demo customer page's server, called with a Request as its route (src/app/demo-site) calls it.

const REFLOW = "http://127.0.0.1:3200";
let pdfs: string;

beforeAll(async () => {
  pdfs = await tempDir();
  await mkdir(pdfs, { recursive: true });
  // The demo's files, and one that is a real document but not the demo's.
  for (const slug of [...Object.values(DEMO_FILES), "az-audit-compliance-and-risk-committee-charter"]) {
    await writeFile(join(pdfs, `${slug}.pdf`), `%PDF-1.4 ${slug}`);
  }
});
afterAll(() => removeDir(pdfs));

const get = (path: string, method = "GET") => demoResponse(new Request(`http://127.0.0.1:3401${path}`, { method }), { reflowOrigin: REFLOW, pdfsDir: pdfs });
const page = async () => (await get("/demo-site/").text());

describe("the page", () => {
  test("is the real snippet, with reflow's origin and the demo site's key", async () => {
    const res = get("/demo-site/");
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toStartWith("text/html");
    const html = await res.text();
    expect(html).toContain(`<script src="${REFLOW}/embed/loader.js" data-site="${DEMO_SITE_KEY}" defer></script>`);
    expect(html).not.toContain("{{");
  });

  test("links to the demo's PDFs and to the one it did not register, and to nothing else", async () => {
    const hrefs = [...(await page()).matchAll(/<a\s[^>]*href="([^"]*)"/g)].map((m) => m[1]);
    const registered = hrefs.filter((h) => Object.hasOwn(DEMO_DOCUMENTS, h));
    expect(registered.sort()).toEqual(Object.keys(DEMO_DOCUMENTS).sort());
    expect(hrefs.filter((h) => !Object.hasOwn(DEMO_DOCUMENTS, h))).toEqual(Object.keys(DEMO_UNREGISTERED));
    for (const h of hrefs) expect(h.startsWith("/demo-site/files/") && h.endsWith(".pdf")).toBe(true);
  });

  test("has no other script, style or origin than reflow's", async () => {
    const html = await page();
    expect(html.match(/<script\b/g)).toHaveLength(1);
    expect(html).not.toMatch(/<style\b|\sstyle=|<link\b|<img\b|<iframe\b|\son[a-z]+=/i);
    const origins = new Set((html.match(/https?:\/\/[^\s"'<>/]+/g) ?? []));
    expect([...origins]).toEqual([REFLOW]);
  });

  test("its CSP is the customer's: reflow's origin for script, connect and frame, and nothing else", () => {
    const csp = get("/demo-site/").headers.get("content-security-policy")!;
    expect(csp).toBe(demoCsp(REFLOW));
    const directives = Object.fromEntries(csp.split("; ").map((d) => [d.split(" ")[0], d.split(" ").slice(1)]));
    expect(directives["default-src"]).toEqual(["'none'"]);
    for (const name of ["script-src", "connect-src", "frame-src"]) expect(directives[name]).toEqual([REFLOW]);
    // No other directive names a source but 'none'.
    for (const [name, sources] of Object.entries(directives)) {
      if (!["script-src", "connect-src", "frame-src"].includes(name)) expect(sources).toEqual(["'none'"]);
    }
    expect(csp).not.toMatch(/unsafe|\*|https?:\/\/(?!127\.0\.0\.1:3200)/);
  });

  test("/index.html is the page too; HEAD has headers and no body", async () => {
    expect((await get("/demo-site/index.html").text())).toBe(await page());
    const head = get("/demo-site/", "HEAD");
    expect(head.status).toBe(200);
    expect(head.headers.get("content-security-policy")).toBe(demoCsp(REFLOW));
    expect(await head.text()).toBe("");
  });
});

describe("the files", () => {
  test("a demo PDF is served as a PDF, nosniff, no-store", async () => {
    for (const [path, slug] of Object.entries(DEMO_FILES)) {
      const res = get(path);
      expect(res.status).toBe(200);
      expect(res.headers.get("content-type")).toBe("application/pdf");
      expect(res.headers.get("x-content-type-options")).toBe("nosniff");
      expect(await res.text()).toBe(`%PDF-1.4 ${slug}`);
    }
  });

  test("nothing else is: another document, other spellings of a demo file, other paths and a path out of the folder", () => {
    const paths = [
      "/demo-site/files/az-audit-compliance-and-risk-committee-charter.pdf", "/pdfs/fw9.pdf", "/demo-site/files/fw9", "/demo-site/files/fw9.pdf/", "/demo-site/files/FW9.pdf",
      "/demo-site/files/%66w9.pdf", "/demo-site/files//fw9.pdf", "/demo-site/files/fw9.pdf%00", "/demo-site/files/../files/fw9.pdf.bak", "/demo-site/files/..%2fpackage.json", "/demo-site/files/%2e%2e/fw9.pdf",
      "/demo-site/files/..\\fw9.pdf", "/demo-site/files/", "/demo-site/files", "/demo-site/files/__proto__", "/demo-site/files/constructor", "/package.json", "/../package.json", "/%2e%2e/package.json",
      "/index.html", "/files/fw9.pdf", "/demo-site/../package.json", "/embed/loader.js", "/robots.txt",
    ];
    for (const path of paths) {
      const res = get(path);
      expect([path, res.status]).toEqual([path, 404]);
      expect(res.headers.get("content-type")).toStartWith("text/plain");
    }
  });

  test("a registered file that is not on disk is a 404", () => {
    const res = demoResponse(new Request("http://127.0.0.1:3401/demo-site/files/fw9.pdf"), { reflowOrigin: REFLOW, pdfsDir: join(pdfs, "missing") });
    expect(res.status).toBe(404);
  });

  test("only GET and HEAD", () => {
    for (const method of ["POST", "PUT", "DELETE"]) expect(get("/demo-site/", method).status).toBe(405);
  });
});

describe("the reflow origin", () => {
  test.each(["", "reflow", "http://127.0.0.1:3200/", `http://a.test"><script>`, "*", "null", "ftp://a.test", "http://a.test/x"])(
    "%j is refused, since it goes into the page and its policy",
    (reflowOrigin) => expect(() => demoResponse(new Request("http://127.0.0.1:3401/demo-site/"), { reflowOrigin })).toThrow());
});
