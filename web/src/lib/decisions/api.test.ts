import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { GET, POST } from "@/app/api/decisions/[slug]/route";
import { GET as ENABLED } from "@/app/api/builds/enabled/route";
import { listJobs } from "../builds/record";
import { Runner } from "../builds/runner";
import { BuildStore } from "../builds/store";
import { FakeProcs, fakeSpawn } from "../builds/testkit";
import { makeBuilds, setBuildsForTests } from "../builds/system";
import { append, readStore } from "./store";
import { storePath } from "../paths";
import {
  alt, correct, emptyLedger, FINDING, keep, ocrItem, PDF, publications, removeRoot, REV, SUGGESTION_ID, tempRoot,
  textAnchor, writePublished,
} from "./testkit";
import type { Ledger } from "./types";

let dir: string;
let docs: string;
const SAVED = { ...process.env };

beforeEach(async () => {
  dir = await tempRoot();
  docs = join(dir, "docs");
  process.env.REFLOW_BUILDS = "1";
  process.env.REFLOW_DATA_ROOT = join(dir, "data");
  serve(publications({}));
  await writePublished(docs, "fw9");
});
afterEach(async () => {
  setBuildsForTests(undefined);
  process.env = { ...SAVED };
  await removeRoot(dir);
});

/** The server's objects, with `publications` answering as the test says. */
function serve(p: ReturnType<typeof publications>) {
  setBuildsForTests(makeBuilds({
    store: { publications: p } as unknown as BuildStore, runner: {} as Runner, docsRoot: docs,
    slugs: () => ["fw9", "resnet", "p17"], revalidate: async () => {},
  }));
}

const HOST = { host: "localhost:3447", origin: "http://localhost:3447" };
const url = (slug: string) => `http://localhost:3447/api/decisions/${slug}`;
const ctx = (slug: string) => ({ params: Promise.resolve({ slug }) }) as never;
const post = (body: unknown, headers: Record<string, string> = {}, slug = "fw9", raw?: string) =>
  POST(new Request(url(slug), {
    method: "POST", headers: { "content-type": "application/json", ...HOST, ...headers }, body: raw ?? JSON.stringify(body),
  }), ctx(slug));
const get = (headers: Record<string, string> = {}, slug = "fw9") =>
  GET(new Request(url(slug), { headers: { host: HOST.host, ...headers } }), ctx(slug));

const root = () => join(dir, "data");
const storeLines = async (slug = "fw9") => (await readFile(storePath(root(), slug), "utf8")).split("\n").filter(Boolean).map((l) => JSON.parse(l));
/** Whether the request touched the store's folder at all (a request refused before the store is read must not). */
const dataExists = () => readdir(join(root(), ".decisions")).then(() => true, () => false);
/** Whether any store file exists (a request refused after checking the store may take its lock, but writes no store). */
const wrote = () => readdir(join(root(), ".decisions")).then((n) => n.some((f) => f.endsWith(".jsonl")), () => false);

const ITEM = "5:1a2b3c4d5e6f";
const accept = { rev: REV, item: ITEM, choice: "accept", suggestion: SUGGESTION_ID };

describe("the gate", () => {
  test("off: GET and POST are an empty 404 and write nothing; the builds probe still answers", async () => {
    delete process.env.REFLOW_BUILDS;
    for (const r of [await get(), await post(accept)]) {
      expect(r.status).toBe(404);
      expect(await r.text()).toBe("");
      expect(r.headers.get("cache-control")).toBe("no-store");
    }
    expect(await dataExists()).toBe(false);
    const probe = await ENABLED();
    expect(probe.status).toBe(200);
    expect(await probe.json()).toEqual({ enabled: false });
  });
});

describe("the boundary", () => {
  test("POST: no Origin is 403; a matching Origin with no Sec-Fetch-Site passes; cross-site and another host's Origin are 403", async () => {
    expect((await post(accept, { origin: undefined as never })).status).toBe(403);
    const noOrigin = new Request(url("fw9"), { method: "POST", headers: { "content-type": "application/json", host: HOST.host }, body: JSON.stringify(accept) });
    expect((await POST(noOrigin, ctx("fw9"))).status).toBe(403);
    expect((await post(accept, { "sec-fetch-site": "cross-site" })).status).toBe(403);
    expect((await post(accept, { origin: "http://evil.example" })).status).toBe(403);
    expect(await dataExists()).toBe(false);
    expect((await post(accept)).status).toBe(201);
  });

  test("Host: loopback names pass, others are 403 for GET and POST", async () => {
    for (const host of ["localhost", "LOCALHOST:3447", "127.0.0.1:3417", "[::1]:3447"]) {
      expect((await get({ host })).status).toBe(200);
    }
    for (const host of ["localhost.", "localhost.example", "127.0.0.1.nip.io", "user@localhost", "localhost:abc", "localhost:70000", "", "example.com"]) {
      expect((await get({ host })).status).toBe(403);
      expect((await post(accept, { host, origin: `http://${host}` })).status).toBe(403);
    }
    expect((await GET(new Request(url("fw9")), ctx("fw9"))).status).toBe(403);
    expect(await dataExists()).toBe(false);
  });

  // THE DOCUMENTED LIMIT: a client that sends `Host: localhost` and a matching `Origin` is accepted,
  // wherever it connects from (docs/builds.md). Listening on 127.0.0.1 is what keeps the network out.
  test("a forged Host: localhost with a matching Origin is accepted", async () => {
    const r = await post(accept, { host: "localhost", origin: "http://localhost", "x-forwarded-for": "203.0.113.9" });
    expect(r.status).toBe(201);
  });

  test("GET refuses a cross-site request, allows the address bar", async () => {
    expect((await get({ "sec-fetch-site": "cross-site" })).status).toBe(403);
    expect((await get({ "sec-fetch-site": "none" })).status).toBe(200);
    expect((await get({ origin: "http://evil.example" })).status).toBe(403);
  });

  test("a body over 8 KB is 413, a wrong Content-Type is 400; every answer is no-store", async () => {
    const big = await post({ ...accept, now: "x".repeat(9000) });
    expect(big.status).toBe(413);
    const wrong = await post(accept, { "content-type": "text/plain" });
    expect(wrong.status).toBe(400);
    expect((await post(accept, {}, "fw9", "[]")).status).toBe(400);
    const answers = [big, wrong, await get(), await post(accept), await post(accept, { origin: "http://x.example" }), await get({}, "nope")];
    for (const r of answers) expect(r.headers.get("cache-control")).toBe("no-store");
    expect(await dataExists()).toBe(true); // only the one valid accept above wrote
    expect(await storeLines()).toHaveLength(2);
  });

  test("a reviewer's name is refused, not stored", async () => {
    expect((await post({ ...accept, by: "Sam" })).status).toBe(400);
    expect((await post({ ...accept, name: "Sam" })).status).toBe(400);
    expect(await dataExists()).toBe(false);
  });
});

describe("slugs", () => {
  test("an unknown or malformed slug is an empty 404 before any file is touched", async () => {
    for (const slug of ["nope", "../x", "a%2Fb", "A-Upper", "", "fw9/../resnet", "fw9\0"]) {
      const g = await get({}, slug);
      const p = await post(accept, {}, slug);
      expect([g.status, p.status]).toEqual([404, 404]);
      expect(await p.text()).toBe("");
    }
    expect(await dataExists()).toBe(false);
  });
});

describe("a decision starts nothing", () => {
  test("recording one leaves the build record empty: no job, no claim, no publication", async () => {
    const builds = new BuildStore({ dir: join(dir, "builds") });
    setBuildsForTests(makeBuilds({
      store: Object.assign(builds, { publications: publications({}) }), runner: new Runner(builds, { concurrency: 1, spawn: fakeSpawn(new FakeProcs()).spawn }),
      docsRoot: docs, slugs: () => ["fw9", "resnet", "p17"], revalidate: async () => {},
    }));
    expect((await post(accept)).status).toBe(201);
    expect(await listJobs(builds.dir)).toEqual([]);
    expect(await readdir(dir)).not.toContain("builds");
  });
});

describe("binding a choice to the build the reviewer saw", () => {
  test("a valid accept writes one line whose target and pdf are the snapshot's", async () => {
    const r = await post(accept);
    expect(r.status).toBe(201);
    const body = await r.json();
    expect(Object.keys(body).sort()).toEqual(["decision", "pending"]);
    expect(body.pending).toBe(1);
    const [header, rec, ...rest] = await storeLines();
    expect(rest).toEqual([]);
    expect(header).toMatchObject({ kind: "store", slug: "fw9" });
    expect(rec).toEqual({
      v: 1, id: body.decision, at: expect.stringMatching(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/), pdf: PDF, kind: "correct",
      target: { anchor: textAnchor(), range: { was: "8011O", before: "CO ", after: "", rule: "zip-digit" } }, now: "80110",
    });
    expect(body.decision).toMatch(/^[0-9a-z]+-[0-9a-f]{8}$/);
    expect(JSON.stringify(rec)).not.toContain('"by"');
  });

  test("a stale rev, a suggestion that is gone and a missing item are 409 and write nothing", async () => {
    for (const bad of [{ ...accept, rev: "old" }, { ...accept, suggestion: "0000000000000000" }, { ...accept, item: "9:gone" }]) {
      const r = await post(bad);
      expect(r.status).toBe(409);
      expect((await r.json()).reload).toBe(true);
    }
    expect(await wrote()).toBe(false);
  });

  test("the same item id with reordered or changed suggestions: the old suggestion id is 409", async () => {
    await writePublished(docs, "fw9", { items: [ocrItem({ suggestions: [{ ...ocrItem().suggestions[0], id: "1234567890abcdef", now: "80111" }] })] });
    expect((await post(accept)).status).toBe(409);
    expect(await wrote()).toBe(false);
  });

  test("a publication in progress, or a generation that changes between the two reads, is 409", async () => {
    serve(publications({ publishing: "20260101-000000-abcdef" }));
    const a = await post(accept);
    expect(a.status).toBe(409);
    expect(await a.json()).toEqual({ error: "The document is being published. Reload the page.", reload: true });
    serve(publications({ generation: 3 }, { generation: 4 }));
    expect((await post(accept)).status).toBe(409);
    serve(publications({ generation: 3 }, { generation: 4 }));
    expect((await get()).status).toBe(409);
    expect(await wrote()).toBe(false);
  });

  test("a document with no decisions.json is 409 with the rebuild sentence", async () => {
    await writePublished(docs, "fw9", { ledger: null });
    const r = await post(accept);
    expect(r.status).toBe(409);
    expect(await r.json()).toEqual({ error: "Rebuild this document once before recording decisions." });
    expect(await wrote()).toBe(false);
  });

  test("keep, another reading, alt, dismiss and reattach", async () => {
    expect((await post({ ...accept, choice: "keep" })).status).toBe(201);
    expect((await post({ ...accept, choice: "other", now: "8O110" })).status).toBe(201);
    expect((await post({ rev: REV, item: "2:ffeeddccbbaa", choice: "alt", alt: { text: "A map", decorative: false } })).status).toBe(201);
    expect((await post({ rev: REV, item: "3:aabbccddeeff", choice: "dismiss", finding: "5555666677778888" })).status).toBe(201);
    const recs = (await storeLines()).slice(1);
    expect(recs.map((r) => r.kind)).toEqual(["keep", "correct", "alt", "dismiss"]);
    expect(recs[2]).toMatchObject({ alt: { text: "A map", decorative: false }, target: { anchor: { crop_sha: "fedcba9876543210" } } });
  });
});

describe("values", () => {
  test("now: over the limit, a newline, a bidi control, empty", async () => {
    for (const now of ["x".repeat(41), "80\n10", "80\u202e10", ""]) expect((await post({ ...accept, choice: "other", now })).status).toBe(400);
    expect(await wrote()).toBe(false);
  });
  test("alt: over 1,000 characters or with a control character", async () => {
    for (const text of ["a".repeat(1001), "a\u0007b"]) {
      expect((await post({ rev: REV, item: "2:ffeeddccbbaa", choice: "alt", alt: { text, decorative: false } })).status).toBe(400);
    }
    expect(await wrote()).toBe(false);
  });
  test("dismiss of an error finding is 400", async () => {
    expect((await post({ rev: REV, item: "2:ffeeddccbbaa", choice: "dismiss", finding: "1111222233334444" })).status).toBe(400);
    expect(await wrote()).toBe(false);
  });
});

describe("undo", () => {
  test("undo of a decision whose item a rebuild removed writes the record", async () => {
    const id = (await (await post(accept)).json()).decision as string;
    await writePublished(docs, "fw9", { rev: "newbuild00001", items: [] }); // the rebuild removed the item
    const r = await post({ choice: "undo", of: id });
    expect(r.status).toBe(201);
    expect((await storeLines()).at(-1)).toMatchObject({ kind: "undo", of: id, pdf: PDF });
  });

  test("an id from another document's store is 404; undo of an undo and of an undone decision are 409", async () => {
    await append("resnet", correct({ id: "m2k9x4q1-0000000a" }), root());
    expect((await post({ choice: "undo", of: "m2k9x4q1-0000000a" })).status).toBe(404);
    const id = (await (await post(accept)).json()).decision as string;
    const undo = await post({ choice: "undo", of: id });
    const undoId = (await undo.json()).decision as string;
    expect((await post({ choice: "undo", of: undoId })).status).toBe(409);
    expect((await post({ choice: "undo", of: id })).status).toBe(409);
    expect(await storeLines()).toHaveLength(3);
  });

  test("two undos at once: one wins, one is 409", async () => {
    const id = (await (await post(accept)).json()).decision as string;
    const rs = await Promise.all([post({ choice: "undo", of: id }), post({ choice: "undo", of: id })]);
    expect(rs.map((r) => r.status).sort()).toEqual([201, 409]);
  });
});

describe("the store", () => {
  test("a torn tail then a POST: a newline, then the record", async () => {
    await append("fw9", correct(), root());
    await Bun.write(storePath(root(), "fw9"), (await readFile(storePath(root(), "fw9"), "utf8")) + '{"v":1,"id":"m2k9');
    expect((await post(accept)).status).toBe(201);
    const snap = await readStore("fw9", root());
    expect(snap.unreadable).toHaveLength(1);
    expect(snap.entries).toHaveLength(2);
    expect(snap.torn).toBe(0);
  });

  test("20 parallel POSTs give 20 lines that all parse", async () => {
    const rs = await Promise.all(Array.from({ length: 20 }, (_, i) => post({ ...accept, choice: "other", now: `8011${i % 10}` })));
    expect(rs.map((r) => r.status)).toEqual(Array(20).fill(201));
    const snap = await readStore("fw9", root());
    expect(snap.lines).toBe(21);
    expect(snap.unreadable).toHaveLength(0);
    expect(snap.entries.every((e) => e.parsed.type === "record")).toBe(true);
  }, 60_000);
});

describe("GET", () => {
  const ledgerOf = (over: Partial<Ledger>): Ledger => emptyLedger(over);

  test("an empty store: no decisions, nothing pending, the build's source and ledger store", async () => {
    const r = await get();
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({
      slug: "fw9", rev: REV, generation: 3, source: { sha256: PDF }, decisions: [], pending: 0,
      ledger: { store: { id: null, bytes: 0, lines: 0, torn: 0, unreadable: 0, duplicate: 0, invalid: [], unsupported: [], error: null } },
      store: { id: null, bytes: 0, lines: 0, torn: 0, unreadable: 0, duplicate: 0, error: null },
    });
  });

  test("each decision with its status against the published decisions.json", async () => {
    const a = correct({ id: "m2k9x4q1-00000001" });
    const b = keep({ id: "m2k9x4q1-00000002", anchor: textAnchor({ page: 2, text_sha: "1111111111111111" }) });
    const c = alt({ id: "m2k9x4q1-00000003" });
    const d = correct({ id: "m2k9x4q1-00000004", anchor: textAnchor({ page: 3, text_sha: "2222222222222222" }) });
    const e = correct({ id: "m2k9x4q1-00000005", anchor: textAnchor({ page: 4, text_sha: "3333333333333333" }) });
    const f = correct({ id: "m2k9x4q1-00000006", anchor: textAnchor({ page: 6, text_sha: "4444444444444444" }) });
    const g = correct({ id: "m2k9x4q1-00000007", anchor: textAnchor({ page: 7, text_sha: "5555555555555555" }) });
    const dis = { kind: "dismiss", id: "m2k9x4q1-00000008", at: "x", pdf: PDF, findings: [FINDING] } as const;
    const dis2 = { kind: "dismiss", id: "m2k9x4q1-00000009", at: "x", pdf: PDF, findings: ["abababababababab"] } as const;
    const first = [a, b, c, d, e, f, dis, dis2];
    for (const r of first) await append("fw9", r as never, root());
    const mid = await readStore("fw9", root());
    // a later correct of the same range replaces `a`; g is new; an undo of c
    const a2 = correct({ id: "m2k9x4q1-0000000a", supersedes: a.id });
    await append("fw9", [a2, g, { kind: "undo", id: "m2k9x4q1-0000000b", at: "x", pdf: PDF, of: c.id }], root());
    await writePublished(docs, "fw9", {
      ledger: ledgerOf({
        store: { id: mid.store, bytes: mid.bytes, lines: mid.lines, torn: 0, unreadable: 0, duplicate: 0, invalid: [], unsupported: [], error: null },
        applied: [{ id: a.id, kind: "correct", page: 1, bbox: [], node: "n", was: "8011O", now: "80110", tier: "exact", title_unchanged: false }],
        satisfied: [{ id: d.id, page: 3, node: "n", now: "x" }],
        orphaned: [{ id: e.id, kind: "correct", page: 4, bbox: null, reason: "no-node", was: "a", now: "b", text_now: "" }],
        dismissed: [{ id: dis.id, page: 1, code: "low-confidence", finding: FINDING }],
        refused: [{ id: dis2.id, finding: "abababababababab", reason: "refused-error" }],
        stale: [{ id: f.id, kind: "correct", finding: "" }],
      }),
    });
    const body = await (await get()).json();
    const by = Object.fromEntries(body.decisions.map((x: { id: string }) => [x.id, x]));
    expect(by[a.id]).toBeUndefined(); // replaced by a2: history, not listed
    expect(by[a2.id]).toMatchObject({ kind: "correct", status: "pending", pending: true, page: 1, was: "8011O", now: "80110", supersedes: a.id });
    expect(by[b.id]).toMatchObject({ kind: "keep", status: "pending" }); // no ledger entry for it: next build
    expect(by[c.id]).toMatchObject({ kind: "alt", status: "undone", pending: true, text: "A map", decorative: false });
    expect(by[d.id]).toMatchObject({ status: "satisfied", pending: false });
    expect(by[e.id]).toMatchObject({ status: "orphaned", reason: "no-node", pending: false });
    expect(by[f.id]).toMatchObject({ status: "stale" });
    expect(by[g.id]).toMatchObject({ status: "pending", pending: true });
    expect(by[dis.id]).toMatchObject({ kind: "dismiss", status: "dismissed", findings: [FINDING] });
    expect(by[dis2.id]).toMatchObject({ status: "refused", reason: "refused-error" });
    expect(body.pending).toBe(3); // a2, g, the undo
    expect(JSON.stringify(body)).not.toContain('"by"');
  });

  test("an applied decision shows applied; the same store with another id in the ledger is all pending", async () => {
    const a = correct({ id: "m2k9x4q1-00000001" });
    await append("fw9", a, root());
    const snap = await readStore("fw9", root());
    const led = (id: string | null) => ledgerOf({
      store: { id, bytes: snap.bytes, lines: snap.lines, torn: 0, unreadable: 0, duplicate: 0, invalid: [], unsupported: [], error: null },
      applied: [{ id: a.id, kind: "correct", page: 1, bbox: [], node: "n", was: "8011O", now: "80110", tier: "exact", title_unchanged: false }],
    });
    await writePublished(docs, "fw9", { ledger: led(snap.store) });
    let body = await (await get()).json();
    expect(body.decisions[0]).toMatchObject({ status: "applied", pending: false });
    expect(body.pending).toBe(0);
    await writePublished(docs, "fw9", { ledger: led("ffffffffffffffff") });
    body = await (await get()).json();
    expect(body.decisions[0]).toMatchObject({ status: "pending", pending: true });
    expect(body.pending).toBe(1);
  });

  test("a document with no ledger still lists the store, all pending", async () => {
    await append("fw9", correct(), root());
    await writePublished(docs, "fw9", { ledger: null });
    const body = await (await get()).json();
    expect(body.source).toBeNull();
    expect(body.decisions).toHaveLength(1);
    expect(body.pending).toBe(1);
  });

  test("a store that cannot be read is a 500 that says nothing about the file", async () => {
    await append("fw9", correct(), root());
    const { symlink, rm } = await import("node:fs/promises");
    await rm(storePath(root(), "fw9"));
    await symlink(join(dir, "x"), storePath(root(), "fw9"));
    const r = await get();
    expect(r.status).toBe(500);
    expect(JSON.stringify(await r.json())).not.toContain(dir);
  });
});
