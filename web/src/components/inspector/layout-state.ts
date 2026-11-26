import { PANEL_ORDER, type PanelId } from "./panels";
import { readStored, removeStored, writeStored } from "@/lib/storage";

export const LAYOUT_KEY = "reflow:inspect-layout";
const OLD_PANELS_KEY = "reflow:inspect-panels"; // the three-column layout, before v2

type InspectorLayout = {
  v: 2;
  /** Shown panels, in PANEL_ORDER; never empty. Where they sit on a desktop is the dock's own
   *  saved layout (DockLayout). */
  visible: PanelId[];
};

export const DEFAULT_LAYOUT: InspectorLayout = { v: 2, visible: ["page", "transcript", "details"] };

/** A stored layout's shown panels. Sizes stored before the dock are ignored. */
export function validLayout(raw: unknown): InspectorLayout | null {
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Record<string, unknown>;
  if (r.v !== 2) return null;
  const stored = Array.isArray(r.visible) ? r.visible : [];
  const visible = PANEL_ORDER.filter((p) => stored.includes(p));
  return visible.length ? { v: 2, visible } : null;
}

/**
 * One-time move from the three-column key. Only the shown panels carry over; the old sizes were
 * for three columns and are dropped. Runs on mount; safe to call more than once.
 */
export function migrateLayout(): void {
  const old = readStored(OLD_PANELS_KEY);
  if (old === null) return;
  if (readStored(LAYOUT_KEY) !== null) {
    // The new layout already exists: the old key has nothing left to say.
    removeStored(OLD_PANELS_KEY);
    return;
  }
  let visible = DEFAULT_LAYOUT.visible;
  try {
    const parsed = JSON.parse(old) as { visible?: unknown };
    const v = Array.isArray(parsed.visible) ? PANEL_ORDER.filter((p) => (parsed.visible as unknown[]).includes(p)) : [];
    if (v.length) visible = v;
  } catch { /* unreadable: defaults */ }
  writeStored(LAYOUT_KEY, JSON.stringify({ ...DEFAULT_LAYOUT, visible }));
  removeStored(OLD_PANELS_KEY);
}
