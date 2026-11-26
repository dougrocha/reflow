import { describe, expect, test } from "bun:test";
import { ClientLimiter, clientKey, readClientLimitConfig, SHARED_CLIENT } from "./client-limit";

describe("ClientLimiter", () => {
  test("each client has its own minute", () => {
    let t = 0;
    const limiter = new ClientLimiter({ perMinute: 2, now: () => t });
    expect(limiter.charge("a")).toEqual({ charged: 0 });
    t = 10_000;
    expect(limiter.charge("a")).toEqual({ charged: 10_000 });
    t = 20_000;
    expect(limiter.charge("a")).toEqual({ busy: 40 });
    expect(limiter.charge("b")).toEqual({ charged: 20_000 });
    t = 60_000;
    expect(limiter.charge("a")).toEqual({ charged: 60_000 });
  });

  test("a refund gives a call back, and an emptied client leaves the table", () => {
    const limiter = new ClientLimiter({ perMinute: 1, now: () => 5 });
    const spent = limiter.charge("a");
    if ("busy" in spent) throw new Error("refused");
    expect(limiter.charge("a")).toEqual({ busy: 60 });
    limiter.refund("a", spent.charged);
    expect(limiter.size).toBe(0);
    expect(limiter.charge("a")).toEqual({ charged: 5 });
  });

  test("clients idle for a minute are dropped", () => {
    let t = 0;
    const limiter = new ClientLimiter({ perMinute: 1, now: () => t });
    for (const c of ["a", "b", "c"]) limiter.charge(c);
    expect(limiter.size).toBe(3);
    t = 30_000;
    limiter.charge("d");
    expect(limiter.size).toBe(4);
    t = 61_000;
    limiter.charge("e");
    expect(limiter.size).toBe(2);
  });

  test("the table never holds more than maxClients; the one idle longest goes first", () => {
    let t = 0;
    const limiter = new ClientLimiter({ perMinute: 1, maxClients: 3, now: () => t });
    for (const c of ["a", "b", "c", "d", "e"]) {
      t += 1;
      limiter.charge(c);
      expect(limiter.size).toBeLessThanOrEqual(3);
    }
    expect(limiter.size).toBe(3);
    // "a" was evicted, so it starts afresh; "e" is still held to its one call.
    expect("charged" in limiter.charge("a")).toBe(true);
    expect("busy" in limiter.charge("e")).toBe(true);
  });

  test("a refused request leaves no record", () => {
    const limiter = new ClientLimiter({ perMinute: 1, maxClients: 2, now: () => 1 });
    limiter.charge("a");
    limiter.charge("b");
    for (let i = 0; i < 100; i++) limiter.charge("a");
    expect(limiter.size).toBe(2);
  });
});

describe("clientKey", () => {
  const h = (init: Record<string, string>) => new Headers(init);

  test("no header configured: every request is the shared client, whatever it sends", () => {
    expect(clientKey(h({ "x-forwarded-for": "9.9.9.9" }), undefined)).toBe(SHARED_CLIENT);
  });

  test("a configured header: its last entry, which a proxy appended", () => {
    const headers = h({ "x-forwarded-for": "6.6.6.6, 7.7.7.7, 203.0.113.9" });
    expect(clientKey(headers, "x-forwarded-for")).toBe("203.0.113.9");
  });

  test("a missing or empty header, or a header cut to size, still gives a bounded key", () => {
    expect(clientKey(h({}), "x-real-ip")).toBe(SHARED_CLIENT);
    expect(clientKey(h({ "x-real-ip": " , " }), "x-real-ip")).toBe(SHARED_CLIENT);
    expect(clientKey(h({ "x-real-ip": "A".repeat(500) }), "x-real-ip").length).toBe(64);
  });
});

describe("readClientLimitConfig", () => {
  test("defaults to 5 a minute and no trusted header", () => {
    expect(readClientLimitConfig({})).toEqual({ perMinute: 5, header: undefined });
  });

  test("reads both; a bad count is the default, a header name is lower-cased", () => {
    expect(readClientLimitConfig({ REFLOW_ASK_PER_CLIENT: "12", REFLOW_ASK_CLIENT_HEADER: " CF-Connecting-IP " })).toEqual({
      perMinute: 12,
      header: "cf-connecting-ip",
    });
    for (const bad of ["0", "-3", "2.5", "many", ""]) {
      expect(readClientLimitConfig({ REFLOW_ASK_PER_CLIENT: bad }).perMinute).toBe(5);
    }
  });
});
