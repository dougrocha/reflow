import { describe, expect, test } from "bun:test";
import { formatDuration, plural, secondsLabel } from "./format";

describe("plural", () => {
  test("one is singular, everything else takes the plural", () => {
    expect([plural(1, "error"), plural(0, "error"), plural(3, "error")]).toEqual(["1 error", "0 errors", "3 errors"]);
    expect(plural(2, "match", "matches")).toBe("2 matches");
  });
});

describe("the two times", () => {
  test("a measured time keeps a decimal under a minute", () => {
    expect([secondsLabel(12.44), secondsLabel(125)]).toEqual(["12.4 s", "2 min 5 s"]);
  });

  test("a build's duration pads its seconds", () => {
    expect([formatDuration(42_000), formatDuration(187_000), formatDuration(-5)]).toEqual(["42 s", "3 min 07 s", "0 s"]);
  });
});
