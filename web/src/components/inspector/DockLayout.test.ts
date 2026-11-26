import { describe, expect, test } from "bun:test";
import { parseSavedDock } from "./DockLayout";

describe("the inspector's dock", () => {
  test("a saved dock is read only when it is ours", () => {
    const layout = { grid: {}, panels: { page: {}, details: {} } };
    expect(parseSavedDock(JSON.stringify({ v: 1, layout }))).toEqual(layout as never);
    expect(parseSavedDock(JSON.stringify({ v: 2, layout }))).toBeNull();
    expect(parseSavedDock(JSON.stringify({ v: 1, layout: { panels: { page: {}, other: {} } } }))).toBeNull();
    expect(parseSavedDock("not json")).toBeNull();
    expect(parseSavedDock(null)).toBeNull();
  });
});
