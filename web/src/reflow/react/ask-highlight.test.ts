// Run with `bun test`. quoteSpan and unwrapMark are the parts of marking an answer's quote that run
// without a browser (unwrapMark here on a hand-made tree); the DOM wrapping (markQuote,
// clearAskMarks, and find's marks over them) is covered by the browser check, scripts/ask-e2e.ts.

import { describe, expect, test } from "bun:test";
import { quoteSpan } from "./ask-highlight";
import { unwrapMark } from "./find";

/** Just enough of a DOM node for unwrapMark: a tree with parentNode, firstChild, insertBefore and removeChild. */
class Tree {
  parentNode: Tree | null = null;
  children: Tree[] = [];
  constructor(readonly name: string, ...kids: Tree[]) {
    kids.forEach((k) => this.append(k));
  }
  get firstChild(): Tree | null {
    return this.children[0] ?? null;
  }
  append(k: Tree) {
    k.parentNode?.removeChild(k);
    k.parentNode = this;
    this.children.push(k);
  }
  insertBefore(k: Tree, ref: Tree) {
    k.parentNode?.removeChild(k);
    k.parentNode = this;
    this.children.splice(this.children.indexOf(ref), 0, k);
  }
  removeChild(k: Tree) {
    this.children.splice(this.children.indexOf(k), 1);
    k.parentNode = null;
  }
  names(): string {
    return this.children.map((c) => (c.children.length ? `${c.name}(${c.names()})` : c.name)).join(" ");
  }
}
const tree = (name: string, ...kids: Tree[]) => new Tree(name, ...kids);
const unwrap = (t: Tree) => unwrapMark(t as unknown as Element);

describe("unwrapMark", () => {
  test("puts the mark's children in its place, in order", () => {
    const find = tree("find", tree("b"), tree("c"));
    const root = tree("root", tree("a"), find, tree("d"));
    expect(unwrap(find)).toBe(root as unknown as Node);
    expect(root.names()).toBe("a b c d");
  });

  test("a mark of the other kind nested inside survives, with its text", () => {
    const ask = tree("ask", tree("x"));
    const find = tree("find", tree("b"), ask, tree("e"));
    const root = tree("root", find);
    unwrap(find);
    expect(root.names()).toBe("b ask(x) e");
    expect(ask.parentNode).toBe(root);
  });

  test("clearing the outer then the inner kind leaves only text", () => {
    const find = tree("find", tree("b"));
    const ask = tree("ask", find);
    const root = tree("root", tree("a"), ask);
    unwrap(ask);
    expect(root.names()).toBe("a find(b)");
    unwrap(find);
    expect(root.names()).toBe("a b");
  });

  test("a mark outside any tree is left alone", () => {
    expect(unwrap(tree("lone", tree("x")))).toBeNull();
  });
});

describe("quoteSpan", () => {
  const text = "the deepest we evaluate has 152 layers.\nnext paragraph";

  test("finds a quote and returns offsets into the text", () => {
    const span = quoteSpan(text, "152 layers");
    expect(span).toEqual([28, 38]);
    expect(text.slice(...span!)).toBe("152 layers");
  });

  test("ignores case", () => {
    const span = quoteSpan(text, "We Evaluate");
    expect(text.slice(...span!)).toBe("we evaluate");
  });

  test("a whitespace run in the quote matches any whitespace in the text", () => {
    expect(text.slice(...quoteSpan(text, "layers.   next\tparagraph")!)).toBe("layers.\nnext paragraph");
  });

  test("a single space in the quote matches a run of spaces in the text", () => {
    const spaced = "a  b   c";
    expect(spaced.slice(...quoteSpan(spaced, "a b c")!)).toBe("a  b   c");
  });

  test("regex characters in the quote are literal", () => {
    const t = "cost (usd) is $5.00 [approx]";
    expect(t.slice(...quoteSpan(t, "(usd) is $5.00 [approx]")!)).toBe("(usd) is $5.00 [approx]");
    expect(quoteSpan("abc", "a.c")).toBeNull();
  });

  test("not found, empty or blank is null", () => {
    expect(quoteSpan(text, "153 layers")).toBeNull();
    expect(quoteSpan(text, "")).toBeNull();
    expect(quoteSpan(text, "   ")).toBeNull();
  });

  test("leading and trailing space in the quote is dropped", () => {
    expect(text.slice(...quoteSpan(text, "  deepest ")!)).toBe("deepest");
  });
});
