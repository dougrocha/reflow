// "How this transcript was made": the public build record docs:build publishes beside a document
// (public/docs/<slug>/build-info.json, reflow.provenance.build_info), and the lines the viewer and
// the inspector show of it.

import { plural, secondsLabel } from "./format";

type BuildStep = {
  step: string;
  provider: string;
  /** The resolved model id, never "the default". */
  model: string;
  /** Questions the model answered (fresh or from the cache), questions sent this build, the cap on
   *  them, and those left unasked at the cap. Page vision has only the cap. */
  answers?: number;
  sent?: number;
  cap?: number;
  unasked?: number;
};

/** A direct build's model run (--engine direct, reflow.provenance.direct_run): a cached page keeps
 *  the tokens, dollars and seconds of the request that made it. */
export type DirectRun = {
  model: string;
  provider: string;
  /** The prompt's id, in the answer cache's key ("html-1", "html-forms-1"). Older builds have none. */
  prompt?: string | null;
  pages: number;
  cached: number;
  unread: number[];
  tokensIn: number;
  tokensOut: number;
  dollars: number;
  seconds: number;
  /** Dollars this build spent (cached pages cost nothing), and whether it stopped at its cap. */
  spent: number;
  stopped: boolean;
  perPage: { page: number; tokensIn: number; tokensOut: number; dollars: number; seconds: number; cached: boolean }[];
};

/** A polish run (--polish, reflow.provenance.model_run "polish"): the same numbers, and the
 *  edits it applied and the ones its checks rejected. */
export type PolishRun = DirectRun & { applied: number; rejected: number };

export type BuildInfo = {
  schema: 1;
  engine: string;
  ocr: { mode: string; runner: string | null; pages: number[] };
  ai: BuildStep[];
  altText: { provider: string; model: string; described: number; cached: number } | null;
  decisionsApplied: number;
  converter: { commit: string; edits: boolean } | null;
  /** ISO time, UTC. */
  built: string;
  direct?: DirectRun;
  polish?: PolishRun;
  /** The source PDF's /Producer and /Creator. Builds before 2026-11 did not record them. */
  pdf?: { producer: string | null; creator: string | null };
  /** Seconds each stage of the conversion took, in the converter's order, and the part of each spent
   *  waiting for the GPU lease. Builds before 2026-11 did not record them. */
  timings?: { seconds: Record<string, number>; gpuWait: Record<string, number> };
};

/** The record, when it is one this app can show; null otherwise. */
export function parseBuildInfo(raw: unknown): BuildInfo | null {
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Partial<BuildInfo>;
  if (r.schema !== 1 || typeof r.engine !== "string" || !Array.isArray(r.ai) || typeof r.built !== "string" || !r.ocr) return null;
  return r as BuildInfo;
}

const ENGINES: Record<string, string> = {
  heuristic: "Reflow heuristics",
  docling: "Docling (local ML layout model)",
  hybrid: "Hybrid: reflow heuristics with Docling",
  direct: "Direct: a model transcribes each page, with no reflow rules",
};
const PROVIDERS: Record<string, string> = {
  anthropic: "Anthropic API",
  ollama: "Ollama, on this machine",
  local: "on this machine",
};

export const engineLabel = (engine: string) => ENGINES[engine] ?? engine;
export const providerLabel = (provider: string) => PROVIDERS[provider] ?? provider;

/** "1, 3-5, 9" */
export function pageRanges(pages: number[]): string {
  const out: string[] = [];
  for (let i = 0; i < pages.length; i++) {
    let j = i;
    while (j + 1 < pages.length && pages[j + 1] === pages[j] + 1) j++;
    out.push(i === j ? `${pages[i]}` : `${pages[i]}-${pages[j]}`);
    i = j;
  }
  return out.join(", ");
}

export function ocrLine(ocr: BuildInfo["ocr"]): string {
  if (ocr.pages.length) {
    return `Ran on ${ocr.pages.length === 1 ? "page" : "pages"} ${pageRanges(ocr.pages)}${ocr.runner && ocr.runner !== "none" ? ` (${ocr.runner})` : ""}`;
  }
  return ocr.mode === "none" ? "Off" : "Not needed: every page has a text layer";
}

/** "12 answers; 8 questions sent of a cap of 25" (and the ones the cap left unasked). */
export function answersLine(step: BuildStep): string | null {
  if (step.answers === undefined) return step.cap !== undefined ? `Up to ${plural(step.cap, "page")}` : null;
  const parts = [plural(step.answers, "answer")];
  if (step.sent !== undefined && step.cap !== undefined) parts.push(`${plural(step.sent, "question")} sent of a cap of ${step.cap}`);
  if (step.unasked) parts.push(`${step.unasked} left unasked at the cap`);
  return parts.join("; ");
}

export function altTextLine(alt: NonNullable<BuildInfo["altText"]>): string {
  const cached = alt.cached ? ` (${alt.cached} from the previous build)` : "";
  return `${plural(alt.described + alt.cached, "figure")} described${cached}`;
}

/** "$0.0109", "$1.24", "$0": small sums keep four places. */
export const dollarsLabel = (n: number) => (n === 0 ? "$0" : n < 1 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`);

/** A run's dollars per 1,000 pages: what it paid for its pages (a cached page counts what its
 *  request cost), scaled to 1,000. 0 for a run that read no page. */
export const perThousandPages = (d: DirectRun) => (d.pages ? (d.dollars / d.pages) * 1000 : 0);

/** "about $1.30 per 1,000 pages": the headline cost of a model run. Under 10 cents keeps two
 *  significant digits, so a cheap run never reads as $0.00. */
export function perThousandLabel(d: DirectRun): string {
  const n = perThousandPages(d);
  if (n === 0) return "$0 per 1,000 pages";
  return `about $${n >= 0.1 ? n.toFixed(2) : n.toPrecision(2)} per 1,000 pages`;
}

/** "6 pages transcribed, 2 from the cache; 1 page left unread (4) at the spending cap" */
export function directPagesLine(d: DirectRun): string {
  const parts = [`${plural(d.pages, "page")} transcribed${d.cached ? `, ${d.cached} from the cache` : ""}`];
  if (d.unread.length) parts.push(`${plural(d.unread.length, "page")} left unread (${pageRanges(d.unread)})${d.stopped ? " at the spending cap" : ""}`);
  return parts.join("; ");
}

/** "33,021 tokens in, 15,289 out; $0.0109 to transcribe, $0.007 spent by this build; 15.7 s of
 *  model time": a cached page keeps what its request cost, but this build paid nothing for it, and
 *  a billed request that gave no page (a refusal) adds to what the build spent only. */
export const directCostLine = (d: DirectRun) =>
  `${d.tokensIn.toLocaleString("en-US")} tokens in, ${d.tokensOut.toLocaleString("en-US")} out; ${dollarsLabel(d.dollars)} to transcribe` +
  `${d.cached || Math.abs(d.spent - d.dollars) > 1e-9 ? `, ${dollarsLabel(d.spent)} spent by this build` : ""}; ${secondsLabel(d.seconds)} of model time`;

/** "8d36858b7fb8+edits" */
/** The tools that made the source PDF: "Adobe PDF Library 17.0, from Adobe InDesign 18.1", or
 *  null when the PDF names none (or the build did not record them). */
export function pdfMadeLine(pdf: BuildInfo["pdf"]): string | null {
  const producer = pdf?.producer?.trim() || "";
  const creator = pdf?.creator?.trim() || "";
  if (!producer || !creator || producer === creator) return producer || creator || null;
  return `${producer}, from ${creator}`;
}

export const commitLabel = (c: NonNullable<BuildInfo["converter"]>) => `${c.commit}${c.edits ? "+edits" : ""}`;

/** Whether any model took part: an AI step or alt text. */
export const usedAi = (info: BuildInfo) => info.ai.length > 0 || info.altText !== null || !!info.direct || !!info.polish;

/** "4 pages read, 1 from the cache; 7 edits applied, 2 rejected by the checks" */
export function polishLine(p: PolishRun): string {
  const parts = [`${plural(p.pages, "page")} read${p.cached ? `, ${p.cached} from the cache` : ""}`];
  if (p.unread.length) parts.push(`${plural(p.unread.length, "page")} left unpolished (${pageRanges(p.unread)})${p.stopped ? " at the spending cap" : ""}`);
  parts.push(`${plural(p.applied, "edit")} applied${p.rejected ? `, ${p.rejected} rejected by the checks` : ""}`);
  return parts.join("; ");
}

/** "33,021 tokens in, 15,289 out; $0.0109 for this document, $0.007 spent by this build; 15.7 s
 *  of model time": the exact numbers under a polish run's headline. */
export const polishCostLine = (d: DirectRun) => directCostLine(d).replace(" to transcribe", " for this document");

/** One line of the time breakdown: a stage, its seconds, and the part of them spent waiting for the
 *  GPU lease (0 when it did not wait, or waited under a tenth of a second). */
export type TimingRow = { stage: string; seconds: number; gpuWait: number };

/** Stages shorter than this are summed as "other", the floor `reflow convert-many` prints at too. */
export const STAGE_FLOOR = 0.1;

/** The build's total seconds and its stages, largest first: those of 0.1 s or more by name, the rest
 *  summed as "other" (last, whatever its size). No rows when no stage reached 0.1 s. Null when the
 *  build recorded no timings. */
export function timingsView(timings: BuildInfo["timings"]): { total: number; rows: TimingRow[] } | null {
  if (!timings) return null;
  const stages = Object.entries(timings.seconds ?? {}).filter(([, s]) => Number.isFinite(s) && s >= 0);
  if (!stages.length) return null;
  const waited = (stage: string) => {
    const w = timings.gpuWait?.[stage];
    return typeof w === "number" && w >= STAGE_FLOOR ? w : 0;
  };
  const rows = stages.filter(([, s]) => s >= STAGE_FLOOR).map(([stage, seconds]) => ({ stage, seconds, gpuWait: waited(stage) }));
  rows.sort((a, b) => b.seconds - a.seconds);
  const rest = stages.filter(([, s]) => s < STAGE_FLOOR).reduce((sum, [, s]) => sum + s, 0);
  // Alone it would repeat the total, and under 0.05 s it would read "0 s".
  if (rows.length && rest >= 0.05) rows.push({ stage: "other", seconds: rest, gpuWait: 0 });
  return { total: stages.reduce((sum, [, s]) => sum + s, 0), rows };
}

/** "docling 14 s (9 s waiting for the GPU)": the CLI's style for a stage. */
export const timingLine = (row: TimingRow) =>
  `${row.stage} ${secondsLabel(row.seconds)}${row.gpuWait ? ` (${secondsLabel(row.gpuWait)} waiting for the GPU)` : ""}`;
