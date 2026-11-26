// The ways a document can be built (pipelines), and its variants: one transcript of the document's
// PDF per pipeline. A document is one entry in the site (SAMPLE_DOCS); its variants live
// under its slug. The default variant is the document's own build (its slug); another variant's
// storage key is `<slug>~<pipeline>`, used wherever a build is stored or tracked: DATA_ROOT/<key>,
// public/docs/<key>, its provenance record, its build jobs and its decisions. Each variant keeps
// its options in its provenance, and its build info beside its transcript.

import { sampleDoc, type ConvertOptions, type SampleDoc } from "./docs";

export type Pipeline = {
  /** The variant's short name: [a-z0-9_-], unique per document. */
  name: string;
  label: string;
  /** Where it runs: nothing leaves the machine, or the Anthropic API (the operator's key). */
  where: "local" | "api";
  options: ConvertOptions;
};

/** The Claude models the API pipelines run on. */
export const HAIKU = "claude-haiku-5-5";
const SONNET = "claude-sonnet-5-5";
const OPUS = "claude-opus-5-5";

export const DEFAULT_PIPELINE = "default";
const KEY_SEPARATOR = "~";

/** The shape of a storage key. The last guard before a key is used in a file name, so it can never
 *  hold a separator or a dot. Whether it names a document is variantOf's to say. */
export const SLUG_RE = new RegExp(`^[a-z0-9][a-z0-9-]{0,79}(${KEY_SEPARATOR}[a-z0-9][a-z0-9._-]{0,79})?$`);

/** Throws on a key that is not a plain document name, so no path is built from anything else. */
export function assertSlug(slug: string): void {
  if (!SLUG_RE.test(slug)) throw new Error(`not a document slug: ${JSON.stringify(slug)}`);
}

/** Every assist step and the alt text, on one model through the Anthropic API. */
const anthropic = (model: string): ConvertOptions => ({
  alt_text: "anthropic", alt_text_model: model,
  assist_labels: true, assist_structure: true, assist_scan_fields: true,
  assist_provider: "anthropic", assist_model: model,
});

/** A model transcribes every page itself, with none of reflow's rules (docs/direct.md). */
const direct = (model: string, prompt?: string): ConvertOptions =>
  ({ engine: "direct", direct_model: model, ...(prompt ? { direct_prompt: prompt } : {}) });

/** The pipelines every document offers. Ollama ones are made per model (ollamaPipeline). */
export const PIPELINES: Pipeline[] = [
  { name: DEFAULT_PIPELINE, label: "Default", where: "local", options: {} },
  { name: "docling", label: "Docling", where: "local", options: { engine: "docling" } },
  { name: "hybrid", label: "Hybrid", where: "local", options: { engine: "hybrid" } },
  { name: "haiku", label: "Haiku", where: "api", options: anthropic(HAIKU) },
  { name: "sonnet", label: "Sonnet", where: "api", options: anthropic(SONNET) },
  { name: "opus", label: "Opus", where: "api", options: anthropic(OPUS) },
  { name: "direct-haiku", label: "Haiku, direct", where: "api", options: direct(HAIKU) },
  { name: "direct-sonnet", label: "Sonnet, direct", where: "api", options: direct(SONNET) },
  { name: "direct-opus", label: "Opus, direct", where: "api", options: direct(OPUS) },
  { name: "direct-glm-ocr", label: "GLM-OCR, direct", where: "local", options: direct("glm-ocr") },
  // The heuristics with a Haiku polish pass, and Haiku direct with each prompt variant.
  { name: "polish-haiku", label: "Heuristics + Haiku polish", where: "api", options: { polish: true, polish_model: HAIKU } },
  { name: "direct-haiku-complete", label: "Haiku, direct (every entry)", where: "api", options: direct(HAIKU, "complete") },
  { name: "direct-haiku-forms", label: "Haiku, direct (form rules)", where: "api", options: direct(HAIKU, "forms") },
  { name: "direct-haiku-example", label: "Haiku, direct (example)", where: "api", options: direct(HAIKU, "example") },
];

const OLLAMA = "ollama-";
const OLLAMA_MODEL = /^[a-z0-9][a-z0-9.-]*(:[a-z0-9][a-z0-9.-]*)?$/;

/** The pipeline that runs every assist step and the alt text on a local Ollama model (one from
 *  `ollama list`, such as gemma4:e4b), or null for a model id this naming cannot hold. */
export function ollamaPipeline(model: string): Pipeline | null {
  if (!OLLAMA_MODEL.test(model)) return null;
  return {
    name: OLLAMA + model.replace(":", "_"), label: model, where: "local",
    options: {
      alt_text: "ollama", alt_text_model: model,
      assist_labels: true, assist_structure: true, assist_scan_fields: true,
      assist_provider: "ollama", assist_model: model,
    },
  };
}

/** Whether `pipeline` is one of the fixed ones every document offers, not one made for an Ollama model. */
export const isFixedPipeline = (pipeline: Pipeline) => PIPELINES.includes(pipeline);

/** The pipeline of a variant name, or null when there is none. */
export function pipelineOf(name: string): Pipeline | null {
  const fixed = PIPELINES.find((p) => p.name === name);
  if (fixed) return fixed;
  if (!name.startsWith(OLLAMA)) return null;
  const model = name.slice(OLLAMA.length).replace("_", ":");
  const made = ollamaPipeline(model);
  return made?.name === name ? made : null;
}

/** The storage key of a document's variant: the slug for the default. */
export const variantKey = (slug: string, name: string = DEFAULT_PIPELINE) =>
  name === DEFAULT_PIPELINE ? slug : `${slug}${KEY_SEPARATOR}${name}`;

export type Variant = { key: string; doc: SampleDoc; pipeline: Pipeline };

/** Whether `key` is the key of a variant of the document `slug`, the default's included. */
export const isKeyOf = (slug: string, key: string) => key === slug || key.startsWith(slug + KEY_SEPARATOR);

/** The document and pipeline of a storage key, or null when it names no variant of a sample. */
export function variantOf(key: string): Variant | null {
  const at = key.indexOf(KEY_SEPARATOR);
  const slug = at < 0 ? key : key.slice(0, at);
  const name = at < 0 ? DEFAULT_PIPELINE : key.slice(at + 1);
  if (at >= 0 && name === DEFAULT_PIPELINE) return null; // the default's key is the slug alone
  const doc = sampleDoc(slug);
  const pipeline = pipelineOf(name);
  return doc && pipeline ? { key, doc, pipeline } : null;
}

/** The terminal command that builds the document or variant of `key`: `--model <id>` for an Ollama
 *  variant, `--variant <name>` for another, so no one has to type the `~` of a storage key. */
export function buildCommand(key: string): string {
  const v = variantOf(key);
  if (!v || v.pipeline.name === DEFAULT_PIPELINE) return `bun run docs:build ${key}`;
  const flag = isFixedPipeline(v.pipeline) ? `--variant ${v.pipeline.name}` : `--model ${v.pipeline.label}`;
  return `bun run docs:build ${flag} ${v.doc.slug}`;
}

export const isVariantKey = (key: string) => variantOf(key) !== null;

/** "Dental Claim Form (Haiku)" for a variant's key; the title alone for the default; null for no variant. */
export function variantTitle(key: string): string | null {
  const v = variantOf(key);
  if (!v) return null;
  return v.pipeline.name === DEFAULT_PIPELINE ? v.doc.title : `${v.doc.title} (${v.pipeline.label})`;
}

/** The options a variant converts with: the document's own, then its pipeline's. */
export const variantOptions = (v: Variant): ConvertOptions => ({ ...v.doc.options, ...v.pipeline.options });

/** The variant keys among folder names (of public/docs or DATA_ROOT): the default's are not here. */
export const variantKeysIn = (names: string[]) =>
  names.filter((n) => n.includes(KEY_SEPARATOR) && isVariantKey(n));
