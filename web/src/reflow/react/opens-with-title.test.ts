import { describe, expect, test } from "bun:test";
import { contract } from "../contract-fixture";
import { normalizeManifest } from "../core";
import { opensWithTitle } from "./DocumentViewer";

describe("opensWithTitle", () => {
  const manifest = normalizeManifest(contract.manifest);
  test("a title the converter found on page 1", () => {
    expect(manifest.titleOnPage).toBe(true);
    expect(opensWithTitle(manifest, "Medicare notice")).toBe(true);
  });
  test("a first heading on page 1 that reads as the title", () => {
    const toc = [{ ...manifest.toc[0], text: "Annual  Report", page: 1 }];
    expect(opensWithTitle({ toc, titleOnPage: undefined }, "annual report")).toBe(true);
  });
  test("otherwise the large title shows", () => {
    const toc = manifest.toc;
    expect(opensWithTitle({ toc, titleOnPage: undefined }, "Budget")).toBe(false);
    expect(opensWithTitle({ toc: [{ ...toc[0], text: "Budget", page: 2 }], titleOnPage: undefined }, "Budget")).toBe(false);
    expect(opensWithTitle({ toc: [], titleOnPage: undefined }, "Budget")).toBe(false);
  });
});
