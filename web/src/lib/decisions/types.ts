// The decision store's types (docs/review-decisions.md): the records, the snapshot of the file,
// the fold and the ledger. Every line of a store is parsed once, in parse.ts, into these. Nothing
// past the parser touches a raw JSON value. They mirror src/reflow/analyze/decisions.py.

export const VERSION = 1;
export const MAX_LINE = 8 * 1024;
export const NO_HEADER = "no-header";

export type Box = [number, number, number, number];

/** Copied from a published review item or suggestion, never made up. Field names are the wire's. */
export type Anchor = { page: number; bbox: Box; text_sha: string; crop_sha: string; hint: string };

/** OCR's reading and up to 20 characters of the node's text on each side. No offsets. */
export type Range = { was: string; before: string; after: string; rule: string };

type Base = { id: string; at: string; pdf: string };

export type Correct = Base & { kind: "correct"; anchor: Anchor; range: Range; now: string; supersedes: string | null };
export type Keep = Base & { kind: "keep"; anchor: Anchor; range: Range; supersedes: string | null };
export type AltText = Base & { kind: "alt"; anchor: Anchor; text: string; decorative: boolean; supersedes: string | null };
export type Dismiss = Base & { kind: "dismiss"; findings: string[] };
export type Undo = Base & { kind: "undo"; of: string };
export type Reattach = Base & { kind: "reattach"; from: string; to: string };
type ProposalVerdict = Base & { kind: "proposal"; proposal: string; verdict: "accept" | "reject"; text: string };
/** A reviewer's answer to a font-map item: reject puts the text layer's reading back. */
type FontMapVerdict = Base & { kind: "font-map"; map: string; verdict: "accept" | "reject" };

export type DecisionRecord = Correct | Keep | AltText | Dismiss | ProposalVerdict | FontMapVerdict;
export type StoreRecord = DecisionRecord | Undo | Reattach;
type RecordKind = StoreRecord["kind"];

export const KINDS: readonly RecordKind[] = ["correct", "keep", "alt", "dismiss", "undo", "reattach", "proposal", "font-map"];

/** What one line after the header parses to. An unknown `v` or `kind` is unsupported (kept,
 *  listed, never applied); a known kind with a wrong field is invalid. */
export type Parsed =
  | { type: "record"; record: StoreRecord }
  | { type: "unsupported"; id: string | null; v: unknown; kind: unknown }
  | { type: "invalid"; id: string | null; reason: string };

export type Header = { id: string; slug: string; created: string };

/** A parsed line, where its line starts (a byte offset in the file) and its number (the header is 1). */
export type Entry = { offset: number; line: number; parsed: Parsed };

/** The one read of a store. `store` is the header's id (null without a valid header), `bytes` the
 *  offset after the last complete line, `lines` the complete lines. `entries` are the lines after
 *  the header, empty when there is an error. */
export type Snapshot = {
  store: string | null;
  bytes: number;
  lines: number;
  torn: 0 | 1;
  /** Byte offset of each complete line that is not a JSON object. */
  unreadable: number[];
  entries: Entry[];
  error: typeof NO_HEADER | null;
  slug: string | null;
};

export type InvalidRecord = { id: string | null; reason: string };
export type UnsupportedRecord = { id: string | null; v: unknown; kind: unknown };

export type Fold = {
  /** The active decisions, in store order. */
  active: DecisionRecord[];
  reattach: Reattach[];
  /** Record lines read: duplicates, invalid and unsupported too. */
  records: number;
  torn: number;
  unreadable: number;
  duplicate: number;
  invalid: InvalidRecord[];
  unsupported: UnsupportedRecord[];
  error: typeof NO_HEADER | null;
  store: string | null;
  /** The ids of the decisions an `undo` took off (not in the converter's fold; the web needs it). */
  undone: string[];
};

// -- the published ledger (decisions.json) ---------------------------------------------------------

type LedgerStore = {
  id: string | null; bytes: number; lines: number; torn: number; unreadable: number; duplicate: number;
  invalid: { id: string | null; reason: string }[];
  unsupported: { id: string | null; v: unknown; kind: unknown }[];
  error: string | null;
};
export type LedgerApplied = { id: string; kind: string; page: number; bbox: number[]; node: string; was: string; now: string; tier: string; title_unchanged: boolean };
export type LedgerOrphan = { id: string; kind: string; page: number | null; bbox: number[] | null; reason: string; was: string; now: string; text_now: string };
export type Ledger = {
  v: 1;
  source: { sha256: string };
  store: LedgerStore;
  applied: LedgerApplied[];
  satisfied: { id: string; page: number; node: string; now: string }[];
  dismissed: { id: string; page: number | null; code: string; finding: string }[];
  refused: { id: string; finding: string; reason: string }[];
  orphaned: LedgerOrphan[];
  stale: { id: string; kind: string; finding: string }[];
  proposals: { id: string; proposal: string; verdict: string }[];
  /** Only in a build that applied a font-map answer. */
  fonts?: { id: string; map: string; verdict: string; nodes: number }[];
};

/** The wire form of a record, for the store file: the shape docs/review-decisions.md gives. */
export function toWire(r: StoreRecord): Record<string, unknown> {
  const base = { v: VERSION, id: r.id, at: r.at, pdf: r.pdf, kind: r.kind };
  const sup = (x: { supersedes: string | null }) => (x.supersedes === null ? {} : { supersedes: x.supersedes });
  switch (r.kind) {
    case "correct": return { ...base, target: { anchor: r.anchor, range: r.range }, now: r.now, ...sup(r) };
    case "keep": return { ...base, target: { anchor: r.anchor, range: r.range }, ...sup(r) };
    case "alt": return { ...base, target: { anchor: r.anchor }, alt: { text: r.text, decorative: r.decorative }, ...sup(r) };
    case "dismiss": return { ...base, findings: r.findings };
    case "undo": return { ...base, of: r.of };
    case "reattach": return { ...base, from: r.from, to: r.to };
    case "proposal": return { ...base, proposal: r.proposal, verdict: r.verdict, text: r.text };
    case "font-map": return { ...base, map: r.map, verdict: r.verdict };
  }
}
