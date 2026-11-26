// An allow-list for the markup of a chunk page, applied before it reaches the viewer's DOM.
//
// Chunk HTML is written by the converter from untrusted PDFs, and its safety rests on the
// renderer's escaping. The viewer does not rely on that alone: it keeps the elements, attributes
// and URLs the renderer writes (the five documents of web/scripts/capture.ts and the 165 of the
// corpus lose nothing) and drops everything else. Anything the renderer starts writing needs
// an entry here; sanitize.test.ts and the contract fixture show what is kept.

const XHTML = "http://www.w3.org/1999/xhtml";
const MATHML = "http://www.w3.org/1998/Math/MathML";

// Elements whose content is not page text, or that run or embed something: removed whole.
const DROP = new Set([
  "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "svg", "math",
  "noscript", "template", "link", "meta", "base", "title", "head", "xmp", "plaintext", "noembed",
  "noframes", "audio", "video", "source", "track", "canvas", "dialog", "slot", "portal",
]);

// The MathML the renderer writes for a model's formula (render/mathml.py ALLOWED), and the
// attributes it may carry. Every other MathML element goes with its content (annotation-xml,
// maction and mglyph are where HTML parsers switch namespace), and an HTML element inside
// MathML goes too, so what is serialized and parsed again is the tree that was checked.
const MATH_ELEMENTS = new Set(["math", "mrow", "mi", "mn", "mo", "mtext", "mfrac", "msqrt", "mroot",
  "msub", "msup", "msubsup", "mover"]);
const MATH_ATTRIBUTES: Record<string, Record<string, string>> = {
  math: { display: "block" },
  mi: { mathvariant: "normal" },
};

// Elements the transcript uses. Any other element is opened: its children stay, its tag and
// attributes go.
const ELEMENTS = new Set([
  "a", "aside", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "del", "details",
  "div", "dl", "dt", "em", "fieldset", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
  "i", "img", "input", "ins", "label", "legend", "li", "ol", "optgroup", "option", "p", "pre",
  "section", "select", "small", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
  "textarea", "tfoot", "th", "thead", "tr", "ul",
]);

// On every element. `data-*` (the viewer and fill-pdf.ts read data-node, data-page,
// data-pdf-field, data-pdf-value, data-sc, ...) and `aria-*` are matched by pattern.
const GLOBAL = new Set(["id", "class", "lang", "dir", "title", "role", "hidden"]);
const PATTERNS = [/^data-[a-z0-9_.:-]+$/, /^aria-[a-z]+$/];

// Only on the elements named. There is no `target`, `form`, `formaction`, `action`, `name` outside
// a control (it would shadow a property of `document`), `style` apart from the width below, or
// event handler.
const CONTROL = ["autocomplete", "disabled", "name", "required", "style"];
const PER_ELEMENT: Record<string, string[]> = {
  a: ["href", "rel"],
  img: ["src", "srcset", "sizes", "alt", "width", "height", "loading", "decoding", "style"],
  input: [...CONTROL, "type", "value", "checked", "readonly", "maxlength", "minlength", "min", "max",
    "step", "placeholder", "inputmode", "size"],
  select: [...CONTROL, "multiple", "size"],
  textarea: [...CONTROL, "rows", "cols", "maxlength", "minlength", "placeholder", "readonly", "wrap"],
  option: ["value", "selected", "disabled", "label"],
  optgroup: ["disabled", "label"],
  label: ["for"],
  fieldset: ["disabled"],
  td: ["colspan", "rowspan", "headers"],
  th: ["colspan", "rowspan", "headers", "scope", "abbr"],
  col: ["span"],
  colgroup: ["span"],
  ol: ["type", "start", "reversed"],
  li: ["value"],
  details: ["open"],
};

// `class` tokens the transcript stylesheet (src/reflow/viewer/transcript.css) or the renderer
// uses. The transcript's rules share a CSS layer with the app's utility classes, so a class
// such as "fixed inset-0" in a chunk would lay it over the viewer.
const CLASSES = new Set([
  "ai-badge", "ai-draft", "caps", "caption", "continued", "day-note", "day-status", "field",
  "field-check", "field-row", "field-section", "fields", "figure-alt", "figure-alt-label",
  "figure-chip-label", "filled-box", "font-note", "footnote", "form-note", "i", "ink-green",
  "ink-red", "lead", "legal", "marker", "note", "ocr-doubt", "ocr-note", "pg", "review-note",
  "reviewed-fix", "ruled-box", "sc", "sr-only", "status", "table-wrap", "title", "unread",
  "unread-note", "unread-word",
]);

// `type` of an <input>: the others (image, file, submit, ...) are not page content. A type that
// is not here is removed, and the control is a text field.
const INPUT_TYPES = new Set(["text", "checkbox", "radio", "tel", "email", "number", "date", "url", "search",
  "password"]);

// The renderer's only style: `--w:<n>pt`, the width of a control or a figure.
const WIDTH = /^--w:\d+(\.\d+)?pt;?$/;

// What a browser ignores before and inside a URL's scheme: leading control characters and
// spaces, and tabs and newlines anywhere ("java\nscript:" is "javascript:").
const scheme = (url: string) =>
  /^([a-z][a-z0-9+.-]*):/i.exec(url.replace(/^[\x00-\x20]+/, "").replace(/[\t\n\r]/g, ""))?.[1].toLowerCase();

const LINK_SCHEMES = new Set(["http", "https", "mailto", "tel"]);

/** A link target: a fragment, a path inside the document, or an http(s), mailto or tel URL. */
export function isSafeLink(url: string): boolean {
  const s = scheme(url);
  // "/x" and "\x" name a path or host of the site that serves the viewer, not of the document.
  return s ? LINK_SCHEMES.has(s) : !/^[\x00-\x20]*[/\\]/.test(url);
}

/** An image source: a path inside the document (loadChunkPages resolves it against the base).
 *  The renderer writes nothing else, and an image that names another site tells it who reads. */
export function isSafeImage(url: string): boolean {
  return !scheme(url) && !/^[\x00-\x20]*[/\\]/.test(url);
}

/** A manifest href to a file of the document: a relative path that stays inside the base. */
export function isInsideBase(path: string): boolean {
  if (!isSafeImage(path) || /[\x00-\x1f\\]/.test(path)) return false;
  // Ask the URL parser where the path lands, rather than guess what it climbs with: it trims
  // spaces and reads "%2e%2e" as "..", as the browser will. Against two folders of different
  // names, so a path that climbs out and names its way back in ("../a/x") stays in neither.
  try {
    return ["/a/", "/b/"].every((dir) => {
      const url = new URL(path, `http://base.invalid${dir}`);
      return url.origin === "http://base.invalid" && url.pathname.startsWith(dir);
    });
  } catch {
    return false;
  }
}

const candidates = (srcset: string) =>
  srcset.split(",").map((c) => c.trim()).filter(Boolean).map((c) => c.split(/\s+/)[0]);

function allowed(el: Element, name: string, value: string): boolean {
  if (GLOBAL.has(name) || PATTERNS.some((p) => p.test(name))) return true;
  if (!PER_ELEMENT[el.localName]?.includes(name)) return false;
  if (name === "href") return isSafeLink(value);
  if (name === "src") return isSafeImage(value);
  if (name === "srcset") return candidates(value).every(isSafeImage);
  if (name === "style") return WIDTH.test(value);
  if (name === "type" && el.localName === "input") return INPUT_TYPES.has(value.toLowerCase());
  if (name === "name") return ["input", "select", "textarea"].includes(el.localName);
  return true;
}

/**
 * Sanitize `root` in place: remove what runs or embeds, open elements the transcript does not
 * use, and drop attributes and URLs that are not on the lists above. Text is never changed.
 * `root` belongs to a document made by DOMParser, so nothing in it has run or loaded.
 */
export function sanitizeChunk(root: Element): void {
  for (const el of [...root.querySelectorAll("*")]) {
    const tag = el.localName;
    if (el.namespaceURI === MATHML && MATH_ELEMENTS.has(tag)) {
      for (const { name, value } of [...el.attributes]) {
        if (MATH_ATTRIBUTES[tag]?.[name] !== value) el.removeAttribute(name);
      }
    } else if (el.namespaceURI !== XHTML || DROP.has(tag) || el.parentElement?.namespaceURI === MATHML) el.remove();
    else if (!ELEMENTS.has(tag)) el.replaceWith(...el.childNodes);
    else {
      for (const { name, value } of [...el.attributes]) {
        if (!allowed(el, name, value)) el.removeAttribute(name);
      }
      const classes = el.getAttribute("class");
      if (classes !== null) {
        const kept = classes.split(/\s+/).filter((c) => CLASSES.has(c));
        if (kept.length) el.setAttribute("class", kept.join(" "));
        else el.removeAttribute("class");
      }
    }
  }
}
