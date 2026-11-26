import { describe, expect, test } from "bun:test";
import type { AskResult } from "@/lib/ask-contract";
import { announcementFor, highlightedInTranscript, messageFor, notInTranscript, shownOnPage, summaryBy } from "./ask-copy";

const label = (p: number) => String(p);
const ev = (page: number) => ({
  page,
  node: "n",
  quote: "q",
  section: "",
  region: { bbox: [0, 0, 1, 1] as [number, number, number, number], width: 1, height: 1 },
});
const answer = (...pages: number[]): AskResult => ({
  kind: "answer",
  answer: "a",
  model: "m",
  evidence: pages.map(ev) as [ReturnType<typeof ev>],
});

const others: AskResult[] = [
  { kind: "none", why: "no-match", model: null },
  { kind: "none", why: "not-stated", model: "m" },
  { kind: "none", why: "unverified", model: "m" },
  { kind: "unavailable", why: "not-indexed" },
  { kind: "unavailable", why: "stale" },
  { kind: "unavailable", why: "model-offline" },
  { kind: "unavailable", why: "no-credentials" },
  { kind: "busy", retryAfterSeconds: 7 },
  { kind: "error", why: "bad-request" },
  { kind: "error", why: "unknown-document" },
  { kind: "error", why: "timeout" },
  { kind: "error", why: "unusable-reply" },
  { kind: "error", why: "provider-failed" },
  { kind: "error", why: "network" },
];

describe("ask-copy", () => {
  test("every non-answer has a distinct plain sentence, spoken the same way", () => {
    const seen = new Set<string>();
    for (const r of others) {
      const m = messageFor(r);
      expect(m).toBeTruthy();
      expect(m).not.toMatch(/!|oops/i);
      expect(announcementFor(r, label)).toBe(m!);
      seen.add(m!);
    }
    expect(seen.size).toBe(others.length);
  });
  test("busy carries its seconds", () => {
    expect(messageFor({ kind: "busy", retryAfterSeconds: 7 })).toContain("7 seconds");
    expect(messageFor({ kind: "busy", retryAfterSeconds: 1 })).toContain("1 second.");
  });
  test("an answer has no message and announces only the pages", () => {
    expect(messageFor(answer(5))).toBeNull();
    expect(announcementFor(answer(5), label)).toBe("Answer found on page 5.");
    expect(announcementFor(answer(9, 5), label)).toBe("Answer found on page 5 and page 9.");
    expect(announcementFor(answer(5, 9, 12), label)).toBe("Answer found on page 5, page 9 and page 12.");
    expect(announcementFor(answer(5, 5), (p) => `v${p}`)).toBe("Answer found on page v5.");
  });
  test("summary and the two show sentences", () => {
    expect(summaryBy("gemma4:e4b")).toBe("Summary by gemma4:e4b");
    expect(shownOnPage("5", "3. A > 3.1. Residual Learning")).toBe(
      "Showing 3.1. Residual Learning on page 5. Press Back to answer to return.",
    );
    expect(shownOnPage("5", "")).toBe("Showing the passage on page 5. Press Back to answer to return.");
    expect(highlightedInTranscript("5", "A > B")).toBe(
      "Highlighted on page 5, B. Press Alt+Shift+R to return to the answer.",
    );
    expect(highlightedInTranscript("iv", "")).toBe(
      "Highlighted on page iv. Press Alt+Shift+R to return to the answer.",
    );
  });
});

describe("notInTranscript", () => {
  test("says the passage could not be found, alone when there is no page image to fall back on", () => {
    expect(notInTranscript(null)).toBe("Could not find the passage in the transcript.");
  });
  test("with a fallback, it is followed by what the fallback says", () => {
    const shown = shownOnPage("5", "3.1. Residual Learning");
    expect(notInTranscript(shown)).toBe(`Could not find the passage in the transcript. ${shown}`);
  });
});
