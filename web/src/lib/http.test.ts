import { describe, expect, test } from "bun:test";
import { crossSite, originIsHost, readCapped, readJsonObject } from "./http";

describe("readJsonObject", () => {
  const post = (body: string, type = "application/json") =>
    new Request("http://localhost/x", { method: "POST", headers: { "content-type": type }, body });

  test("the cap is an argument; builds keep 1,000 bytes", async () => {
    const big = JSON.stringify({ a: "x".repeat(2000) });
    expect(await readJsonObject(post(big))).toEqual({ error: "The request is too big.", status: 413 });
    const ok = await readJsonObject(post(big), 8192);
    expect("body" in ok && ok.body.a).toBe("x".repeat(2000));
    expect(await readJsonObject(post(JSON.stringify({ a: "x".repeat(9000) })), 8192)).toEqual({ error: "The request is too big.", status: 413 });
  });

  test("a wrong content type or body is a 400", async () => {
    expect(await readJsonObject(post("{}", "text/plain"))).toEqual({ error: "Send application/json.", status: 400 });
    expect(await readJsonObject(post("[]"))).toEqual({ error: "The body must be a JSON object.", status: 400 });
    expect(await readJsonObject(post("nope"))).toEqual({ error: "The body must be a JSON object.", status: 400 });
  });
});

describe("readCapped", () => {
  test("stops reading at the cap, whatever Content-Length says", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(new Uint8Array(600)); c.enqueue(new Uint8Array(600)); c.close(); },
    });
    const req = new Request("http://localhost/x", { method: "POST", body, duplex: "half" } as RequestInit);
    expect(await readCapped(req, 1000)).toBeNull();
    expect((await readCapped(new Request("http://localhost/x", { method: "POST", body: "abc" }), 1000))?.byteLength).toBe(3);
  });
});

describe("the same-site rule", () => {
  test("an Origin is the host asked, without case; anything else, and a non-URL, is not", () => {
    expect(originIsHost("http://localhost:3000", "Localhost:3000")).toBe(true);
    expect(originIsHost("http://localhost:3001", "localhost:3000")).toBe(false);
    expect(originIsHost("null", "localhost:3000")).toBe(false);
  });

  test("a request with no Origin is let through by crossSite, which is what a script sends", () => {
    const get = (headers: Record<string, string>) => new Request("http://localhost:3000/x", { method: "POST", headers });
    expect(crossSite(get({}))).toBeNull();
    expect(crossSite(get({ origin: "http://evil.test" }))).toBe("Cross-origin requests are refused.");
    expect(crossSite(get({ "sec-fetch-site": "cross-site" }))).toBe("Cross-site requests are refused.");
  });
});
