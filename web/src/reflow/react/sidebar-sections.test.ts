import { describe, expect, test } from "bun:test";
import { DEFAULT_SECTIONS, parseSections } from "./sidebar-sections";

describe("parseSections", () => {
  test("reads both flags", () => {
    expect(parseSections({ toc: false, ask: true })).toEqual({ toc: false, ask: true });
  });
  test("a missing or wrong-typed flag takes its default", () => {
    expect(parseSections({})).toEqual(DEFAULT_SECTIONS);
    expect(parseSections({ toc: "no", ask: 1 })).toEqual(DEFAULT_SECTIONS);
    expect(parseSections({ ask: true })).toEqual({ toc: true, ask: true });
  });
  test("anything but an object does not fit", () => {
    expect(parseSections(null)).toBeNull();
    expect(parseSections("open")).toBeNull();
    expect(parseSections([true, false])).toBeNull();
  });
});
