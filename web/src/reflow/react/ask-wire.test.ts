import { afterEach, describe, expect, test } from "bun:test";
import { parseAskResult, postAsk } from "./ask-wire";

const evidence = {
  page: 5,
  node: "n12",
  quote: "Residual learning.",
  section: "3. A > 3.1. B",
  region: { bbox: [1, 2, 3, 4], width: 612, height: 792 },
};
const answer = { kind: "answer", answer: "Yes.", evidence: [evidence], model: "gemma4:e4b" };
const network = { kind: "error", why: "network" };

describe("parseAskResult", () => {
  test("accepts a valid answer", () => {
    expect(parseAskResult(answer)).toEqual(answer as never);
  });
  test("turns bad answers into a network error", () => {
    expect(parseAskResult({ ...answer, evidence: [] })).toEqual(network as never);
    expect(parseAskResult({ ...answer, evidence: [evidence, evidence, evidence, evidence] })).toEqual(network as never);
    const nan = { ...evidence, region: { ...evidence.region, bbox: [1, NaN, 3, 4] } };
    expect(parseAskResult({ ...answer, evidence: [nan] })).toEqual(network as never);
    expect(parseAskResult({ ...answer, evidence: [{ ...evidence, page: 0 }] })).toEqual(network as never);
    expect(parseAskResult({ ...answer, model: 3 })).toEqual(network as never);
  });
  test("keeps the doubtful ranges of a quote, and refuses one outside it", () => {
    const doubtful = { ...evidence, doubt: [[0, 8]] };
    expect(parseAskResult({ ...answer, evidence: [doubtful] })).toEqual({ ...answer, evidence: [doubtful] } as never);
    expect(parseAskResult({ ...answer, evidence: [{ ...evidence, doubt: [[0, 99]] }] })).toEqual(network as never);
  });
  test("turns an unknown kind, an unknown reason and a non-object into a network error", () => {
    expect(parseAskResult({ kind: "maybe" })).toEqual(network as never);
    expect(parseAskResult({ kind: "none", why: "bored", model: null })).toEqual(network as never);
    expect(parseAskResult({ kind: "unavailable", why: "no-match" })).toEqual(network as never);
    for (const v of [null, undefined, "x", 4, [answer]]) expect(parseAskResult(v)).toEqual(network as never);
  });
  test("accepts the other kinds", () => {
    const ok = [
      { kind: "none", why: "no-match", model: null },
      { kind: "none", why: "unverified", model: "m" },
      { kind: "unavailable", why: "stale" },
      { kind: "busy", retryAfterSeconds: 4 },
      { kind: "error", why: "timeout" },
    ];
    for (const r of ok) expect(parseAskResult(r)).toEqual(r as never);
    expect(parseAskResult({ kind: "busy", retryAfterSeconds: -1 })).toEqual(network as never);
  });
});

describe("postAsk", () => {
  const real = globalThis.fetch;
  const set = (f: unknown) => void (globalThis.fetch = f as typeof fetch);
  const req = { question: "q", version: "v" };
  afterEach(() => void (globalThis.fetch = real));

  test("parses the body whatever the status", async () => {
    set(async () => new Response(JSON.stringify({ kind: "busy", retryAfterSeconds: 3 }), { status: 429 }));
    expect(await postAsk("/u", req, new AbortController().signal)).toEqual({ kind: "busy", retryAfterSeconds: 3 });
  });
  test("a failed or unreadable reply is a network error", async () => {
    set(async () => {
      throw new TypeError("down");
    });
    expect(await postAsk("/u", req, new AbortController().signal)).toEqual(network as never);
    set(async () => new Response("<html>", { status: 502 }));
    expect(await postAsk("/u", req, new AbortController().signal)).toEqual(network as never);
  });
  test("rejects with the abort reason", async () => {
    const ctl = new AbortController();
    set(async () => {
      ctl.abort("gone");
      throw new Error("aborted");
    });
    await expect(postAsk("/u", req, ctl.signal)).rejects.toBe("gone");
  });
});
