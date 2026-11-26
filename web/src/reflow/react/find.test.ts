import { beforeAll, describe, expect, test } from "bun:test";
import { contractPages } from "../contract-fixture";
import { collectText, textOfHtml } from "./find";

function page(html: string): HTMLElement {
  const el = document.createElement("div");
  el.innerHTML = html;
  return el;
}

// The pages of the document Python publishes (schema/viewer-contract/), as the viewer holds them.
let pages: Map<number, string>;
beforeAll(async () => { pages = await contractPages(); });

describe("collectText", () => {
  // Page 4 was read by OCR: reflow's note, then a block OCR read.
  test("text reflow wrote itself is in neither the inert index nor the live map", () => {
    const html = pages.get(4) ?? "";
    for (const live of [false, true]) {
      const map = collectText(page(html), live);
      expect(map.text).not.toContain("read by ocr");
      expect(map.text).toContain("notes.");
      expect(map.parts.every((p) => !p.node.parentElement?.closest("[data-generated]"))).toBe(true);
    }
  });
  test("both read the same offsets", () => {
    const html = pages.get(4) ?? "";
    expect(collectText(page(html), true).text).toBe(collectText(page(html), false).text);
  });
});

describe("a figure a model described", () => {
  const html = () => pages.get(2) ?? "";
  test("its badge, alt and description are not found; its caption and labels are", () => {
    for (const text of [textOfHtml(html()), collectText(page(html()), true).text]) {
      for (const word of ["ai-drafted", "bar chart", "blue bars", "image description"]) {
        expect(text).not.toContain(word);
      }
      expect(text).toContain("fig. 1. sales");
      expect(text).toContain("q1 revenue");
    }
    expect(textOfHtml(html())).toBe(collectText(page(html()), true).text);
  });
});
