import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ASK_ANSWER_CHARS, ASK_MAX_EVIDENCE, ASK_QUOTE_CHARS } from "@/lib/ask-contract";
import { type Passage, parseCorpus } from "./corpus";
import type { Label } from "./prompt";
import { validateReply } from "./validate";

function passage(order: number, text: string): Passage {
  return { id: `n${order}`, node: `n${order}`, order, page: order + 1, bbox: [0, 0, 100, 100], kind: "p", text, trail: "", lead: "" };
}

const P1 = passage(0, "Late fees of $25 apply after the 5th of the month. Residents may pay online.");
const P2 = passage(1, "The office is open Monday to Friday, 9 to 5. It closes for 3 holidays a year.");
const P3 = passage(2, "Parking permits cost 1,200 dollars per year for 2 vehicles in the garage.");
const offered = new Map<Label, Passage>([["P1", P1], ["P2", P2], ["P3", P3]]);

const HEADING = { ...passage(10, "FY 2024-25 revenue and financing $18.6 million"), kind: "h" as const, trail: "Budget-in-Brief" };
const LABEL = { ...passage(11, "Projected Budget Revenues 2024-25"), trail: "Budget-in-Brief > FY 2024-25 revenue and financing $18.6 million" };
const carmel = new Map<Label, Passage>([["P1", LABEL], ["P2", HEADING]]);
const question = "What is the total revenue and financing for FY 2024-25?";

const reply = (o: object) => JSON.stringify(o);
const good = (over: object = {}) =>
  reply({ evidence: [{ passage: "P1", quote: "Late fees of $25 apply after the 5th" }], found: true, answer: "Late fees are $25.", ...over });

describe("validateReply", () => {
  test("accepts a correct reply and returns the document's slice", () => {
    const v = validateReply(good(), offered);
    expect(v.kind).toBe("answer");
    if (v.kind !== "answer") return;
    expect(v.answer).toBe("Late fees are $25.");
    expect(v.picks).toHaveLength(1);
    expect(v.picks[0].passage).toBe(P1);
    expect(v.picks[0].quote).toBe("Late fees of $25 apply after the 5th");
  });

  test("not JSON, wrong shape, or no object is malformed", () => {
    for (const raw of ["", "I cannot tell.", "[1,2]", "{", reply({ found: true }), reply({ evidence: "x", found: true, answer: "a" }),
      reply({ evidence: [], found: "yes", answer: "a" }), reply({ evidence: [], found: true, answer: 3 }),
      reply({ evidence: [{ passage: "P1" }], found: true, answer: "a" })]) {
      expect(validateReply(raw, offered).kind).toBe("malformed");
    }
  });

  test("prose and code fences around the JSON are tolerated", () => {
    const raw = "Sure! Here you go:\n```json\n" + good() + "\n```\nHope that helps {really}.";
    expect(validateReply(raw, offered).kind).toBe("answer");
  });

  test("braces inside strings do not break extraction; the first valid object wins", () => {
    const raw = `{not json} ${good({ answer: "Fees are $25 {approx}." })} ${reply({ found: false, evidence: [], answer: "" })}`;
    const v = validateReply(raw, offered);
    expect(v.kind).toBe("answer");
    if (v.kind === "answer") expect(v.answer).toContain("{approx}");
  });

  test("found false is not-stated and its evidence is ignored", () => {
    const v = validateReply(good({ found: false }), offered);
    expect(v).toEqual({ kind: "none", why: "not-stated" });
    expect(validateReply(reply({ found: false, answer: "", evidence: [{ bad: 1 }] }), offered)).toEqual({ kind: "none", why: "not-stated" });
  });

  test("a label that was not offered is dropped; none left is unverified", () => {
    const v = validateReply(good({ evidence: [{ passage: "P9", quote: "Late fees of $25 apply after the 5th" }] }), offered);
    expect(v).toEqual({ kind: "none", why: "unverified" });
  });

  test("a quote from a different offered passage is rebound to that passage", () => {
    const v = validateReply(
      good({ evidence: [{ passage: "P2", quote: "Late fees of $25 apply after the 5th" }] }),
      offered,
    );
    expect(v.kind).toBe("answer");
    if (v.kind === "answer") expect(v.picks[0].passage).toBe(P1);
  });

  test("curly quotes, case and whitespace differences still verify; the slice is the document's", () => {
    const doc = passage(0, "The board said “no new fees”   will\n be charged this year, $25 aside.");
    const m = new Map<Label, Passage>([["P1", doc]]);
    const v = validateReply(
      reply({ evidence: [{ passage: "P1", quote: 'THE BOARD SAID "no new fees" will be charged this year' }], found: true, answer: "No new fees, except $25." }),
      m,
    );
    expect(v.kind).toBe("answer");
    if (v.kind === "answer") expect(v.picks[0].quote).toBe("The board said “no new fees”   will\n be charged this year");
  });

  test("a fabricated quote is unverified", () => {
    const v = validateReply(good({ evidence: [{ passage: "P1", quote: "Late fees of $50 apply after the 10th" }] }), offered);
    expect(v).toEqual({ kind: "none", why: "unverified" });
  });

  test("a quote too short to trust is dropped", () => {
    expect(validateReply(good({ evidence: [{ passage: "P1", quote: "fees" }] }), offered)).toEqual({ kind: "none", why: "unverified" });
  });

  test("extra keys are ignored", () => {
    const v = validateReply(good({ confidence: 0.9, evidence: [{ passage: "P1", quote: "Late fees of $25 apply after the 5th", why: "x" }] }), offered);
    expect(v.kind).toBe("answer");
  });

  test("an empty answer with surviving evidence is unverified", () => {
    expect(validateReply(good({ answer: "  " }), offered)).toEqual({ kind: "none", why: "unverified" });
  });

  test("an answer over ASK_ANSWER_CHARS is unverified; one at the limit passes", () => {
    const pad = (n: number) => "Late fees apply. ".repeat(Math.ceil(n / 17)).slice(0, n).trim();
    expect(validateReply(good({ answer: pad(ASK_ANSWER_CHARS) }), offered).kind).toBe("answer");
    expect(validateReply(good({ answer: pad(ASK_ANSWER_CHARS + 50) }), offered)).toEqual({ kind: "none", why: "unverified" });
  });

  test("bidi and other control characters are stripped from the answer", () => {
    const dirty = "Late\u202E fees\u2066 are\u2069 $25\u0007.\u0085\u009f\u202A";
    const v = validateReply(good({ answer: dirty }), offered);
    expect(v.kind).toBe("answer");
    if (v.kind === "answer") expect(v.answer).toBe("Late fees are $25.");
  });

  test("invisible format characters go; the joiners words need stay", () => {
    const dirty = "Late\u200E fees\u200B are\uFEFF $25\u2028.\u{E0041}";
    const v = validateReply(good({ answer: dirty }), offered);
    expect(v.kind === "answer" && v.answer).toBe("Late fees are $25.");
    const persian = validateReply(good({ answer: "می\u200Cخواهم $25." }), offered);
    expect(persian.kind === "answer" && persian.answer).toBe("می\u200Cخواهم $25.");
  });

  test("line breaks in the answer become spaces; an answer of only controls is unverified", () => {
    const v = validateReply(good({ answer: "Late fees\nare\t$25." }), offered);
    if (v.kind === "answer") expect(v.answer).toBe("Late fees are $25.");
    else throw new Error("expected an answer");
    expect(validateReply(good({ answer: "\u202E\u0000\u2066" }), offered)).toEqual({ kind: "none", why: "unverified" });
  });

  test("number guard passes for numbers in the cited text, with separators normalised", () => {
    const v = validateReply(
      reply({ evidence: [{ passage: "P3", quote: "Parking permits cost 1,200 dollars per year" }], found: true, answer: "A permit costs 1200 dollars a year for 2 vehicles." }),
      offered,
    );
    expect(v.kind).toBe("answer");
  });

  test("number guard fails for a figure absent from the cited passages", () => {
    const v = validateReply(good({ answer: "Late fees are $30." }), offered);
    expect(v).toEqual({ kind: "none", why: "unverified" });
  });

  test("a number that is only in an uncited offered passage does not count", () => {
    const v = validateReply(good({ answer: "Late fees are $25 and the office closes for 3 holidays." }), offered);
    expect(v).toEqual({ kind: "none", why: "unverified" });
  });

  test("a number only in the cited passage beyond the quoted span still counts", () => {
    const v = validateReply(good({ answer: "Fees are $25 after the 5th, per the 5th rule." }), offered);
    expect(v.kind).toBe("answer");
  });

  test("a digit in another script is held to the same guard as its ASCII twin", () => {
    expect(validateReply(good({ answer: "Late fees are $３０." }), offered)).toEqual({ kind: "none", why: "unverified" });
    expect(validateReply(good({ answer: "Late fees are $٣٠." }), offered)).toEqual({ kind: "none", why: "unverified" });
    expect(validateReply(good({ answer: "Late fees are $۳۰." }), offered)).toEqual({ kind: "none", why: "unverified" });
    expect(validateReply(good({ answer: "Late fees are $２５." }), offered).kind).toBe("answer");
    expect(validateReply(good({ answer: "Late fees are $३०." }), offered)).toEqual({ kind: "none", why: "unverified" });
    expect(validateReply(good({ answer: "Late fees are $๓๐." }), offered)).toEqual({ kind: "none", why: "unverified" });
    expect(validateReply(good({ answer: "Late fees are $২৫." }), offered).kind).toBe("answer");
  });

  test("a cited page or section number is not a number the answer may use as a fact", () => {
    const cited = { ...passage(0, "You have 30 days to reply."), page: 3, trail: "2. Replies", lead: "" };
    const offer = new Map<Label, Passage>([["P1", cited]]);
    const ask = (answer: string) =>
      validateReply(reply({ evidence: [{ passage: "P1", quote: "You have 30 days to reply." }], found: true, answer }), offer);
    expect(ask("You have 3 days to reply.")).toEqual({ kind: "none", why: "unverified" });
    expect(ask("You have 2 days to reply.")).toEqual({ kind: "none", why: "unverified" });
    expect(ask("Page 3 says you have 30 days to reply.").kind).toBe("answer");
  });

  test("the page and the section trail of a cited passage are known numbers; uncited ones are not", () => {
    const cited = { ...passage(0, "Late fees of $25 apply after the 5th of the month."), page: 7, trail: "3. Fees > 3.1. Late payment", lead: "Table 2. Fees." };
    const uncited = { ...passage(1, "The office is open Monday to Friday."), page: 9, trail: "4. Hours" };
    const offer = new Map<Label, Passage>([["P1", cited], ["P2", uncited]]);
    const ask = (answer: string) =>
      validateReply(reply({ evidence: [{ passage: "P1", quote: "Late fees of $25 apply after the 5th" }], found: true, answer }), offer);
    expect(ask("On page 7, section 3.1 says late fees are $25.").kind).toBe("answer");
    expect(ask("Under 3. Fees, in Table 2, late fees are $25.").kind).toBe("answer");
    expect(ask("On page 9, late fees are $25.")).toEqual({ kind: "none", why: "unverified" });
    expect(ask("Section 4 says late fees are $25.")).toEqual({ kind: "none", why: "unverified" });
    expect(ask("Late fees are $26.")).toEqual({ kind: "none", why: "unverified" });
  });

  test("a number the reader wrote in the question is not a fabrication", () => {
    const hoh = passage(0, "Head of household—$23,625.");
    const offer = new Map<Label, Passage>([["P1", hoh]]);
    const raw = reply({
      evidence: [{ passage: "P1", quote: "Head of household—$23,625." }],
      found: true,
      answer: "For 2025, the standard deduction for head of household is $23,625.",
    });
    expect(validateReply(raw, offer)).toEqual({ kind: "none", why: "unverified" });
    const v = validateReply(raw, offer, "What is the standard deduction for head of household in 2025?");
    expect(v.kind).toBe("answer");
    // The question's numbers excuse only themselves: a figure in neither is still refused.
    const wrong = reply({
      evidence: [{ passage: "P1", quote: "Head of household—$23,625." }],
      found: true,
      answer: "For 2025, it is $24,000.",
    });
    expect(validateReply(wrong, offer, "What is it in 2025?")).toEqual({ kind: "none", why: "unverified" });
  });

  test("a long quote is clipped to ASK_QUOTE_CHARS at a word boundary", () => {
    const long = Array.from({ length: 80 }, (_, i) => `word${i}`).join(" ");
    const doc = passage(0, long);
    const v = validateReply(
      reply({ evidence: [{ passage: "P1", quote: long }], found: true, answer: "Words." }),
      new Map<Label, Passage>([["P1", doc]]),
    );
    expect(v.kind).toBe("answer");
    if (v.kind !== "answer") return;
    const q = v.picks[0].quote;
    expect(q.length).toBeLessThanOrEqual(ASK_QUOTE_CHARS);
    expect(long.startsWith(q)).toBe(true);
    expect(/^word\d+$/.test(q.split(" ").at(-1)!)).toBe(true);
    expect(long.charAt(q.length)).toBe(" ");
  });

  test("duplicates and overlapping spans in one passage collapse to one pick", () => {
    const v = validateReply(
      good({
        evidence: [
          { passage: "P1", quote: "Late fees of $25 apply after the 5th" },
          { passage: "P1", quote: "late fees of $25 apply after the 5th" },
          { passage: "P1", quote: "of $25 apply after the 5th of the month" },
        ],
      }),
      offered,
    );
    expect(v.kind === "answer" && v.picks.length).toBe(1);
  });

  test("disjoint quotes in one passage both survive; picks are in document order", () => {
    const v = validateReply(
      reply({
        evidence: [
          { passage: "P2", quote: "The office is open Monday to Friday" },
          { passage: "P1", quote: "Residents may pay online" },
          { passage: "P1", quote: "Late fees of $25 apply after the 5th" },
        ],
        found: true,
        answer: "Fees are $25, pay online; open Monday to Friday.",
      }),
      offered,
    );
    expect(v.kind).toBe("answer");
    if (v.kind !== "answer") return;
    expect(v.picks.map((p) => [p.passage.order, p.quote])).toEqual([
      [0, "Late fees of $25 apply after the 5th"],
      [0, "Residents may pay online"],
      [1, "The office is open Monday to Friday"],
    ]);
  });

  test("at most ASK_MAX_EVIDENCE picks", () => {
    const quotes = ["Late fees of $25 apply", "Residents may pay online", "The office is open Monday", "Parking permits cost 1,200"];
    const labels = ["P1", "P1", "P2", "P3"];
    const v = validateReply(
      reply({ evidence: quotes.map((quote, i) => ({ passage: labels[i], quote })), found: true, answer: "Many things." }),
      offered,
    );
    expect(v.kind === "answer" && v.picks.length).toBe(ASK_MAX_EVIDENCE);
  });

  test("a heading is quotable: its number is the document's own", () => {
    const raw = reply({ evidence: [{ passage: "P2", quote: "FY 2024-25 revenue and financing $18.6 million" }], found: true, answer: "The total revenue and financing for FY 2024-25 is $18.6 million." });
    const v = validateReply(raw, carmel, question);
    expect(v.kind).toBe("answer");
    if (v.kind === "answer") expect(v.picks[0].passage).toBe(HEADING);
  });

  test("the same answer citing only the chart label is still unverified", () => {
    const raw = reply({ evidence: [{ passage: "P1", quote: "Projected Budget Revenues 2024-25" }], found: true, answer: "The total revenue and financing for FY 2024-25 is $18.6 million." });
    expect(validateReply(raw, carmel, question)).toEqual({ kind: "none", why: "unverified" });
  });
});

describe("a calendar day's status, from the converter", () => {
  // The September table of the school calendar: the cell of the 2nd carries the colour
  // key's label and the dated list's note, so Ask can quote them against the table.
  const text = readFileSync(join(import.meta.dir, "fixtures", "calendar-passages.jsonl"), "utf8");
  const corpus = parseCorpus("docaccess-calendar", text);
  const september = corpus.retrieve("Labor Day Closed Holiday September", 8).map((h) => h.passage).find((p) => p.lead === "SEPTEMBER 2024");

  test("the September table is retrieved, and a quote of its holiday validates against it", () => {
    expect(september).toBeDefined();
    if (!september) return;
    for (const quote of ["Closed / Holiday", "02 — Closed / Holiday — Labor Day"]) {
      const v = validateReply(
        reply({ evidence: [{ passage: "P1", quote }], found: true, answer: "Labor Day is a holiday: the district is closed." }),
        new Map<Label, Passage>([["P1", september]]),
      );
      expect(v.kind).toBe("answer");
      if (v.kind === "answer") expect(v.picks[0].passage.id).toBe(september.id);
    }
  });
});
