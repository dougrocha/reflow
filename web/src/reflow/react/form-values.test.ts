// Run with `bun test`. setMarkup is the part of the transcript download that decides which
// attributes record what the reader typed; it takes a small interface instead of a DOM
// element, so it is tested here with plain objects. The browser path (fillMarkup on real
// markup, and the filled PDF) was checked in headless Chromium.

import { describe, expect, test } from "bun:test";
import { radioKey, recordRadio, setMarkup, typedFor, type MarkupControl, type TypedValues } from "./form-values";

function fake(kind: MarkupControl["kind"], optionValues: string[] = []) {
  const attrs = new Map<string, string>();
  const optionAttrs = optionValues.map(() => new Map<string, string>());
  let text = "";
  const control: MarkupControl = {
    kind,
    setAttribute: (n, v) => void attrs.set(n, v),
    removeAttribute: (n) => void attrs.delete(n),
    setText: (s) => { text = s; },
    options: optionValues.map((value, i) => ({
      value,
      setAttribute: (n, v) => void optionAttrs[i].set(n, v),
      removeAttribute: (n) => void optionAttrs[i].delete(n),
    })),
  };
  return { control, attrs, optionAttrs, text: () => text };
}

describe("setMarkup", () => {
  test("a text input gets a value attribute", () => {
    const f = fake("text");
    setMarkup(f.control, { text: 'Ann "A" <b>' });
    expect(f.attrs.get("value")).toBe('Ann "A" <b>');
  });

  test("a textarea gets its text", () => {
    const f = fake("textarea");
    setMarkup(f.control, { text: "line one\nline two" });
    expect(f.text()).toBe("line one\nline two");
  });

  test("a checkbox or radio gets, or loses, the checked attribute", () => {
    for (const kind of ["checkbox", "radio"] as const) {
      const f = fake(kind);
      f.attrs.set("checked", "");
      setMarkup(f.control, { checked: false });
      expect(f.attrs.has("checked")).toBe(false);
      setMarkup(f.control, { checked: true });
      expect(f.attrs.has("checked")).toBe(true);
    }
  });

  test("a select marks exactly the chosen options", () => {
    const f = fake("select", ["CA", "NY", "TX"]);
    f.optionAttrs[0].set("selected", ""); // the converted default
    setMarkup(f.control, { selected: ["TX"] });
    expect(f.optionAttrs.map((m) => m.has("selected"))).toEqual([false, false, true]);
  });

  test("a password is never written", () => {
    const f = fake("password");
    setMarkup(f.control, { text: "secret" });
    expect(f.attrs.size).toBe(0);
  });
});

describe("typedFor", () => {
  const radio = (id: string, field: string) =>
    ({ id, getAttribute: (n: string) => ({ "data-pdf-field": field, type: "radio" })[n] ?? null }) as unknown as Element;

  test("a radio group split over pages follows the latest choice for its PDF field", () => {
    const values: TypedValues = new Map();
    values.set("f1-3", { checked: true }); // an earlier per-page record
    values.set(radioKey("choice"), { text: "f2-7" }); // the latest choice, on another page
    expect(typedFor(radio("f1-3", "choice"), values)).toEqual({ checked: false });
    expect(typedFor(radio("f2-7", "choice"), values)).toEqual({ checked: true });
    expect(typedFor(radio("f2-8", "other"), values)).toBeUndefined();
  });
});

describe("recordRadio", () => {
  // A Yes / No pair: each radio is a PDF checkbox field of its own, so each has its own key.
  const yes = { id: "f1-1", field: "Yes" };
  const no = { id: "f1-2", field: "No" };
  const pair = (yesOn: boolean) => [{ ...yes, checked: yesOn }, { ...no, checked: !yesOn }];
  const radio = (id: string, field: string) =>
    ({ id, getAttribute: (n: string) => ({ "data-pdf-field": field, type: "radio" })[n] ?? null }) as unknown as Element;

  test("choosing the other of two radios that are fields of their own clears the first", () => {
    const values: TypedValues = new Map();
    recordRadio(values, yes, pair(true));
    recordRadio(values, no, pair(false));
    expect(typedFor(radio(yes.id, "Yes"), values)).toEqual({ checked: false });
    expect(typedFor(radio(no.id, "No"), values)).toEqual({ checked: true });
    recordRadio(values, yes, pair(true)); // and back again
    expect(typedFor(radio(yes.id, "Yes"), values)).toEqual({ checked: true });
    expect(typedFor(radio(no.id, "No"), values)).toEqual({ checked: false });
  });

  test("radios that share one PDF field keep one latest choice for it", () => {
    const values: TypedValues = new Map();
    const group = (on: string) => ["a", "b", "c"].map((k) => ({ id: `f1-${k}`, field: "choice", checked: k === on }));
    recordRadio(values, { id: "f1-a", field: "choice" }, group("a"));
    recordRadio(values, { id: "f1-c", field: "choice" }, group("c"));
    expect(typedFor(radio("f1-a", "choice"), values)).toEqual({ checked: false });
    expect(typedFor(radio("f1-c", "choice"), values)).toEqual({ checked: true });
  });
});
