// Review items: the parts of a converted document that reflow could not finish on its own
// (public/docs/<slug>/review.json). Plain functions only, so server and client both use them.

// One entry of review.json, plus the page's printed label (added by the page loader). Boxes are [x0, top, x1, bottom] in PDF points with the origin at the
// top-left.
export type ReviewItem = {
  id: string;
  severity: "error" | "warning";
  role: string;
  page: number | null;
  confidence: number;
  source: string;
  needs: string | null;
  warn: string | null;
  /** What the review queue shows for the item's code (reflow's code table). */
  label?: string | null;
  text: string;
  /** Repeated warnings on one page; id and bbox still identify the first block. */
  group?: { nodeIds: string[] };
  alt?: string | null;
  description?: string | null;
  /** The number the document prints on that page, when it differs from the PDF page index. */
  pageLabel?: string | null;
  bbox: number[] | null;
  pageSize: number[] | null;
  /** Words OCR may have misread, each with the reading the document's own evidence suggests. */
  suggestions?: OcrSuggestion[];
  /** Every place on the page a reviewer must look, each with the code of the fact that found it.
   * A page item's bbox is their union; a node item's bbox stays the node's box. */
  targets?: ReviewTarget[];
  /** What a decision copies to find the node or figure again (a figure's has a crop hash). */
  anchor?: ReviewAnchor;
  /** The facts a reviewer can settle, one fingerprint each (a group item lists its members'). */
  findings?: ReviewFinding[];
};

type ReviewAnchor = { page: number; bbox: number[]; text_sha: string; crop_sha: string; hint: string };
export type ReviewFinding = { code: string; severity: "error" | "warning"; id: string };

/** OCR's reading of a word, the reading the document suggests, the rule that found it, and the
 *  box of the OCR line that holds it. The transcript keeps OCR's reading. */
export type OcrSuggestion = {
  was: string; now: string; rule: string; bbox: number[] | null;
  /** What a decision on it copies: the node's anchor, the text on each side of the word, and an id of all of those. */
  anchor?: ReviewAnchor | null; before?: string; after?: string; id?: string;
};

const RULE_NAMES: Record<string, string> = { "zip-digit": "ZIP code digit", "split-word": "split word" };

/** "OCR read ‘8011O’ · suggested ‘80110’ (ZIP code digit)". */
export function suggestionText(s: OcrSuggestion): string {
  return `OCR read ‘${s.was}’ · suggested ‘${s.now}’ (${RULE_NAMES[s.rule] ?? s.rule})`;
}

type ReviewTarget = { code: string; box: number[] };

/** The boxes to draw for an item: its targets, or its bbox when it has none. */
export function reviewBoxes(item: ReviewItem): number[][] {
  if (item.targets?.length) return item.targets.map((t) => t.box);
  return item.bbox ? [item.bbox] : [];
}

/** Every block represented by this queue item, including its first block. */
export function reviewNodeIds(item: ReviewItem): string[] {
  // A decision's orphan, a page item (`page:N`) and a document item carry a page and maybe a box, and no node:
  // their ids are not node ids.
  if (item.role === "decision" || item.role === "page" || item.role === "doc") return [];
  return item.group?.nodeIds ?? [item.id];
}

/** The item a deep link names: by its own id (a node-less item has no node), or by one of its nodes. */
export function itemFor(items: ReviewItem[], id: string | null | undefined, warn?: string | null): ReviewItem | null {
  if (!id) return null;
  // Document items all carry the document's id, so `warn` (the finding's code) picks one of them.
  return (warn ? items.find((i) => i.id === id && i.warn === warn) : undefined) ?? items.find((i) => i.id === id) ?? items.find((i) => reviewNodeIds(i).includes(id)) ?? null;
}

// The type of problem an item reports. The catalogue groups items by it.
export function issueType(item: ReviewItem): string {
  if (item.label) return item.label;
  if (item.needs === "alt") return "Figure missing alt text";
  if (item.needs) return `Needs ${item.needs}`;
  if (item.warn) return item.label ?? item.warn;
  return "Low-confidence structure";
}

/** The catalogue's grouping: by type, the largest group first (ties in first-seen order), each
 *  group's items in walk order. */
export function groupByType(items: ReviewItem[]): [string, ReviewItem[]][] {
  const groups = new Map<string, ReviewItem[]>();
  for (const item of items) {
    const type = issueType(item);
    groups.set(type, [...(groups.get(type) ?? []), item]);
  }
  return [...groups].sort((a, b) => b[1].length - a[1].length);
}
