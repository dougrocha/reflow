"""A model's transcript of one page (HTML, or markdown from an OCR model) as reflow nodes, for
the prompt-only pipeline (analyze.direct).

The model's markup is never published. It is parsed into a small tree, and only the elements in
the allowed set become nodes: headings, paragraphs, lists, tables, figures with alt, notes,
form fields, and inline emphasis, superscripts and links. A wrapper outside the set (div, span,
section, thead, ...) is opened and its content kept; an element whose content is not page text
(script, style, svg, ...) is dropped with its content. Attributes are read only where a node
needs one (colspan, rowspan, scope, alt, href, type, checked, start) and every value is checked.
Each drop or unwrap of an unknown element is counted as a repair, which the pass records on the
page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from reflow.analyze.constants import DOCLING_TITLE_SIZE
from reflow.model import (BBox, HEADING_BY_LEVEL, Mark, Node, Role, Source, Span, ai_draft_attrs,
                          make_id, mark_order)
from reflow.text_quality import squash

CONFIDENCE = 0.85   # one value for every node the model wrote, like Docling's
HEADINGS = {role.value: role for role in HEADING_BY_LEVEL}
INLINE_MARKS = {"strong": "strong", "b": "strong", "em": "em", "i": "em", "sup": "sup"}
# Elements whose content is not page text: dropped whole.
DROPPED = {"script", "style", "svg", "math", "iframe", "object", "embed", "noscript", "template",
           "head", "title", "meta", "link", "base", "canvas", "video", "audio", "button",
           "option-group"}
# Wrappers that carry no meaning of their own here: opened silently.
TRANSPARENT = {"html", "body", "main", "article", "section", "div", "span", "thead", "tbody",
               "tfoot", "colgroup", "col", "center", "font", "u", "s", "small", "big", "mark",
               "abbr", "time", "address", "nav", "code", "kbd", "samp", "var", "cite", "q",
               "blockquote", "picture", "source", "ins", "del", "sub", "label-group"}
VOID = {"br", "img", "input", "hr", "col", "source", "meta", "link", "base", "wbr"}
BLOCKS = {"p", "ul", "ol", "li", "table", "tr", "td", "th", "caption", "figure", "figcaption",
          "aside", "header", "footer", "fieldset", "legend", "label", "select", "textarea",
          "dl", "dt", "dd", "pre", "hr", "img", "input", *HEADINGS}
CONTROLS = {"input", "select", "textarea"}
SAFE_HREF = re.compile(r"^(https?://|mailto:)\S+$", re.I)
MAX_SPAN = 50   # the largest colspan or rowspan kept
SPACE = re.compile(r"\s+")


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Element | str] = field(default_factory=list)


@dataclass
class Repairs:
    """What the converter changed in the model's markup: unknown elements opened, and
    elements dropped with their content."""
    unwrapped: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)

    def count(self) -> int:
        return sum(self.unwrapped.values()) + sum(self.dropped.values())


class _Builder(HTMLParser):
    """A forgiving tree builder: void elements close themselves, an end tag closes the open
    element of that name and everything inside it, a stray end tag is ignored, and a block that
    cannot sit in an open paragraph closes it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("root")
        self.stack = [self.root]

    def _close_to(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def _open_tags(self) -> list[str]:
        return [e.tag for e in self.stack]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        open_tags = self._open_tags()
        if tag in BLOCKS and "p" in open_tags and tag not in ("img", "label", *CONTROLS):
            self._close_to("p")
        if tag == "li" and open_tags[-1:] != ["ul"] and open_tags[-1:] != ["ol"]:
            for i in range(len(self.stack) - 1, 0, -1):   # a sibling item closes the open one
                if self.stack[i].tag in ("ul", "ol"):
                    break
                if self.stack[i].tag == "li":
                    del self.stack[i:]
                    break
        if tag == "tr":
            self._close_cell(("tr",))
        if tag in ("td", "th"):
            self._close_cell(("td", "th"))
        element = Element(tag, {k.lower(): (v or "") for k, v in attrs})
        self.stack[-1].children.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def _close_cell(self, tags: tuple[str, ...]) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == "table":
                return
            if self.stack[i].tag in tags:
                del self.stack[i:]
                return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self._close_to(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._open_tags()[1:]:
            self._close_to(tag)

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(markup: str) -> Element:
    builder = _Builder()
    builder.feed(markup)
    builder.close()
    return builder.root


def from_markdown(text: str) -> str:
    """Markdown (with any HTML it holds, such as an OCR model's tables) as HTML. markdown-it-py
    comes with rich, a dependency of reflow."""
    from markdown_it import MarkdownIt
    return MarkdownIt("commonmark", {"html": True}).enable("table").render(text)


def strip_fence(text: str) -> str:
    """The answer without a code fence around it (```html ... ```)."""
    match = re.fullmatch(r"\s*```[a-zA-Z]*\n(.*?)\n?```\s*", text, re.S)
    return match.group(1) if match else text


# ---------------------------------------------------------------------------- inline text

class _Inline:
    """Text gathered from inline content: whitespace collapsed as a browser would, with the
    marks and links over it at their offsets in the final text."""

    def __init__(self) -> None:
        self.text = ""
        self.marks: list[tuple[int, int, str]] = []
        self.spans: list[tuple[int, int, str]] = []
        self.lines = False   # a <br> was seen: the node keeps its line breaks

    def add(self, piece: str) -> None:
        piece = SPACE.sub(" ", piece)
        if not self.text or self.text.endswith((" ", "\n")):
            piece = piece.lstrip(" ")
        self.text += piece

    def br(self) -> None:
        self.text = self.text.rstrip(" ")
        if self.text and not self.text.endswith("\n"):
            self.text += "\n"
            self.lines = True

    def result(self) -> tuple[str, list[str] | None, list[Mark], list[Span]]:
        text = self.text.rstrip(" \n")
        size = len(text)
        lines = [line.strip() for line in text.split("\n")] if self.lines and "\n" in text else None
        flat = text.replace("\n", " ")
        marks, taken = [], set()
        for start, end, kind in self.marks:
            start, end = _trim(flat, start, min(end, size))
            if start < end and (start, end, kind) not in taken:
                taken.add((start, end, kind))
                marks.append(Mark(start, end, kind, flat[start:end]))
        spans, last = [], 0
        for start, end, href in sorted(self.spans):
            start, end = _trim(flat, start, min(end, size))
            if start < end and start >= last:
                spans.append(Span(start, end, "link", href))
                last = end
        if lines is not None and " ".join(lines) != flat:
            lines = None   # a line with its own spaces: set as one line, the text is what counts
        return flat, lines, marks, spans


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] == " ":
        start += 1
    while end > start and text[end - 1] == " ":
        end -= 1
    return start, end


# ---------------------------------------------------------------------------- the converter

class Converter:
    """One page's tree as nodes. `page` is the page number every node gets; `size` the page's
    width and height in points, for a figure's box."""

    def __init__(self, page: int, size: tuple[float, float]) -> None:
        self.page = page
        self.size = size
        self.repairs = Repairs()
        self.count = 0
        self.fields = 0

    # -- helpers

    def node(self, role: Role, text: str | None = None, **attrs) -> Node:
        self.count += 1
        return Node(role, id=make_id(self.page, None, f"direct:{self.count}:{text or ''}"),
                    page=self.page, text=text, attrs=attrs, confidence=CONFIDENCE,
                    source=Source.LLM)

    def text_node(self, role: Role, inline: _Inline, **attrs) -> Node | None:
        text, lines, marks, spans = inline.result()
        if not text:
            return None
        node = self.node(role, text, **attrs)
        if lines and len(lines) > 1:
            node.attrs["lines"] = lines
        node.marks = sorted(marks, key=mark_order)
        node.spans = spans
        node.check_inline()
        return node

    def gather(self, children: list[Element | str], inline: _Inline | None = None,
               marks: tuple[str, ...] = ()) -> _Inline:
        """The inline text of `children`, every element's text included (a nested block's
        words too, so a cell keeps all its words)."""
        inline = inline or _Inline()
        for child in children:
            if isinstance(child, str):
                inline.add(child)
                continue
            tag = child.tag
            if tag in DROPPED:
                self._drop(tag)
                continue
            if tag == "br":
                inline.br()
                continue
            if marks == ("cell",) and (tag in CONTROLS or (tag == "label" and any(
                    x.tag in CONTROLS for x in _walk(child)))):
                continue   # a cell's controls are its children (Converter.table)
            start = len(inline.text)
            if tag in BLOCKS and inline.text and not inline.text.endswith((" ", "\n")):
                inline.add(" ")
                start = len(inline.text)
            self.gather(child.children, inline, marks)
            if tag in BLOCKS and inline.text and not inline.text.endswith((" ", "\n")):
                inline.add(" ")
            end = len(inline.text)
            if tag in INLINE_MARKS:
                inline.marks.append((start, end, INLINE_MARKS[tag]))
            elif tag == "a":
                href = child.attrs.get("href", "").strip()
                if SAFE_HREF.match(href):
                    inline.spans.append((start, end, href))
            elif tag not in BLOCKS and tag not in TRANSPARENT and tag not in ("a", "option"):
                self.repairs.unwrapped[tag] = self.repairs.unwrapped.get(tag, 0) + 1
        return inline

    def _drop(self, tag: str) -> None:
        self.repairs.dropped[tag] = self.repairs.dropped.get(tag, 0) + 1

    # -- blocks

    def blocks(self, children: list[Element | str]) -> list[Node]:
        """The block nodes of `children`; loose inline content between blocks is a paragraph."""
        out: list[Node] = []
        run: list[Element | str] = []

        def flush() -> None:
            if run:
                if (node := self.text_node(Role.P, self.gather(run))) is not None:
                    out.append(node)
                run.clear()

        for child in children:
            if isinstance(child, str) or (
                    (child.tag in INLINE_MARKS or child.tag in ("a", "br")
                     or (child.tag not in BLOCKS and child.tag not in TRANSPARENT
                         and child.tag not in DROPPED and not _holds_block(child)))
                    and not _holds_control(child)):
                run.append(child)
                continue
            flush()
            out.extend(self.block(child))
        flush()
        return out

    def block(self, e: Element) -> list[Node]:
        tag = e.tag
        if tag in DROPPED:
            self._drop(tag)
            return []
        if tag in HEADINGS:
            node = self.text_node(HEADINGS[tag], self.gather(e.children))
            if node is not None and tag == "h1":
                # As Docling's title: ranked above every section heading, so the title pass
                # promotes it (evidence.witness); fix_heading_levels sets the rest.
                node.role = Role.H2
                node.attrs["font_size"] = DOCLING_TITLE_SIZE
            return [node] if node else []
        if tag in ("p", "dt", "dd", "pre", "legend"):
            if _holds_block(e) or _holds_control(e):
                return self.blocks(e.children)
            node = self.text_node(Role.P, self.gather(e.children))
            return [node] if node else []
        if tag in ("ul", "ol"):
            node = self.list(e)
            return [node] if node else []
        if tag == "li":   # an item outside a list: a list of its own
            return self.blocks([Element("ul", {}, [e])])
        if tag == "table":
            node = self.table(e)
            return [node] if node else []
        if tag in ("figure", "img"):
            return self.figure(e)
        if tag == "aside":
            node = self.text_node(Role.NOTE, self.gather(e.children))
            return [node] if node else []
        if tag in ("header", "footer"):
            node = self.text_node(Role.ARTIFACT, self.gather(e.children))
            return [node] if node else []
        if tag == "caption" or tag == "figcaption":
            node = self.text_node(Role.CAPTION, self.gather(e.children))
            return [node] if node else []
        if tag == "fieldset":
            return [self.group(e)]
        if tag in ("label", "input", "select", "textarea"):
            node = self.field(e)
            return [node] if node else []
        if tag == "hr":
            return []
        if tag in INLINE_MARKS or tag == "a":   # emphasis around a control: the control counts
            return self.blocks(e.children)
        if tag not in TRANSPARENT:
            self.repairs.unwrapped[tag] = self.repairs.unwrapped.get(tag, 0) + 1
        return self.blocks(e.children)

    def list(self, e: Element) -> Node | None:
        ordered = e.tag == "ol"
        start = _int(e.attrs.get("start"), 1, 1, 100000) if ordered else 1
        items: list[Node] = []
        for child in _elements(e.children):
            if child.tag != "li":
                if child.tag in ("ul", "ol") and items:   # a nested list beside its item
                    if (nested := self.list(child)) is not None:
                        items[-1].children.append(nested)
                continue
            words = [c for c in child.children if isinstance(c, str) or c.tag not in ("ul", "ol")]
            marker = f"{start + len(items)}." if ordered else ""
            item = self.text_node(Role.LI, self.gather(words, marks=("cell",)),
                                  **({"marker": marker} if marker else {}))
            if item is None:
                item = self.node(Role.LI, "", **({"marker": marker} if marker else {}))
            # An item's controls are its children, as a cell's are (Converter.table).
            item.children = [f for f in (self.field(x) for x in _walk(child, stop={"label", "ul", "ol"})
                                         if x.tag in (*CONTROLS, "label"))
                             if f is not None and f.role == Role.FIELD]
            for sub in _elements(child.children):
                if sub.tag in ("ul", "ol") and (nested := self.list(sub)) is not None:
                    item.children.append(nested)
            items.append(item)
        if not items:
            return None
        node = self.node(Role.LIST, ordered=ordered)
        node.children = items
        return node

    def table(self, e: Element) -> Node | None:
        table = self.node(Role.TABLE)
        rows: list[Node] = []
        for child in _walk(e, stop={"table"}):
            if child.tag == "caption":
                if (cap := self.text_node(Role.CAPTION, self.gather(child.children))) is not None:
                    table.children.append(cap)
            elif child.tag == "tr":
                tr = self.node(Role.TR, f"row {len(rows)}")
                for cell in _elements(child.children):
                    if cell.tag not in ("td", "th"):
                        continue
                    attrs: dict = {}
                    for key in ("colspan", "rowspan"):
                        if (span := _int(cell.attrs.get(key), 1, 1, MAX_SPAN)) > 1:
                            attrs[key] = span
                    role = Role.TD
                    if cell.tag == "th":
                        role = Role.TH
                        if cell.attrs.get("scope", "").lower() == "row":
                            attrs["scope"] = "row"
                    words = self.gather(cell.children, marks=("cell",))
                    text, _, marks, spans = words.result()
                    node = self.node(role, text, **attrs)
                    node.marks, node.spans = sorted(marks, key=mark_order), spans
                    node.children = [f for f in (self.field(x) for x in _walk(cell, stop={"label"})
                                                 if x.tag in (*CONTROLS, "label"))
                                     if f is not None and f.role == Role.FIELD]
                    tr.children.append(node)
                if tr.children:
                    rows.append(tr)
        if not rows:
            return None
        table.children.extend(rows)
        return table

    def figure(self, e: Element) -> list[Node]:
        img = e if e.tag == "img" else next((x for x in _walk(e) if x.tag == "img"), None)
        alt = squash(img.attrs.get("alt", "") if img else "")
        attrs: dict = ai_draft_attrs()
        if img is not None and "alt" in img.attrs and not alt:
            attrs["decorative"] = True
        if alt:
            attrs["alt"] = alt
        figure = self.node(Role.FIGURE, **attrs)
        if not alt and not attrs.get("decorative"):
            for key in ai_draft_attrs():
                del figure.attrs[key]
            figure.attrs["needs"] = "alt"
        if img is not None and (box := self.box(img.attrs.get("data-box", ""))) is not None:
            figure.bbox = box
        for child in _walk(e):
            if child.tag == "figcaption":
                if (cap := self.text_node(Role.CAPTION, self.gather(child.children))) is not None:
                    figure.children.append(cap)
        if e.tag == "figure":   # anything else printed inside it is page text, kept after it
            rest = [c for c in e.children if not (isinstance(c, Element) and c.tag in ("img", "figcaption"))
                    and not (isinstance(c, str) and not c.strip())]
            return [figure, *self.blocks(rest)]
        return [figure]

    def box(self, value: str) -> BBox | None:
        """A figure's box, given in thousandths of the page from its top left, in points."""
        parts = re.split(r"[\s,]+", value.strip())
        try:
            x0, y0, x1, y1 = (float(p) for p in parts)
        except ValueError:
            return None
        if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
            return None
        w, h = self.size
        return (round(x0 * w / 1000, 2), round(y0 * h / 1000, 2),
                round(x1 * w / 1000, 2), round(y1 * h / 1000, 2))

    # -- form fields

    def field(self, e: Element, group: str | None = None) -> Node | None:
        """A control and its caption: a <label> holding the caption and its control, or a bare
        control (named by its own placeholder or name, if any)."""
        control = e if e.tag in ("input", "select", "textarea") else next(
            (x for x in _walk(e) if x.tag in ("input", "select", "textarea")), None)
        if control is None:   # a label with no control is printed text
            node = self.text_node(Role.P, self.gather(e.children))
            return node
        caption = self.gather([c for c in e.children if not (isinstance(c, Element) and c.tag in ("select", "textarea"))]
                              ) if e is not control else _Inline()
        text, _, _, _ = caption.result()
        kind = control.attrs.get("type", "text").lower() if control.tag == "input" else \
            ("combo" if control.tag == "select" else "text")
        if kind not in ("text", "checkbox", "radio", "combo"):
            kind = "text"
        self.fields += 1
        attrs: dict = {"kind": kind, "fid": f"d{self.page}-{self.fields}"}
        if control.tag == "textarea":
            attrs.update(multiline=True, height=36.0)
        if kind in ("checkbox", "radio") and "checked" in control.attrs:
            attrs["checked"] = True
        if kind == "combo":
            attrs["options"] = [squash(self.gather(o.children).result()[0])
                                for o in _walk(control) if o.tag == "option"]
        if group is not None and kind == "radio":
            attrs["group"] = group
        if not text:
            name = " ".join((control.attrs.get("aria-label") or control.attrs.get("placeholder")
                             or "").split())
            if name:
                attrs["aria"] = name
            else:
                attrs["needs"] = "label"
        return self.node(Role.FIELD, text or None, **attrs)

    def group(self, e: Element) -> Node:
        legend = next((x for x in _elements(e.children) if x.tag == "legend"), None)
        text = self.gather(legend.children).result()[0] if legend else ""
        group = self.node(Role.GROUP, text or None)
        if not text:
            group.attrs["aria"] = "Choices"
        gid = f"d{self.page}-g{self.count}"
        for child in _elements(e.children):
            if child is legend:
                continue
            if child.tag in ("label", "input", "select", "textarea"):
                if (node := self.field(child, gid)) is not None:
                    group.children.append(node)
            else:
                for inner in self.block(child):
                    if inner.role == Role.FIELD and inner.attrs.get("kind") == "radio":
                        inner.attrs["group"] = gid
                    group.children.append(inner)
        return group


def _elements(children: list[Element | str]) -> list[Element]:
    return [c for c in children if isinstance(c, Element)]


def _walk(e: Element, stop: set[str] = frozenset()):
    """Every element under `e`, depth first, not entering a nested element named in `stop`."""
    for child in _elements(e.children):
        yield child
        if child.tag not in stop:
            yield from _walk(child, stop)


def _holds_block(e: Element) -> bool:
    return any(x.tag in BLOCKS and x.tag not in ("img", "input", "label") for x in _walk(e))


def _holds_control(e: Element) -> bool:
    return any(x.tag in CONTROLS for x in _walk(e))


def _int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if low <= n <= high else default


def page_nodes(markup: str, page: int, size: tuple[float, float], *, markdown: bool = False
               ) -> tuple[list[Node], Repairs]:
    """The nodes of one page's transcript, and what was repaired on the way. `markdown` reads
    the answer as markdown first (an OCR model's)."""
    text = strip_fence(markup)
    if markdown:
        text = from_markdown(text)
    converter = Converter(page, size)
    nodes = converter.blocks(parse(text).children)
    return nodes, converter.repairs


__all__ = ["Converter", "Repairs", "from_markdown", "page_nodes", "parse", "strip_fence"]
