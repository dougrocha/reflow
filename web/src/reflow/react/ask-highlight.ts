// Marking an answer's quote in the transcript. Separate from find's marks: <mark data-ask> versus
// <mark data-find>, so neither clears the other.

import { collectText, markSpan, unwrapMarks } from "./find";

const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Where `quote` occurs in `text` (a find.collectText string: one character per source character,
 *  whitespace as spaces, lower case), comparing case-insensitively with any whitespace run in either
 *  matching any whitespace run in the other. [start, end) offsets in `text`, or null. Pure. */
export function quoteSpan(text: string, quote: string): [number, number] | null {
  const words = quote.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return null;
  // Words joined by \s+, so "a   b" in the text matches "a b" in the quote and the reverse. The
  // text keeps its whitespace characters one for one, so a match offset is a text-node offset.
  const re = new RegExp(words.map(escapeRegExp).join("\\s+"), "i");
  const m = re.exec(text);
  return m ? [m.index, m.index + m[0].length] : null;
}

/** Wrap `quote` inside `block` (a transcript block) in <mark data-ask> elements, one per text node
 *  it spans, located with find.collectText(block, true) and quoteSpan. Returns the first mark, or
 *  null when the quote is not found: the caller then sets data-ask-block on the block instead. */
export function markQuote(block: HTMLElement, quote: string): HTMLElement | null {
  const { text, parts } = collectText(block, true);
  const span = quoteSpan(text, quote);
  if (!span) return null;
  return markSpan(parts, span[0], span[1], "ask");
}

/** Remove every mark[data-ask] under `root`, restoring its text, and every data-ask-block attribute.
 *  Find marks (mark[data-find]) that overlap the quote stay. */
export function clearAskMarks(root: ParentNode): void {
  unwrapMarks(root, "ask");
  root.querySelectorAll("[data-ask-block]").forEach((el) => el.removeAttribute("data-ask-block"));
}
