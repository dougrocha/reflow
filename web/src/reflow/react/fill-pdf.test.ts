// Run with `bun test`. Builds small AcroForm PDFs with pdf-lib and fills them through fillPdf,
// then reads the result back, so the export paths that can fail (non-Latin text, multi-widget
// checkboxes, XFA) are covered without a browser.

import { describe, expect, test } from "bun:test";
import { PDFArray, PDFBool, PDFDict, PDFDocument, PDFName, PDFString } from "pdf-lib";
import { drawable, fillPdf, FillError, type FieldFill } from "./fill-pdf";

async function formPdf(build: (form: ReturnType<PDFDocument["getForm"]>, page: ReturnType<PDFDocument["addPage"]>, doc: PDFDocument) => void) {
  const doc = await PDFDocument.create();
  const page = doc.addPage([300, 300]);
  build(doc.getForm(), page, doc);
  // Not updateFieldAppearances: that calls getForm(), which would strip the test's XFA.
  const bytes = await doc.save({ updateFieldAppearances: false });
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

const text = (field: string, value: string): FieldFill => ({ id: field, field, kind: "text", text: value });

describe("fillPdf", () => {
  test("a CJK value does not block the export and is stored in the field", async () => {
    const src = await formPdf((form, page) => {
      form.createTextField("name").addToPage(page, { x: 10, y: 10, width: 100, height: 20 });
      form.createTextField("city").addToPage(page, { x: 10, y: 50, width: 100, height: 20 });
    });
    const report = await fillPdf(src, [text("name", "中文"), text("city", "Lisboa")]);
    expect(report.noAppearance).toEqual(["name"]);
    expect(report.filled).toBe(2);
    const out = await PDFDocument.load(report.bytes);
    expect(out.getForm().getTextField("name").getText()).toBe("中文");
    expect(out.getForm().getTextField("city").getText()).toBe("Lisboa");
    const flag = out.getForm().acroForm.dict.get(PDFName.of("NeedAppearances"));
    expect(flag).toBe(PDFBool.True);
  });

  test("drawable tells WinAnsi text from text it cannot encode", () => {
    const encode = (t: string) => { if (/[^\x20-\x7e]/.test(t)) throw new Error("no"); return t; };
    expect(drawable(encode, "plain\nlines")).toBe(true);
    expect(drawable(encode, "中文")).toBe(false);
  });

  test("a multi-widget checkbox ticks the widget with the chosen export value", async () => {
    const src = await formPdf((form, page) => {
      const box = form.createCheckBox("pick");
      box.addToPage(page, { x: 10, y: 10, width: 12, height: 12 });
      box.addToPage(page, { x: 10, y: 40, width: 12, height: 12 });
      // Give the second widget its own on-state name, as a PDF with "Yes"/"Maybe" boxes would.
      const [first, second] = box.acroField.getWidgets();
      const ap = second.dict.lookup(PDFName.of("AP"), PDFDict).lookup(PDFName.of("N"), PDFDict);
      const on = ap.get(PDFName.of("Yes")) ?? ap.keys().map((k) => ap.get(k))[0];
      ap.delete(PDFName.of("Yes"));
      ap.set(PDFName.of("Maybe"), on!);
      second.dict.set(PDFName.of("AS"), PDFName.of("Off"));
      first.dict.set(PDFName.of("AS"), PDFName.of("Off"));
    });
    const fill: FieldFill = { id: "f1-1", field: "pick", kind: "checkbox", exportValue: "Maybe", checked: true };
    const out = await PDFDocument.load((await fillPdf(src, [fill])).bytes);
    const widgets = out.getForm().getCheckBox("pick").acroField.getWidgets();
    expect(widgets.map((w) => w.getAppearanceState()?.decodeText())).toEqual(["Off", "Maybe"]);
    expect(out.getForm().getCheckBox("pick").acroField.dict.get(PDFName.of("V"))?.toString()).toBe("/Maybe");
  });

  test("radios that are checkbox fields of their own fill each field: the chosen one ticked, the other cleared", async () => {
    const src = await formPdf((form, page) => {
      const yes = form.createCheckBox("Yes");
      yes.addToPage(page, { x: 10, y: 10, width: 12, height: 12 });
      yes.check(); // the PDF has Yes ticked already
      form.createCheckBox("No").addToPage(page, { x: 10, y: 40, width: 12, height: 12 });
    });
    const fills: FieldFill[] = [
      { id: "f1-1", field: "Yes", kind: "radio", exportValue: "On", checked: false },
      { id: "f1-2", field: "No", kind: "radio", exportValue: "On", checked: true },
    ];
    const report = await fillPdf(src, fills);
    expect(report.skipped).toEqual([]);
    expect(report.filled).toBe(2);
    const form = (await PDFDocument.load(report.bytes)).getForm();
    expect(form.getCheckBox("Yes").isChecked()).toBe(false);
    expect(form.getCheckBox("No").isChecked()).toBe(true);
  });

  test("a real radio group takes its ticked member and ignores the unticked ones", async () => {
    const src = await formPdf((form, page) => {
      const group = form.createRadioGroup("plan");
      group.addOptionToPage("gold", page, { x: 10, y: 10, width: 12, height: 12 });
      group.addOptionToPage("silver", page, { x: 10, y: 40, width: 12, height: 12 });
    });
    const fills: FieldFill[] = [
      { id: "f1-1", field: "plan", kind: "radio", exportValue: "gold", checked: false },
      { id: "f1-2", field: "plan", kind: "radio", exportValue: "silver", checked: true },
    ];
    const report = await fillPdf(src, fills);
    expect(report.skipped).toEqual([]);
    expect(report.filled).toBe(1);
    expect((await PDFDocument.load(report.bytes)).getForm().getRadioGroup("plan").getSelected()).toBe("silver");
  });

  test("an XFA layer is noticed and dropped", async () => {
    const src = await formPdf((form, page, doc) => {
      form.createTextField("a").addToPage(page, { x: 10, y: 10, width: 100, height: 20 });
      form.acroForm.dict.set(PDFName.of("XFA"), PDFArray.withContext(doc.context));
      form.acroForm.dict.set(PDFName.of("Marker"), PDFString.of("x"));
    });
    const report = await fillPdf(src, [text("a", "hi")]);
    expect(report.droppedXfa).toBe(true);
    const out = await PDFDocument.load(report.bytes);
    expect(out.getForm().acroForm.dict.has(PDFName.of("XFA"))).toBe(false);
  });

  test("a PDF with no fields is refused with a message", async () => {
    const src = await formPdf(() => {});
    await expect(fillPdf(src, [text("a", "hi")])).rejects.toBeInstanceOf(FillError);
  });
});
