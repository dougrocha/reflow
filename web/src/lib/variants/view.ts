import type { JobRecord } from "@/lib/builds/view";
import { DEFAULT_PIPELINE, HAIKU, PIPELINES, ollamaPipeline, variantKey, variantOf, type Pipeline } from "@/pipelines";

// A document's variants (src/pipelines.ts) as the build dropdown shows them: which pipelines are
// built for it, being built, or can be built.

export type VariantState = "built" | "stale" | "queued" | "running" | "failed" | "not built";

export type VariantView = {
  name: string;
  label: string;
  where: Pipeline["where"];
  key: string;
  state: VariantState;
  /** What the pipeline turns on, for the dropdown: "Docling layout", "Labels, headings, scanned fields, alt text". */
  steps: string[];
  /** What a run sends to the model, which the confirmation of an API run spells out: every page
   *  to transcribe, every page with reflow's transcript to polish, or crops of what the rules are
   *  unsure of. */
  sends: "pages" | "pages-and-transcript" | "crops";
  /** Whether an assist step is on, so the run asks the model about fields, headings or scanned fields. */
  asks: boolean;
  /** The variant's latest job while it is queued or running, or the one that failed. */
  job: Pick<JobRecord, "id" | "state" | "stage" | "done" | "total" | "error"> | null;
};

/** A variant's tab: the built variants of a document, for its tab strip. */
export type VariantTab = Pick<VariantView, "name" | "label" | "key">;

export type VariantsAnswer = {
  slug: string;
  /** Whether this server builds (the builds gate): only then are unbuilt pipelines listed. */
  builds: boolean;
  variants: VariantView[];
};

/** A variant's state from its newest job (if any) and whether it is published. A failed job shows
 *  only while nothing is published. */
export function variantState(job: Pick<JobRecord, "state"> | undefined, built: boolean): VariantState {
  if (job?.state === "queued" || job?.state === "running") return job.state;
  if (built) return "built";
  return job?.state === "failed" ? "failed" : "not built";
}

/** The view of every variant of `slug`: the built ones, the ones in the builds queue, and (with
 *  `offer`) every pipeline it could be built with, `models` naming the Ollama ones. */
export function variantViews(slug: string, built: Set<string>, jobs: JobRecord[], offer: boolean, models: string[] = []): VariantView[] {
  const pipelines = new Map<string, Pipeline>();
  const add = (p: Pipeline | null) => { if (p && !pipelines.has(p.name)) pipelines.set(p.name, p); };
  const known = (key: string) => {
    const v = variantOf(key);
    if (v && v.doc.slug === slug) add(v.pipeline);
  };
  for (const key of built) known(key);
  for (const job of jobs) known(job.slug);
  if (offer) {
    PIPELINES.forEach(add);
    models.forEach((m) => add(ollamaPipeline(m)));
  }
  const latest = new Map<string, JobRecord>(); // the newest job of each key
  for (const job of jobs) {
    const was = latest.get(job.slug);
    if (!was || job.enqueuedAt > was.enqueuedAt) latest.set(job.slug, job);
  }
  return [...pipelines.values()].map((p) => {
    const key = variantKey(slug, p.name);
    const job = latest.get(key);
    const state = variantState(job, built.has(key));
    const shown = state === "queued" || state === "running" || state === "failed" ? job : undefined;
    return {
      name: p.name, label: p.label, where: p.where, key, state, steps: pipelineSteps(p), ...sendsOf(p),
      job: shown ? { id: shown.id, state: shown.state, stage: shown.stage, done: shown.done, total: shown.total, error: shown.error } : null,
    };
  }).filter((v) => offer || v.state !== "not built")
    .sort((a, b) => Number(b.name === DEFAULT_PIPELINE) - Number(a.name === DEFAULT_PIPELINE));
}

const sendsOf = ({ options: o }: Pipeline): Pick<VariantView, "sends" | "asks"> => ({
  sends: o.engine === "direct" ? "pages" : o.polish ? "pages-and-transcript" : "crops",
  asks: Boolean(o.assist_labels || o.assist_structure || o.assist_scan_fields),
});

export function pipelineSteps(p: Pipeline): string[] {
  const o = p.options;
  if (o.engine === "direct") {
    const prompt = o.direct_prompt && o.direct_prompt !== "base" ? ` (prompt: ${o.direct_prompt})` : "";
    return [`${o.direct_model ?? HAIKU} transcribes every page, with no reflow rules${prompt}`];
  }
  const steps = [o.engine === "docling" ? "Docling layout" : o.engine === "hybrid" ? "Hybrid layout (heuristics with Docling)" : "Reflow heuristics"];
  const asked = [o.assist_labels && "field labels", o.assist_structure && "headings", o.assist_scan_fields && "scanned-form fields"].filter(Boolean);
  if (asked.length) steps.push(`${o.assist_model ?? "the default model"} picks ${asked.join(", ")}`);
  if (o.polish) steps.push(`${o.polish_model ?? HAIKU} polishes the structure (headings, lists, tables, order, form groups)`);
  if (o.alt_text && o.alt_text !== "none") steps.push(`${o.alt_text_model ?? o.alt_text} drafts alt text`);
  return steps;
}
