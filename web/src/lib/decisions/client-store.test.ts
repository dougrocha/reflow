import { describe, expect, test } from "bun:test";
import { DecisionsStore, publishedGeneration } from "./client-store";

const answer = (n: number) => ({ slug: "fw9", rev: "r", generation: 1, source: null, pending: n, decisions: [] });

function fake() {
  const calls: { url: string; method: string }[] = [];
  let pending = 0;
  const f = (async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? "GET" });
    if (init?.method === "POST") { pending++; return new Response(JSON.stringify({ decision: "d", pending }), { status: 201 }); }
    return new Response(JSON.stringify(answer(pending)), { status: 200 });
  }) as unknown as typeof fetch;
  return { f, calls };
}

describe("DecisionsStore", () => {
  test("refresh reads once for concurrent callers", async () => {
    const { f, calls } = fake();
    const s = new DecisionsStore("fw9", f);
    await Promise.all([s.refresh(), s.refresh()]);
    expect(calls.length).toBe(1);
    expect(s.get().status).toBe("ready");
  });
  test("a saved post reads the list again, so the count follows", async () => {
    const { f, calls } = fake();
    const s = new DecisionsStore("fw9", f);
    await s.refresh();
    const sent = await s.post({ choice: "undo", of: "x" });
    expect(sent.ok).toBe(true);
    expect(calls.map((c) => c.method)).toEqual(["GET", "POST", "GET"]);
    expect(s.get().data?.pending).toBe(1);
  });
  test("a refused post does not read again", async () => {
    const calls: string[] = [];
    const f = (async (_u: string, init?: RequestInit) => { calls.push(init?.method ?? "GET"); return new Response(JSON.stringify({ error: "old", reload: true }), { status: 409 }); }) as unknown as typeof fetch;
    const s = new DecisionsStore("fw9", f);
    const sent = await s.post({ choice: "undo", of: "x" });
    expect(sent).toEqual({ ok: false, message: "old", reload: true });
    expect(calls).toEqual(["POST"]);
  });
  test("a document with no publication yet is generation 0 once the builds data is in, so its first build reads again", async () => {
    expect(publishedGeneration(null, "fw9")).toBeUndefined();
    expect(publishedGeneration({ published: {} }, "fw9")).toBe(0);
    expect(publishedGeneration({ published: { fw9: 2 } }, "fw9")).toBe(2);
    const { f, calls } = fake();
    const s = new DecisionsStore("fw9", f);
    await s.refresh();
    s.noteGeneration(publishedGeneration({ published: {} }, "fw9"));
    s.noteGeneration(publishedGeneration({ published: { fw9: 1 } }, "fw9"));
    await s.refresh();
    expect(calls.length).toBe(2);
  });
  test("a failed read keeps what was shown and says why", async () => {
    const s = new DecisionsStore("fw9", (async () => new Response("{}", { status: 500 })) as unknown as typeof fetch);
    await s.refresh();
    expect(s.get().status).toBe("error");
  });
  test("a new publication generation reads the list again; the first sighting and a repeat do not", async () => {
    const { f, calls } = fake();
    const s = new DecisionsStore("fw9", f);
    await s.refresh();
    s.noteGeneration(3);
    s.noteGeneration(3);
    expect(calls.length).toBe(1);
    s.noteGeneration(4);
    s.noteGeneration(4);
    await s.refresh();
    expect(calls.length).toBe(2);
  });
  test("subscribers hear every change", async () => {
    const { f } = fake();
    const s = new DecisionsStore("fw9", f);
    let n = 0;
    s.subscribe(() => n++);
    await s.refresh();
    expect(n).toBe(2);
  });
});
