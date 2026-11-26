import { fold, pending } from "./fold";
import type { Anchor, Box, DecisionRecord, Range, Ledger, Snapshot } from "./types";

// What GET /api/decisions/<slug> says about each decision: the folded store set against the
// published decisions.json. A pure function of the two.

type Status = "pending" | "applied" | "satisfied" | "dismissed" | "orphaned" | "stale" | "refused" | "undone";

type Common = {
  id: string;
  at: string;
  status: Status;
  /** True when the next build will change something for this decision: its line, or an undo of it,
   *  is not in the published build, or the build did not report it. */
  pending: boolean;
  supersedes: string | null;
  /** The page its anchor names (a dismissal: the page the build found its finding on). */
  page: number | null;
  bbox: Box | null;
  /** The ledger's reason when the build orphaned or refused it. */
  reason: string | null;
};

export type DecisionView = Common & (
  | { kind: "correct"; was: string; now: string; anchor: Anchor; range: Range }
  | { kind: "keep"; was: string; anchor: Anchor; range: Range }
  | { kind: "alt"; text: string; decorative: boolean; anchor: Anchor }
  | { kind: "dismiss"; findings: string[] }
  | { kind: "proposal"; proposal: string; verdict: "accept" | "reject" }
  | { kind: "font-map"; map: string; verdict: "accept" | "reject" }
);

const NOTHING = { store: null, bytes: 0 } as const;

type Verdict = { status: Status; reason: string | null; page: number | null };

function settled(rec: DecisionRecord, ledger: Ledger): Verdict | null {
  const id = rec.id;
  const has = <T extends { id: string }>(rows: T[]): T[] => rows.filter((r) => r.id === id);
  const applied = has(ledger.applied)[0];
  if (applied) return { status: "applied", reason: null, page: applied.page };
  const satisfied = has(ledger.satisfied)[0];
  if (satisfied) return { status: "satisfied", reason: null, page: satisfied.page };
  // a font-map answer also settles its finding (ledger.dismissed): it is applied, not dismissed
  if (has(ledger.fonts ?? [])[0]) return { status: "applied", reason: null, page: null };
  const dismissed = has(ledger.dismissed)[0];
  if (dismissed) return { status: "dismissed", reason: null, page: dismissed.page };
  const orphaned = has(ledger.orphaned)[0];
  if (orphaned) return { status: "orphaned", reason: orphaned.reason, page: orphaned.page };
  const refused = has(ledger.refused)[0];
  if (refused) return { status: "refused", reason: refused.reason, page: null };
  if (has(ledger.stale)[0]) return { status: "stale", reason: null, page: null };
  if (has(ledger.proposals)[0]) return { status: "applied", reason: null, page: null };
  return null;
}

/** The decisions of a store that are active or undone, in store order, with their status for the
 *  published build. A decision a later one replaced is history and is not listed. `ledger` is null
 *  for a build with no decisions.json: every decision is then pending. */
export function decisionViews(snapshot: Snapshot, ledger: Ledger | null): { decisions: DecisionView[]; pending: number } {
  const folded = fold(snapshot);
  const waiting = pending(snapshot, ledger ? { store: ledger.store.id, bytes: ledger.store.bytes } : NOTHING);
  const waitingIds = new Set<string>();
  for (const e of waiting) {
    if (e.parsed.type !== "record") continue;
    waitingIds.add(e.parsed.record.id);
    if (e.parsed.record.kind === "undo") waitingIds.add(e.parsed.record.of);
  }
  const active = new Set(folded.active.map((r) => r.id));
  const undone = new Set(folded.undone);
  const listed = new Set<string>();
  const decisions: DecisionView[] = [];
  for (const { parsed } of snapshot.entries) {
    if (parsed.type !== "record" || parsed.record.kind === "undo" || parsed.record.kind === "reattach") continue;
    const rec = parsed.record;
    if (listed.has(rec.id) || !(active.has(rec.id) || undone.has(rec.id))) continue;
    listed.add(rec.id);
    const entryWaiting = waiting.some((e) => e.parsed.type === "record" && e.parsed.record.id === rec.id);
    const verdict = undone.has(rec.id) ? { status: "undone" as const, reason: null, page: null }
      : entryWaiting || !ledger ? null : settled(rec, ledger);
    const status: Status = verdict?.status ?? "pending";
    const common = (page: number | null, bbox: Box | null): Common => ({
      // An undone decision changes the next build only when the build has it: one whose own line
      // was never built and was undone since cancels out.
      id: rec.id, at: rec.at, status, pending: status === "undone" ? !entryWaiting && waitingIds.has(rec.id) : status === "pending" || waitingIds.has(rec.id),
      supersedes: "supersedes" in rec ? rec.supersedes : null, page: page ?? verdict?.page ?? null, bbox, reason: verdict?.reason ?? null,
    });
    switch (rec.kind) {
      case "correct": decisions.push({ ...common(rec.anchor.page, rec.anchor.bbox), kind: "correct", was: rec.range.was, now: rec.now, anchor: rec.anchor, range: rec.range }); break;
      case "keep": decisions.push({ ...common(rec.anchor.page, rec.anchor.bbox), kind: "keep", was: rec.range.was, anchor: rec.anchor, range: rec.range }); break;
      case "alt": decisions.push({ ...common(rec.anchor.page, rec.anchor.bbox), kind: "alt", text: rec.text, decorative: rec.decorative, anchor: rec.anchor }); break;
      case "dismiss": decisions.push({ ...common(null, null), kind: "dismiss", findings: rec.findings }); break;
      case "proposal": decisions.push({ ...common(null, null), kind: "proposal", proposal: rec.proposal, verdict: rec.verdict }); break;
      case "font-map": decisions.push({ ...common(null, null), kind: "font-map", map: rec.map, verdict: rec.verdict }); break;
    }
  }
  return { decisions, pending: waiting.filter((e) => e.parsed.type === "record").length };
}
