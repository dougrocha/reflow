import { describe, expect, test } from "bun:test";
import { groupByType, itemFor, reviewNodeIds, suggestionText, type ReviewItem } from "./review";
import { itemUrl } from "./urls";

describe("suggestionText", () => {
  test.each([
    [{ was: "8011O", now: "80110", rule: "zip-digit" }, "OCR read ‘8011O’ · suggested ‘80110’ (ZIP code digit)"],
    [{ was: "Ge neral", now: "General", rule: "split-word" }, "OCR read ‘Ge neral’ · suggested ‘General’ (split word)"],
    [{ was: "x", now: "y", rule: "new-rule" }, "OCR read ‘x’ · suggested ‘y’ (new-rule)"],
  ])("%p", (s, text) => {
    expect(suggestionText({ ...s, bbox: null })).toBe(text);
  });
});

describe("reviewNodeIds", () => {
  const base = { severity: "warning" as const, page: 1, confidence: 1, source: "human", needs: null, warn: null, text: "", bbox: null, pageSize: null };
  test("a node item is its own node", () => {
    expect(reviewNodeIds({ ...base, id: "n-1", role: "p" })).toEqual(["n-1"]);
  });
  test("a group lists its members", () => {
    expect(reviewNodeIds({ ...base, id: "n-1", role: "p", group: { nodeIds: ["n-1", "n-2"] } })).toEqual(["n-1", "n-2"]);
  });
  test("a page item and a document item have no node either: their ids are synthetic", () => {
    expect(reviewNodeIds({ ...base, id: "page:5", role: "page" })).toEqual([]);
    expect(reviewNodeIds({ ...base, id: "doc-1", role: "doc", page: null })).toEqual([]);
  });
  test("an orphaned decision has a box and no node", () => {
    expect(reviewNodeIds({ ...base, id: "decision:abc", role: "decision" })).toEqual([]);
  });
});

describe("itemById", () => {
  const base = { severity: "warning" as const, page: 1, confidence: 1, source: "human", needs: null, warn: null, text: "", bbox: null, pageSize: null };
  const items = [
    { ...base, id: "decision:abc", role: "decision" },
    { ...base, id: "n-1", role: "p", group: { nodeIds: ["n-1", "n-2"] } },
  ];
  test("finds an item by its id, or by one of its nodes", () => {
    expect(itemFor(items, "decision:abc")?.id).toBe("decision:abc");
    expect(itemFor(items, "n-2")?.id).toBe("n-1");
    expect(itemFor(items, "nope")).toBeNull();
    expect(itemFor(items, undefined)).toBeNull();
  });
});

describe("itemFor with document items that share one id", () => {
  const base = { severity: "warning" as const, page: null, confidence: 1, source: "human", needs: null, text: "", bbox: null, pageSize: null };
  const items = [
    { ...base, id: "doc", role: "doc", warn: "title-suspect" },
    { ...base, id: "doc", role: "doc", warn: "decisions-other-pdf" },
  ];
  test("the warn code tells them apart", () => {
    expect(itemFor(items, "doc", "decisions-other-pdf")?.warn).toBe("decisions-other-pdf");
    expect(itemFor(items, "doc")?.warn).toBe("title-suspect");
  });
  test("a link names the code for a document item only", () => {
    expect(itemUrl("fw9", items[1]!)).toBe("/inspect/fw9?item=doc&warn=decisions-other-pdf");
    expect(itemUrl("fw9", { ...base, id: "n-1", role: "p", page: 2, warn: null })).toBe("/inspect/fw9?page=2&item=n-1");
  });
});

// review.json for a low-confidence node on page 2 and a clean warning on page 1 (tests/test_review.py):
// page 2 first, then page 1, then a second low-confidence node on page 3.
const LINK = "Link to a page this document does not have, shown as plain text";
const LOW = "Low-confidence structure";
const walkBase = { severity: "warning" as const, role: "p", source: "heuristic", needs: null, bbox: null, pageSize: null };
const walk: ReviewItem[] = [
  { ...walkBase, id: "n-2", page: 2, confidence: 0.2, warn: "low-confidence", label: LOW, text: "weak" },
  { ...walkBase, id: "n-1", page: 1, confidence: 0.9, warn: "link-unresolved", label: LINK, text: "link" },
  { ...walkBase, id: "n-3", page: 3, confidence: 0.3, warn: "low-confidence", label: LOW, text: "shaky" },
];

describe("groupByType", () => {
  test("puts the largest group first, each group in walk order", () => {
    const [weak, link, shaky] = walk;
    expect(groupByType([link!, weak!, shaky!]).map(([type, group]) => [type, group.map((i) => i.id)])).toEqual([
      [LOW, ["n-2", "n-3"]],
      [LINK, ["n-1"]],
    ]);
  });
  test("keeps groups of equal size in first-seen order", () => {
    const [, link, shaky] = walk;
    expect(groupByType([link!, shaky!]).map(([type]) => type)).toEqual([LINK, LOW]);
  });
});
