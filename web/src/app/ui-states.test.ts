import { describe, expect, test } from "bun:test";
import tailwind from "@tailwindcss/postcss";
import postcss from "postcss";

// Compiles ui-states.css with Tailwind for the given classes and returns the CSS.
async function compile(classes: string[]): Promise<string> {
  const css = `@import "tailwindcss" source(none);\n@import "./ui-states.css";\n`
    + `@source inline("${classes.join(" ")}");`;
  const out = await postcss([tailwind()]).process(css, { from: `${import.meta.dir}/x.css` });
  return out.css.replace(/\s+/g, " ");
}

// The rule body for one class, from the compiled CSS.
function rule(css: string, selectorStart: string): string {
  const at = css.indexOf(selectorStart);
  expect(at).toBeGreaterThanOrEqual(0);
  return css.slice(at, css.indexOf("}", at) + 1);
}

describe("ui-states.css", () => {
  test("a state variant matches Base UI's attribute unless it is false, and Radix's data-state", async () => {
    const css = await compile(["data-open:flex", "data-closed:flex", "data-active:flex"]);
    expect(rule(css, ".data-open\\:flex")).toContain(
      '.data-open\\:flex:where([data-state="open"]), .data-open\\:flex:where([data-open]:not([data-open="false"]))');
    expect(rule(css, ".data-closed\\:flex")).toContain('[data-closed]:not([data-closed="false"])');
    expect(rule(css, ".data-active\\:flex")).toContain('[data-state="active"]');
  });

  test("disabled matches data-disabled, and orientation variants match data-orientation", async () => {
    const css = await compile(["data-disabled:flex", "group-data-vertical/tabs:flex", "data-horizontal:flex"]);
    expect(rule(css, ".data-disabled\\:flex")).toContain('[data-disabled]:not([data-disabled="false"])');
    expect(css).toContain(
      ':is(:where(.group\\/tabs):where([data-orientation="vertical"]) .group-data-vertical\\/tabs\\:flex)');
    expect(rule(css, ".data-horizontal\\:flex")).toContain('[data-orientation="horizontal"]');
  });

  test("animate-in and animate-out play the enter and exit keyframes, timed by duration-*", async () => {
    const css = await compile(["animate-in", "animate-out", "duration-100"]);
    expect(rule(css, ".animate-in")).toContain("animation: enter var(--tw-duration, 0.15s) var(--tw-ease, ease)");
    expect(rule(css, ".animate-out")).toContain("animation: exit var(--tw-duration, 0.15s)");
    expect(rule(css, ".duration-100")).toContain("--tw-duration: 100ms");
    expect(css).toContain("@keyframes enter { from { opacity: var(--tw-enter-opacity, 1);");
    expect(css).toContain("@keyframes exit { to { opacity: var(--tw-exit-opacity, 1);");
  });

  test("fade, zoom and slide set the enter and exit values", async () => {
    const css = await compile(["fade-in-0", "fade-out-0", "zoom-in-95", "zoom-out-95", "slide-in-from-top-2",
      "slide-in-from-bottom-2", "slide-in-from-left-2", "slide-in-from-right-2"]);
    expect(rule(css, ".fade-in-0")).toContain("--tw-enter-opacity: calc(0 / 100)");
    expect(rule(css, ".fade-out-0")).toContain("--tw-exit-opacity: calc(0 / 100)");
    expect(rule(css, ".zoom-in-95")).toContain("--tw-enter-scale: calc(95 / 100)");
    expect(rule(css, ".zoom-out-95")).toContain("--tw-exit-scale: calc(95 / 100)");
    expect(rule(css, ".slide-in-from-top-2")).toContain("--tw-enter-translate-y: calc(2 * var(--spacing) * -1)");
    expect(rule(css, ".slide-in-from-bottom-2")).toContain("--tw-enter-translate-y: calc(2 * var(--spacing))");
    expect(rule(css, ".slide-in-from-left-2")).toContain("--tw-enter-translate-x: calc(2 * var(--spacing) * -1)");
    expect(rule(css, ".slide-in-from-right-2")).toContain("--tw-enter-translate-x: calc(2 * var(--spacing))");
  });

  test("the enter and exit values don't inherit", async () => {
    const css = await compile(["fade-in-0"]);
    expect(css).toContain("@property --tw-enter-opacity { syntax: \"*\"; inherits: false; initial-value: 1; }");
  });
});
