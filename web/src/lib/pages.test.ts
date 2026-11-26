import { describe, expect, test } from "bun:test";
import { pageCountText, pageImagePath, pageStem, pageText } from "./pages";

// The same case table is in tests/test_pagenames.py. Keep the two in step.
describe("pageCountText", () => {
  test.each([
    [1, "1 page"],
    [2, "2 pages"],
    [1234, "1,234 pages"],
  ])("%d", (n, text) => {
    expect(pageCountText(n)).toBe(text);
  });
});

describe("pageText", () => {
  test.each([
    [5, "3", "5 (printed 3)"],
    [5, "5", "5"],
    [5, null, "5"],
    [5, undefined, "5"],
  ])("%d, printed %p", (page, printed, text) => {
    expect(pageText(page, printed)).toBe(text);
  });
});

// The stem is also the name stem of page_filename and sidecar_name in src/reflow/render (tests/test_pagenames.py).
describe("pageStem and pageImagePath", () => {
  test.each([
    [1, "0001", "pages/0001.webp"],
    [7, "0007", "pages/0007.webp"],
    [123, "0123", "pages/0123.webp"],
    [12345, "12345", "pages/12345.webp"],
  ])("%d", (page, stem, image) => {
    expect(pageStem(page)).toBe(stem);
    expect(pageImagePath(page)).toBe(image);
  });
});
