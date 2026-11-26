import { fetchJson, type Answer } from "../fetch-json";
import { plural } from "../format";
import type { ReviewFinding, ReviewItem, OcrSuggestion } from "../review";
import type { DecisionView } from "./status";
import type { Anchor } from "./types";

// What the browser knows about decisions: the sentences it shows, which published item a stored
// decision belongs to, and one function that sends a choice. Plain functions, no React and no
// server imports, so both sides and the tests use them. The server builds every record from the
// published files; the client only says what it saw (`rev`, the item, the suggestion or finding)
// and what it chose.

export const SAVED = "Saved. Takes effect when you rebuild.";
export const ERRORS_NOT_DISMISSED = "Errors are fixed, not dismissed";

/** The GET /api/decisions/<slug> answer, as far as the page reads it. */
export type DecisionsAnswer = {
  slug: string;
  rev: string | null;
  generation: number;
  source: { sha256: string } | null;
  pending: number;
  decisions: DecisionView[];
};

// -- sentences -------------------------------------------------------------------------------------

/** How many of the listed decisions change the next build (the server's `pending` counts lines, an undo included). */
export function changesNextBuild(decisions: DecisionView[]): number {
  return decisions.filter((d) => d.pending).length;
}

/** "3 decisions will apply at the next build", or null for none. */
export function pendingLine(n: number): string | null {
  if (n <= 0) return null;
  return `${plural(n, "decision")} will apply at the next build`;
}

const REASONS: Record<string, string> = {
  "other-pdf": "it was made on another PDF",
  "no-node": "the block it named is gone",
  ambiguous: "more than one place matches",
  "range-missing": "the words are not there any more",
  "not-flagged": "the page no longer flags it",
  conflict: "it conflicts with another decision",
  "not-ocr": "the text is not from OCR any more",
  "invalid-now": "the new reading cannot be used",
  "figure-changed": "the figure changed",
  "refused-error": "an error cannot be dismissed",
};

export function describeDecision(d: DecisionView): string {
  switch (d.kind) {
    case "correct": return `Corrected ‘${d.was}’ to ‘${d.now}’`;
    case "keep": return `Kept OCR’s reading ‘${d.was}’`;
    case "alt": return d.decorative ? "Marked a figure decorative" : `Alt text: “${d.text}”`;
    case "dismiss": return d.findings.length === 1 ? "Dismissed a warning" : `Dismissed ${d.findings.length} warnings`;
    case "proposal": return `${d.verdict === "accept" ? "Accepted" : "Rejected"} a proposal`;
    case "font-map": return d.verdict === "accept" ? "Accepted a decoded font" : "Rejected a decoded font";
  }
}

/** Where a decision stands for the published build, in words (never colour alone). */
export function statusWords(d: DecisionView): string {
  const why = d.reason ? ` because ${REASONS[d.reason] ?? d.reason}` : "";
  switch (d.status) {
    case "pending": return "Waiting for the next build";
    case "applied": return d.pending ? "Applied by the current build; a later change waits for the next build" : "Applied by the current build";
    case "satisfied": return "Already true in the current build";
    case "dismissed": return "Warning dismissed in the current build";
    case "orphaned": return `Not applied: it no longer matches the page${why}`;
    case "stale": return "Its warning is gone from the current build";
    case "refused": return `Refused${why}`;
    case "undone": return d.pending ? "Undone; takes effect at the next build" : "Undone";
  }
}

/** "Decided: kept OCR’s reading", for an item whose decision waits for the next build. */
export function decidedWords(d: DecisionView): string {
  switch (d.kind) {
    case "correct": return `Decided: corrected to ‘${d.now}’`;
    case "keep": return "Decided: kept OCR’s reading";
    case "alt": return d.decorative ? "Decided: marked decorative" : "Decided: alt text written";
    case "dismiss": return "Decided: warning dismissed";
    case "proposal": return d.verdict === "accept" ? "Decided: proposal accepted" : "Decided: proposal rejected";
    case "font-map": return d.verdict === "accept" ? "Decided: decoded font accepted" : "Decided: decoded font rejected";
  }
}

// -- matching --------------------------------------------------------------------------------------

const sameAnchor = (a: Anchor, b: { page: number; bbox: number[]; text_sha: string; crop_sha: string; hint: string } | null | undefined): boolean =>
  !!b && a.page === b.page && a.text_sha === b.text_sha && a.crop_sha === b.crop_sha && a.hint === b.hint
  && a.bbox.length === b.bbox.length && a.bbox.every((v, i) => v === b.bbox[i]);

/** Whether a stored correction or keep is about this suggestion: its anchor and the words it was
 *  recorded against. (The suggestion's id also hashes the proposed `now`, which a keep does not hold.) */
function aboutSuggestion(d: DecisionView, s: OcrSuggestion): boolean {
  if (d.kind !== "correct" && d.kind !== "keep") return false;
  return sameAnchor(d.anchor, s.anchor) && d.range.was === s.was && d.range.before === (s.before ?? "")
    && d.range.after === (s.after ?? "") && d.range.rule === s.rule;
}

/** The warnings of an item a reviewer may dismiss: not errors, not the `decision-*` ones. */
export function dismissible(item: ReviewItem): ReviewFinding[] {
  return (item.findings ?? []).filter((f) => f.severity === "warning" && !f.code.startsWith("decision"));
}

/** Whether the item has an error finding, which is fixed and never dismissed. */
export function hasError(item: ReviewItem): boolean {
  return (item.findings ?? []).some((f) => f.severity === "error") || (!item.findings?.length && item.severity === "error");
}

/** The decisions waiting for the next build that are about this queue item: a correction or keep
 *  of one of its suggestions, an alt text on its figure, or a dismissal of one of its findings. */
export function pendingFor(item: ReviewItem, decisions: DecisionView[]): DecisionView[] {
  const fingerprints = new Set((item.findings ?? []).map((f) => f.id));
  return decisions.filter((d) => {
    if (d.status !== "pending") return false;
    switch (d.kind) {
      case "correct":
      case "keep": return (item.suggestions ?? []).some((s) => aboutSuggestion(d, s));
      case "alt": return sameAnchor(d.anchor, item.anchor);
      case "dismiss": return d.findings.some((f) => fingerprints.has(f));
      default: return false;
    }
  });
}

/** Whether a pending decision is about this suggestion: only that suggestion's controls are done. */
export function isSettled(waiting: DecisionView[], s: OcrSuggestion): boolean {
  return waiting.some((w) => aboutSuggestion(w, s));
}

export function hasAltDecision(waiting: DecisionView[]): boolean {
  return waiting.some((w) => w.kind === "alt");
}

/** The warnings of the item that are still open to dismiss: none a pending dismissal names. */
export function openFindings(item: ReviewItem, waiting: DecisionView[]): ReviewFinding[] {
  const done = new Set(waiting.flatMap((w) => (w.kind === "dismiss" ? w.findings : [])));
  return dismissible(item).filter((f) => !done.has(f.id));
}

/** The id of the decision a node's `reviewed-*` fact records, or null. */
export function decisionOfFact(fact: { code: string; data?: Record<string, unknown> }): string | null {
  if (!["reviewed-correction", "reviewed-kept", "reviewed-alt"].includes(fact.code)) return null;
  const id = fact.data?.decision;
  return typeof id === "string" && id !== "" ? id : null;
}

/** The id of the decision an orphaned-decision item (`decision:<id>`) is about. */
export function orphanId(item: ReviewItem): string | null {
  return item.role === "decision" && item.id.startsWith("decision:") && item.id.length > 9 ? item.id.slice(9) : null;
}

/** What a decision's actions are: Change for an applied correction, keep or alt text; Undo while it is not undone. */
export function actionsOf(d: DecisionView): { undo: boolean; change: boolean } {
  const live = d.status !== "undone";
  return { undo: live && d.kind !== "proposal", change: live && d.status === "applied" && (d.kind === "correct" || d.kind === "keep" || d.kind === "alt") };
}

// -- the choices -----------------------------------------------------------------------------------

export type Body = Record<string, unknown>;

export const bodies = {
  accept: (rev: string, item: string, suggestion: string, supersedes?: string): Body => ({ rev, item, choice: "accept", suggestion, ...(supersedes ? { supersedes } : {}) }),
  keep: (rev: string, item: string, suggestion: string, supersedes?: string): Body => ({ rev, item, choice: "keep", suggestion, ...(supersedes ? { supersedes } : {}) }),
  other: (rev: string, item: string, suggestion: string, now: string, supersedes?: string): Body => ({ rev, item, choice: "other", suggestion, now, ...(supersedes ? { supersedes } : {}) }),
  /** `node`: the block being looked at, so the server can refuse a figure the item does not name. */
  alt: (rev: string, item: string, alt: { text: string; decorative: boolean }, supersedes?: string, node?: string): Body =>
    ({ rev, item, choice: "alt", alt, ...(supersedes ? { supersedes } : {}), ...(node ? { node } : {}) }),
  dismiss: (rev: string, item: string, findings: string[]): Body =>
    ({ rev, item, choice: "dismiss", ...(findings.length === 1 ? { finding: findings[0] } : { findings }) }),
  undo: (of: string): Body => ({ choice: "undo", of }),
  reattach: (rev: string, item: string): Body => ({ rev, item, choice: "reattach" }),
  changeKeep: (supersedes: string): Body => ({ choice: "keep", supersedes }),
  changeOther: (supersedes: string, now: string): Body => ({ choice: "other", supersedes, now }),
  changeAlt: (supersedes: string, alt: { text: string; decorative: boolean }): Body => ({ choice: "alt", supersedes, alt }),
};

export type Sent =
  | { ok: true; message: string; decision: string; pending: number }
  | { ok: false; message: string; reload: boolean };

/** Send one choice and say what happened in the sentence the reader hears. */
export async function send(slug: string, body: Body, doFetch: typeof fetch = fetch): Promise<Sent> {
  let res: Answer;
  try {
    res = await fetchJson(`/api/decisions/${slug}`, { body }, doFetch);
  } catch {
    return { ok: false, message: "The decision could not be sent. Check that the server is running, then try again.", reload: false };
  }
  const answer = res.body;
  if (res.status === 201 && typeof answer.decision === "string") {
    return { ok: true, message: SAVED, decision: answer.decision, pending: typeof answer.pending === "number" ? answer.pending : 0 };
  }
  const error = typeof answer.error === "string" ? answer.error : `The decision was not saved (${res.status}).`;
  return { ok: false, message: error, reload: answer.reload === true };
}
