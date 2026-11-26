import { describe, expect, test } from "bun:test";
import type { ReviewItem } from "../review";
import {
  actionsOf, bodies, hasAltDecision, isSettled, openFindings, changesNextBuild, decidedWords, decisionOfFact, describeDecision, dismissible, hasError, orphanId, pendingFor, pendingLine, SAVED, send, statusWords,
} from "./client";
import type { DecisionView } from "./status";

const anchor = { page: 5, bbox: [72, 100, 300, 112] as [number, number, number, number], text_sha: "0123456789abcdef", crop_sha: "", hint: "n-1" };
const sug = { was: "8011O", now: "80110", rule: "zip-digit", bbox: anchor.bbox, anchor, before: "CO ", after: "", id: "9f8e7d6c5b4a3210" };
const item = (over: Partial<ReviewItem> = {}): ReviewItem => ({
  id: "n-1", severity: "warning", role: "p", page: 5, confidence: 0.5, source: "ocr", needs: null, warn: "ocr-suggestion", text: "CO 8011O",
  bbox: anchor.bbox, pageSize: [612, 792], anchor, suggestions: [sug],
  findings: [{ code: "ocr-suggestion", severity: "warning", id: "f1" }, { code: "low-confidence", severity: "warning", id: "f2" }], ...over,
});
const common = { id: "d1", at: "t", status: "pending" as const, pending: true, supersedes: null, page: 5, bbox: anchor.bbox, reason: null };
const correct = (over: Record<string, unknown> = {}): DecisionView =>
  ({ ...common, kind: "correct", was: "8011O", now: "80110", anchor, range: { was: "8011O", before: "CO ", after: "", rule: "zip-digit" }, ...over }) as DecisionView;

describe("pendingLine", () => {
  test("words", () => {
    expect(pendingLine(0)).toBeNull();
    expect(pendingLine(1)).toBe("1 decision will apply at the next build");
    expect(pendingLine(3)).toBe("3 decisions will apply at the next build");
  });
});

describe("changesNextBuild", () => {
  test("counts the decisions that change the next build, not the lines", () => {
    expect(changesNextBuild([correct(), correct({ status: "undone", pending: false }), correct({ status: "applied", pending: false })])).toBe(1);
  });
});

describe("pendingFor", () => {
  test("a pending correction or keep matches its suggestion by anchor and words", () => {
    expect(pendingFor(item(), [correct()]).length).toBe(1);
    expect(pendingFor(item(), [correct({ kind: "keep" })]).length).toBe(1);
  });
  test("another word, another anchor or a settled decision does not match", () => {
    expect(pendingFor(item(), [correct({ range: { was: "8011O", before: "ZZ ", after: "", rule: "zip-digit" } })])).toEqual([]);
    expect(pendingFor(item(), [correct({ anchor: { ...anchor, text_sha: "ffff" } })])).toEqual([]);
    expect(pendingFor(item(), [correct({ status: "applied", pending: false })])).toEqual([]);
  });
  test("a dismissal matches by finding fingerprint", () => {
    const d = { ...common, kind: "dismiss", findings: ["f2"] } as DecisionView;
    expect(pendingFor(item(), [d]).length).toBe(1);
    expect(pendingFor(item(), [{ ...d, findings: ["zz"] } as DecisionView])).toEqual([]);
  });
  test("an alt matches a figure by its anchor", () => {
    const fig = { ...anchor, text_sha: "", crop_sha: "fedcba9876543210" };
    const d = { ...common, kind: "alt", text: "A map", decorative: false, anchor: fig } as DecisionView;
    expect(pendingFor(item({ role: "figure", anchor: fig, suggestions: undefined }), [d]).length).toBe(1);
    expect(pendingFor(item(), [d])).toEqual([]);
  });
});

describe("what is still open on an item with a pending decision", () => {
  const two = item({ suggestions: [sug, { ...sug, was: "Ge neral", now: "General", rule: "split-word", id: "other-id", before: "x", after: "y" }] });
  test("one suggestion's decision settles only that suggestion", () => {
    const w = pendingFor(two, [correct()]);
    expect(isSettled(w, two.suggestions![0]!)).toBe(true);
    expect(isSettled(w, two.suggestions![1]!)).toBe(false);
  });
  test("a dismissed finding leaves the item's other warnings to dismiss", () => {
    const d = { ...common, kind: "dismiss", findings: ["f2"] } as DecisionView;
    expect(openFindings(item(), pendingFor(item(), [d])).map((f) => f.id)).toEqual(["f1"]);
    expect(openFindings(item(), []).map((f) => f.id)).toEqual(["f1", "f2"]);
  });
  test("an alt decision is its own state", () => {
    const fig = { ...anchor, text_sha: "", crop_sha: "fedcba9876543210" };
    const d = { ...common, kind: "alt", text: "A map", decorative: false, anchor: fig } as DecisionView;
    expect(hasAltDecision(pendingFor(item({ role: "figure", anchor: fig, suggestions: undefined }), [d]))).toBe(true);
    expect(hasAltDecision(pendingFor(item(), [correct()]))).toBe(false);
  });
});

describe("findings", () => {
  test("warnings can be dismissed, errors and decision warnings cannot", () => {
    const it = item({ findings: [
      { code: "low-confidence", severity: "warning", id: "a" }, { code: "alt-missing", severity: "error", id: "b" },
      { code: "decision-orphaned", severity: "warning", id: "c" }] });
    expect(dismissible(it).map((f) => f.id)).toEqual(["a"]);
    expect(hasError(it)).toBe(true);
    expect(hasError(item())).toBe(false);
  });
});

describe("sentences", () => {
  test("statuses are words", () => {
    expect(statusWords(correct())).toBe("Waiting for the next build");
    expect(statusWords(correct({ status: "applied", pending: false }))).toBe("Applied by the current build");
    expect(statusWords(correct({ status: "orphaned", reason: "range-missing" }))).toContain("the words are not there any more");
    expect(statusWords(correct({ status: "undone", pending: true }))).toBe("Undone; takes effect at the next build");
  });
  test("a decision and a decided item", () => {
    expect(describeDecision(correct())).toBe("Corrected ‘8011O’ to ‘80110’");
    expect(decidedWords(correct({ kind: "keep" }))).toBe("Decided: kept OCR’s reading");
  });
});

describe("facts and orphans", () => {
  test("the decision a reviewed fact records", () => {
    expect(decisionOfFact({ code: "reviewed-correction", data: { decision: "d1" } })).toBe("d1");
    expect(decisionOfFact({ code: "ocr-suggestion", data: { decision: "d1" } })).toBeNull();
    expect(decisionOfFact({ code: "reviewed-alt" })).toBeNull();
  });
  test("an orphan item names its decision", () => {
    expect(orphanId(item({ id: "decision:abc", role: "decision" }))).toBe("abc");
    expect(orphanId(item())).toBeNull();
  });
  test("actions", () => {
    expect(actionsOf(correct({ status: "applied", pending: false }))).toEqual({ undo: true, change: true });
    expect(actionsOf(correct())).toEqual({ undo: true, change: false });
    expect(actionsOf(correct({ status: "undone" }))).toEqual({ undo: false, change: false });
  });
});

describe("bodies", () => {
  test("the wire shapes", () => {
    expect(bodies.accept("r", "n-1", "s")).toEqual({ rev: "r", item: "n-1", choice: "accept", suggestion: "s" });
    expect(bodies.alt("r", "n-1", { text: "x", decorative: false }, "o")).toEqual({ rev: "r", item: "n-1", choice: "alt", alt: { text: "x", decorative: false }, supersedes: "o" });
    expect(bodies.undo("d")).toEqual({ choice: "undo", of: "d" });
    expect(bodies.alt("r", "n-1", { text: "x", decorative: false }, undefined, "n-1")).toMatchObject({ node: "n-1" });
    expect(bodies.changeOther("d", "x")).toEqual({ choice: "other", supersedes: "d", now: "x" });
  });
});

describe("send", () => {
  const reply = (status: number, body: unknown): typeof fetch => (async () => new Response(JSON.stringify(body), { status })) as unknown as typeof fetch;
  test("201 is the saved sentence", async () => {
    expect(await send("fw9", bodies.undo("d"), reply(201, { decision: "x", pending: 2 }))).toEqual({ ok: true, message: SAVED, decision: "x", pending: 2 });
  });
  test("409 keeps the server's sentence and asks for a reload", async () => {
    const out = await send("fw9", bodies.undo("d"), reply(409, { error: "This page shows an older build. Reload to decide on the current one.", reload: true }));
    expect(out).toEqual({ ok: false, message: "This page shows an older build. Reload to decide on the current one.", reload: true });
  });
  test("a 400 shows the reason without a reload", async () => {
    expect(await send("fw9", bodies.undo("d"), reply(400, { error: "now is too long" }))).toEqual({ ok: false, message: "now is too long", reload: false });
  });
  test("a network failure is a sentence", async () => {
    const out = await send("fw9", bodies.undo("d"), (async () => { throw new Error("x"); }) as unknown as typeof fetch);
    expect(out.ok).toBe(false);
  });
});
