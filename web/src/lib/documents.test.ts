import { describe, expect, test } from "bun:test";
import type { DocumentData } from "./docs-data";
import { summarize } from "./documents";
import type { ReviewItem } from "./review";

describe("summarize", () => {
  // review.json for a low-confidence node on page 2 and a clean warning on page 1: page 2 first.
  const base = { severity: "warning" as const, role: "p", source: "heuristic", needs: null, bbox: null, pageSize: null };
  const review: ReviewItem[] = [
    { ...base, id: "n-2", page: 2, confidence: 0.2, warn: "low-confidence", label: "Low-confidence structure", text: "weak" },
    { ...base, id: "n-1", page: 1, confidence: 0.9, warn: "link-unresolved", label: "Link to a page this document does not have, shown as plain text", text: "link" },
    { ...base, id: "n-3", page: 3, confidence: 0.3, warn: "low-confidence", label: "Low-confidence structure", text: "shaky" },
  ];
  // Only what summarize reads of a document.
  const summary = summarize({ slug: "fw9", generation: 1, pages: 3, review } as unknown as DocumentData);

  test("the first-issue link opens the walk's first item, page 2's", () => {
    expect(summary.firstIssueHref).toBe("/inspect/fw9?page=2&item=n-2");
    expect(summary.items?.map((i) => i.id)).toEqual(["n-2", "n-1", "n-3"]);
  });
  test("the top issue is the largest group", () => {
    expect(summary.topIssue).toEqual({ type: "Low-confidence structure", count: 2 });
  });
});
