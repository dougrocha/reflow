import { describe, expect, test } from "bun:test";
import { foldWithMap, locateQuote, numbersIn } from "./quote";

describe("foldWithMap", () => {
  test("folds case, whitespace, quotes, dashes and invisible characters", () => {
    const { folded } = foldWithMap("  The “City”  of\n\tMesa­​ – it’s  ");
    expect(folded).toBe('the "city" of mesa - it\'s');
  });

  test("maps every folded character to its original index", () => {
    const text = "A  ­B’c";
    const { folded, origin } = foldWithMap(text);
    expect(folded).toBe("a b'c");
    expect(folded.length).toBe(origin.length);
    expect(origin.map((i) => text[i])).toEqual(["A", " ", "B", "’", "c"]);
  });

  test("expansions (NFKC ligatures) map to their one original character", () => {
    const { folded, origin } = foldWithMap("oﬃce");
    expect(folded).toBe("office");
    expect(origin).toEqual([0, 1, 1, 1, 2, 3]);
  });

  test("empty and blank text fold to nothing", () => {
    expect(foldWithMap("").folded).toBe("");
    expect(foldWithMap(" \n ").folded).toBe("");
  });
});

describe("locateQuote", () => {
  const text = "Fees are due on the first day of each month.\n\nLate fees of $25 apply after the 5th.";

  test("finds an exact quote with original offsets", () => {
    const [s, e] = locateQuote(text, "Late fees of $25 apply")!;
    expect(text.slice(s, e)).toBe("Late fees of $25 apply");
  });

  test("ignores case, whitespace runs and curly quotes", () => {
    const t = "The board said “no new fees”   will\n be charged this year.";
    const [s, e] = locateQuote(t, 'the board said "no new fees" will be charged')!;
    expect(t.slice(s, e)).toBe("The board said “no new fees”   will\n be charged");
  });

  test("null when the quote is not in the text", () => {
    expect(locateQuote(text, "Late fees of $30 apply after the 5th")).toBeNull();
  });

  test("refuses short quotes unless they are the whole text", () => {
    expect(locateQuote(text, "fees")).toBeNull();
    expect(locateQuote("Open daily", "open  DAILY")).toEqual([0, 10]);
    expect(locateQuote("Open daily, 9 to 5", "Open daily")).toBeNull();
  });

  test("an empty quote never matches", () => {
    expect(locateQuote(text, "")).toBeNull();
    expect(locateQuote("", "")).toBeNull();
  });

  test("end offset covers a trailing astral character", () => {
    const t = "Ratings are shown as stars \u{1F31F}";
    const [s, e] = locateQuote(t, "are shown as stars \u{1F31F}")!;
    expect(t.slice(s, e)).toBe("are shown as stars \u{1F31F}");
  });
});

describe("numbersIn", () => {
  test("normalises thousands separators and keeps decimals", () => {
    expect(numbersIn("1,200 students, 3.57 GPA, 12 clubs")).toEqual(["1200", "3.57", "12"]);
  });

  test("a decimal comma is a decimal; 0.125 stays", () => {
    expect(numbersIn("3,5 and 0.125 and 0,125")).toEqual(["3.5", "0.125", "0.125"]);
  });

  test("European thousands and multi-group numbers", () => {
    expect(numbersIn("1.200 and 1,234,567")).toEqual(["1200", "1234567"]);
  });

  test("fullwidth, superscript, Arabic-Indic and Eastern Arabic-Indic digits count as their ASCII twins", () => {
    expect(numbersIn("total ２０２５")).toEqual(["2025"]);
    expect(numbersIn("x² and 10³")).toEqual(["2", "103"]);
    expect(numbersIn("٣٤ and ١٢٣")).toEqual(["34", "123"]);
    expect(numbersIn("३० ๓๐ ২৫ ௫ 𝟕")).toEqual(["30", "30", "25", "5", "7"]);
    expect(numbersIn("۴۵ and ۰۹")).toEqual(["45", "09"]);
    expect(numbersIn("１,２００ and ３.５")).toEqual(["1200", "3.5"]);
  });

  test("none", () => {
    expect(numbersIn("no figures here")).toEqual([]);
  });
});
