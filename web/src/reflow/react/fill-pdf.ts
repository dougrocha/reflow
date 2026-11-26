// Fill the original PDF's AcroForm fields with what the reader typed, in the browser. Loaded
// on demand (import() from DocumentViewer), and pdf-lib is imported only when a fill runs, so
// neither is part of the viewer's first load. Nothing is sent anywhere: the original is
// fetched from the same place the viewer already got it, filled in memory, and handed to the
// browser as a download or a new tab.

import { CONTROL_SELECTOR, typedFor, type TypedValues } from "./form-values";

/** One typed control, mapped to its PDF field by the converter's data-pdf-* attributes. */
export type FieldFill = {
  id: string;
  field: string;
  kind: "text" | "checkbox" | "radio" | "select";
  exportValue?: string;
  text?: string;
  checked?: boolean;
  selected?: string[];
};

type FillReport = {
  bytes: Uint8Array<ArrayBuffer>;
  filled: number;
  /** Fields the PDF did not have, or whose value it would not take. */
  skipped: string[];
  /** Text fields whose value the standard font cannot draw (CJK and the like). The value is
   *  stored in the field and the form is marked for redrawing, but no appearance was made. */
  noAppearance: string[];
  /** The form carried an XFA layer, which was dropped so the AcroForm values are the ones shown. */
  droppedXfa: boolean;
};

/** Thrown with a message fit to show the reader. */
export class FillError extends Error {}

/**
 * Pair each typed control with its PDF field, reading the data-pdf-* attributes from the
 * transcript's HTML (every page, so pages never scrolled to are covered). Only controls the
 * reader touched are returned: every other field keeps whatever the PDF already holds. Radios
 * of a group are all returned, ticked or not: where each radio is a checkbox field of its own
 * (a Yes / No pair), the unticked one must be cleared in the PDF; where they share one radio
 * field, fillPdf takes the ticked one and ignores the rest.
 */
export function collectFills(pageHtml: Iterable<string>, values: TypedValues): FieldFill[] {
  const fills: FieldFill[] = [];
  for (const html of pageHtml) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    for (const el of tpl.content.querySelectorAll(CONTROL_SELECTOR)) {
      const t = typedFor(el, values);
      const field = el.getAttribute("data-pdf-field");
      if (!t || !field) continue;
      const type = (el.getAttribute("type") ?? "text").toLowerCase();
      if (type === "password") continue;
      const kind = el.tagName === "SELECT" ? "select" : type === "checkbox" || type === "radio" ? type : "text";
      fills.push({ id: el.id, field, kind, exportValue: el.getAttribute("data-pdf-value") ?? undefined, ...t });
    }
  }
  return fills;
}

/** True when the standard (WinAnsi) font can draw every character of `text`. */
export function drawable(encode: (text: string) => unknown, text: string): boolean {
  try {
    encode(text.replace(/[\r\n\t]/g, " "));
    return true;
  } catch {
    return false;
  }
}

/** Fill the PDF's bytes. Throws FillError when there is nothing to fill. */
export async function fillPdf(source: ArrayBuffer, fills: FieldFill[]): Promise<FillReport> {
  const { PDFDocument, PDFName, PDFDict, PDFHexString, PDFBool, StandardFonts, PDFCheckBox, PDFDropdown, PDFOptionList, PDFRadioGroup, PDFTextField } = await import("pdf-lib");
  let doc;
  try {
    doc = await PDFDocument.load(source, { updateMetadata: false });
  } catch {
    throw new FillError("This PDF is protected or damaged, so it cannot be filled in the browser. Download the transcript (HTML) instead.");
  }
  // Look for an XFA layer before getForm(), which would hide it. An XFA layer takes precedence
  // in Adobe's reader and would hide the AcroForm values, so it is dropped from the copy.
  const acro = doc.catalog.lookupMaybe(PDFName.of("AcroForm"), PDFDict);
  const xfa = PDFName.of("XFA");
  const droppedXfa = !!acro?.has(xfa);
  if (acro && droppedXfa) acro.delete(xfa);

  const form = doc.getForm();
  if (form.getFields().length === 0) {
    throw new FillError("This PDF no longer has fillable fields (it was flattened, or its form is XFA only), so it cannot be filled. Download the transcript (HTML) to keep what you typed.");
  }
  const font = await doc.embedFont(StandardFonts.Helvetica);
  const needRedraw = () => form.acroForm.dict.set(PDFName.of("NeedAppearances"), PDFBool.True);

  // Checkboxes that share a field (several widgets) are decided together: the export values
  // ticked for it, in order. A radio is read the same way when its PDF field turns out to be a
  // checkbox (the converter makes radios of Yes / No boxes, each a field of its own).
  const ticked = new Map<string, string[]>();
  for (const f of fills) {
    if (f.kind !== "checkbox" && f.kind !== "radio") continue;
    const list = ticked.get(f.field) ?? [];
    if (f.checked && f.exportValue) list.push(f.exportValue);
    if (f.checked && !f.exportValue) list.push("");
    ticked.set(f.field, list);
  }

  const skipped: string[] = [];
  const noAppearance: string[] = [];
  const textFields: string[] = [];
  const done = new Set<string>();
  let filled = 0;
  for (const f of fills) {
    const field = form.getFieldMaybe(f.field);
    try {
      if (!field) throw new Error("missing");
      if ((f.kind === "checkbox" || f.kind === "radio") && field instanceof PDFCheckBox) {
        if (done.has(f.field)) continue;
        done.add(f.field);
        const want = ticked.get(f.field)?.at(-1);
        const widgets = field.acroField.getWidgets();
        const match = want ? widgets.find((w) => w.getOnValue()?.decodeText() === want) : undefined;
        if (want === undefined) field.uncheck();
        else if (!match) field.check(); // no widget has this export value: the field's own on-value
        else {
          // Tick this export value's widget(s), not the first widget's on-value.
          const on = match.getOnValue()!;
          field.acroField.dict.set(PDFName.of("V"), on);
          for (const w of widgets) w.setAppearanceState(w.getOnValue()?.decodeText() === want ? on : PDFName.of("Off"));
        }
      } else if (f.kind === "radio" && field instanceof PDFRadioGroup) {
        if (!f.checked) continue; // the choice is the ticked member; the others need nothing
        if (!f.exportValue) throw new Error("no export value");
        field.select(f.exportValue);
      } else if (f.kind === "select" && (field instanceof PDFDropdown || field instanceof PDFOptionList)) {
        field.select(f.selected ?? []);
      } else if (f.kind === "text" && field instanceof PDFTextField) {
        const text = f.text ?? "";
        if (!text) field.setText(undefined);
        else if (drawable((t) => font.encodeText(t), text)) { field.setText(text); textFields.push(f.field); }
        else {
          // Store the value only (UTF-16), with no appearance; readers redraw it from /V.
          field.acroField.setValue(PDFHexString.fromText(text));
          noAppearance.push(f.field);
          needRedraw();
        }
      } else {
        throw new Error("type mismatch");
      }
      filled++;
    } catch {
      skipped.push(f.field);
    }
  }
  // One bad field must never block the export: if drawing the fields fails, save with the
  // values only and let the reader redraw them.
  let bytes: Uint8Array;
  try {
    bytes = await doc.save();
  } catch {
    needRedraw();
    noAppearance.push(...textFields);
    try {
      bytes = await doc.save({ updateFieldAppearances: false });
    } catch {
      throw new FillError("The filled PDF could not be written. Download the transcript (HTML) to keep what you typed.");
    }
  }
  return { bytes: new Uint8Array(bytes) as Uint8Array<ArrayBuffer>, filled, skipped, noAppearance, droppedXfa };
}
