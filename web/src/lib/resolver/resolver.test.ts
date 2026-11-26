import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdir, utimes, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { removeDir, tempDir } from "@/lib/builds/testkit";
import { NO_FRAMING, frameAncestor } from "./embed";
import { fileStore, parseIndex } from "./index";
import { manifestIsReady, resolveRequest } from "./resolve";
import { normalizeSourceUrl } from "./url";

// The resolver outside Next: a real index file and real manifests in a temp folder, and the
// handler called with a Request, as the route calls it.

const W9 = "https://example.org/files/w9.pdf";
const ORIGIN = "https://example.org";
let dir: string;
let docs: string;
let indexFile: string;

/** A manifest.json as reflow writes it (schema/manifest.schema.json). */
const manifest = (pages: number) => JSON.stringify({
  v: "abc123", title: "T", lang: "en", dir: "ltr", pageCount: pages, pageSize: [612, 792], labelOffset: 0,
  imageWidths: [800], pages: {}, chunks: [{ href: "chunks/0001.html", first: 1, last: Math.max(pages, 1) }],
  toc: [], textBase: 1.1, text: Array.from({ length: pages }, () => [0, 0, 0]), tocComplete: true,
});

const INDEX = {
  sites: {
    acme: { origins: [ORIGIN], documents: { [W9]: "fw9", "https://example.org/files/resnet.pdf": "resnet" } },
    other: { origins: ["https://other.test"], documents: { "https://other.test/p17.pdf": "p17" } },
  },
};

beforeAll(async () => {
  dir = await tempDir();
  docs = join(dir, "public", "docs");
  indexFile = join(dir, "resolver.json");
  await mkdir(join(docs, "fw9"), { recursive: true }); // converted
  await writeFile(join(docs, "fw9", "manifest.json"), manifest(6));
  await mkdir(join(docs, "p17"), { recursive: true }); // converted, belongs to another site
  await writeFile(join(docs, "p17", "manifest.json"), manifest(3));
  // resnet is registered and has no manifest: not converted.
  await writeFile(indexFile, JSON.stringify(INDEX));
});
afterAll(() => removeDir(dir));

const deps = () => ({ store: fileStore(indexFile, () => {}), isReady: (id: string) => manifestIsReady(id, docs) });
const ask = (query: Record<string, string>, headers: Record<string, string> = {}) =>
  resolveRequest(new Request(`https://reflow.test/v1/resolve?${new URLSearchParams(query)}`, { headers }), deps());
const body = async (res: Response) => (await res.json()) as Record<string, unknown>;

describe("normalizeSourceUrl", () => {
  test.each([
    ["HTTPS://Example.ORG:443/a/../files/w9.pdf#page=2", "https://example.org/files/w9.pdf"],
    ["http://example.org:80/x.pdf", "http://example.org/x.pdf"],
    ["https://example.org/download?id=3", "https://example.org/download?id=3"],
    ["  https://example.org/x.pdf ", "https://example.org/x.pdf"],
  ])("%s", (raw, normal) => expect(normalizeSourceUrl(raw)).toBe(normal));

  test.each([
    "/files/w9.pdf", "javascript:alert(1)", "data:application/pdf;base64,AA==", "ftp://example.org/x.pdf",
    "https://user:pw@example.org/x.pdf", "", "https://" + "a".repeat(2100) + ".org/x.pdf",
  ])("refuses %s", (raw) => expect(normalizeSourceUrl(raw)).toBeNull());

  test("keeps a different query a different document", () => {
    expect(normalizeSourceUrl("https://e.org/d?id=3")).not.toBe(normalizeSourceUrl("https://e.org/d?id=4"));
  });
});

describe("parseIndex", () => {
  test("drops what is not right and says so", () => {
    const { sites, problems } = parseIndex({
      sites: {
        ok: {
          origins: ["https://a.test", "https://a.test/path", "*", "null", "ftp://a.test"],
          documents: { "https://a.test/x.pdf#frag": "fw9", "https://a.test/y.pdf": "../etc", "https://a.test/z.pdf": "fw9~docling", "nope": "fw9" },
        },
        "bad key!": {},
      },
    });
    expect([...sites.keys()]).toEqual(["ok"]);
    expect(sites.get("ok")!.origins).toEqual(["https://a.test"]);
    expect([...sites.get("ok")!.documents]).toEqual([["https://a.test/x.pdf", "fw9"]]);
    expect(problems).toHaveLength(8);
  });
  test("an index that is not an object has no sites", () => {
    expect(parseIndex([]).sites.size).toBe(0);
    expect(parseIndex({ sites: { __proto__: 1 } }).sites.size).toBe(0);
  });
});

describe("GET /v1/resolve", () => {
  test("a converted document of the site is ready, with the absolute URL of its embeddable viewer for the site", async () => {
    const res = await ask({ site: "acme", url: W9 });
    expect(res.status).toBe(200);
    expect(await body(res)).toEqual({ status: "ready", viewer: "https://reflow.test/embed/fw9?site=acme" });
  });

  test("the link is matched by its normal form", async () => {
    const res = await ask({ site: "acme", url: "HTTPS://EXAMPLE.org:443/files/w9.pdf#page=3" });
    expect((await body(res)).status).toBe("ready");
  });

  test("a document with no published manifest is unknown", async () => {
    expect(await body(await ask({ site: "acme", url: "https://example.org/files/resnet.pdf" }))).toEqual({ status: "unknown" });
  });

  test("a manifest the viewer cannot read is unknown", async () => {
    await mkdir(join(docs, "resnet"), { recursive: true });
    await writeFile(join(docs, "resnet", "manifest.json"), "{\"pageCount\": 3}");
    expect(await body(await ask({ site: "acme", url: "https://example.org/files/resnet.pdf" }))).toEqual({ status: "unknown" });
  });

  test("another site's document, a URL the site does not list, and an unknown site all answer alike", async () => {
    const answers = [
      await ask({ site: "acme", url: "https://other.test/p17.pdf" }),
      await ask({ site: "acme", url: "https://example.org/other.pdf" }),
      await ask({ site: "nobody", url: W9 }),
    ];
    for (const res of answers) {
      expect(res.status).toBe(200);
      expect(await body(res)).toEqual({ status: "unknown" });
      expect(res.headers.get("access-control-allow-origin")).toBeNull();
    }
  });

  test("CORS: the site's own origin is echoed; any other origin gets unknown and no CORS header", async () => {
    const own = await ask({ site: "acme", url: W9 }, { Origin: ORIGIN });
    expect(own.headers.get("access-control-allow-origin")).toBe(ORIGIN);
    expect(own.headers.get("vary")).toBe("Origin, Host");
    expect((await body(own)).status).toBe("ready");
    for (const origin of ["https://evil.test", "https://other.test", "null"]) {
      const res = await ask({ site: "acme", url: W9 }, { Origin: origin });
      expect(await body(res)).toEqual({ status: "unknown" });
      expect(res.headers.get("access-control-allow-origin")).toBeNull();
    }
  });

  test("CORS: the site's own origin can read an unknown answer too", async () => {
    for (const url of ["https://example.org/other.pdf", "https://example.org/files/resnet.pdf"]) {
      const res = await ask({ site: "acme", url }, { Origin: ORIGIN });
      expect(await body(res)).toEqual({ status: "unknown" });
      expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
      expect(res.headers.get("vary")).toBe("Origin, Host");
    }
  });

  test("answers are cached for a minute, shared; a bad request is not cached", async () => {
    expect((await ask({ site: "acme", url: W9 })).headers.get("cache-control")).toBe("public, max-age=60");
    const res = await ask({ site: "acme" });
    expect(res.status).toBe(400);
    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  test.each([
    [{}], [{ site: "acme" }], [{ url: W9 }], [{ site: "a b", url: W9 }], [{ site: "acme", url: "/files/w9.pdf" }],
    [{ site: "acme", url: "javascript:alert(1)" }], [{ site: "acme", url: "x".repeat(3000) }],
  ])("%j is a 400", async (query) => {
    expect((await ask(query)).status).toBe(400);
  });

  test("a repeated parameter is a 400", async () => {
    const res = await resolveRequest(new Request(`https://reflow.test/v1/resolve?site=acme&site=other&url=${encodeURIComponent(W9)}`), deps());
    expect(res.status).toBe(400);
  });
});

describe("fileStore", () => {
  test("no file has no sites; an edit is live; a broken file has none", async () => {
    const file = join(dir, "live.json");
    const store = fileStore(file, () => {});
    expect(await store.site("acme")).toBeNull();
    await writeFile(file, JSON.stringify(INDEX));
    expect((await store.site("acme"))?.documents.get(W9)).toBe("fw9");
    await writeFile(file, JSON.stringify({ sites: { acme: { origins: [], documents: {} } } }));
    await utimes(file, new Date(), new Date(Date.now() + 5000));
    expect((await store.site("acme"))?.documents.size).toBe(0);
    await writeFile(file, "{not json");
    await utimes(file, new Date(), new Date(Date.now() + 10000));
    expect(await store.site("acme")).toBeNull();
  });
});

describe("frameAncestor: who may frame /embed/<id>", () => {
  const INSTANCE = "0123456789abcdef0123456789abcdef";
  const ancestor = (id: string, query: Record<string, string> | string) =>
    frameAncestor(fileStore(indexFile, () => {}), id, new URLSearchParams(query));

  test("the parent origin, when the site lists it and registered the document", async () => {
    expect(await ancestor("fw9", { site: "acme", parent: ORIGIN, instance: INSTANCE })).toBe(ORIGIN);
  });

  test("nobody, for every other query", async () => {
    const cases: [string, Record<string, string> | string][] = [
      ["fw9", {}], // the shareable page
      ["fw9", { site: "acme", parent: ORIGIN }], // no instance
      ["fw9", { site: "acme", parent: ORIGIN, instance: "short" }],
      ["fw9", { site: "acme", instance: INSTANCE }], // no parent
      ["fw9", { site: "acme", parent: "https://evil.test", instance: INSTANCE }], // an origin the site does not list
      ["fw9", { site: "acme", parent: "https://other.test", instance: INSTANCE }], // another site's origin
      ["fw9", { site: "acme", parent: `${ORIGIN}/`, instance: INSTANCE }], // not an exact origin
      ["fw9", { site: "acme", parent: "*", instance: INSTANCE }],
      ["fw9", { site: "acme", parent: "null", instance: INSTANCE }],
      ["p17", { site: "acme", parent: ORIGIN, instance: INSTANCE }], // a document of another site
      ["fw9", { site: "nobody", parent: ORIGIN, instance: INSTANCE }],
      ["fw9", { site: "bad key!", parent: ORIGIN, instance: INSTANCE }],
      ["fw9", `site=acme&parent=${encodeURIComponent(ORIGIN)}&parent=https%3A%2F%2Fevil.test&instance=${INSTANCE}`], // twice
      ["fw9", `site=acme&site=other&parent=${encodeURIComponent(ORIGIN)}&instance=${INSTANCE}`],
    ];
    for (const [id, query] of cases) expect(await ancestor(id, query)).toBe(NO_FRAMING);
  });
});
