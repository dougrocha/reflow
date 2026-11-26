import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { sampleDoc } from "@/docs";
import { removeDir, tempDir } from "@/lib/builds/testkit";
import { DEMO_DOCUMENTS, DEMO_SITE_KEY, DEMO_UNREGISTERED, demoSite } from "./demo";
import { NO_FRAMING, frameAncestor } from "./embed";
import { fileStore, resolverStore, withDemo } from "./index";
import { publicOrigin } from "./origin";
import { manifestIsReady, resolveRequest } from "./resolve";

// The built-in demo site, and the public origin the resolver names in its answers.

const DEMO = "http://127.0.0.1:3401";
const INSTANCE = "0123456789abcdef0123456789abcdef";
let dir: string;
let docs: string;

const manifest = (pages: number) => JSON.stringify({
  v: "abc123", title: "T", lang: "en", dir: "ltr", pageCount: pages, pageSize: [612, 792], labelOffset: 0,
  imageWidths: [800], pages: {}, chunks: [{ href: "chunks/0001.html", first: 1, last: Math.max(pages, 1) }],
  toc: [], textBase: 1.1, text: Array.from({ length: pages }, () => [0, 0, 0]), tocComplete: true,
});

beforeAll(async () => {
  dir = await tempDir();
  docs = join(dir, "public", "docs");
  for (const id of ["fw9", "az-accident-insurance-flyer", "resnet"]) {
    await mkdir(join(docs, id), { recursive: true });
    await writeFile(join(docs, id, "manifest.json"), manifest(2));
  }
});
afterAll(() => removeDir(dir));

describe("the demo site", () => {
  test("its record: key, the one origin, and the documents of its page on that origin", () => {
    const site = demoSite(DEMO)!;
    expect(site.key).toBe(DEMO_SITE_KEY);
    expect(site.origins).toEqual([DEMO]);
    expect([...site.documents]).toEqual([
      [`${DEMO}/demo-site/files/fw9.pdf`, "fw9"],
      [`${DEMO}/demo-site/files/accident-insurance-flyer.pdf`, "az-accident-insurance-flyer"],
    ]);
  });

  test("every document it registers is a catalogue document, and the unregistered one is not registered", () => {
    for (const id of [...Object.values(DEMO_DOCUMENTS), ...Object.values(DEMO_UNREGISTERED)]) expect(sampleDoc(id)).toBeDefined();
    for (const path of Object.keys(DEMO_UNREGISTERED)) expect(DEMO_DOCUMENTS).not.toHaveProperty(path);
  });

  test.each([undefined, "", "demo", "localhost:3401", "http://127.0.0.1:3401/", "http://127.0.0.1:3401/x", "*", "null", "ftp://x.test", "http://*.test"])(
    "%j is not an exact origin: no demo site", (origin) => expect(demoSite(origin)).toBeNull());
});

describe("resolverStore", () => {
  const env = (extra: Record<string, string>) => ({ REFLOW_DATA_ROOT: dir, ...extra });

  test("the demo site is there when REFLOW_DEMO_ORIGIN is an exact origin", async () => {
    expect((await resolverStore(env({ REFLOW_DEMO_ORIGIN: DEMO })).site("demo"))?.origins).toEqual([DEMO]);
  });

  test("unset, empty or not an origin, `demo` is an unknown site", async () => {
    for (const value of [undefined, "", "demo", `${DEMO}/`, "*"]) {
      expect(await resolverStore(env(value === undefined ? {} : { REFLOW_DEMO_ORIGIN: value })).site("demo")).toBeNull();
    }
  });

  test("no other key is the demo site", async () => {
    const store = resolverStore(env({ REFLOW_DEMO_ORIGIN: DEMO }));
    for (const key of ["Demo", "demo2", "acme", ""]) expect(await store.site(key)).toBeNull();
  });

  test("a site `demo` in the operator's file wins, whole", async () => {
    const own = await tempDir();
    try {
      await writeFile(join(own, "resolver.json"), JSON.stringify({
        sites: { demo: { origins: ["https://mine.test"], documents: { "https://mine.test/x.pdf": "resnet" } } },
      }));
      const site = await resolverStore({ REFLOW_DATA_ROOT: own, REFLOW_DEMO_ORIGIN: DEMO }).site("demo");
      expect(site?.origins).toEqual(["https://mine.test"]);
      expect([...site!.documents.values()]).toEqual(["resnet"]);
    } finally {
      await removeDir(own);
    }
  });

  test("withDemo leaves the file's other sites alone", async () => {
    const files = fileStore(join(dir, "none.json"), () => {});
    expect(await withDemo(files, DEMO).site("acme")).toBeNull();
  });
});

describe("the demo site through the resolver and the framing rule", () => {
  const store = () => resolverStore({ REFLOW_DATA_ROOT: dir, REFLOW_DEMO_ORIGIN: DEMO });
  const ask = (url: string, headers: Record<string, string> = {}, base = "http://127.0.0.1:3200") =>
    resolveRequest(new Request(`${base}/v1/resolve?${new URLSearchParams({ site: "demo", url })}`, { headers }), {
      store: store(), isReady: (id) => manifestIsReady(id, docs),
    });

  test("a registered link is ready, and its CORS header is the demo origin", async () => {
    const res = await ask(`${DEMO}/demo-site/files/fw9.pdf`, { Origin: DEMO });
    expect(await res.json()).toEqual({ status: "ready", viewer: "http://127.0.0.1:3200/embed/fw9?site=demo" });
    expect(res.headers.get("access-control-allow-origin")).toBe(DEMO);
  });

  test("another origin is told nothing, and the unregistered link is unknown", async () => {
    for (const origin of ["http://localhost:3401", "https://evil.test", "null"]) {
      const res = await ask(`${DEMO}/demo-site/files/fw9.pdf`, { Origin: origin });
      expect(await res.json()).toEqual({ status: "unknown" });
      expect(res.headers.get("access-control-allow-origin")).toBeNull();
    }
    const res = await ask(`${DEMO}${Object.keys(DEMO_UNREGISTERED)[0]}`, { Origin: DEMO });
    expect(await res.json()).toEqual({ status: "unknown" });
  });

  test("the viewer's frame-ancestors is granted to the demo origin, for demo documents only", async () => {
    const ancestor = (id: string, parent: string) => frameAncestor(store(), id, new URLSearchParams({ site: "demo", parent, instance: INSTANCE }));
    for (const id of Object.values(DEMO_DOCUMENTS)) expect(await ancestor(id, DEMO)).toBe(DEMO);
    for (const id of [...Object.values(DEMO_UNREGISTERED), "az-audit-compliance-and-risk-committee-charter", "nope"]) {
      expect(await ancestor(id, DEMO)).toBe(NO_FRAMING);
    }
    for (const parent of ["http://localhost:3401", "http://127.0.0.1:3402", "https://127.0.0.1:3401", "https://evil.test"]) {
      expect(await ancestor("fw9", parent)).toBe(NO_FRAMING);
    }
  });

  test("with the demo off, nothing is framed", async () => {
    const off = resolverStore({ REFLOW_DATA_ROOT: dir });
    expect(await frameAncestor(off, "fw9", new URLSearchParams({ site: "demo", parent: DEMO, instance: INSTANCE }))).toBe(NO_FRAMING);
  });
});

describe("publicOrigin", () => {
  const req = (url: string, headers: Record<string, string> = {}) => new Request(url, { headers });

  test("the origin the request was made to: Host wins over the name the server believes it has", () => {
    // Next gives req.url the host it listens as, `localhost`, while the client reached 127.0.0.1.
    expect(publicOrigin(req("http://localhost:3200/v1/resolve", { host: "127.0.0.1:3200" }), {})).toBe("http://127.0.0.1:3200");
    expect(publicOrigin(req("https://a.test/x", { host: "b.test" }), {})).toBe("https://b.test");
  });

  test("the scheme is the request's, a default port and upper case are normalised", () => {
    expect(publicOrigin(req("https://a.test/x", { host: "B.test:443" }), {})).toBe("https://b.test");
    expect(publicOrigin(req("http://a.test/x", { host: "[::1]:3000" }), {})).toBe("http://[::1]:3000");
  });

  test("with no Host header, the request's own origin", () => {
    expect(publicOrigin(req("https://a.test:8443/x"), {})).toBe("https://a.test:8443");
  });

  test.each(["", "a.test/x", "a.test?x", "a.test#x", "u:p@a.test", "a b.test", "a.test:99999x", "a.test:", "*.test", "a.test, evil.test", "-.test"])(
    "a Host that is not a plain host and port (%j) is ignored", (host) => {
      expect(publicOrigin(req("https://a.test/x", { host }), {})).toBe("https://a.test");
    });

  test("REFLOW_PUBLIC_ORIGIN, when it is an exact origin, wins over everything the request says", () => {
    const r = req("http://localhost:3200/x", { host: "evil.test", "x-forwarded-host": "evil.test", "x-forwarded-proto": "http" });
    expect(publicOrigin(r, { REFLOW_PUBLIC_ORIGIN: "https://reflow.example" })).toBe("https://reflow.example");
  });

  test("a REFLOW_PUBLIC_ORIGIN that is not an exact origin is ignored", () => {
    for (const value of ["reflow.example", "https://reflow.example/", "*", ""]) {
      expect(publicOrigin(req("http://localhost:3200/x", { host: "127.0.0.1:3200" }), { REFLOW_PUBLIC_ORIGIN: value })).toBe("http://127.0.0.1:3200");
    }
  });

  test("forwarding headers are never read", () => {
    const r = req("http://a.test/x", { host: "a.test", "x-forwarded-host": "evil.test", forwarded: "host=evil.test" });
    expect(publicOrigin(r, {})).toBe("http://a.test");
  });

  test("the resolver's viewer URL is on the public origin", async () => {
    const store = withDemo(fileStore(join(dir, "none.json"), () => {}), DEMO);
    const res = await resolveRequest(
      new Request(`http://localhost:3200/v1/resolve?${new URLSearchParams({ site: "demo", url: `${DEMO}/demo-site/files/fw9.pdf` })}`, { headers: { host: "127.0.0.1:3200" } }),
      { store, isReady: (id) => manifestIsReady(id, docs) },
    );
    expect(await res.json()).toEqual({ status: "ready", viewer: "http://127.0.0.1:3200/embed/fw9?site=demo" });
    const behind = await resolveRequest(
      new Request(`http://localhost:3200/v1/resolve?${new URLSearchParams({ site: "demo", url: `${DEMO}/demo-site/files/fw9.pdf` })}`),
      { store, isReady: (id) => manifestIsReady(id, docs), env: { REFLOW_PUBLIC_ORIGIN: "https://reflow.example" } },
    );
    expect(await behind.json()).toEqual({ status: "ready", viewer: "https://reflow.example/embed/fw9?site=demo" });
  });
});
