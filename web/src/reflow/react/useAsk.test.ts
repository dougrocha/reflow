import { describe, expect, test } from "bun:test";
import type { AskResult } from "@/lib/ask-contract";
import { askReducer, escapeAction, submitAction, type AskState } from "./useAsk";

const idle: AskState = { status: "idle" };
const res: AskResult = { kind: "unavailable", why: "stale" };

describe("askReducer", () => {
  test("submit, then result", () => {
    const asking = askReducer(idle, { type: "submit", question: "q" });
    expect(asking).toEqual({ status: "asking", question: "q" });
    expect(askReducer(asking, { type: "result", result: res })).toEqual({ status: "done", question: "q", result: res });
  });
  test("a new submit replaces a finished answer", () => {
    const done: AskState = { status: "done", question: "a", result: res };
    expect(askReducer(done, { type: "submit", question: "b" })).toEqual({ status: "asking", question: "b" });
  });
  test("a late result after Clear is dropped", () => {
    expect(askReducer(idle, { type: "result", result: res })).toBe(idle);
  });
  test("clear returns to idle from any state", () => {
    expect(askReducer({ status: "asking", question: "q" }, { type: "clear" })).toEqual(idle);
    expect(askReducer({ status: "done", question: "q", result: res }, { type: "clear" })).toEqual(idle);
  });
});

describe("submitAction", () => {
  const asking: AskState = { status: "asking", question: "what is a fee" };
  const done: AskState = { status: "done", question: "what is a fee", result: res };
  test("an empty box asks nothing", () => {
    expect(submitAction(idle, "  ")).toBe("ignore");
    expect(submitAction(asking, "")).toBe("ignore");
  });
  test("the question already in flight is not sent twice, whatever the spacing", () => {
    expect(submitAction(asking, "what is a fee")).toBe("ignore");
    expect(submitAction(asking, "  what is a fee ")).toBe("ignore");
  });
  test("a different question while one is in flight replaces it", () => {
    expect(submitAction(asking, "what is a deposit")).toBe("ask");
  });
  test("idle and finished states ask; a finished question may be asked again", () => {
    expect(submitAction(idle, "q")).toBe("ask");
    expect(submitAction(done, "what is a fee")).toBe("ask");
  });
});

describe("escapeAction", () => {
  test("while a question is in flight Escape cancels it and leaves the box alone", () => {
    expect(escapeAction(true, "q")).toBe("cancel");
    expect(escapeAction(true, "")).toBe("cancel");
  });
  test("otherwise it clears a non-empty box, then closes the band", () => {
    expect(escapeAction(false, "text")).toBe("clear-box");
    expect(escapeAction(false, "")).toBe("close");
  });
});
