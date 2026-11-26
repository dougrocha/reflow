// The document viewer's keyboard shortcuts. A plain module (no "use client"), so the viewer,
// its help dialog and the server-rendered /about#keyboard table all read the same list.
//
// Every shortcut is Alt+Shift+<letter>: WCAG 2.1.4 asks that a single-character shortcut can be
// turned off or remapped, and a chord with modifiers is exempt. They match on KeyboardEvent.code
// (the physical key), because Alt+Shift changes `key` on macOS (Option+V types a lozenge).

type ViewerShortcutId = "view" | "page" | "outline" | "next" | "prev" | "find" | "ask" | "answer" | "help";

type ViewerShortcut = {
  id: ViewerShortcutId;
  /** KeyboardEvent.code. */
  code: string;
  /** The letter on the key, for display. */
  letter: string;
  action: string;
  /** Works while the cursor is in a text field. True only for shortcuts that move focus away. */
  inFields: boolean;
};

export const VIEWER_SHORTCUTS: ViewerShortcut[] = [
  { id: "view", code: "KeyV", letter: "V", action: "Switch between the accessible transcript and the original pages", inFields: false },
  { id: "page", code: "KeyG", letter: "G", action: "Go to a page: move to the page number box", inFields: true },
  { id: "outline", code: "KeyO", letter: "O", action: "Move to the contents (opens it on small screens)", inFields: true },
  { id: "next", code: "KeyN", letter: "N", action: "Next page", inFields: false },
  { id: "prev", code: "KeyB", letter: "B", action: "Previous page", inFields: false },
  { id: "find", code: "KeyF", letter: "F", action: "Find in the document: move to the find box", inFields: true },
  { id: "ask", code: "KeyA", letter: "A", action: "Ask a question about the document: move to the question box", inFields: true },
  { id: "answer", code: "KeyR", letter: "R", action: "Return to the answer to your question", inFields: true },
  { id: "help", code: "KeyK", letter: "K", action: "Show these keyboard shortcuts", inFields: true },
];

/** The keys to press, as separate labels: ["Alt", "Shift", "V"]. */
export function shortcutKeys(s: ViewerShortcut): string[] {
  return ["Alt", "Shift", s.letter];
}

/** The shortcut an Alt+Shift key press stands for, or undefined. */
export function shortcutFor(e: KeyboardEvent): ViewerShortcut | undefined {
  if (!e.altKey || !e.shiftKey || e.ctrlKey || e.metaKey || e.repeat) return undefined;
  return VIEWER_SHORTCUTS.find((s) => s.code === e.code);
}

/** The key press is in a text field, a select or other editable element, where a single-key
 *  shortcut would steal the typing. */
export function typingIn(target: EventTarget | null): boolean {
  const t = target as HTMLElement | null;
  return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}
