// Focus across the sidebar's two homes. The sidebar is a column on a wide screen and a sheet on a
// narrow one; crossing the breakpoint swaps one for the other, so a focused control in the old one
// would be gone and focus would drop to <body>. The viewer notes where focus is as it moves (a
// FocusSpot) and, when the layout changes, puts it on the matching control in the new home.

/** Where focus was in the sidebar, and a selector that finds the same control in either home. */
export type FocusSpot = { home: "column" | "sheet"; selector: string | null };

const COLUMN = "aside[aria-label]";
const SHEET = "[data-slot=sheet-content]";

/** The same control, found by what it is rather than by where it is: null when it has no twin. */
function selectorFor(el: Element): string | null {
  if (el.matches("form[role=search] input")) return "form[role=search] input";
  if (el.matches("form[role=search] button[type=submit]")) return "form[role=search] button[type=submit]";
  if (el.matches("article[aria-labelledby]")) return "article[aria-labelledby]";
  for (const attr of ["data-ask-show-page", "data-ask-show-transcript", "data-ask-clear"]) {
    if (el.hasAttribute(attr)) return `[${attr}="${el.getAttribute(attr) ?? ""}"]`;
  }
  const controls = el.getAttribute("aria-controls");
  if (controls?.endsWith("-ask-panel")) return "button[aria-controls$='-ask-panel']";
  if (controls?.endsWith("-toc-panel")) return "button[aria-controls$='-toc-panel']";
  if (el.matches("nav a[href]")) return `nav a[href="${el.getAttribute("href")?.replace(/"/g, '\\"')}"]`;
  return null;
}

/** The spot a newly focused element is, or null when it is not in the sidebar. */
export function spotOf(el: EventTarget | null): FocusSpot | null {
  if (!(el instanceof Element)) return null;
  if (el.closest(SHEET)) return { home: "sheet", selector: selectorFor(el) };
  if (el.closest(COLUMN)) return { home: "column", selector: selectorFor(el) };
  return null;
}

/** The control for a spot inside `home`, or null when that home has none (the TOC header stands in). */
export function findSpot(home: "column" | "sheet", spot: FocusSpot): HTMLElement | null {
  const root = document.querySelector<HTMLElement>(home === "column" ? COLUMN : SHEET);
  if (!root) return null;
  return (spot.selector ? root.querySelector<HTMLElement>(spot.selector) : null)
    ?? root.querySelector<HTMLElement>("button[aria-controls$='-toc-panel']");
}

/**
 * Focuses what `find` returns as soon as it exists and can take focus, for up to about half a
 * second: the sheet that is closing, or the portal that is being drawn, is still in the way for a
 * frame or two. Gives up quietly. Returns a cancel function.
 */
export function focusWhenPossible(find: () => HTMLElement | null, frames = 30): () => void {
  let id = 0;
  let left = frames;
  const tick = () => {
    const el = find();
    el?.focus();
    if (el && document.activeElement === el) return;
    if (--left > 0) id = requestAnimationFrame(tick);
  };
  tick();
  return () => cancelAnimationFrame(id);
}
