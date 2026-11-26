import type { InspectNode } from "@/lib/inspect";
import { DEFAULT_PIPELINE } from "@/pipelines";

// The compare screen: one page of a document, the original page image first, then a column
// per built variant. Blocks are matched across columns by where they sit on the page, and each
// column's differences are worked out against the first variant column (the reference) and the
// PDF's own text. Pure functions, so the screen and the tests share them.

export type Block = {
  id: string;
  role: string;
  bbox: number[];
  text: string;
  /** A form field's label. */
  label: string | null;
  /** A figure's alt text. */
  alt: string | null;
  /** A table's shape, "45 rows × 30 columns" (the sidecar does not keep its cells' text). */
  shape: string | null;
};

/** Roles that only group other blocks (their children are compared instead), or are not content. */
const SKIP = new Set(["artifact", "list", "group"]);

/** The blocks of a page's sidecar that the compare screen shows, in reading order. */
export function blocksOf(nodes: InspectNode[]): Block[] {
  return nodes.filter((n) => !SKIP.has(n.role) && n.bbox?.length === 4).map((n) => ({
    id: n.id,
    role: n.role,
    bbox: n.bbox,
    text: n.text ?? "",
    label: typeof n.attrs?.label === "string" ? n.attrs.label : null,
    alt: typeof n.attrs?.alt === "string" ? n.attrs.alt : null,
    shape: n.role === "table" && typeof n.attrs?.rows === "number" && typeof n.attrs?.columns === "number"
      ? `${n.attrs.rows} rows × ${n.attrs.columns} columns` : null,
  }));
}

/** How much two boxes cover each other: their intersection over the smaller one's area (0 to 1). A
 *  block one engine splits in two still matches the whole the other kept. */
function overlap(a: number[], b: number[]): number {
  const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
  const h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
  if (w <= 0 || h <= 0) return 0;
  const area = (r: number[]) => Math.max(1e-6, (r[2] - r[0]) * (r[3] - r[1]));
  return (w * h) / Math.min(area(a), area(b));
}

/** Blocks that cover each other this much are the same block. */
const SAME = 0.5;

/** Whether two blocks are the same block: they cover each other, and a table only matches a table
 *  (a table holds the blocks around it, which would otherwise all match it). */
export function sameBlock(a: Pick<Block, "role" | "bbox">, b: Pick<Block, "role" | "bbox">): boolean {
  return (a.role === "table") === (b.role === "table") && overlap(a.bbox, b.bbox) >= SAME;
}

/** The block among `blocks` that best matches `block`, or null. */
export function matchOf(block: Pick<Block, "role" | "bbox">, blocks: Block[]): Block | null {
  let best: Block | null = null;
  let score = SAME;
  for (const b of blocks) {
    if ((block.role === "table") !== (b.role === "table")) continue;
    const s = overlap(block.bbox, b.bbox);
    if (s >= score) [best, score] = [b, s];
  }
  return best;
}

export type Word = { text: string; changed: boolean; notInPdf: boolean };

const words = (s: string) => s.split(/\s+/).filter(Boolean);
/** A word as the PDF check compares it: case and the punctuation around it do not count. */
const bare = (w: string) => w.toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");

/** The words of `text`, each marked when it is not in the reference's text at that place (a word
 *  diff: the longest common subsequence) and when the PDF's text has no such word, and the
 *  reference's words that `text` leaves out. With no reference
 *  nothing is "changed"; with no PDF words (a scan) nothing is "not in the PDF". */
export function diffWords(text: string, reference: string | null, pdfWords: Set<string> | null): { words: Word[]; removed: string[] } {
  const mine = words(text);
  const kept = new Array<boolean>(mine.length).fill(reference === null);
  const theirs = reference === null ? [] : words(reference);
  const theirsKept = new Array<boolean>(theirs.length).fill(false);
  if (reference !== null) {
    const n = mine.length, m = theirs.length;
    // lcs[i][j]: the common subsequence of mine[i:] and theirs[j:].
    const lcs = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        lcs[i][j] = mine[i] === theirs[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
    }
    for (let i = 0, j = 0; i < n && j < m;) {
      if (mine[i] === theirs[j]) { kept[i] = true; theirsKept[j] = true; i++; j++; }
      else if (lcs[i + 1][j] >= lcs[i][j + 1]) i++;
      else j++;
    }
  }
  return {
    words: mine.map((w, i) => ({
      text: w,
      changed: !kept[i],
      notInPdf: pdfWords !== null && pdfWords.size > 0 && bare(w) !== "" && !pdfWords.has(bare(w)),
    })),
    removed: theirs.filter((_, j) => !theirsKept[j]),
  };
}

/** The words of the PDF's own text on the page (its text lines), as diffWords compares them. */
export function pdfWordsOf(lines: { text: string }[]): Set<string> {
  return new Set(lines.flatMap((l) => words(l.text).map(bare)).filter(Boolean));
}

export type BlockDiff = {
  block: Block;
  /** The reference column's block at the same place, or null (this column alone has it). */
  match: Block | null;
  words: Word[];
  /** The reference block's words this one leaves out. */
  removed: string[];
  roleChanged: boolean;
  labelChanged: boolean;
  altChanged: boolean;
  shapeChanged: boolean;
  /** Anything differs from the reference. */
  differs: boolean;
};

/** A column's blocks, each against the reference column's (null for the reference itself). */
export function diffColumn(blocks: Block[], reference: Block[] | null, pdfWords: Set<string> | null): BlockDiff[] {
  return blocks.map((block) => {
    const match = reference ? matchOf(block, reference) : null;
    const { words: w, removed } = diffWords(block.text, reference ? (match?.text ?? "") : null, pdfWords);
    const roleChanged = !!match && match.role !== block.role;
    const labelChanged = !!match && (match.label ?? "") !== (block.label ?? "");
    const altChanged = !!match && (match.alt ?? "") !== (block.alt ?? "");
    const shapeChanged = !!match && (match.shape ?? "") !== (block.shape ?? "");
    const differs = reference !== null && (!match || roleChanged || labelChanged || altChanged || shapeChanged || removed.length > 0 || w.some((x) => x.changed));
    return { block, match, words: w, removed, roleChanged, labelChanged, altChanged, shapeChanged, differs };
  });
}

export type PageSummary = {
  blocks: number;
  headings: number;
  fields: number;
  figures: number;
  /** Figures with alt text. */
  described: number;
  /** Blocks that differ from the reference (0 for the reference). */
  differ: number;
  /** The reference's blocks this column has nothing at (0 for the reference). */
  missing: number;
  /** Words that are not in the PDF's text. */
  notInPdf: number;
};

export function summarize(diffs: BlockDiff[], reference: Block[] | null): PageSummary {
  const blocks = diffs.map((d) => d.block);
  return {
    blocks: blocks.length,
    headings: blocks.filter((b) => /^h[1-6]$/.test(b.role)).length,
    fields: blocks.filter((b) => b.role === "field").length,
    figures: blocks.filter((b) => b.role === "figure").length,
    described: blocks.filter((b) => b.role === "figure" && b.alt).length,
    differ: diffs.filter((d) => d.differs).length,
    missing: reference ? reference.filter((r) => !matchOf(r, blocks)).length : 0,
    notInPdf: diffs.reduce((n, d) => n + d.words.filter((w) => w.notInPdf).length, 0),
  };
}

/** A role as a short tag: "H2", "Field", "Figure", "Item", "Text". */
export function roleTag(role: string): string {
  if (/^h[1-6]$/.test(role)) return role.toUpperCase();
  return ({ field: "Field", figure: "Figure", table: "Table", li: "Item", p: "Text", th: "Header cell", td: "Cell" } as Record<string, string>)[role] ?? role;
}

/** The columns a `cols` query names, among the built pipelines, in its order and without repeats.
 *  None named (or none built): the default and the next built one. */
export function parseCols(param: string | string[] | undefined, built: string[]): string[] {
  const raw = (Array.isArray(param) ? param.join(",") : param ?? "").split(",").map((s) => s.trim());
  const cols = [...new Set(raw.filter((c) => built.includes(c)))];
  if (cols.length) return cols;
  const rest = built.filter((b) => b !== DEFAULT_PIPELINE);
  return [...(built.includes(DEFAULT_PIPELINE) ? [DEFAULT_PIPELINE] : []), ...rest].slice(0, 2);
}

/** The drag payload of a compare column's header (reordering) and of a variant tab (dragged to the
 *  side to compare): the pipeline's name. */
export const COLUMN_DRAG = "application/x-reflow-variant";

/** The columns the Compare button opens from a variant's tab: the default and the variant shown,
 *  or, on the default, the default and the next built variant. */
export function compareColsFrom(active: string, built: string[]): string[] {
  const other = active === DEFAULT_PIPELINE ? built.find((b) => b !== DEFAULT_PIPELINE) : active;
  return [...new Set([...(built.includes(DEFAULT_PIPELINE) ? [DEFAULT_PIPELINE] : []), ...(other ? [other] : [])])];
}

export function moveCol(cols: string[], name: string, index: number): string[] {
  const rest = cols.filter((c) => c !== name);
  rest.splice(Math.max(0, Math.min(index, rest.length)), 0, name);
  return rest;
}

/** The sidecars cut a block's text at this many characters (TEXT_LIMIT in reflow/render/sidecars.py). */
export const SIDECAR_TEXT_LIMIT = 200;

/** The texts longer than SIDECAR_TEXT_LIMIT of a tree's nodes on `page`, by id. */
export function fullTextsOf(tree: unknown, page: number): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (v: unknown): void => {
    if (!v || typeof v !== "object") return;
    const n = v as { id?: unknown; page?: unknown; text?: unknown };
    if (typeof n.id === "string" && n.page === page && typeof n.text === "string" && n.text.length > SIDECAR_TEXT_LIMIT) out[n.id] = n.text;
    for (const x of Array.isArray(v) ? v : Object.values(v)) walk(x);
  };
  walk(tree);
  return out;
}
