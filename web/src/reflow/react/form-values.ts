// What the reader typed into the transcript's form controls, and how it gets written back
// into markup (for the transcript download and print). Small and DOM-light: it is part of the
// viewer's first load, so the heavy parts (pdf-lib, the HTML round trip) live in other files.

/** One control's live state. Only the fields that fit its kind are set. */
type Typed = { text?: string; checked?: boolean; selected?: string[] };
/** Typed state by control id (the converter gives every control a unique id, f<page>-<n>). */
export type TypedValues = Map<string, Typed>;

type Control = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;

/** The controls the converter writes: inputs, textareas and selects with a document-unique id. */
export const CONTROL_SELECTOR = "input[id], textarea[id], select[id]";

/**
 * Radios are decided by PDF field, not by page: one group can have members on several pages.
 * The store holds the latest choice under this key, as the chosen control's id in `text`.
 */
export const radioKey = (field: string) => `radio:${field}`;

const isToggle = (el: Element): el is HTMLInputElement =>
  el instanceof HTMLInputElement && (el.type === "checkbox" || el.type === "radio");

function readControl(el: Control): Typed {
  if (el instanceof HTMLSelectElement) return { selected: Array.from(el.selectedOptions, (o) => o.value) };
  if (isToggle(el)) return { checked: el.checked };
  return { text: el.value };
}

/**
 * A ticked radio and the radios of its group (same `name`). Ticking one unticks the others
 * without an event of their own, so the whole group is recorded. Radios of one PDF field (a
 * real radio group) share one latest choice under `radioKey`. Radios that each stand for a PDF
 * checkbox field of their own (the converter makes these from Yes / No and "check only one"
 * boxes) have a key per field, so a choice recorded for a member that is no longer ticked is
 * dropped, or `typedFor` would tick it again.
 */
export function recordRadio(
  values: TypedValues,
  chosen: { id: string; field: string | null },
  group: { id: string; field: string | null; checked: boolean }[],
): void {
  for (const r of group) {
    values.set(r.id, { checked: r.checked });
    if (r.field && r.field !== chosen.field) values.delete(radioKey(r.field));
  }
  if (chosen.field) values.set(radioKey(chosen.field), { text: chosen.id }); // the latest choice wins
}

/**
 * Record what an input or change event reports. Ticking a radio unticks the others of its
 * group without an event of their own, so a radio records its whole group.
 */
export function record(values: TypedValues, el: Element): void {
  if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) || !el.id) return;
  if (el instanceof HTMLInputElement && el.type === "radio") {
    const scope = el.closest("[data-transcript]") ?? document;
    const group = el.name
      ? Array.from(scope.querySelectorAll<HTMLInputElement>('input[type="radio"][id]'))
          .filter((r) => r.name === el.name)
          .map((r) => ({ id: r.id, field: r.getAttribute("data-pdf-field"), checked: r.checked }))
      : [];
    recordRadio(values, { id: el.id, field: el.getAttribute("data-pdf-field") }, group);
    if (!el.name) values.set(el.id, readControl(el));
    return;
  }
  values.set(el.id, readControl(el));
}

/** A control's recorded state; a radio whose PDF field has a recorded choice follows that choice. */
export function typedFor(el: Element, values: TypedValues): Typed | undefined {
  const field = el.getAttribute("data-pdf-field");
  if (field && el.getAttribute("type") === "radio") {
    const chosen = values.get(radioKey(field));
    if (chosen) return { checked: chosen.text === el.id };
  }
  return values.get(el.id);
}

/** Put recorded state back into live controls (a page card that has been mounted again). */
export function restore(root: Element, values: TypedValues): void {
  if (!values.size) return;
  for (const el of root.querySelectorAll<Control>(CONTROL_SELECTOR)) {
    const t = typedFor(el, values);
    if (!t) continue;
    if (el instanceof HTMLSelectElement) {
      for (const o of el.options) o.selected = !!t.selected?.includes(o.value);
    } else if (isToggle(el)) {
      el.checked = !!t.checked;
    } else if (t.text !== undefined) {
      el.value = t.text;
    }
  }
}

/**
 * The minimal surface setMarkup needs, so the logic can be tested without a browser DOM.
 */
export type MarkupControl = {
  kind: "text" | "checkbox" | "radio" | "textarea" | "select" | "password";
  setAttribute(name: string, value: string): void;
  removeAttribute(name: string): void;
  setText(text: string): void;
  options: { value: string; setAttribute(n: string, v: string): void; removeAttribute(n: string): void }[];
};

/**
 * Write typed state into a control's markup: the value, checked and selected attributes (and
 * a textarea's text), which is what a saved or printed copy reads. Passwords are never written.
 */
export function setMarkup(c: MarkupControl, t: Typed): void {
  switch (c.kind) {
    case "password":
      return;
    case "checkbox":
    case "radio":
      if (t.checked) c.setAttribute("checked", ""); else c.removeAttribute("checked");
      return;
    case "select":
      for (const o of c.options) {
        if (t.selected?.includes(o.value)) o.setAttribute("selected", ""); else o.removeAttribute("selected");
      }
      return;
    case "textarea":
      c.setText(t.text ?? "");
      return;
    default:
      c.setAttribute("value", t.text ?? "");
  }
}

function asMarkupControl(el: Element): MarkupControl | null {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute("type") ?? "text").toLowerCase();
  let kind: MarkupControl["kind"];
  if (tag === "select" || tag === "textarea") kind = tag;
  else if (tag === "input") kind = type === "checkbox" || type === "radio" || type === "password" ? type : "text";
  else return null;
  return {
    kind,
    setAttribute: (n, v) => el.setAttribute(n, v),
    removeAttribute: (n) => el.removeAttribute(n),
    setText: (s) => { el.textContent = s; },
    options: Array.from(el.querySelectorAll("option")).map((o) => ({
      value: o.getAttribute("value") ?? o.textContent ?? "",
      setAttribute: (n, v) => o.setAttribute(n, v),
      removeAttribute: (n) => o.removeAttribute(n),
    })),
  };
}

/**
 * A page's HTML with the typed values written into its controls. With nothing typed for the
 * page the string comes back untouched, so an unfilled document serializes as it always did.
 */
export function fillMarkup(html: string, values: TypedValues): string {
  if (!values.size) return html;
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  let touched = false;
  for (const el of tpl.content.querySelectorAll(CONTROL_SELECTOR)) {
    const t = typedFor(el, values);
    const c = t && asMarkupControl(el);
    if (!c || !t) continue;
    setMarkup(c, t);
    touched = true;
  }
  return touched ? tpl.innerHTML : html;
}
