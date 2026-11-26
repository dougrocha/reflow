import { describe, expect, test } from "bun:test";
import { variants, type VariantProps } from "./variants";

const button = variants("btn", {
  variants: {
    variant: { default: "solid", ghost: "clear" },
    size: { default: "h-8", sm: "h-7", "icon-sm": "size-7" },
  },
  defaultVariants: { variant: "default", size: "default" },
});

describe("variants", () => {
  test("with no props it gives the base and each default", () => {
    expect(button()).toBe("btn solid h-8");
    expect(button({})).toBe("btn solid h-8");
  });

  test("an asked value replaces the default, and className comes last", () => {
    expect(button({ variant: "ghost", size: "icon-sm", className: "w-full" })).toBe("btn clear size-7 w-full");
  });

  test("undefined falls back to the default, and null leaves the variant out", () => {
    expect(button({ variant: undefined, size: null })).toBe("btn solid");
  });

  test("a variant without a default adds nothing until it is asked for", () => {
    const alert = variants("alert", { variants: { tone: { info: "blue", danger: "red" } } });
    expect(alert()).toBe("alert");
    expect(alert({ tone: "danger" })).toBe("alert red");
  });

  test("an empty className or variant class adds no stray space", () => {
    const tabs = variants("tabs", { variants: { variant: { default: "", line: "gap-1" } } });
    expect(tabs({ variant: "default", className: "" })).toBe("tabs");
  });

  test("VariantProps types the props a component takes", () => {
    const props: VariantProps<typeof button> = { variant: "ghost", size: "sm" };
    expect(button(props)).toBe("btn clear h-7");
    // @ts-expect-error: a value the variant does not have
    button({ size: "huge" });
  });
});
