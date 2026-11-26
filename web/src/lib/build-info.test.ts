import { describe, expect, test } from "bun:test";
import {
  answersLine, altTextLine, commitLabel, directCostLine, directPagesLine, dollarsLabel, ocrLine, pageRanges,
  parseBuildInfo, pdfMadeLine, perThousandLabel, perThousandPages, polishCostLine, polishLine, timingLine, timingsView, usedAi,
  type BuildInfo,
  type DirectRun, type PolishRun,
} from "./build-info";

const none: BuildInfo = {
  schema: 1, engine: "heuristic", ocr: { mode: "auto", runner: "ocrmypdf", pages: [] }, ai: [], altText: null,
  decisionsApplied: 0, converter: { commit: "8d36858b7fb8", edits: false }, built: "2026-11-04T20:14:00Z",
};

describe("build info", () => {
  test("only a record of this schema is shown", () => {
    expect(parseBuildInfo(none)).toEqual(none);
    expect(parseBuildInfo({ ...none, schema: 2 })).toBeNull();
    expect(parseBuildInfo(null)).toBeNull();
    expect(parseBuildInfo({ engine: "heuristic" })).toBeNull();
  });

  test("the tools that made the PDF: the producer, then the creator when it differs", () => {
    expect(pdfMadeLine({ producer: "Adobe PDF Library 17.0", creator: "Adobe InDesign 18.1 (Macintosh)" }))
      .toBe("Adobe PDF Library 17.0, from Adobe InDesign 18.1 (Macintosh)");
    expect(pdfMadeLine({ producer: "Designer 6.5", creator: "Designer 6.5" })).toBe("Designer 6.5");
    expect(pdfMadeLine({ producer: "Microsoft: Print To PDF", creator: null })).toBe("Microsoft: Print To PDF");
    expect(pdfMadeLine({ producer: null, creator: "Canva" })).toBe("Canva");
    expect(pdfMadeLine({ producer: " ", creator: null })).toBeNull();
    expect(pdfMadeLine(undefined)).toBeNull();
  });

  test("a build with no AI says so", () => {
    expect(usedAi(none)).toBe(false);
    expect(usedAi({ ...none, altText: { provider: "anthropic", model: "claude-haiku-5-5", described: 1, cached: 0 } })).toBe(true);
  });

  test("OCR names the pages it ran on, or why it did not run", () => {
    expect(pageRanges([1, 2, 3, 5, 7, 8])).toBe("1-3, 5, 7-8");
    expect(ocrLine({ mode: "auto", runner: "ocrmypdf", pages: [2] })).toBe("Ran on page 2 (ocrmypdf)");
    expect(ocrLine({ mode: "auto", runner: "ocrmypdf", pages: [1, 2] })).toBe("Ran on pages 1-2 (ocrmypdf)");
    expect(ocrLine(none.ocr)).toBe("Not needed: every page has a text layer");
    expect(ocrLine({ mode: "none", runner: "none", pages: [] })).toBe("Off");
  });

  test("an AI step gives its answers against the cap", () => {
    const step = { step: "Headings", provider: "anthropic", model: "claude-haiku-5-5", answers: 4, sent: 3, cap: 25, unasked: 0 };
    expect(answersLine(step)).toBe("4 answers; 3 questions sent of a cap of 25");
    expect(answersLine({ ...step, answers: 1, sent: 1, cap: 1, unasked: 2 })).toBe("1 answer; 1 question sent of a cap of 1; 2 left unasked at the cap");
    expect(answersLine({ step: "Page vision", provider: "ollama", model: "qwen3-vl:8b", cap: 100 })).toBe("Up to 100 pages");
  });

  test("a direct build gives its model's pages, tokens, dollars and seconds", () => {
    const run: DirectRun = {
      model: "claude-haiku-5-5", provider: "anthropic", pages: 6, cached: 2, unread: [], tokensIn: 33021, tokensOut: 15289,
      dollars: 0.0109, seconds: 15.66, spent: 0.007, stopped: false,
      perPage: [{ page: 1, tokensIn: 5500, tokensOut: 2500, dollars: 0.0018, seconds: 2.6, cached: true }],
    };
    expect(usedAi({ ...none, engine: "direct", direct: run })).toBe(true);
    expect(directPagesLine(run)).toBe("6 pages transcribed, 2 from the cache");
    expect(directPagesLine({ ...run, unread: [4, 5, 6], stopped: true })).toBe(
      "6 pages transcribed, 2 from the cache; 3 pages left unread (4-6) at the spending cap");
    expect(directCostLine(run)).toBe(
      "33,021 tokens in, 15,289 out; $0.0109 to transcribe, $0.0070 spent by this build; 15.7 s of model time");
    expect(directCostLine({ ...run, cached: 0, spent: 0.0109 })).toBe("33,021 tokens in, 15,289 out; $0.0109 to transcribe; 15.7 s of model time");
    expect(directCostLine({ ...run, cached: 0, dollars: 0, spent: 0.002 })).toBe(
      "33,021 tokens in, 15,289 out; $0 to transcribe, $0.0020 spent by this build; 15.7 s of model time");
    expect([dollarsLabel(0), dollarsLabel(1.234)]).toEqual(["$0", "$1.23"]);
  });

  test("a model run's headline is its cost per 1,000 pages, the document's exact cost small", () => {
    const run: DirectRun = {
      model: "claude-haiku-5-5", provider: "anthropic", pages: 64, cached: 64, unread: [], tokensIn: 400000, tokensOut: 80000,
      dollars: 0.08, seconds: 340, spent: 0, stopped: false, perPage: [],
    };
    expect(perThousandPages(run)).toBeCloseTo(1.25);
    expect(perThousandLabel(run)).toBe("about $1.25 per 1,000 pages");
    expect(perThousandLabel({ ...run, dollars: 0.004 })).toBe("about $0.063 per 1,000 pages");
    expect(perThousandLabel({ ...run, dollars: 0 })).toBe("$0 per 1,000 pages");
    expect(perThousandLabel({ ...run, pages: 0 })).toBe("$0 per 1,000 pages");
    const polish: PolishRun = { ...run, pages: 4, cached: 1, dollars: 0.0012, spent: 0.0009, applied: 7, rejected: 2 };
    expect(usedAi({ ...none, polish })).toBe(true);
    expect(polishLine(polish)).toBe("4 pages read, 1 from the cache; 7 edits applied, 2 rejected by the checks");
    expect(polishCostLine(polish)).toBe("400,000 tokens in, 80,000 out; $0.0012 for this document, $0.0009 spent by this build; 5 min 40 s of model time");
  });

  test("alt text and the converter commit", () => {
    expect(altTextLine({ provider: "anthropic", model: "claude-haiku-5-5", described: 3, cached: 1 })).toBe("4 figures described (1 from the previous build)");
    expect(commitLabel({ commit: "8d36858b7fb8", edits: true })).toBe("8d36858b7fb8+edits");
    expect(commitLabel({ commit: "8d36858b7fb8", edits: false })).toBe("8d36858b7fb8");
  });

  test("the time breakdown: total, stages largest first, the small ones summed as other", () => {
    expect(timingsView(undefined)).toBeNull();
    expect(timingsView({ seconds: {}, gpuWait: {} })).toBeNull();
    const view = timingsView({
      seconds: { extract: 1.2, docling: 14, publish: 0.04, render: 3.5, audit: 0.03, tiny: 0.1 },
      gpuWait: { docling: 9, render: 0.05 },
    });
    expect(view?.total).toBeCloseTo(18.87, 5);
    expect(view?.rows.map(timingLine)).toEqual([
      "docling 14 s (9 s waiting for the GPU)", "render 3.5 s", "extract 1.2 s", "tiny 0.1 s", "other 0.1 s",
    ]);
  });

  test("a stage is named even when it is the only one, and no other row appears without small stages", () => {
    const view = timingsView({ seconds: { extract: 2 }, gpuWait: {} });
    expect(view).toEqual({ total: 2, rows: [{ stage: "extract", seconds: 2, gpuWait: 0 }] });
    expect(timingLine({ stage: "docling", seconds: 75, gpuWait: 62 })).toBe("docling 1 min 15 s (1 min 2 s waiting for the GPU)");
    // Leftovers that round to 0 s are not listed.
    expect(timingsView({ seconds: { extract: 2, a: 0.02, b: 0.01 }, gpuWait: {} })?.rows.map(timingLine)).toEqual(["extract 2 s"]);
    // Every stage under the floor: the total says it all, and "other" would only repeat it.
    expect(timingsView({ seconds: { a: 0.02, b: 0.03 }, gpuWait: {} })).toEqual({ total: 0.05, rows: [] });
  });
});
