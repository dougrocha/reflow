import { isFiniteNumber, isRecord } from "../guards";
import {
  KINDS, MAX_LINE, NO_HEADER, VERSION,
  type Anchor, type Box, type Entry, type Header, type Parsed, type Range, type Snapshot, type StoreRecord,
} from "./types";

// The one place that reads a raw store line, as `parse_record` is in the converter
// (src/reflow/analyze/decisions.py). Handlers get typed values.

const ID = /^[0-9a-z]{1,13}-[0-9a-f]{8}$/;
export const HEX64 = /^[0-9a-f]{64}$/;
export const HEX16 = /^[0-9a-f]{16}$/;
const FONT_MAP = /^fm-[0-9a-f]{16}$/;

type Obj = Record<string, unknown>;

class Bad extends Error {}


function str(d: Obj, key: string, empty = true): string {
  const v = d[key];
  if (typeof v !== "string" || (!empty && v === "")) throw new Bad(`${key} must be a${empty ? "" : " non-empty"} string`);
  return v;
}
function hex(d: Obj, key: string, pattern: RegExp, width: number): string {
  const v = d[key];
  if (typeof v !== "string" || !pattern.test(v)) throw new Bad(`${key} must be ${width} lower-case hex digits`);
  return v;
}
function obj(d: Obj, key: string): Obj {
  const v = d[key];
  if (!isRecord(v)) throw new Bad(`${key} must be an object`);
  return v;
}
function num(v: unknown): number {
  if (!isFiniteNumber(v)) throw new Bad("not a finite number");
  return v;
}

function anchor(d: Obj, text: boolean): Anchor {
  const a = obj(d, "anchor");
  const page = a.page;
  if (typeof page !== "number" || !Number.isInteger(page) || page < 1) throw new Bad("anchor.page must be an integer from 1");
  const box = a.bbox;
  if (!Array.isArray(box) || box.length !== 4) throw new Bad("anchor.bbox must hold four numbers");
  const sha = hex(a, text ? "text_sha" : "crop_sha", HEX16, 16);
  const other = a[text ? "crop_sha" : "text_sha"] ?? "";
  if (typeof other !== "string" || (other !== "" && !HEX16.test(other))) throw new Bad("anchor sha must be 16 hex digits or empty");
  const bbox: Box = [num(box[0]), num(box[1]), num(box[2]), num(box[3])];
  return { page, bbox, text_sha: text ? sha : other, crop_sha: text ? other : sha, hint: str(a, "hint") };
}

function range(d: Obj): Range {
  const r = obj(d, "range");
  return { was: str(r, "was", false), before: str(r, "before"), after: str(r, "after"), rule: str(r, "rule") };
}

const supersedes = (d: Obj): string | null => ("supersedes" in d ? str(d, "supersedes", false) : null);

const hexId = (d: Obj, key: string): string => {
  const v = d[key];
  if (typeof v !== "string" || !ID.test(v)) throw new Bad(`${key} must be a record id`);
  return v;
};

function parseHeader(o: Obj): Header | null {
  if (o.v !== VERSION || o.kind !== "store") return null;
  try {
    return { id: hex(o, "id", HEX16, 16), slug: str(o, "slug"), created: str(o, "created") };
  } catch (e) {
    if (e instanceof Bad) return null;
    throw e;
  }
}

/** One JSON object of the store as a typed record. An unknown `v` or `kind` is unsupported, a
 *  known kind with a wrong field is invalid. */
function parseRecord(o: Obj): Parsed {
  const id = typeof o.id === "string" ? o.id : null;
  const { v, kind } = o;
  if (v !== VERSION || typeof kind !== "string" || !(KINDS as readonly string[]).includes(kind)) {
    return { type: "unsupported", id, v, kind };
  }
  try {
    const base = { id: hexId(o, "id"), at: str(o, "at"), pdf: hex(o, "pdf", HEX64, 64) };
    let record: StoreRecord;
    switch (kind as StoreRecord["kind"]) {
      case "correct": {
        const t = obj(o, "target");
        record = { ...base, kind: "correct", anchor: anchor(t, true), range: range(t), now: str(o, "now"), supersedes: supersedes(o) };
        break;
      }
      case "keep": {
        const t = obj(o, "target");
        record = { ...base, kind: "keep", anchor: anchor(t, true), range: range(t), supersedes: supersedes(o) };
        break;
      }
      case "alt": {
        const alt = obj(o, "alt");
        if (typeof alt.decorative !== "boolean") throw new Bad("alt.decorative must be true or false");
        record = { ...base, kind: "alt", anchor: anchor(obj(o, "target"), false), text: str(alt, "text"), decorative: alt.decorative, supersedes: supersedes(o) };
        break;
      }
      case "dismiss": {
        const found = o.findings;
        if (!Array.isArray(found) || found.length === 0 || !found.every((f) => typeof f === "string" && f !== "")) {
          throw new Bad("findings must be a list of fingerprints");
        }
        record = { ...base, kind: "dismiss", findings: found as string[] };
        break;
      }
      case "undo":
        record = { ...base, kind: "undo", of: str(o, "of", false) };
        break;
      case "reattach":
        record = { ...base, kind: "reattach", from: hex(o, "from", HEX64, 64), to: hex(o, "to", HEX64, 64) };
        break;
      case "proposal": {
        const proposal = str(o, "proposal");
        const verdict = o.verdict;
        if (!proposal.startsWith("pv-")) throw new Bad("proposal must be a proposal id");
        if (verdict !== "accept" && verdict !== "reject") throw new Bad("verdict must be accept or reject");
        record = { ...base, kind: "proposal", proposal, verdict, text: hex(o, "text", HEX64, 64) };
        break;
      }
      case "font-map": {
        const map = str(o, "map");
        const verdict = o.verdict;
        if (!FONT_MAP.test(map)) throw new Bad("map must be a font map id");
        if (verdict !== "accept" && verdict !== "reject") throw new Bad("verdict must be accept or reject");
        record = { ...base, kind: "font-map", map, verdict };
        break;
      }
    }
    return { type: "record", record };
  } catch (e) {
    if (e instanceof Bad) return { type: "invalid", id, reason: e.message };
    throw e;
  }
}

const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/** A line as a JSON object, or null: too long, not UTF-8, not JSON, or not an object. */
function lineObject(line: Uint8Array): Obj | null {
  if (line.length > MAX_LINE) return null;
  try {
    const value: unknown = JSON.parse(decoder.decode(line));
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

const EMPTY: Snapshot = { store: null, bytes: 0, lines: 0, torn: 0, unreadable: [], entries: [], error: null, slug: null };

/** The read protocol over the bytes of a store read once: cut after the last `\n` (the rest is a
 *  torn tail, counted and not read), count the lines that are not JSON objects, require the header. */
export function parseSnapshot(data: Uint8Array): Snapshot {
  if (data.length === 0) return { ...EMPTY };
  const cut = data.lastIndexOf(0x0a) + 1;
  const torn = data.length > cut ? 1 : 0;
  const unreadable: number[] = [];
  const entries: Entry[] = [];
  let header: Header | null = null;
  let offset = 0;
  let number = 0;
  while (offset < cut) {
    const end = data.indexOf(0x0a, offset);
    const o = lineObject(data.subarray(offset, end));
    number++;
    if (o === null) unreadable.push(offset);
    else if (number === 1) header = parseHeader(o);
    else entries.push({ offset, line: number, parsed: parseRecord(o) });
    offset = end + 1;
  }
  if (header === null) return { store: null, bytes: cut, lines: number, torn, unreadable, entries: [], error: NO_HEADER, slug: null };
  return { store: header.id, bytes: cut, lines: number, torn, unreadable, entries, error: null, slug: header.slug };
}

/** The `anchor` of a published review item or suggestion (a text anchor has `text_sha`, a figure's
 *  has `crop_sha`), or null when it is not exactly one. */
export function readAnchor(container: unknown, text: boolean): Anchor | null {
  if (!isRecord(container)) return null;
  try {
    return anchor({ anchor: container }, text);
  } catch (e) {
    if (e instanceof Bad) return null;
    throw e;
  }
}
