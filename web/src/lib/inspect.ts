// Shared types and look of the inspector. A plain module: the server page computes these, and
// the client workspace draws them.

import type { ReviewItem } from "@/lib/review";

// One thing the converter noted about a node (a Fact in the sidecar), with the review label of
// its code. A code the label table does not know shows the code itself.
export type InspectFact = { code: string; label: string; source: string; detail?: string; data?: Record<string, unknown> };

// A node of the page's layout sidecar (layout/NNNN.json): what the converter decided, with its
// box. Boxes are [x0, top, x1, bottom] in PDF points with the origin at the top-left.
export type InspectNode = {
  id: string;
  role: string;
  bbox: number[];
  source: string;
  confidence: number;
  text: string;
  attrs: Record<string, unknown>;
  facts: InspectFact[];
};

// A section of the page (the sidecar's `sections`): the heading it opens and its box.
type InspectSection = { id: string; level: number; bbox: number[] };

// One page's sidecar, as the server reads it.
export type PageSidecar = {
  width: number;
  height: number;
  nodes: InspectNode[];
  sections: InspectSection[];
};

// What the line pass saw on the page (trace.json): every text line, why it was dropped if it
// was, the reading-order split, and the top-level gutters between columns.
export type InspectTrace = {
  lines: { bbox: number[]; text: string; size: number; artifact: string | null }[];
  split: string;
  gutters: number[][];
};

// The review walk, computed on the server: where the selected issue sits in the walk order,
// and the links that move through it. Every link is a URL, so the state is shareable.
export type ReviewBar = {
  total: number;
  /** 1-based position of the selected item in the walk order, or null. */
  position: number | null;
  current: { type: string; page: string | null } | null;
  prevIssueHref: string | null;
  nextIssueHref: string | null;
  pageIssues: {
    id: string;
    nodeIds: string[];
    label: string;
    href: string;
    selected: boolean;
    error: boolean;
    /** What OCR read and what the document suggests, with the box of the line to draw. For an item
     *  with no node (an orphaned decision), the item's own text and box, so the page draws it. */
    suggestions: { text: string; bbox: number[] | null }[];
    /** The queue item itself: the decision controls read its suggestions, findings and anchor. */
    item: ReviewItem;
  }[];
};

// One colour per role, so the page reads at a glance. Table cells are off by default:
// they sit inside the table box and would hide it.
// All at least 4.9:1 on the white page image (WCAG 1.4.11 needs 3:1). Boxes are drawn on bg-white
// in both themes, so one palette serves both.
const ROLE_STYLE: Record<string, { label: string; color: string; on: boolean }> = {
  h1: { label: "Heading 1", color: "#1d4ed8", on: true },
  h2: { label: "Heading 2", color: "#1d4ed8", on: true },
  h3: { label: "Heading 3", color: "#1d4ed8", on: true },
  p: { label: "Paragraph", color: "#15803d", on: true },
  figure: { label: "Figure", color: "#7e22ce", on: true },
  caption: { label: "Caption", color: "#0e7490", on: true },
  table: { label: "Table", color: "#c2410c", on: true },
  tr: { label: "Table row", color: "#c2410c", on: false },
  th: { label: "Table header cell", color: "#c2410c", on: false },
  td: { label: "Table cell", color: "#c2410c", on: false },
  list: { label: "List", color: "#a16207", on: true },
  li: { label: "List item", color: "#a16207", on: true },
  artifact: { label: "Page furniture (dropped)", color: "#57534e", on: true },
};

const FALLBACK = { label: "Other", color: "#be185d", on: true };

export function styleFor(role: string): { label: string; color: string; on: boolean } {
  return ROLE_STYLE[role] ?? { ...FALLBACK, label: role };
}
