import { describe, expect, test } from "bun:test";
import { validLayout } from "./layout-state";

describe("the inspector's layout", () => {
  test("a layout stored before the dock keeps its shown panels; its sizes are dropped", () => {
    const old = { v: 2, visible: ["details", "page"], columns: { page: 50, side: 50 }, detailsSize: 30, detailsCollapsed: true };
    expect(validLayout(old)).toEqual({ v: 2, visible: ["page", "details"] });
    expect(validLayout({ v: 2, visible: [] })).toBeNull();
  });
});
