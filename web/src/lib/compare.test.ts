import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { repo } from "../../scripts/freshness";
import type { InspectNode } from "@/lib/inspect";
import { SIDECAR_TEXT_LIMIT, blocksOf, compareColsFrom, fullTextsOf, diffColumn, diffWords, matchOf, moveCol, parseCols, pdfWordsOf, summarize } from "./compare";
import { compareUrl } from "./urls";

const node = (id: string, role: string, bbox: number[], text = "", attrs: Record<string, unknown> = {}) =>
  ({ id, role, bbox, text, attrs, source: "heuristic", confidence: 1, facts: [] }) as InspectNode;

describe("the compare screen", () => {
  test("containers and artifacts are not blocks; labels and alt text are", () => {
    const blocks = blocksOf([
      node("l", "list", [0, 0, 10, 10]), node("a", "artifact", [0, 0, 1, 1]),
      node("f", "field", [0, 0, 5, 5], "", { label: "Name" }), node("g", "figure", [5, 5, 9, 9], "", { alt: "A logo" }),
    ]);
    expect(blocks.map((b) => [b.id, b.label, b.alt])).toEqual([["f", "Name", null], ["g", null, "A logo"]]);
  });

  test("blocks match by where they sit, and a block split in two still matches the whole", () => {
    const ref = blocksOf([node("a", "p", [0, 0, 100, 20]), node("b", "p", [0, 40, 100, 60])]);
    const at = (bbox: number[], role = "p") => ({ role, bbox });
    expect(matchOf(at([0, 0, 50, 20]), ref)?.id).toBe("a");
    expect(matchOf(at([0, 41, 100, 59]), ref)?.id).toBe("b");
    expect(matchOf(at([200, 200, 210, 210]), ref)).toBeNull();
  });

  test("a table matches only a table, so the blocks it holds do not match it", () => {
    const ref = blocksOf([node("t", "table", [0, 0, 600, 800], "", { rows: 45, columns: 30 })]);
    expect(ref[0].shape).toBe("45 rows × 30 columns");
    expect(matchOf({ role: "h2", bbox: [10, 10, 100, 20] }, ref)).toBeNull();
    const [d] = diffColumn(blocksOf([node("t2", "table", [0, 0, 600, 800], "", { rows: 44, columns: 30 })]), ref, null);
    expect([d.match?.id, d.shapeChanged, d.differs]).toEqual(["t", true, true]);
  });

  test("a word diff marks only the words the reference does not have there", () => {
    const { words: w, removed } = diffWords("Gather both receipts now", "Gather both your receipts", null);
    expect(w.filter((x) => x.changed).map((x) => x.text)).toEqual(["now"]);
    expect(removed).toEqual(["your"]);
    expect(diffWords("same text", null, null).words.some((x) => x.changed)).toBe(false);
  });

  test("a block that only leaves words out differs", () => {
    const ref = blocksOf([node("a", "p", [0, 0, 100, 20], "Gather both your receipts")]);
    const [d] = diffColumn(blocksOf([node("b", "p", [0, 0, 100, 20], "Gather both receipts")]), ref, null);
    expect([d.differs, d.removed]).toEqual([true, ["your"]]);
    expect(summarize([d], ref).differ).toBe(1);
  });

  test("words the PDF's text does not have are marked, ignoring case and punctuation; a scan marks none", () => {
    const pdf = pdfWordsOf([{ text: "Step 1. Gather both" }]);
    expect(diffWords("STEP 1: gather Bothe", null, pdf).words.filter((x) => x.notInPdf).map((x) => x.text)).toEqual(["Bothe"]);
    expect(diffWords("anything", null, new Set()).words.some((x) => x.notInPdf)).toBe(false);
  });

  test("a column against the reference: role, label and alt changes, and what it misses", () => {
    const ref = blocksOf([
      node("h", "h3", [0, 0, 100, 20], "Step 1."), node("f", "field", [0, 30, 50, 40], "", { label: "Name" }),
      node("x", "p", [0, 100, 100, 120], "Only here"),
    ]);
    const mine = blocksOf([
      node("h2", "h2", [0, 0, 100, 20], "Step 1."), node("f2", "field", [0, 30, 50, 40], "", { label: "Full name" }),
      node("n", "figure", [0, 200, 50, 240], "", { alt: "A logo" }),
    ]);
    const diffs = diffColumn(mine, ref, null);
    expect(diffs.map((d) => [d.block.id, d.match?.id ?? null, d.roleChanged, d.labelChanged, d.differs])).toEqual([
      ["h2", "h", true, false, true], ["f2", "f", false, true, true], ["n", null, false, false, true],
    ]);
    const s = summarize(diffs, ref);
    expect([s.blocks, s.headings, s.fields, s.figures, s.described, s.differ, s.missing]).toEqual([3, 1, 1, 1, 1, 3, 1]);
    expect(summarize(diffColumn(ref, null, null), null).differ).toBe(0);
  });

  test("the URL keeps the columns and the page; unknown or repeated columns drop out", () => {
    expect(parseCols("haiku,nope,default,haiku", ["default", "haiku", "docling"])).toEqual(["haiku", "default"]);
    expect(parseCols(undefined, ["docling", "default", "haiku"])).toEqual(["default", "docling"]);
    expect(compareUrl("fw9", ["default", "haiku"], 3)).toBe("/docs/fw9/compare?cols=default,haiku&page=3");
    expect(compareUrl("fw9", ["default"], 1)).toBe("/docs/fw9/compare?cols=default");
    expect(moveCol(["a", "b", "c"], "c", 0)).toEqual(["c", "a", "b"]);
    expect(compareColsFrom("haiku", ["default", "docling", "haiku"])).toEqual(["default", "haiku"]);
    expect(compareColsFrom("default", ["default", "docling", "haiku"])).toEqual(["default", "docling"]);
  });

  test("SIDECAR_TEXT_LIMIT is the length the sidecar writer cuts a block's text at", () => {
    const python = readFileSync(`${repo}/src/reflow/render/sidecars.py`, "utf8");
    expect(Number(/^TEXT_LIMIT = (\d+)$/m.exec(python)?.[1])).toBe(SIDECAR_TEXT_LIMIT);
  });

  test("a block's whole text replaces the sidecar's, cut at 200 characters, so a late difference counts", () => {
    const long = `${"word ".repeat(50)}end`;
    const tree = { root: { children: [{ id: "a", page: 1, text: long }, { id: "b", page: 1, text: "short" }, { id: "c", page: 2, text: long }] } };
    expect(fullTextsOf(tree, 1)).toEqual({ a: long });
    const ref = blocksOf([node("a", "p", [0, 0, 100, 20], long)]);
    const [d] = diffColumn(blocksOf([node("a", "p", [0, 0, 100, 20], long.replace(/end$/, "finish"))]), ref, null);
    expect([d.differs, d.removed]).toEqual([true, ["end"]]);
  });
});
