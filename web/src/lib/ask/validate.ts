// The citation check: accept a reply only if what it cites is really in the offered passages.

import { ASK_ANSWER_CHARS, ASK_MAX_EVIDENCE, ASK_QUOTE_CHARS } from "@/lib/ask-contract";
import { isRecord } from "@/lib/guards";
import type { Passage } from "./corpus";
import type { Label } from "./prompt";
import { asciiDigits, locateQuote, numbersIn } from "./quote";

/** A verified citation. `quote` is the document's slice of `passage.text` at the located offsets,
 *  clipped to ASK_QUOTE_CHARS at a word boundary; the model's own string is never forwarded. */
/** `at`: where the quote starts in the passage's text. */
type Citation = { passage: Passage; quote: string; at?: number };

export type Verdict =
  | { kind: "answer"; answer: string; picks: [Citation, ...Citation[]] }
  | { kind: "none"; why: "not-stated" | "unverified" }
  | { kind: "malformed" };

type Reply = { evidence: { passage: string; quote: string }[]; found: boolean; answer: string };

/** The first balanced {...} in `raw` that parses as a JSON object, skipping prose and fences. */
function firstJsonObject(raw: string): Record<string, unknown> | null {
  for (let start = raw.indexOf("{"); start >= 0; start = raw.indexOf("{", start + 1)) {
    let depth = 0;
    let inString = false;
    for (let i = start; i < raw.length; i++) {
      const c = raw[i];
      if (inString) {
        if (c === "\\") i++;
        else if (c === '"') inString = false;
      } else if (c === '"') inString = true;
      else if (c === "{") depth++;
      else if (c === "}" && --depth === 0) {
        try {
          const v: unknown = JSON.parse(raw.slice(start, i + 1));
          if (isRecord(v)) return v;
        } catch {
          // not JSON; try the next opening brace
        }
        break;
      }
    }
  }
  return null;
}

function shapeOf(o: Record<string, unknown>): Reply | null {
  if (typeof o.found !== "boolean" || typeof o.answer !== "string" || !Array.isArray(o.evidence)) return null;
  const evidence: Reply["evidence"] = [];
  for (const item of o.evidence) {
    const i = item as Record<string, unknown> | null;
    if (!i || typeof i !== "object" || typeof i.passage !== "string" || typeof i.quote !== "string") {
      // Only a reply that claims an answer is held to its evidence; a decline's evidence is ignored.
      if (o.found) return null;
      continue;
    }
    evidence.push({ passage: i.passage, quote: i.quote });
  }
  return { evidence, found: o.found, answer: o.answer };
}

/** The slice [start, end) of `text`, at most ASK_QUOTE_CHARS, cut back to a word boundary. */
function clip(text: string, start: number, end: number): string {
  const slice = text.slice(start, end).trim();
  if (slice.length <= ASK_QUOTE_CHARS) return slice;
  const cut = slice.slice(0, ASK_QUOTE_CHARS);
  const sp = cut.search(/\s\S*$/);
  return (sp > 0 ? cut.slice(0, sp) : cut).trimEnd();
}

/** The model's answer as shown: line breaks and tabs become spaces; bidi overrides and isolates and
 *  the other C0 and C1 controls are dropped. (React escapes markup, so nothing else is touched.) */
function plainAnswer(text: string): string {
  return text
    .replace(/[\t\n\r]+/g, " ")
    // Controls, line and paragraph separators, and invisible format characters (bidi, zero width,
    // BOM, tags), but not the joiners Persian and Indic words and emoji sequences need.
    .replace(/[\p{Cc}\p{Zl}\p{Zp}]|(?![\u200c\u200d])\p{Cf}/gu, "")
    .trim();
}

type Located = { passage: Passage; span: [number, number] };

const REFERENCE = /\b(?:pages?|pp?\.|sections?|sec\.|§|tables?|figures?|fig\.)\s*(\d+(?:\.\d+)*)\.?/giu;

/** The answer without the references to where the cited passages are: "page N" for a cited page,
 *  "section N", "table N" or "figure N" for a number in a cited heading trail or lead-in, and a
 *  cited heading or lead-in quoted whole. Any other reference stays, and its number is checked. */
function withoutReferences(answer: string, kept: Located[]): string {
  const pages = new Set(kept.map((k) => String(k.passage.page)));
  const labels = new Set(kept.flatMap((k) => numbersIn(`${k.passage.trail} ${k.passage.lead}`)));
  const phrases = kept
    .flatMap((k) => [...k.passage.trail.split(" > "), k.passage.lead])
    .map((p) => asciiDigits(p).trim().replace(/\.$/, ""))
    .filter((p) => /\d/.test(p));
  let text = asciiDigits(answer);
  for (const phrase of phrases) text = text.split(phrase).join(" ");
  return text.replace(REFERENCE, (ref, n: string) =>
    (/^(?:pages?|pp?\.)/i.test(ref) ? pages.has(n) : labels.has(n)) ? " " : ref);
}

/**
 * Parse and check a raw reply against what was offered (design section 8), in order:
 *  1. extract the first JSON object and shape-check it by hand; not that shape -> malformed
 *  2. found === false -> none/not-stated (evidence is ignored)
 *  3. per evidence item: label must be in `offered`; quote located (locateQuote) in that passage's
 *     text, else in any other offered passage (rebind); else the item is dropped
 *  4. no surviving item -> none/unverified
 *  5. the answer, stripped of control characters, is non-empty and at most ASK_ANSWER_CHARS, else
 *     none/unverified; every number in it (numbersIn), apart from references to where a cited passage
 *     is (its page, section, table or figure), must occur in a cited passage's text or the question,
 *     else none/unverified
 *  6. at most ASK_MAX_EVIDENCE picks, de-duplicated by (passage, overlapping span)
 * Pure.
 */
export function validateReply(raw: string, offered: ReadonlyMap<Label, Passage>, question = ""): Verdict {
  const obj = firstJsonObject(raw);
  const reply = obj && shapeOf(obj);
  if (!reply) return { kind: "malformed" };
  if (!reply.found) return { kind: "none", why: "not-stated" };

  const kept: Located[] = [];
  for (const item of reply.evidence) {
    const named = offered.get(item.passage as Label);
    if (!named) continue;
    let hit: Located | null = null;
    const own = locateQuote(named.text, item.quote);
    if (own) hit = { passage: named, span: own };
    else {
      for (const other of offered.values()) {
        if (other === named) continue;
        const span = locateQuote(other.text, item.quote);
        if (span) {
          hit = { passage: other, span };
          break;
        }
      }
    }
    if (!hit) continue;
    const { passage, span } = hit;
    if (kept.some((k) => k.passage === passage && k.span[0] < span[1] && span[0] < k.span[1])) continue;
    kept.push(hit);
    if (kept.length === ASK_MAX_EVIDENCE) break;
  }
  const answer = plainAnswer(reply.answer);
  if (kept.length === 0 || answer === "" || answer.length > ASK_ANSWER_CHARS) return { kind: "none", why: "unverified" };

  // A number the reader typed ("in 2025") is not the model's to fabricate, so the answer may repeat it.
  // A reference to where a cited passage is ("page 7", "section 3.1", its heading or lead-in word for
  // word) is not a fact of the answer and is set aside; a bare number must still be in the text.
  const known = new Set([...kept.flatMap((k) => numbersIn(k.passage.text)), ...numbersIn(question)]);
  if (!numbersIn(withoutReferences(answer, kept)).every((n) => known.has(n))) return { kind: "none", why: "unverified" };

  kept.sort((a, b) => a.passage.order - b.passage.order || a.span[0] - b.span[0]);
  const picks = kept.map((k) => {
    const slice = k.passage.text.slice(k.span[0], k.span[1]);
    return { passage: k.passage, quote: clip(k.passage.text, k.span[0], k.span[1]),
             at: k.span[0] + slice.length - slice.trimStart().length };
  });
  return { kind: "answer", answer, picks: picks as [Citation, ...Citation[]] };
}
