import { afterEach, describe, expect, test } from "bun:test";
import { loadChunkPages } from "./core";
import { BASE, serveContract } from "./contract-fixture";
import { isInsideBase, isSafeImage, isSafeLink, sanitizeChunk } from "./sanitize";

// schema/viewer-contract/hostile/chunks/0001.html is written by hand: markup a hostile PDF's
// chunk page could hold, and the markup of a real one that the viewer must keep (marked KEEP).
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

async function loaded(): Promise<{ html: string; root: HTMLElement }> {
  globalThis.fetch = serveContract;
  const html = (await loadChunkPages(BASE, "hostile/chunks/0001.html")).get(1) ?? "";
  const root = document.createElement("div");
  root.innerHTML = html;
  return { html, root };
}

describe("loadChunkPages, on a hostile chunk page", () => {
  test("drops what runs, embeds or restyles the viewer", async () => {
    const { root } = await loaded();
    expect(root.querySelectorAll("script, style, iframe, object, embed, svg, math, template, noscript, link, meta, base, form, button, video, audio, dialog")).toHaveLength(0);
    const handlers = [...root.querySelectorAll("*")].flatMap((el) => [...el.attributes].map((a) => a.name)).filter((n) => n.startsWith("on"));
    expect(handlers).toEqual([]);
    for (const el of root.querySelectorAll("[style]")) expect(el.getAttribute("style")).toMatch(/^--w:\d+(\.\d+)?pt$/);
    for (const el of root.querySelectorAll("[target], [ping], [form], [contenteditable], [tabindex], [accesskey], [autofocus], [formaction], img[name], a[name]")) {
      throw new Error(`kept ${el.outerHTML}`);
    }
    expect((globalThis as { __hostile?: string }).__hostile).toBeUndefined();
  });

  test("keeps the words of what it drops, and leaves no URL that runs or reaches off the document", async () => {
    const { root } = await loaded();
    const text = root.textContent ?? "";
    for (const word of ["HANDLERTEXT", "STYLETEXT", "CLASSTEXT", "ATTRTEXT", "NAMETEXT", "JSTEXT", "JSSPACETEXT",
      "JSTABTEXT", "JSCTRLTEXT", "DATATEXT", "VBTEXT", "BLOBTEXT", "ROOTTEXT", "PROTOTEXT", "BACKSLASHTEXT",
      "TARGETTEXT", "BUTTONTEXT"]) expect(text).toContain(word);
    // Content that is not page text goes with its element.
    for (const word of ["SVGTEXT", "MATHTEXT", "NOSCRIPTTEXT", "DIALOGTEXT", ".pg { display"]) expect(text).not.toContain(word);
    for (const a of root.querySelectorAll("a")) {
      const href = a.getAttribute("href");
      if (href !== null) expect(isSafeLink(href)).toBe(true);
    }
    for (const a of root.querySelectorAll("a")) {
      expect(a.getAttribute("href") ?? "").not.toMatch(/^(javascript|data|vbscript|blob):|^\/|^\\/i);
    }
    const sources = [...root.querySelectorAll("img")].map((img) => img.getAttribute("src"));
    expect(sources.filter((s) => s !== null && !s.startsWith("/doc/figures/"))).toEqual([]);
    for (const img of root.querySelectorAll("img[srcset]")) {
      for (const c of (img.getAttribute("srcset") ?? "").split(",")) expect(c.trim()).toMatch(/^\/doc\/figures\//);
    }
    expect(root.querySelectorAll("img[src*='evil'], img[src^='data:'], img[srcset*='evil'], img[srcset*='javascript']")).toHaveLength(0);
  });

  test("keeps no layout class of the app and no input type that is not page content", async () => {
    const { root } = await loaded();
    expect(root.querySelector(".fixed, .inset-0, .z-50, .bg-white")).toBeNull();
    expect(root.querySelector(".caps")?.textContent).toBe("CLASSTEXT");
    for (const input of root.querySelectorAll("input")) {
      expect(["text", "checkbox", "radio", "tel", "email", "number", "date", "url", "search", "password"]).toContain((input.getAttribute("type") ?? "text").toLowerCase());
    }
  });
});

// happy-dom parses <math> into the HTML namespace, as no browser does, so a tree is built here
// with the namespace a browser gives it.
describe("sanitizeChunk, on MathML", () => {
  const MATHML = "http://www.w3.org/1998/Math/MathML";
  const m = (tag: string, attrs: Record<string, string> = {}, ...kids: (Node | string)[]) => {
    const el = document.createElementNS(MATHML, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    el.append(...kids);
    return el;
  };
  const h = (tag: string, ...kids: (Node | string)[]) => {
    const el = document.createElement(tag);
    el.append(...kids);
    return el;
  };
  const sanitized = (...kids: Node[]) => {
    const root = document.createElement("div");
    root.append(...kids);
    sanitizeChunk(root);
    return root;
  };

  test("keeps what render/mathml.py writes", () => {
    const root = sanitized(h("p", "KEEP ", m("math", { display: "block" },
      m("mfrac", {}, m("mn", {}, "1.497"), m("mrow", {}, m("mn", {}, "4.00"), m("mi", { mathvariant: "normal" }, "kg"))),
      m("mo", {}, "="), m("msubsup", {}, m("mi", {}, "T"), m("mi", {}, "f"), m("mo", {}, "°")),
      m("msqrt", {}, m("mn", {}, "2")), m("mroot", {}, m("mn", {}, "2"), m("mn", {}, "3")),
      m("mover", {}, m("mi", {}, "T"), m("mo", {}, "¯")), m("msub", {}, m("mi", {}, "x"), m("mn", {}, "1")), m("msup", {}, m("mi", {}, "x"), m("mn", {}, "2")),
      m("mtext", {}, "mol")), " after"));
    expect(new Set([...root.querySelectorAll("math, math *")].map((e) => e.localName)).size).toBe(13);
    expect(root.querySelector("math")?.getAttribute("display")).toBe("block");
    expect(root.querySelector('mi[mathvariant="normal"]')?.textContent).toBe("kg");
    expect(root.textContent).toBe("KEEP 1.4974.00kg=Tf°223T¯x1x2mol after");
  });

  test("drops every other MathML element, HTML inside math, and attribute", () => {
    const root = sanitized(h("p", m("math", { onclick: "globalThis.__hostile = 'math'", href: "javascript:alert(1)", display: "inline", style: "color:red" },
      m("mi", { "xlink:href": "javascript:alert(1)", mathvariant: "script", class: "fixed inset-0", id: "x" }, "MATHTEXT"),
      m("annotation-xml", { encoding: "text/html" }, h("img", "ANNOTATIONTEXT")),
      m("maction", { actiontype: "statusline" }, m("mi", {}, "MACTIONTEXT")),
      m("mglyph", { src: "https://evil.example/g.png" }, "MGLYPHTEXT"),
      m("semantics", {}, m("mi", {}, "SEMANTICSTEXT")),
      m("mtext", {}, "MTEXT", h("b", "BOLDINMATHTEXT"), h("style", ".pg { display: none }"), m("mglyph"))),
      h("b", "BOLD")));
    expect(root.querySelector("math")?.attributes).toHaveLength(0);
    expect(root.querySelector("math > mi")?.attributes).toHaveLength(0);
    expect(root.textContent).toBe("MATHTEXTMTEXTBOLD");
    expect(root.querySelector("p > b")?.textContent).toBe("BOLD");
    for (const el of root.querySelectorAll("math *")) expect(["mi", "mtext"]).toContain(el.localName);
  });

  test("drops a <math> that is not in the MathML namespace", () => {
    const root = sanitized(h("p", "KEEP", h("math", h("mi", "HTMLMATHTEXT"))));
    expect(root.textContent).toBe("KEEP");
  });
});

describe("loadChunkPages, on a page root that is not a <div>", () => {
  test("drops the page: its raw text would become markup in the viewer", async () => {
    const html = '<div class="chunk-content"><script class="pg" data-page="1"><img src="x" onerror="globalThis.__pgXss=1"></script>'
      + '<textarea class="pg" data-page="2"><img src="x" onerror="globalThis.__pgXss=1"></textarea>'
      + '<div class="pg" data-page="3"><p>kept</p></div></div>';
    globalThis.fetch = (async () => new Response(html)) as unknown as typeof fetch;
    const pages = await loadChunkPages(BASE, "chunks/0001.html");
    expect([...pages.keys()]).toEqual([3]);
    expect(pages.get(3)).toContain("kept");
  });
});

describe("loadChunkPages, on the markup a real chunk page holds", () => {
  test("keeps form controls, labels and the attributes fill-pdf.ts reads", async () => {
    const { root } = await loaded();
    const name = root.querySelector<HTMLInputElement>("#f1-0");
    expect(name?.getAttribute("data-pdf-field")).toBe("Name");
    expect(name?.getAttribute("style")).toBe("--w:120.5pt");
    expect(name?.getAttribute("maxlength")).toBe("40");
    expect(name?.hasAttribute("required")).toBe(true);
    expect(root.querySelector('label[for="f1-0"]')?.textContent).toBe("Name");
    const check = root.querySelector("#f1-1");
    expect([check?.getAttribute("type"), check?.getAttribute("name"), check?.getAttribute("data-pdf-field"),
      check?.getAttribute("data-pdf-value"), check?.getAttribute("aria-label")]).toEqual(["checkbox", "Agree", "Agree", "Yes", "Agree to terms"]);
    expect(root.querySelector("#f1-2")?.getAttribute("data-pdf-value")).toBe("A");
    expect(root.querySelector("#f1-3")?.getAttribute("inputmode")).toBe("tel");
    expect(root.querySelector("#f1-4")?.getAttribute("data-pdf-field")).toBe("State");
    expect(root.querySelector("#f1-4 option[selected]")?.getAttribute("value")).toBe("AZ");
    expect(root.querySelector("#f1-5")?.tagName).toBe("TEXTAREA");
    expect(root.querySelector("fieldset.fields > legend.sr-only")?.textContent).toBe("KEEP legend");
    // A password field stays one, so the transcript and the filled PDF keep leaving its value out.
    expect(root.querySelector("#f-pw")?.getAttribute("type")).toBe("password");
  });

  test("keeps ids, fragment links, ARIA, lang and dir, data-node and the casing span", async () => {
    const { root } = await loaded();
    const heading = root.querySelector("h2");
    expect([heading?.id, heading?.getAttribute("lang"), heading?.getAttribute("dir"), heading?.getAttribute("data-node")]).toEqual(["n1-keep", "fr", "ltr", "1:keep"]);
    expect(root.querySelector("#h-dir")?.getAttribute("dir")).toBe("rtl");
    const hrefs = [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs).toContain("#p1");
    expect(hrefs).toContain("#n-1:abc");
    expect(hrefs).toContain("#r-1:abc");
    expect(hrefs).toContain("https://example.org/a?b=c#d");
    expect(hrefs).toContain("mailto:a@example.org");
    expect(hrefs).toContain("tel:+15550100");
    expect(root.querySelector("#r-1\\:abc")?.getAttribute("role")).toBe("doc-noteref");
    expect(root.querySelector('a[role="doc-backlink"]')?.getAttribute("aria-label")).toBe("Back to the reference");
    expect(root.querySelector('div[role="region"]')?.getAttribute("aria-label")).toBe("KEEP table");
    const sc = root.querySelector("span.sc");
    expect(sc?.getAttribute("data-sc")).toBe("Mail to");
    expect(sc?.textContent).toBe("MAIL TO");
  });

  test("keeps figures with src and srcset, and tables with scope and headers", async () => {
    const { root } = await loaded();
    const img = root.querySelector("figure#n1-fig img");
    expect(img?.getAttribute("src")).toBe("/doc/figures/p0001-1.webp?v=0123456789ab");
    expect(img?.getAttribute("srcset")).toBe("/doc/figures/p0001-1.webp 400w, /doc/figures/p0001-1-800.webp 800w");
    expect([img?.getAttribute("alt"), img?.getAttribute("sizes"), img?.getAttribute("style"), img?.getAttribute("loading")]).toEqual(["KEEP figure", "50vw", "--w:200pt", "lazy"]);
    expect(root.querySelector("figcaption")?.textContent).toBe("KEEP caption");
    expect(root.querySelector("th#h-a")?.getAttribute("scope")).toBe("col");
    expect(root.querySelector("th#h-b")?.getAttribute("colspan")).toBe("1");
    expect(root.querySelector("td")?.getAttribute("headers")).toBe("h-a");
    expect(root.querySelector("td")?.getAttribute("rowspan")).toBe("1");
  });
});

describe("what counts as a safe URL", () => {
  test("links: a fragment, a path in the document, http(s), mailto and tel", () => {
    for (const url of ["#p3", "chunks/0002.html#n-1:a", "other.html", "https://example.org/", "HTTP://example.org", "mailto:a@b.c", "tel:+1555"]) {
      expect(isSafeLink(url)).toBe(true);
    }
    for (const url of ["javascript:alert(1)", " JaVaScRiPt:alert(1)", "java\nscript:alert(1)", "\x01javascript:x", "data:text/html,x",
      "vbscript:x", "blob:https://x/y", "file:///etc/passwd", "/api/x", "//evil.example", "\\\\evil.example", " /x"]) {
      expect(isSafeLink(url)).toBe(false);
    }
  });

  test("images: only a path in the document", () => {
    expect(isSafeImage("figures/p0002-1.webp?v=abc")).toBe(true);
    for (const url of ["https://example.org/x.png", "data:image/png;base64,AAAA", "/x.png", "//evil.example/x.png", "javascript:x"]) {
      expect(isSafeImage(url)).toBe(false);
    }
  });

  test("a manifest href stays inside the document", () => {
    expect(isInsideBase("chunks/0002.html")).toBe(true);
    expect(isInsideBase("chunks/../chunks/0002.html")).toBe(true);
    for (const url of ["/etc/passwd", "../other/chunks/0001.html", "chunks/../../x", "https://evil.example/c.html", "javascript:x", "chunks\\..\\x",
      "%2e%2e/%2e%2e/api/builds", "%2E./x", ".%2e/x", "chunks/%2e%2e/%2e%2e/x",
      " %2e%2e/api/builds", " ../x", "//evil.example/c.html",
      "../doc/chunks/0001.html", "%2e%2e/doc/chunks/0001.html", "../a/x", "../b/x", "chunks/../../resnet/chunks/0001.html"]) {
      expect(isInsideBase(url)).toBe(false);
    }
  });
});
