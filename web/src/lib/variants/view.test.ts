import { describe, expect, test } from "bun:test";
import { pipelineOf } from "@/pipelines";
import type { JobRecord } from "@/lib/builds/view";
import { pipelineSteps, variantViews } from "./view";

const job = (slug: string, state: JobRecord["state"], enqueuedAt: number, error: string | null = null) =>
  ({ id: `j${enqueuedAt}`, slug, state, enqueuedAt, stage: state === "running" ? "Reading pages" : null, done: null, total: null, error }) as JobRecord;

describe("a document's variants", () => {
  const built = new Set(["fw9", "fw9~haiku"]);

  test("without builds, only the built variants, the default first", () => {
    const views = variantViews("fw9", built, [], false);
    expect(views.map((v) => [v.name, v.state])).toEqual([["default", "built"], ["haiku", "built"]]);
  });

  test("with builds, every pipeline and each Ollama model, with the state of each", () => {
    const jobs = [job("fw9~docling", "running", 2), job("fw9~sonnet", "failed", 1, "boom"), job("fw9~opus", "queued", 3)];
    const views = variantViews("fw9", built, jobs, true, ["gemma4:e4b", "hf.co/x/y"]);
    const state = Object.fromEntries(views.map((v) => [v.name, v.state]));
    expect(state).toEqual({
      default: "built", haiku: "built", docling: "running", sonnet: "failed", opus: "queued",
      hybrid: "not built", "ollama-gemma4_e4b": "not built", "direct-haiku": "not built",
      "direct-sonnet": "not built", "direct-opus": "not built", "direct-glm-ocr": "not built",
      "polish-haiku": "not built", "direct-haiku-complete": "not built", "direct-haiku-forms": "not built",
      "direct-haiku-example": "not built",
    });
    expect(views[0].name).toBe("default");
    expect(views.find((v) => v.name === "sonnet")?.job?.error).toBe("boom");
    expect(views.find((v) => v.name === "docling")?.job?.stage).toBe("Reading pages");
    expect(views.find((v) => v.name === "ollama-gemma4_e4b")?.key).toBe("fw9~ollama-gemma4_e4b");
  });

  test("the newest job of a variant decides, and a failed rebuild of a built one keeps it built", () => {
    const views = variantViews("fw9", built, [job("fw9~haiku", "failed", 5, "boom"), job("fw9~hybrid", "failed", 1), job("fw9~hybrid", "done", 2)], true);
    expect(views.find((v) => v.name === "haiku")?.state).toBe("built");
    expect(views.find((v) => v.name === "hybrid")?.state).toBe("not built");
  });

  test("a built Ollama variant shows even when Ollama does not answer", () => {
    const views = variantViews("fw9", new Set(["fw9", "fw9~ollama-gemma4_e4b"]), [], false);
    expect(views.map((v) => [v.name, v.label])).toEqual([["default", "Default"], ["ollama-gemma4_e4b", "gemma4:e4b"]]);
  });
});

describe("the build dropdown", () => {
  test("what a run sends decides what its confirmation says", () => {
    const views = variantViews("fw9", new Set(), [], true);
    const by = Object.fromEntries(views.map((v) => [v.name, [v.sends, v.asks]]));
    expect(by).toMatchObject({
      haiku: ["crops", true], "direct-haiku": ["pages", false], "polish-haiku": ["pages-and-transcript", false], docling: ["crops", false],
    });
  });

  test("each pipeline says what it turns on", () => {
    expect(pipelineSteps(pipelineOf("default")!)).toEqual(["Reflow heuristics"]);
    expect(pipelineSteps(pipelineOf("docling")!)).toEqual(["Docling layout"]);
    expect(pipelineSteps(pipelineOf("haiku")!)).toEqual([
      "Reflow heuristics", "claude-haiku-5-5 picks field labels, headings, scanned-form fields", "claude-haiku-5-5 drafts alt text",
    ]);
  });
});
