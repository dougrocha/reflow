// Pure text matching for the citation check: is the model's quote really in the passage?

const INVISIBLE = /^[­​-‍⁠﻿]$/;
const SINGLE_QUOTES = /[‘’‚‛′ʼ]/g;
const DOUBLE_QUOTES = /[“”„‟″«»]/g;
const DASHES = /[‐-―−]/g;

/** `text` folded for comparison (NFKC, lower case, whitespace runs to one space, curly quotes and
 *  dashes straightened, soft hyphens and zero-width characters removed) with, for each folded
 *  character, the index of the original character it came from. */
export function foldWithMap(text: string): { folded: string; origin: number[] } {
  const out: string[] = [];
  const origin: number[] = [];
  let pendingSpace = -1; // origin index of a whitespace run not yet written
  let i = 0;
  while (i < text.length) {
    const ch = String.fromCodePoint(text.codePointAt(i)!);
    const at = i;
    i += ch.length;
    if (INVISIBLE.test(ch)) continue;
    const mapped = ch
      .normalize("NFKC")
      .replace(SINGLE_QUOTES, "'")
      .replace(DOUBLE_QUOTES, '"')
      .replace(DASHES, "-")
      .toLowerCase();
    for (const c of mapped) {
      if (/^\s$/.test(c)) {
        if (pendingSpace < 0) pendingSpace = at;
        continue;
      }
      if (pendingSpace >= 0) {
        if (out.length > 0) {
          out.push(" ");
          origin.push(pendingSpace);
        }
        pendingSpace = -1;
      }
      // A folded character may be several UTF-16 units; each maps to the same original index.
      for (let k = 0; k < c.length; k++) {
        out.push(c[k]);
        origin.push(at);
      }
    }
  }
  return { folded: out.join(""), origin };
}

/** Where `quote` sits in `text`, as [start, end) offsets in the ORIGINAL text, or null.
 *  Needs at least 12 folded characters, unless the quote is the whole folded text. */
export function locateQuote(text: string, quote: string): [number, number] | null {
  const t = foldWithMap(text);
  const q = foldWithMap(quote).folded;
  if (q.length === 0) return null;
  if (q.length < 12 && q !== t.folded) return null;
  const at = t.folded.indexOf(q);
  if (at < 0) return null;
  const start = t.origin[at];
  const last = t.origin[at + q.length - 1];
  const end = last + String.fromCodePoint(text.codePointAt(last)!).length;
  return [start, end];
}

const DECIMAL_DIGIT = /\p{Nd}/u;

/** The value of a decimal digit of any script. Unicode puts each script's digits in a run of ten
 *  from 0 to 9 (some runs sit back to back), so the value is the offset from the run's start. */
function digitValue(cp: number): number {
  let start = cp;
  while (start > 0 && DECIMAL_DIGIT.test(String.fromCodePoint(start - 1))) start--;
  return (cp - start) % 10;
}

/** NFKC (fullwidth, superscript and other compatibility digits become ASCII), then every other
 *  decimal digit (Arabic-Indic, Devanagari, Thai and the rest) as its ASCII twin. */
export function asciiDigits(s: string): string {
  return s.normalize("NFKC").replace(/(?![0-9])\p{Nd}/gu, (d) => String(digitValue(d.codePointAt(0)!)));
}

/** The numbers in `s`, normalised for the number guard ("1,200" -> "1200", "3.57" -> "3.57").
 *  A thousands grouping (1-3 digits, then groups of exactly 3 after "," or ".") loses its
 *  separators unless the integer part is 0; otherwise a decimal comma becomes a point. */
export function numbersIn(s: string): string[] {
  const found = asciiDigits(s).match(/\d+(?:[.,]\d+)*/g) ?? [];
  return found.map((raw) => {
    if (/^\d{1,3}(?:[.,]\d{3})+$/.test(raw) && !/^0[.,]/.test(raw)) return raw.replace(/[.,]/g, "");
    if (/^\d+,\d+$/.test(raw)) return raw.replace(",", ".");
    return raw;
  });
}
