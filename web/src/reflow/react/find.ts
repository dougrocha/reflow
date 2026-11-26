// Find in document: the text of a page as one searchable string, and the DOM work to show a
// match. The same collector reads both the chunk HTML (to build the index, no page mounted) and a
// page card that is on screen, so "the 3rd match on page 45" means the same place in both.
//
// Whitespace and case are folded one character to one character, so an offset in the string is
// an offset in the page's text nodes. Block elements are separated by a newline, so a match never
// runs from the end of one paragraph into the start of the next.

const BLOCK = new Set([
  "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "BR", "CAPTION", "DD", "DETAILS", "DIV", "DL", "DT",
  "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER", "HR",
  "LI", "MAIN", "NAV", "OL", "P", "PRE", "SECTION", "SUMMARY", "TABLE", "TBODY", "TD", "TFOOT", "TH",
  "THEAD", "TR", "UL",
]);

/** Text nodes the viewer adds itself ("Show on page" buttons) are not part of the document's text. */
const SKIP_LIVE = "[data-show-source], script, style";
/** Nor is text reflow wrote itself (the page's OCR note), in the chunk HTML and on screen alike. */
const SKIP_ALWAYS = "[data-generated]";

type PageTextMap = { text: string; parts: { node: Text; start: number }[] };

/** The searchable text under `root`, with where each text node starts. */
export function collectText(root: Node, live: boolean): PageTextMap {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const pieces: string[] = [];
  const parts: PageTextMap["parts"] = [];
  let length = 0;
  let lastParent: Element | null = null;
  let lastBlock: Element | null = null;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const parent = node.parentElement;
    if (!parent) continue;
    if (parent.closest(SKIP_ALWAYS) || (live && parent.closest(SKIP_LIVE))) continue;
    if (parent !== lastParent) {
      let block: Element | null = parent;
      while (block && !BLOCK.has(block.tagName)) block = block.parentElement;
      if (lastBlock !== null && block !== lastBlock) {
        pieces.push("\n");
        length += 1;
      }
      lastParent = parent;
      lastBlock = block ?? root.parentElement ?? parent;
    }
    const raw = node.data;
    const folded = raw.replace(/\s/g, " ").toLowerCase();
    const text = folded.length === raw.length ? folded : raw.replace(/\s/g, " ");
    parts.push({ node, start: length });
    pieces.push(text);
    length += text.length;
  }
  return { text: pieces.join(""), parts };
}

let scratch: HTMLTemplateElement | null = null;

/** The searchable text of one page's transcript HTML. The template is inert: nothing loads. */
export function textOfHtml(html: string): string {
  scratch ??= document.createElement("template");
  scratch.innerHTML = html;
  const { text } = collectText(scratch.content, false);
  scratch.innerHTML = "";
  return text;
}

/** A query as the index sees it: whitespace folded, lower case. Under two characters is no query. */
export function normalizeQuery(query: string): string {
  const q = query.trim().replace(/\s+/g, " ").toLowerCase();
  return q.length >= 2 ? q : "";
}

/** Non-overlapping occurrences of `q` in `text`. */
export function countIn(text: string, q: string): number {
  let n = 0;
  for (let i = text.indexOf(q); i >= 0; i = text.indexOf(q, i + q.length)) n++;
  return n;
}

/** Take `mark` out of the tree, leaving its children (text, and any other kind of mark nested in
 *  it) in its place. Returns the parent it was in, or null for a mark that was not in a tree. */
export function unwrapMark(mark: Element): Node | null {
  const parent = mark.parentNode;
  if (!parent) return null;
  while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
  parent.removeChild(mark);
  return parent;
}

/** The two kinds of <mark> the viewer adds: find's matches (data-find) and an answer's quote
 *  (data-ask). Each kind is added and cleared on its own, so neither clears the other. */
export type MarkKind = "find" | "ask";

/** Unwrap every mark of `kind` under `root`, putting the text back as it was. */
export function unwrapMarks(root: ParentNode, kind: MarkKind): void {
  const parents = new Set<Node>();
  root.querySelectorAll(`mark[data-${kind}]`).forEach((m) => {
    const parent = unwrapMark(m);
    if (parent) parents.add(parent);
  });
  parents.forEach((p) => p.normalize());
}

/** Take the highlight off every match under `root`, putting the text back as it was. Answer marks
 *  (mark[data-ask]) that overlap a match stay. */
export function clearMarks(root: ParentNode) {
  unwrapMarks(root, "find");
}

/** Wrap [at, end) of a collectText string in <mark data-`kind`> elements, one per text node it
 *  spans. Returns the first mark, or null when the span covers no text node. */
export function markSpan(parts: PageTextMap["parts"], at: number, end: number, kind: MarkKind): HTMLElement | null {
  const marks: HTMLElement[] = [];
  // Last to first, so splitting one node never moves another's offsets.
  for (let i = parts.length - 1; i >= 0; i--) {
    const { node, start } = parts[i];
    const stop = start + node.length;
    if (stop <= at || start >= end) continue;
    const from = Math.max(at, start) - start;
    const to = Math.min(end, stop) - start;
    let target = node;
    if (from > 0) target = node.splitText(from);
    if (to - from < target.length) target.splitText(to - from);
    const mark = document.createElement("mark");
    mark.dataset[kind] = "";
    target.replaceWith(mark);
    mark.append(target);
    marks.unshift(mark);
  }
  return marks[0] ?? null;
}

/**
 * Highlight the `nth` occurrence of `q` (0-based) in a mounted page, with <mark> elements around
 * its text. A match can span several text nodes (a link or emphasis inside a sentence), so each
 * gets its own mark. Returns the first mark, or null if the page has no such match. The page's
 * HTML belongs to React, which never touches it again unless the HTML itself changes, and then
 * the marks go with it.
 */
export function markMatch(root: HTMLElement, q: string, nth: number): HTMLElement | null {
  const { text, parts } = collectText(root, true);
  let at = -1;
  for (let i = 0, found = text.indexOf(q); found >= 0; found = text.indexOf(q, found + q.length), i++) {
    if (i === nth) { at = found; break; }
  }
  if (at < 0) return null;
  return markSpan(parts, at, at + q.length, "find");
}
