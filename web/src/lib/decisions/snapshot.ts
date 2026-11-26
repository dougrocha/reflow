import { isFiniteNumber, isRecord } from "../guards";
import { assertSlug } from "../../pipelines";
import type { Publications } from "../builds/record";
import { readDecisions, readManifest, readReview } from "../published";
import { HEX16, HEX64, readAnchor } from "./parse";
import type { Anchor, Ledger, LedgerApplied, LedgerOrphan } from "./types";

// One coherent read of what a document published (public/docs/<slug>/), the only thing a decision
// is built from. Not `readDocument`: that is a cached read that can lag the published files. The
// files are parsed here, once, into typed values; nothing past this module reads them raw.

/** Why a request cannot go on: the status, the sentence, and whether reloading the page helps. */
export type Conflict = { ok: false; status: 400 | 404 | 409; error: string; reload?: true };

export const conflict = (status: Conflict["status"], error: string, reload = false): Conflict =>
  reload ? { ok: false, status, error, reload: true } : { ok: false, status, error };

const BEING_PUBLISHED = "The document is being published. Reload the page.";
export const NEEDS_REBUILD = "Rebuild this document once before recording decisions.";
export const OLDER_BUILD = "This page shows an older build. Reload to decide on the current one.";

type PublishedFinding = { code: string; severity: "error" | "warning"; id: string };
type PublishedSuggestion = {
  was: string; now: string; rule: string; bbox: number[] | null; anchor: Anchor; before: string; after: string; id: string;
};
export type PublishedItem = {
  id: string;
  role: string;
  page: number | null;
  /** A node's or a figure's anchor; null for the items that have none (page, document, decision). */
  anchor: Anchor | null;
  findings: PublishedFinding[];
  suggestions: PublishedSuggestion[];
};

export type Published = {
  slug: string;
  /** The manifest's `v`: what the page rendered, as `images.version`. */
  rev: string;
  /** The publication generation both reads agreed on (0 without a build record). */
  generation: number;
  items: PublishedItem[];
  /** decisions.json; null when the build has none (built before decisions) or it is unusable. */
  ledger: Ledger | null;
};

type PublishedEnv = {
  /** public/docs. */
  docsRoot: string;
  /** What each document last published (BuildStore.publications). */
  publications: () => Promise<Publications>;
};

type Obj = Record<string, unknown>;
const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);

function finding(raw: unknown): PublishedFinding | null {
  if (!isRecord(raw) || typeof raw.code !== "string" || typeof raw.id !== "string" || !HEX16.test(raw.id)) return null;
  if (raw.severity !== "error" && raw.severity !== "warning") return null;
  return { code: raw.code, severity: raw.severity, id: raw.id };
}

function suggestion(raw: unknown): PublishedSuggestion | null {
  if (!isRecord(raw)) return null;
  const anchor = readAnchor(raw.anchor, true);
  const { was, now, rule, before, after, id } = raw;
  if (!anchor || typeof was !== "string" || was === "" || typeof now !== "string" || typeof rule !== "string") return null;
  if (typeof before !== "string" || typeof after !== "string" || typeof id !== "string" || !HEX16.test(id)) return null;
  const bbox = Array.isArray(raw.bbox) && raw.bbox.every((n) => typeof n === "number") ? (raw.bbox as number[]) : null;
  return { was, now, rule, bbox, anchor, before, after, id };
}

function item(raw: unknown): PublishedItem | null {
  if (!isRecord(raw) || typeof raw.id !== "string" || raw.id === "") return null;
  // A figure's anchor holds a crop hash, any other item's a text hash.
  const a = raw.anchor === undefined || raw.anchor === null ? null
    : (readAnchor(raw.anchor, false) ?? readAnchor(raw.anchor, true));
  const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
  return {
    id: raw.id, role: str(raw.role), page: typeof raw.page === "number" ? raw.page : null, anchor: a,
    findings: list(raw.findings).flatMap((f) => finding(f) ?? []),
    suggestions: list(raw.suggestions).flatMap((s) => suggestion(s) ?? []),
  };
}

/** The ledger, or null when it is not a usable version-1 ledger of a known PDF. Entries that are
 *  not well-formed are dropped. */
function parseLedger(raw: unknown): Ledger | null {
  if (!isRecord(raw) || raw.v !== 1 || !isRecord(raw.source) || typeof raw.source.sha256 !== "string" || !HEX64.test(raw.source.sha256)) return null;
  const rows = (key: string): Obj[] => {
    const v = raw[key];
    return Array.isArray(v) ? v.filter((r): r is Obj => isRecord(r) && typeof r.id === "string") : [];
  };
  const store = isRecord(raw.store) ? raw.store : {};
  const n = (v: unknown): number => (isFiniteNumber(v) ? v : 0);
  const page = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) ? v : null);
  const nums = (v: unknown): number[] | null => (Array.isArray(v) && v.every((x) => typeof x === "number") ? (v as number[]) : null);
  return {
    v: 1,
    source: { sha256: raw.source.sha256 },
    store: {
      id: typeof store.id === "string" ? store.id : null,
      bytes: n(store.bytes), lines: n(store.lines), torn: n(store.torn), unreadable: n(store.unreadable), duplicate: n(store.duplicate),
      invalid: (Array.isArray(store.invalid) ? store.invalid : []).filter(isRecord).map((i) => ({ id: typeof i.id === "string" ? i.id : null, reason: str(i.reason) })),
      unsupported: (Array.isArray(store.unsupported) ? store.unsupported : []).filter(isRecord).map((u) => ({ id: typeof u.id === "string" ? u.id : null, v: u.v, kind: u.kind })),
      error: typeof store.error === "string" ? store.error : null,
    },
    applied: rows("applied").map((r): LedgerApplied => ({
      id: str(r.id), kind: str(r.kind), page: n(r.page), bbox: nums(r.bbox) ?? [], node: str(r.node), was: str(r.was), now: str(r.now),
      tier: str(r.tier), title_unchanged: r.title_unchanged === true,
    })),
    satisfied: rows("satisfied").map((r) => ({ id: str(r.id), page: n(r.page), node: str(r.node), now: str(r.now) })),
    dismissed: rows("dismissed").map((r) => ({ id: str(r.id), page: page(r.page), code: str(r.code), finding: str(r.finding) })),
    refused: rows("refused").map((r) => ({ id: str(r.id), finding: str(r.finding), reason: str(r.reason) })),
    orphaned: rows("orphaned").map((r): LedgerOrphan => ({
      id: str(r.id), kind: str(r.kind), page: page(r.page), bbox: nums(r.bbox), reason: str(r.reason), was: str(r.was), now: str(r.now), text_now: str(r.text_now),
    })),
    stale: rows("stale").map((r) => ({ id: str(r.id), kind: str(r.kind), finding: str(r.finding) })),
    proposals: rows("proposals").map((r) => ({ id: str(r.id), proposal: str(r.proposal), verdict: str(r.verdict) })),
  };
}

/**
 * The published files of one document as they are now, read without the cache: the publication
 * record, then manifest.json, review.json and decisions.json, then the publication record again.
 * If the generation changed between the two, or `publishing` is set in either, the files may be
 * from two builds: a 409 that says to reload. The slug must already be a document's (the caller
 * checks it against the documents it knows); a name that is not a plain slug throws.
 */
export async function readPublished(slug: string, env: PublishedEnv): Promise<{ ok: true; published: Published } | Conflict> {
  assertSlug(slug);
  const before = (await env.publications())[slug];
  const [manifest, review, ledger] = await Promise.all([
    readManifest(env.docsRoot, slug), readReview(env.docsRoot, slug), readDecisions(env.docsRoot, slug),
  ]);
  const after = (await env.publications())[slug];
  if (before?.publishing || after?.publishing || (before?.generation ?? 0) !== (after?.generation ?? 0)) {
    return conflict(409, BEING_PUBLISHED, true);
  }
  const v = isRecord(manifest) ? manifest.v : undefined;
  const rev = typeof v === "string" && v !== "" ? v : typeof v === "number" ? String(v) : null;
  if (rev === null || !Array.isArray(review)) return conflict(409, BEING_PUBLISHED, true);
  return {
    ok: true,
    published: {
      slug, rev, generation: after?.generation ?? 0,
      items: review.flatMap((r) => item(r) ?? []),
      ledger: parseLedger(ledger),
    },
  };
}

/** decisions.json alone, or null when there is none or it is unusable. */
export async function readLedger(docsRoot: string, slug: string): Promise<Ledger | null> {
  assertSlug(slug);
  return parseLedger(await readDecisions(docsRoot, slug));
}
