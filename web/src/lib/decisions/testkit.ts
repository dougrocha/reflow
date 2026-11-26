import { randomBytes } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Anchor, Correct, Keep, AltText } from "./types";

// Records and a scratch data root for the decisions tests.

export const PDF = "a1".repeat(32);
export const OTHER_PDF = "b2".repeat(32);

export const tempRoot = () => mkdtemp(join(tmpdir(), "reflow-decisions-test-"));
export const removeRoot = (dir: string) => rm(dir, { recursive: true, force: true });

export const textAnchor = (over: Partial<Anchor> = {}): Anchor =>
  ({ page: 1, bbox: [72, 100, 300, 112], text_sha: "0123456789abcdef", crop_sha: "", hint: "n-1", ...over });
const figureAnchor = (over: Partial<Anchor> = {}): Anchor =>
  ({ page: 2, bbox: [10, 20, 110, 220], text_sha: "", crop_sha: "fedcba9876543210", hint: "f-1", ...over });

export const rid = (): string => `m2k9x4q1-${randomBytes(4).toString("hex")}`;

export const correct = (over: Partial<Correct> = {}): Correct => ({
  kind: "correct", id: rid(), at: "2026-10-28T09:14:03Z", pdf: PDF, anchor: textAnchor(),
  range: { was: "8011O", before: "CO ", after: "", rule: "zip-digit" }, now: "80110", supersedes: null, ...over,
});
export const keep = (over: Partial<Keep> = {}): Keep => ({
  kind: "keep", id: rid(), at: "2026-10-28T09:14:03Z", pdf: PDF, anchor: textAnchor(),
  range: { was: "8011O", before: "CO ", after: "", rule: "zip-digit" }, supersedes: null, ...over,
});
export const alt = (over: Partial<AltText> = {}): AltText => ({
  kind: "alt", id: rid(), at: "2026-10-28T09:14:03Z", pdf: PDF, anchor: figureAnchor(),
  text: "A map", decorative: false, supersedes: null, ...over,
});

// -- a published document ------------------------------------------------------------------------

import { mkdir, rm as rmFile, writeFile } from "node:fs/promises";
import type { Ledger } from "./types";
import type { Publication, Publications } from "../builds/record";

export const FINDING = "0a1b2c3d4e5f6789";
export const SUGGESTION_ID = "9f8e7d6c5b4a3210";

const suggestion = (over: Record<string, unknown> = {}) => ({
  was: "8011O", now: "80110", rule: "zip-digit", bbox: [72, 100, 300, 112], anchor: textAnchor(),
  before: "CO ", after: "", id: SUGGESTION_ID, ...over,
});

/** A review item the way review.json holds it, with the fields the decisions read. */
export const ocrItem = (over: Record<string, unknown> = {}) => ({
  id: "5:1a2b3c4d5e6f", severity: "warning", role: "p", page: 5, confidence: 0.4, source: "ocr", needs: null,
  warn: "ocr-suggestion", label: "OCR may have misread a word", reasons: [], text: "CO 8011O", bbox: [72, 100, 300, 112],
  pageSize: [612, 792], targets: [], anchor: textAnchor(),
  findings: [{ code: "ocr-suggestion", severity: "warning", id: FINDING }],
  suggestions: [suggestion()], ...over,
});

export const figureItem = (over: Record<string, unknown> = {}) => ({
  id: "2:ffeeddccbbaa", severity: "error", role: "figure", page: 2, confidence: 0.2, source: "heuristic", needs: "alt",
  warn: null, label: "Figure missing alt text", reasons: [], text: "", bbox: [10, 20, 110, 220], pageSize: [612, 792], targets: [],
  anchor: figureAnchor(), findings: [{ code: "missing-alt", severity: "error", id: "1111222233334444" }], alt: null, description: null, ...over,
});

export const warningItem = (over: Record<string, unknown> = {}) => ({
  id: "3:aabbccddeeff", severity: "warning", role: "h2", page: 3, confidence: 0.3, source: "heuristic", needs: null,
  warn: "low-confidence", label: "Low-confidence structure", reasons: ["x"], text: "Heading", bbox: [1, 2, 3, 4], pageSize: [612, 792],
  targets: [], anchor: textAnchor({ page: 3 }), findings: [{ code: "low-confidence", severity: "warning", id: "5555666677778888" }], ...over,
});

export const emptyLedger = (over: Partial<Ledger> = {}): Ledger => ({
  v: 1, source: { sha256: PDF },
  store: { id: null, bytes: 0, lines: 0, torn: 0, unreadable: 0, duplicate: 0, invalid: [], unsupported: [], error: null },
  applied: [], satisfied: [], dismissed: [], refused: [], orphaned: [], stale: [], proposals: [], ...over,
});

type PublishedFiles = { rev?: string; items?: unknown[]; ledger?: Ledger | null; manifest?: unknown };

export async function writePublished(docsRoot: string, slug: string, files: PublishedFiles = {}): Promise<void> {
  const dir = `${docsRoot}/${slug}`;
  await mkdir(dir, { recursive: true });
  await writeFile(`${dir}/manifest.json`, JSON.stringify(files.manifest ?? { v: files.rev ?? "b4915b7aad14" }));
  await writeFile(`${dir}/review.json`, JSON.stringify(files.items ?? [ocrItem(), figureItem(), warningItem()]));
  if (files.ledger !== null) await writeFile(`${dir}/decisions.json`, JSON.stringify(files.ledger ?? emptyLedger()));
  else await rmFile(`${dir}/decisions.json`, { force: true });
}

export const REV = "b4915b7aad14";

/** `publications()` for a test: the publication it answers is the one in `states`, one per call (the last repeats). */
export function publications(...states: Partial<Publication>[]): () => Promise<Publications> {
  let calls = 0;
  return async () => {
    const s = states[Math.min(calls++, states.length - 1)] ?? {};
    return { fw9: { generation: 3, publishedAt: 1, publishing: null, ...s } };
  };
}
