import { describe, expect, test } from "bun:test";
import { SAMPLE_DOCS } from "./docs";
import {
  PIPELINES, buildCommand, isVariantKey, ollamaPipeline, pipelineOf, variantKey, variantKeysIn, variantOf, variantOptions,
} from "./pipelines";
import { buildDocOf, buildOptions, convertArgs, docOptions, provenanceSpec, reflowEnv, usesAnthropic } from "../scripts/freshness";

describe("variants of a document", () => {
  test("a PDF is one document; its variants are keyed by its slug and a pipeline", () => {
    expect(new Set(SAMPLE_DOCS.map((d) => d.source)).size).toBe(SAMPLE_DOCS.length);
    expect(variantKey("az-pharmacy-claim")).toBe("az-pharmacy-claim");
    expect(variantKey("az-pharmacy-claim", "haiku")).toBe("az-pharmacy-claim~haiku");
    const v = variantOf("az-pharmacy-claim~haiku")!;
    expect([v.doc.slug, v.pipeline.name]).toEqual(["az-pharmacy-claim", "haiku"]);
    expect(variantOf("az-pharmacy-claim")!.pipeline.name).toBe("default");
    for (const bad of ["az-pharmacy-claim~default", "az-pharmacy-claim~nope", "nope~haiku", "az-pharmacy-claim-haiku", "../fw9"]) {
      expect(isVariantKey(bad)).toBe(false);
    }
  });

  test("an Ollama pipeline is named by its model, and back", () => {
    const p = ollamaPipeline("gemma4:e4b")!;
    expect(p.name).toBe("ollama-gemma4_e4b");
    expect(pipelineOf(p.name)?.options.assist_model).toBe("gemma4:e4b");
    expect(pipelineOf("ollama-llama3.2-vision_11b")?.options.alt_text_model).toBe("llama3.2-vision:11b");
    expect(ollamaPipeline("hf.co/x/y")).toBeNull();
    expect(pipelineOf("ollama-")).toBeNull();
  });

  test("the build command names a variant by flag, never by its ~ key", () => {
    expect(buildCommand("fw9")).toBe("bun run docs:build fw9");
    expect(buildCommand("fw9~docling")).toBe("bun run docs:build --variant docling fw9");
    expect(buildCommand("fw9~ollama-gemma4_e4b")).toBe("bun run docs:build --model gemma4:e4b fw9");
  });

  test("only variant folders count as variant keys", () => {
    expect(variantKeysIn(["fw9", "fw9~haiku", "fw9~nope", "x~haiku", ".builds"])).toEqual(["fw9~haiku"]);
  });

  test("pipelines have unique names, and the API ones run every step on their model", () => {
    expect(new Set(PIPELINES.map((p) => p.name)).size).toBe(PIPELINES.length);
    for (const p of PIPELINES.filter((p) => p.where === "api")) expect(usesAnthropic(p.options)).toBe(true);
    for (const p of PIPELINES.filter((p) => p.where === "local")) expect(usesAnthropic(p.options)).toBe(false);
  });
});

describe("per-variant convert options", () => {
  test("a variant's options go over the build's, and become convert-many flags", () => {
    const own = docOptions(buildDocOf("az-pharmacy-claim~haiku")!, buildOptions({ REFLOW_ENGINE: "hybrid", REFLOW_ALT_TEXT: "local" } as unknown as NodeJS.ProcessEnv));
    expect(own.engine).toBe("hybrid");
    expect(own.alt_text).toBe("anthropic");
    expect(convertArgs(own)).toEqual([
      "--engine", "hybrid", "--alt-text", "anthropic", "--alt-text-model", "claude-haiku-5-5",
      "--assist-provider", "anthropic", "--assist-model", "claude-haiku-5-5",
      "--assist-labels", "--assist-structure", "--assist-scan-fields",
    ]);
    expect(convertArgs({})).toEqual([]);
    expect(variantOptions(variantOf("az-pharmacy-claim~docling")!)).toEqual({ engine: "docling" });
  });

  test("only a conversion that asks the Anthropic API needs the key", () => {
    expect(usesAnthropic({})).toBe(false);
    expect(usesAnthropic({ assist_provider: "anthropic" })).toBe(false); // no assist asked
    expect(usesAnthropic({ alt_text: "anthropic" })).toBe(true);
    expect(usesAnthropic({ assist_provider: "anthropic", assist_labels: true })).toBe(true);
    expect(usesAnthropic({ engine: "direct" })).toBe(true); // Haiku by default
    expect(usesAnthropic({ engine: "direct", direct_model: "glm-ocr" })).toBe(false);
  });

  test("a direct variant has a model transcribe every page", () => {
    const v = variantOf("fw9~direct-opus")!;
    expect(variantOptions(v)).toEqual({ engine: "direct", direct_model: "claude-opus-5-5" });
    expect(convertArgs(variantOptions(v))).toEqual(["--engine", "direct", "--direct-model", "claude-opus-5-5"]);
    expect(pipelineOf("direct-glm-ocr")?.where).toBe("local");
    for (const name of ["direct-haiku", "direct-sonnet", "direct-opus"]) expect(pipelineOf(name)?.where).toBe("api");
  });

  test("Haiku polish and each Haiku prompt variant are variants", () => {
    const polish = variantOptions(variantOf("fw9~polish-haiku")!);
    expect(polish).toEqual({ polish: true, polish_model: "claude-haiku-5-5" });
    expect(convertArgs(polish)).toEqual(["--polish-model", "claude-haiku-5-5", "--polish"]);
    expect(usesAnthropic(polish)).toBe(true);
    const forms = variantOptions(variantOf("fw9~direct-haiku-forms")!);
    expect(convertArgs(forms)).toEqual(["--engine", "direct", "--direct-model", "claude-haiku-5-5", "--direct-prompt", "forms"]);
    for (const name of ["direct-haiku-complete", "direct-haiku-example"]) expect(pipelineOf(name)?.options.direct_prompt).toBeTruthy();
  });

  test("no reflow process inherits ANTHROPIC_API_KEY, which Bun loads from .env", () => {
    const env = reflowEnv({ ANTHROPIC_API_KEY: "sk-test", PATH: "/bin" } as unknown as NodeJS.ProcessEnv);
    expect("ANTHROPIC_API_KEY" in env).toBe(false);
    expect(env.PATH).toBe("/bin");
  });

  test("the provenance spec carries a variant's options, its own folders and its document's PDF copy", () => {
    const spec = provenanceSpec(["az-pharmacy-claim", "az-pharmacy-claim~haiku"], { data: "/d", pdfs: "/p" }, {});
    const [plain, haiku] = spec.docs as { data: string; pdfCopy: string; options?: object }[];
    expect("options" in plain).toBe(false);
    expect(haiku.options).toEqual(PIPELINES.find((p) => p.name === "haiku")!.options);
    expect(haiku.data).toBe("/d/az-pharmacy-claim~haiku");
    expect(haiku.pdfCopy).toBe("/p/az-pharmacy-claim.pdf");
  });
});
