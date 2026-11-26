import { describe, expect, mock, test } from "bun:test";
import type { AskResult } from "@/lib/ask-contract";

// The route is a shell over @/lib/ask. Only the barrel is faked, so no other test file is affected.
let next: AskResult = { kind: "none", why: "no-match", model: null };
const seen: { slug: string; body: unknown }[] = [];
// oxlint-disable-next-line no-restricted-imports -- the test needs the real module behind the mocked barrel
const real = await import("@/lib/ask/ask");
mock.module("@/lib/ask", () => ({
  ask: async (slug: string, body: unknown) => {
    seen.push({ slug, body });
    return slug === "nope" ? ({ kind: "error", why: "unknown-document" } as AskResult) : next;
  },
  statusOf: real.statusOf,
  cleanQuestion: real.cleanQuestion,
}));
const { POST } = await import("./route");

const call = (slug: string, body: unknown, headers: Record<string, string> = {}, raw?: string) =>
  POST(
    new Request(`http://localhost/api/ask/${slug}`, {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: raw ?? JSON.stringify(body),
    }),
    { params: Promise.resolve({ slug }) } as RouteContext<"/api/ask/[slug]">,
  );
const good = { question: "What is it?", version: "v1" };

describe("POST /api/ask/[slug]", () => {
  test("a good request is answered with the result, no-store", async () => {
    seen.length = 0;
    const res = await call("resnet", good);
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(await res.json()).toEqual(next);
    expect(seen).toEqual([{ slug: "resnet", body: good }]);
  });

  test("the cleaned question is what ask receives", async () => {
    seen.length = 0;
    await call("resnet", { ...good, question: "  a   b \n c " });
    expect(seen[0].body).toEqual({ question: "a b c", version: "v1" });
  });

  test("same-origin and absent Sec-Fetch-Site are allowed", async () => {
    expect((await call("resnet", good, { "sec-fetch-site": "same-origin" })).status).toBe(200);
  });

  test("cross-site and same-site are refused before ask runs", async () => {
    seen.length = 0;
    for (const site of ["cross-site", "same-site", "none"]) {
      const res = await call("resnet", good, { "sec-fetch-site": site });
      expect(res.status).toBe(400);
      expect(await res.json()).toEqual({ kind: "error", why: "bad-request" });
      expect(res.headers.get("cache-control")).toBe("no-store");
    }
    expect(seen).toEqual([]);
  });

  test("wrong content type, bad JSON and wrong shapes are 400", async () => {
    expect((await call("resnet", good, { "content-type": "text/plain" })).status).toBe(400);
    expect((await call("resnet", good, {}, "{not json")).status).toBe(400);
    for (const body of [[], null, "q", { question: 1, version: "v1" }, { question: "q" }, { version: "v1" }]) {
      expect((await call("resnet", body)).status).toBe(400);
    }
  });

  test("empty, blank, long questions and huge bodies are 400", async () => {
    expect((await call("resnet", { ...good, question: "  " })).status).toBe(400);
    expect((await call("resnet", { ...good, question: "x".repeat(301) })).status).toBe(400);
    expect((await call("resnet", { ...good, question: "x".repeat(5000) })).status).toBe(400);
    expect((await call("resnet", { ...good, question: "x".repeat(300) })).status).toBe(200);
  });

  test("a body is refused on its Content-Length before any of it is read", async () => {
    seen.length = 0;
    let pulled = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled++;
        // Bounded, so the current code fails the test instead of reading until memory runs out.
        if (pulled > 20000) controller.close();
        else controller.enqueue(new Uint8Array(1024));
      },
    });
    const res = await POST(
      new Request("http://localhost/api/ask/resnet", {
        method: "POST",
        headers: { "content-type": "application/json", "content-length": "100000000" },
        body,
        duplex: "half",
      } as RequestInit),
      { params: Promise.resolve({ slug: "resnet" }) } as RouteContext<"/api/ask/[slug]">,
    );
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ kind: "error", why: "bad-request" });
    expect(pulled).toBeLessThanOrEqual(1);
    expect(seen).toEqual([]);
  });

  test("a streamed body with no or a false Content-Length is cut off at the cap", async () => {
    seen.length = 0;
    for (const lie of [undefined, "10"]) {
      let pulled = 0;
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          pulled++;
          if (pulled > 5000) controller.close();
          else controller.enqueue(new Uint8Array(1024).fill(32));
        },
      });
      const headers: Record<string, string> = { "content-type": "application/json" };
      if (lie) headers["content-length"] = lie;
      const res = await POST(
        new Request("http://localhost/api/ask/resnet", { method: "POST", headers, body, duplex: "half" } as RequestInit),
        { params: Promise.resolve({ slug: "resnet" }) } as RouteContext<"/api/ask/[slug]">,
      );
      expect(res.status).toBe(400);
      // Eight 1 KiB chunks pass the 8,000-byte cap; the stream must not be drained to the end.
      expect(pulled).toBeLessThan(100);
    }
    expect(seen).toEqual([]);
  });

  test("an unknown document is 404", async () => {
    expect((await call("nope", good)).status).toBe(404);
  });

  test("statuses follow statusOf, and busy carries Retry-After", async () => {
    next = { kind: "busy", retryAfterSeconds: 7 };
    const busy = await call("resnet", good);
    expect(busy.status).toBe(429);
    expect(busy.headers.get("retry-after")).toBe("7");
    next = { kind: "unavailable", why: "model-offline" };
    expect((await call("resnet", good)).status).toBe(503);
    next = { kind: "error", why: "timeout" };
    expect((await call("resnet", good)).status).toBe(502);
  });
});
