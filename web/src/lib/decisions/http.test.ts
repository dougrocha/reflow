import { describe, expect, test } from "bun:test";
import { loopbackHost, sameOriginWrite } from "../http";

const req = (headers: Record<string, string>, method = "POST") =>
  new Request("http://localhost:3447/api/decisions/fw9", { method, headers });

describe("loopbackHost", () => {
  test("accepts localhost, 127.0.0.1 and [::1], any case, with an optional port", () => {
    for (const host of ["localhost", "LOCALHOST:3447", "127.0.0.1:3417", "[::1]:3447", "[::1]", "127.0.0.1", "LocalHost:1", "localhost:65535"]) {
      expect(loopbackHost(req({ host }))).toBe(true);
    }
  });

  test("refuses everything else", () => {
    for (const host of [
      "localhost.", "localhost.example", "127.0.0.1.nip.io", "user@localhost", "localhost:abc", "localhost:70000",
      "localhost:0", "localhost:", "localhost:123456", "", "example.com", "0.0.0.0:3000",
      "127.0.0.2", "[::2]", "::1", "localhost:80:80", "evil.example:3447",
    ]) {
      expect(loopbackHost(req({ host }))).toBe(false);
    }
  });

  test("a request with no Host is refused", () => {
    expect(loopbackHost(new Request("http://localhost:3447/x"))).toBe(false);
  });
});

describe("sameOriginWrite", () => {
  const host = "localhost:3447";
  test("needs an Origin, and it must be the Host", () => {
    expect(sameOriginWrite(req({ host }))).not.toBeNull();
    expect(sameOriginWrite(req({ host, origin: "http://evil.example" }))).not.toBeNull();
    expect(sameOriginWrite(req({ host, origin: "http://localhost:3000" }))).not.toBeNull();
    expect(sameOriginWrite(req({ host, origin: "null" }))).not.toBeNull();
    expect(sameOriginWrite(req({ host, origin: "not a url" }))).not.toBeNull();
    expect(sameOriginWrite(req({ host, origin: "http://localhost:3447" }))).toBeNull();
    expect(sameOriginWrite(req({ host: "LOCALHOST:3447", origin: "http://localhost:3447" }))).toBeNull();
  });

  test("Sec-Fetch-Site, when present, must be same-origin; absent is fine", () => {
    const origin = "http://localhost:3447";
    expect(sameOriginWrite(req({ host, origin }))).toBeNull();
    expect(sameOriginWrite(req({ host, origin, "sec-fetch-site": "same-origin" }))).toBeNull();
    for (const site of ["cross-site", "same-site", "none", ""]) {
      expect(sameOriginWrite(req({ host, origin, "sec-fetch-site": site }))).not.toBeNull();
    }
  });

  test("a request with no Host is refused", () => {
    expect(sameOriginWrite(new Request("http://localhost:3447/x", { method: "POST", headers: { origin: "http://localhost:3447" } }))).not.toBeNull();
  });

  // THE DOCUMENTED LIMIT (docs/builds.md, docs/review-decisions.md). A route cannot see who
  // connected: Next takes `x-forwarded-for` from the client when it sends one, and `Host` and
  // `Origin` are whatever the client writes. So a program that can reach the port and sends
  // `Host: localhost` with a matching `Origin` passes both checks. The checks stop a web page
  // (cross-site, DNS rebinding), not a local process or, on a server bound to a network
  // interface, a host on that network. If this test fails because the checks now refuse such a
  // request, the docs that say what the boundary does not stop must change with it.
  test("a forged Host: localhost with a matching Origin is accepted", () => {
    const forged = req({ host: "localhost", origin: "http://localhost", "x-forwarded-for": "203.0.113.9" });
    expect(loopbackHost(forged)).toBe(true);
    expect(sameOriginWrite(forged)).toBeNull();
  });
});
