import { isRecord } from "../guards";
import { randomBytes } from "node:crypto";
import { fold } from "./fold";
import {
  conflict, NEEDS_REBUILD, OLDER_BUILD, type Conflict, type Published, type PublishedItem,
} from "./snapshot";
import type { AltText, Correct, Dismiss, Keep, Reattach, Snapshot, StoreRecord, Undo } from "./types";


// A request body, parsed once into a typed value (`parseRequest`), and the record built from it
// (`buildRecords`). The record is built from the published snapshot and the store, never from the
// client's copy of them: the client says what it saw (`rev`, the item, the suggestion's or the
// finding's id) and what it chose. Anything that does not match exactly is a 4xx and writes nothing.

// -- the request -----------------------------------------------------------------------------------

/** Where a text decision's target comes from: the published suggestion the reviewer saw, or (to
 *  change a decision that is no longer in the queue) the earlier decision in the store. */
type TextTarget =
  | { type: "suggestion"; rev: string; item: string; suggestion: string; supersedes: string | null }
  | { type: "applied"; supersedes: string };
type AltTarget =
  | { type: "figure"; rev: string; item: string; supersedes: string | null; node: string | null }
  | { type: "applied"; supersedes: string };

export type Request =
  | { choice: "accept"; target: Extract<TextTarget, { type: "suggestion" }> }
  | { choice: "keep"; target: TextTarget }
  | { choice: "other"; target: TextTarget; now: string }
  | { choice: "alt"; target: AltTarget; alt: { text: string; decorative: boolean } }
  | { choice: "dismiss"; rev: string; item: string; findings: string[] }
  | { choice: "undo"; of: string }
  | { choice: "reattach"; rev: string; item: string };

type Obj = Record<string, unknown>;
const bad = (error: string): Conflict => conflict(400, error);
const MAX_FINDINGS = 64;

/** The keys a body may hold, checked before anything is read: a key not listed (a name, say) is a 400. */
function only(body: Obj, ...keys: string[]): Conflict | null {
  const extra = Object.keys(body).filter((k) => !keys.includes(k));
  return extra.length ? bad(`Unknown field: ${extra[0]!.slice(0, 40)}.`) : null;
}
function text(body: Obj, key: string): string | Conflict {
  const v = body[key];
  return typeof v === "string" && v !== "" ? v : bad(`${key} must be a non-empty string.`);
}
const isConflict = (x: unknown): x is Conflict => isRecord(x) && (x as Obj).ok === false;

/** The decision's fields, by the `choice` the body names. */
export function parseRequest(body: Obj): { ok: true; request: Request } | Conflict {
  const choice = body.choice;
  const optional = (key: string): string | null | Conflict => (key in body ? text(body, key) : null);
  switch (choice) {
    case "undo": {
      const extra = only(body, "choice", "of");
      if (extra) return extra;
      const of = text(body, "of");
      return isConflict(of) ? of : { ok: true, request: { choice, of } };
    }
    case "reattach":
    case "dismiss": {
      const extra = only(body, "choice", "rev", "item", ...(choice === "dismiss" ? ["finding", "findings"] : []));
      if (extra) return extra;
      const rev = text(body, "rev"), item = text(body, "item");
      if (isConflict(rev)) return rev;
      if (isConflict(item)) return item;
      if (choice === "reattach") return { ok: true, request: { choice, rev, item } };
      if ("finding" in body === "findings" in body) return bad("Give one finding, or a list of findings.");
      const list = "finding" in body ? [body.finding] : body.findings;
      if (!Array.isArray(list) || list.length < 1 || list.length > MAX_FINDINGS || !list.every((f) => typeof f === "string" && f !== "")) {
        return bad("finding must be a fingerprint.");
      }
      return { ok: true, request: { choice, rev, item, findings: list as string[] } };
    }
    case "accept":
    case "keep":
    case "other":
    case "alt": {
      const extra = only(body, "choice", "rev", "item", "supersedes", ...(choice === "other" ? ["now"] : []), ...(choice === "alt" ? ["alt", "node"] : []),
        ...(choice === "accept" || choice === "keep" || choice === "other" ? ["suggestion"] : []));
      if (extra) return extra;
      const supersedes = optional("supersedes");
      if (isConflict(supersedes)) return supersedes;
      let target: TextTarget | AltTarget;
      if ("item" in body) {
        const rev = text(body, "rev"), item = text(body, "item");
        if (isConflict(rev)) return rev;
        if (isConflict(item)) return item;
        if (choice === "alt") {
          // `node`: the block the reviewer is looking at, when it is not known to be the item's own.
          const node = "node" in body ? text(body, "node") : null;
          if (isConflict(node)) return node;
          target = { type: "figure", rev, item, supersedes, node };
        } else {
          const suggestion = text(body, "suggestion");
          if (isConflict(suggestion)) return suggestion;
          target = { type: "suggestion", rev, item, suggestion, supersedes };
        }
      } else {
        // Changing a decision that is not in the queue any more: only a keep, another reading or an alt text.
        if (supersedes === null || "rev" in body || "suggestion" in body || choice === "accept") return bad("Name the item, the build and the suggestion.");
        target = { type: "applied", supersedes };
      }
      if (choice === "accept") return { ok: true, request: { choice, target: target as Extract<TextTarget, { type: "suggestion" }> } };
      if (choice === "keep") return { ok: true, request: { choice, target: target as TextTarget } };
      if (choice === "other") {
        const now = body.now;
        return typeof now === "string" ? { ok: true, request: { choice, target: target as TextTarget, now } } : bad("now must be a string.");
      }
      const a = body.alt;
      if (!isRecord(a) || only(a, "text", "decorative") || typeof a.text !== "string" || typeof a.decorative !== "boolean") {
        return bad("alt must be {text, decorative}.");
      }
      return { ok: true, request: { choice, target: target as AltTarget, alt: { text: a.text, decorative: a.decorative } } };
    }
    default:
      return bad("Unknown choice.");
  }
}

/** Whether the choice reads the published snapshot (every one but an undo and a change of an earlier decision). */
export function needsPublished(r: Request): boolean {
  if (r.choice === "undo") return false;
  if (r.choice === "dismiss" || r.choice === "reattach") return true;
  return r.target.type !== "applied";
}

// -- values ----------------------------------------------------------------------------------------

import { ALT_MAX_CHARS, NOW_MAX_CHARS, NOW_MAX_GROWTH } from "./limits";
const BIDI = /[؜‎‏‪-‮⁦-⁩]/u;
const CONTROL = /[\p{Cc}\p{Zl}\p{Zp}]/u;

/** Why a correction's `now` cannot be applied, or null: the same limits as `invalid_now` in the
 *  converter. One non-empty line, no control or bidi character, at most 200 characters and 4
 *  times the length of `was` (counted in characters, as Python does). */
export function invalidNow(now: string, was: string): string | null {
  if (now === "") return "empty";
  const length = [...now].length;
  if (length > NOW_MAX_CHARS) return `longer than ${NOW_MAX_CHARS} characters`;
  if (length > NOW_MAX_GROWTH * [...was].length) return `longer than ${NOW_MAX_GROWTH} times the word it replaces`;
  if (BIDI.test(now)) return "holds a bidirectional control character";
  if (CONTROL.test(now)) return "holds a control or line-break character";
  return null;
}

/** Why an alt text is refused (not stripped, so the reviewer sees why), or null. */
export function invalidAlt(alt: string): string | null {
  if ([...alt].length > ALT_MAX_CHARS) return `longer than ${ALT_MAX_CHARS} characters`;
  if (BIDI.test(alt)) return "holds a bidirectional control character";
  if (CONTROL.test(alt)) return "holds a control or line-break character";
  return null;
}

const BASE36 = 36;

/** `<milliseconds since the epoch in base 36>-<8 random hex>`. */
export function mintId(nowMs: number = Date.now()): string {
  return `${nowMs.toString(BASE36)}-${randomBytes(4).toString("hex")}`;
}

/** The record's `at`: UTC, to the second. */
export const stamp = (nowMs: number = Date.now()): string => new Date(nowMs).toISOString().replace(/\.\d+Z$/, "Z");

// -- the record ------------------------------------------------------------------------------------

type BuildContext = {
  /** The published snapshot; null for the choices that need none (see `needsPublished`). */
  published: Published | null;
  /** The store as it is, read under the lock when it is written. */
  store: Snapshot;
  id: () => string;
  at: string;
};

type Built = { ok: true; records: StoreRecord[] } | Conflict;
const one = (r: StoreRecord): Built => ({ ok: true, records: [r] });

const STALE = "The page you are on shows something that is no longer in this build. Reload to see the current one.";
const NO_LONGER = "That decision is no longer there.";

/** An earlier decision of this store that a new one may replace: unknown is 404, not active or of
 *  another kind is 409. */
function earlier(ctx: BuildContext, id: string, kinds: readonly StoreRecord["kind"][]): Correct | Keep | AltText | Conflict {
  const first = ctx.store.entries.find((e) => e.parsed.type === "record" && e.parsed.record.id === id);
  if (!first) return conflict(404, "No such decision.");
  const active = fold(ctx.store).active.find((r) => r.id === id);
  if (!active || !kinds.includes(active.kind)) return conflict(409, NO_LONGER);
  return active as Correct | Keep | AltText;
}

/** The item the reviewer acted on. Document items share the document's id, so `fits` (the
 *  suggestion, finding or warning the request names) picks the one they saw. */
function found(ctx: BuildContext, rev: string, itemId: string,
               fits: (item: PublishedItem) => boolean = () => true): { published: Published; item: PublishedItem } | Conflict {
  const published = ctx.published;
  if (!published) return conflict(409, STALE, true);
  if (!published.ledger) return conflict(409, NEEDS_REBUILD);
  if (rev !== published.rev) return conflict(409, OLDER_BUILD, true);
  const item = published.items.find((i) => i.id === itemId && fits(i));
  return item ? { published, item } : conflict(409, STALE, true);
}

function textRecord(ctx: BuildContext, r: Extract<Request, { choice: "accept" | "keep" | "other" }>): Built {
  const t = r.target;
  let base: { pdf: string; anchor: Correct["anchor"]; range: Correct["range"]; supersedes: string | null };
  if (t.type === "applied") {
    const old = earlier(ctx, t.supersedes, ["correct", "keep"]);
    if (isConflict(old)) return old;
    if (old.kind === "alt") return conflict(409, NO_LONGER);
    base = { pdf: old.pdf, anchor: old.anchor, range: old.range, supersedes: old.id };
  } else {
    const f = found(ctx, t.rev, t.item, (i) => i.suggestions.some((x) => x.id === t.suggestion));
    if (isConflict(f)) return f;
    const s = f.item.suggestions.find((x) => x.id === t.suggestion);
    if (!s) return conflict(409, STALE, true);
    if (t.supersedes !== null) {
      const old = earlier(ctx, t.supersedes, ["correct", "keep"]);
      if (isConflict(old)) return old;
    }
    base = {
      pdf: f.published.ledger!.source.sha256, anchor: s.anchor,
      range: { was: s.was, before: s.before, after: s.after, rule: s.rule }, supersedes: t.supersedes,
    };
    if (r.choice === "accept") {
      const why = invalidNow(s.now, s.was);
      if (why) return bad(`The suggested reading ${why}.`);
      return one({ kind: "correct", id: ctx.id(), at: ctx.at, ...base, now: s.now });
    }
  }
  if (r.choice === "keep") return one({ kind: "keep", id: ctx.id(), at: ctx.at, ...base });
  if (r.choice !== "other") return bad("Name the item, the build and the suggestion.");
  const why = invalidNow(r.now, base.range.was);
  if (why) return bad(`The reading ${why}.`);
  return one({ kind: "correct", id: ctx.id(), at: ctx.at, ...base, now: r.now });
}

function altRecord(ctx: BuildContext, r: Extract<Request, { choice: "alt" }>): Built {
  const why = invalidAlt(r.alt.text);
  if (why) return bad(`The alt text ${why}.`);
  if (r.alt.decorative ? r.alt.text !== "" : r.alt.text.trim() === "") {
    return bad(r.alt.decorative ? "A decorative image has no alt text." : "Write the alt text, or mark the image decorative.");
  }
  const t = r.target;
  if (t.type === "applied") {
    const old = earlier(ctx, t.supersedes, ["alt"]);
    if (isConflict(old)) return old;
    if (old.kind !== "alt") return conflict(409, NO_LONGER);
    return one({ kind: "alt", id: ctx.id(), at: ctx.at, pdf: old.pdf, anchor: old.anchor, ...r.alt, supersedes: old.id });
  }
  const f = found(ctx, t.rev, t.item);
  if (isConflict(f)) return f;
  if (!f.item.anchor || f.item.anchor.crop_sha === "") return conflict(409, STALE, true);
  // A grouped item names its first figure only (its id and anchor): never write for another member.
  if (t.node !== null && t.node !== f.item.id) return conflict(409, "This item covers several figures and names only the first. Open that figure to write its alt text.");
  if (t.supersedes !== null) {
    const old = earlier(ctx, t.supersedes, ["alt"]);
    if (isConflict(old)) return old;
  }
  return one({ kind: "alt", id: ctx.id(), at: ctx.at, pdf: f.published.ledger!.source.sha256, anchor: f.item.anchor, ...r.alt, supersedes: t.supersedes });
}

function dismissRecord(ctx: BuildContext, r: Extract<Request, { choice: "dismiss" }>): Built {
  const f = found(ctx, r.rev, r.item, (i) => r.findings.every((id) => i.findings.some((x) => x.id === id)));
  if (isConflict(f)) return f;
  const chosen = r.findings.map((id) => f.item.findings.find((x) => x.id === id));
  if (chosen.some((x) => x === undefined)) return conflict(409, STALE, true);
  for (const x of chosen) {
    if (x!.severity === "error") return bad("Errors are fixed, not dismissed.");
    if (x!.code.startsWith("decision-") || x!.code.startsWith("decisions-")) return bad("A warning about decisions is settled by its own controls, not dismissed.");
  }
  const rec: Dismiss = { kind: "dismiss", id: ctx.id(), at: ctx.at, pdf: f.published.ledger!.source.sha256, findings: [...new Set(r.findings)] };
  return one(rec);
}

const UNDOABLE: readonly StoreRecord["kind"][] = ["correct", "keep", "alt", "dismiss"];

function undoRecord(ctx: BuildContext, r: Extract<Request, { choice: "undo" }>): Built {
  const first = ctx.store.entries.find((e) => e.parsed.type === "record" && e.parsed.record.id === r.of);
  if (!first || first.parsed.type !== "record") return conflict(404, "No such decision.");
  const target = first.parsed.record;
  if (!UNDOABLE.includes(target.kind)) return conflict(409, "Only a correction, a kept reading, an alt text or a dismissal can be undone.");
  if (fold(ctx.store).undone.includes(target.id)) return conflict(409, "That decision is already undone.");
  const rec: Undo = { kind: "undo", id: ctx.id(), at: ctx.at, pdf: target.pdf, of: target.id };
  return one(rec);
}

function reattachRecords(ctx: BuildContext, r: Extract<Request, { choice: "reattach" }>): Built {
  const f = found(ctx, r.rev, r.item, (i) => i.findings.some((x) => x.code === "decisions-other-pdf"));
  if (isConflict(f)) return f;
  if (!f.item.findings.some((x) => x.code === "decisions-other-pdf")) return conflict(409, STALE, true);
  const ledger = f.published.ledger!;
  const to = ledger.source.sha256;
  const byId = new Map<string, StoreRecord>();
  for (const e of ctx.store.entries) if (e.parsed.type === "record" && !byId.has(e.parsed.record.id)) byId.set(e.parsed.record.id, e.parsed.record);
  const done = new Set(fold(ctx.store).reattach.filter((x) => x.to === to).map((x) => x.from));
  const from = [...new Set(ledger.orphaned.filter((o) => o.reason === "other-pdf").flatMap((o) => byId.get(o.id)?.pdf ?? []))]
    .filter((h) => h !== to && !done.has(h));
  if (from.length === 0) return conflict(409, "There is nothing to try on this PDF. Rebuild to see where the decisions stand.");
  return { ok: true, records: from.map((h): Reattach => ({ kind: "reattach", id: ctx.id(), at: ctx.at, pdf: to, from: h, to })) };
}

/** The records one request writes, or why it writes none. Pure: the snapshot, the store and the
 *  minted id and time come in. */
export function buildRecords(r: Request, ctx: BuildContext): Built {
  switch (r.choice) {
    case "accept":
    case "keep":
    case "other": return textRecord(ctx, r);
    case "alt": return altRecord(ctx, r);
    case "dismiss": return dismissRecord(ctx, r);
    case "undo": return undoRecord(ctx, r);
    case "reattach": return reattachRecords(ctx, r);
  }
}
