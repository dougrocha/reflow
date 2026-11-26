import { describe, expect, test } from "bun:test";
import { isPlainClick, LOADER_PATH, pdfLink, readyDocument, resolveUrl, viewerUrl } from "./loader-rules";

// The loader's rules (loader-rules.ts), on real anchors: which links it takes, which clicks, and
// which answers from the resolver it opens.

const PAGE = "https://example.org/reports/";

/** The anchor a page would hold: `href` resolves against the page, as in a browser. */
function link(attrs: Record<string, string>): HTMLAnchorElement {
  const a = document.createElement("a");
  for (const [name, value] of Object.entries(attrs)) a.setAttribute(name, value);
  a.setAttribute("href", new URL(attrs.href ?? "", PAGE).href);
  return a;
}

describe("pdfLink: what counts as a PDF link", () => {
  test.each([
    ["https://example.org/files/w9.pdf", "https://example.org/files/w9.pdf"],
    ["https://example.org/files/W9.PDF", "https://example.org/files/W9.PDF"],
    ["https://example.org/files/w9.pdf?download=1", "https://example.org/files/w9.pdf?download=1"],
    ["https://example.org/files/w9.pdf#page=2", "https://example.org/files/w9.pdf"],
    ["w9.pdf", "https://example.org/reports/w9.pdf"],
    ["/files/w9.pdf?x=1#top", "https://example.org/files/w9.pdf?x=1"],
    ["http://other.test/a.pdf", "http://other.test/a.pdf"],
  ])("takes %s", (href, expected) => expect(pdfLink(link({ href }))).toBe(expected));

  test.each([
    ["no .pdf in the path", { href: "/files/w9.html" }],
    [".pdf only in the query", { href: "/view?file=w9.pdf" }],
    [".pdf only in the fragment", { href: "/view#w9.pdf" }],
    [".pdf in the middle of the path", { href: "/w9.pdf/page" }],
    ["a link to something like a PDF", { href: "/files/w9.pdfx" }],
    ["a download link", { href: "/files/w9.pdf", download: "" }],
    ["a download link with a name", { href: "/files/w9.pdf", download: "form.pdf" }],
    ["a link opted out", { href: "/files/w9.pdf", "data-reflow": "off" }],
    ["a link opted out in capitals", { href: "/files/w9.pdf", "data-reflow": " OFF " }],
    ["an opt-out that wins over a PDF type", { href: "/files/w9.pdf", type: "application/pdf", "data-reflow": "off" }],
    ["a link the page sends to a new tab", { href: "/files/w9.pdf", target: "_blank" }],
    ["a link the page sends to a named frame", { href: "/files/w9.pdf", target: "viewer" }],
    ["a link the page sends to the top window", { href: "/files/w9.pdf", target: "_top" }],
    ["a mailto link", { href: "mailto:a@example.org?subject=x.pdf" }],
    ["a javascript link", { href: "javascript:alert(1)//.pdf" }],
    ["a link with no href", {}],
  ])("leaves %s", (_, attrs) => expect(pdfLink(link(attrs))).toBeNull());

  test("takes a link by its type, whatever the path", () => {
    expect(pdfLink(link({ href: "/download?id=3", type: "application/pdf" }))).toBe("https://example.org/download?id=3");
    expect(pdfLink(link({ href: "/download?id=3", type: "Application/PDF; charset=binary" }))).toBe("https://example.org/download?id=3");
    expect(pdfLink(link({ href: "/download?id=3", type: "text/html" }))).toBeNull();
  });

  test("takes a link that carries data-reflow, so a link without .pdf can be named", () => {
    expect(pdfLink(link({ href: "/download?id=3", "data-reflow": "" }))).toBe("https://example.org/download?id=3");
    expect(pdfLink(link({ href: "/download?id=3", "data-reflow": "on" }))).toBe("https://example.org/download?id=3");
    expect(pdfLink(link({ href: "/download?id=3" }))).toBeNull();
  });

  test("takes target=_self, which is the default", () => {
    expect(pdfLink(link({ href: "/a.pdf", target: "_self" }))).toBe("https://example.org/a.pdf");
    expect(pdfLink(link({ href: "/a.pdf", target: "" }))).toBe("https://example.org/a.pdf");
  });

  test("leaves a URL the resolver would refuse", () => {
    expect(pdfLink(link({ href: `/${"a".repeat(2100)}.pdf` }))).toBeNull();
  });

  test("never changes the link", () => {
    const a = link({ href: "/files/w9.pdf", "aria-label": "Form W-9" });
    const html = a.outerHTML;
    pdfLink(a);
    expect(a.outerHTML).toBe(html);
  });
});

describe("isPlainClick", () => {
  const click = (over: Partial<Parameters<typeof isPlainClick>[0]> = {}) =>
    ({ button: 0, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, defaultPrevented: false, ...over });

  test("takes the main button with no modifier, which is also Enter on a link", () => {
    expect(isPlainClick(click())).toBe(true);
  });

  test.each([
    ["a middle click", { button: 1 }],
    ["a right click", { button: 2 }],
    ["Ctrl", { ctrlKey: true }],
    ["Meta (Cmd)", { metaKey: true }],
    ["Shift", { shiftKey: true }],
    ["Alt", { altKey: true }],
    ["a click the page already handled", { defaultPrevented: true }],
  ])("leaves %s", (_, over) => expect(isPlainClick(click(over))).toBe(false));
});

describe("readyDocument: the resolver's answer is untrusted", () => {
  const ORIGIN = "https://reflow.test";
  const ready = (viewer: unknown, extra: Record<string, unknown> = {}) => ({ status: "ready", viewer, ...extra });
  const read = (answer: unknown) => readyDocument(answer, ORIGIN, "acme");

  test("takes the viewer of its own origin and returns only the document id", () => {
    expect(read(ready("https://reflow.test/embed/fw9?site=acme"))).toBe("fw9");
    expect(read(ready("/embed/fw9?site=acme"))).toBe("fw9");
    expect(read(ready("https://reflow.test/embed/az-accident-insurance-flyer?site=acme"))).toBe("az-accident-insurance-flyer");
  });

  test.each([
    ["another origin", "https://evil.test/embed/fw9?site=acme"],
    ["a look-alike origin", "https://reflow.test.evil.test/embed/fw9?site=acme"],
    ["another scheme", "http://reflow.test/embed/fw9?site=acme"],
    ["another port", "https://reflow.test:8443/embed/fw9?site=acme"],
    ["credentials in the URL", "https://user:pw@reflow.test/embed/fw9?site=acme"],
    ["a protocol-relative URL to another host", "//evil.test/embed/fw9?site=acme"],
    ["a backslash that makes another host", "https:\\\\evil.test\\embed\\fw9?site=acme"],
    ["a javascript URL", "javascript:alert(1)"],
    ["a data URL", "data:text/html,<script>alert(1)</script>"],
    ["a path outside /embed/", "https://reflow.test/docs/fw9/index.html?site=acme"],
    ["a path that only starts like /embed", "https://reflow.test/embedded/fw9?site=acme"],
    ["a nested path", "https://reflow.test/embed/fw9/extra?site=acme"],
    ["a dot segment out of /embed", "https://reflow.test/embed/../docs/fw9?site=acme"],
    ["an encoded slash in the id", "https://reflow.test/embed/a%2f..%2fb?site=acme"],
    ["an encoded dot segment as the id", "https://reflow.test/embed/%2e%2e?site=acme"],
    ["no id", "https://reflow.test/embed/?site=acme"],
    ["an id that is not a name", "https://reflow.test/embed/fw9%00?site=acme"],
    ["a malformed escape", "https://reflow.test/embed/%E0%A4%A?site=acme"],
    ["another site", "https://reflow.test/embed/fw9?site=other"],
    ["no site", "https://reflow.test/embed/fw9"],
    ["a doubled site", "https://reflow.test/embed/fw9?site=acme&site=acme"],
    ["an extra parameter", "https://reflow.test/embed/fw9?site=acme&parent=https://evil.test"],
    ["a fragment", "https://reflow.test/embed/fw9?site=acme#x"],
    ["an empty string", ""],
    ["a string that is not a URL", "http://"],
    ["a URL over the limit", `https://reflow.test/embed/${"a".repeat(3000)}?site=acme`],
  ])("refuses a viewer with %s", (_, viewer) => expect(read(ready(viewer))).toBeNull());

  test.each([
    ["unknown", { status: "unknown" }],
    ["no status", { viewer: "https://reflow.test/embed/fw9?site=acme" }],
    ["a status that only looks ready", { status: "Ready", viewer: "https://reflow.test/embed/fw9?site=acme" }],
    ["a viewer that is not a string", ready(["https://reflow.test/embed/fw9?site=acme"])],
    ["a viewer that is an object", ready({ href: "https://reflow.test/embed/fw9?site=acme" })],
    ["no viewer", { status: "ready" }],
    ["an error body", { error: "site is required" }],
    ["an array", [ready("https://reflow.test/embed/fw9?site=acme")]],
    ["a string", "ready"],
    ["null", null],
    ["a number", 3],
  ])("refuses %s", (_, answer) => expect(read(answer)).toBeNull());

  test("ignores everything else in the answer", () => {
    expect(read(ready("/embed/fw9?site=acme", { html: "<img onerror=alert(1)>", src: "https://evil.test" }))).toBe("fw9");
  });
});

describe("the URLs the loader builds", () => {
  test("the viewer's URL is built from the origin, the id and the site, with the id and site escaped", () => {
    expect(viewerUrl("https://reflow.test", "fw9", "acme")).toBe("https://reflow.test/embed/fw9?site=acme");
    expect(viewerUrl("https://reflow.test", "a/b", "a&b")).toBe("https://reflow.test/embed/a%2Fb?site=a%26b");
  });

  test("the resolver's URL escapes the PDF URL as one parameter", () => {
    const url = new URL(resolveUrl("https://reflow.test", "acme", "https://example.org/a.pdf?x=1&y=2#f"));
    expect(url.origin + url.pathname).toBe("https://reflow.test/v1/resolve");
    expect(url.searchParams.getAll("site")).toEqual(["acme"]);
    expect(url.searchParams.getAll("url")).toEqual(["https://example.org/a.pdf?x=1&y=2#f"]);
  });

  test("the loader is served beside the viewer's pages", () => {
    expect(LOADER_PATH).toBe("/embed/loader.js");
  });
});
