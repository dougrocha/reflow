/** Text folded for comparing an answer with what a page or a golden set says: NFKC, curly quotes
 *  made straight, whitespace runs as one space, trimmed and in lower case. */
export const norm = (s: string): string =>
  s.normalize("NFKC").replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/\s+/g, " ").trim().toLowerCase();

/** A byte count in KB, one decimal: "12.3 KB". */
export const kb = (n: number): string => `${(n / 1024).toFixed(1)} KB`;
