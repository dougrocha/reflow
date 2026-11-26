"""The HTML of the document model: inline text with its marks and links (inline) and each
block (NodeRenderer)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from markupsafe import Markup, escape

from reflow.analyze.language import (
    STOPWORDS,
    block_language,
    detect_latin_language,
    is_ltr_text,
    is_rtl_text,
)
from reflow.analyze.input_purpose import input_purpose
from reflow.analyze.sections import heading_of
from reflow.names import FIGURES_DIR
from reflow.extract.forms import DetectionSource
from reflow.model import (ACCENTS, HEADING_LEVEL, MARK_RANK, LabelSource, Mark, Node, Role, Source, Span,
                          figure_role, is_ai_draft, is_ocr)
from reflow.render import mathml
from reflow.render.alt import figure_alt
from reflow.render.annotations import SEPARATOR, annotations
from reflow.render.casing import LABEL_MAX_WORDS, RUN_MAX_WORDS, Casing, in_capitals
from reflow.render.links import (
    anchor,
    anchor_tags,
    attribute,
    auto_links,
    linkify,
    note_id,
    page_link,
    ref_id,
)
from reflow.render.tables import header_rows
from reflow.text_quality import squash



# Roman numerals up to 39 (the list markers the layout reads: "i" to "xxxix").
ROMAN_NUMERAL = re.compile(r"^x{0,3}(ix|iv|v?i{0,3})$", re.I)


ROMAN_VALUE = {"i": 1, "v": 5, "x": 10}


def _is_legal_label(marker: str) -> bool:
    """A parenthesized label as printed: "(a)", "(1)", "(A)", "(ii)"."""
    return marker.startswith("(") and marker.endswith(")")


def _roman_value(token: str) -> int | None:
    """The value of a roman numeral token ("iv" is 4), or None when it is not one."""
    if not token or not ROMAN_NUMERAL.match(token):
        return None
    vals = [ROMAN_VALUE[c] for c in token.lower()]
    return sum(-v if i + 1 < len(vals) and v < vals[i + 1] else v for i, v in enumerate(vals))


def _ordered_attrs(markers: list[str]) -> str:
    """The type and start of an ordered list from its printed markers: "3." starts at 3, "iv."
    is roman from 4, "c." is the letter from 3. A lone "i" is a letter unless "ii" follows it."""
    token = (markers[0] if markers else "").strip("().")
    if token.isdigit():
        return f" {attribute('start', int(token))}" if token != "1" else ""
    value = _roman_value(token)
    if value is not None and (len(token) > 1 or (len(markers) > 1 and
                                                  _roman_value(markers[1].strip("().")) == 2)):
        kind = "i" if token.islower() else "I"
        return f" {attribute('type', kind)}" + (f" {attribute('start', value)}" if value != 1 else "")
    if len(token) == 1 and token.isalpha():
        kind = "a" if token.islower() else "A"
        start = ord(token.lower()) - 96
        return f" {attribute('type', kind)}" + (f" {attribute('start', start)}" if start != 1 else "")
    return ""


# Authoring-tool defaults that say nothing about the field: "Text1", "Text Field 3",
# "undefined_2", "Check Box1", "fill_4", "Field12", a bare number.
_JUNK_FIELD_NAME = re.compile(
    r"^(?:(?:text(?: ?field| ?box)?|check ?box|checkbox|radio(?: ?button)?|button|"
    r"combo(?: ?box)?|list(?: ?box)?|dropdown|drop down|undefined|fill|field|f)"
    r" ?\d*(?: \d+)?|\d+)$", re.I)


def _bold_lead_in(node: Node) -> bool:
    """A paragraph set wholly in bold, such as a lead-in over the paragraph it introduces. The
    stylesheet keeps it close to what follows (.lead)."""
    text = node.text or ""
    start, end = len(text) - len(text.lstrip()), len(text.rstrip())
    return end > start and any(m.kind == "strong" and m.start <= start and m.end >= end and
                               text[m.start:m.end] == m.text for m in node.marks)


_DOUBT = "OCR is unsure of this word"


# Nesting by rank, outermost first: an anchor (a link or note reference) at 0, then the marks
# in the model's order (MARK_RANK), one rank further in.
_ANCHOR_RANK = 0


_UNREAD = ('<del class="unread-word" data-generated><span role="img" '
           'aria-label="struck word, unreadable"></span></del>')


def _mark_tags(mark: Mark) -> tuple[str, str]:
    if mark.kind == "fix":   # a reviewer's correction: the page shows the word, the tooltip OCR's reading
        was = escape(mark.alt)
        return (f'<span class="reviewed-fix" {attribute("data-was", was)} '
                f'{attribute("title", "Corrected by a reviewer. OCR read: ", was)}>'), "</span>"
    if mark.kind == "doubt":
        if mark.alt:
            alt = escape(mark.alt)
            return (f'<span class="ocr-doubt" {attribute("data-alt", alt)} '
                    f'{attribute("title", _DOUBT, ". The document suggests: ", alt)}>'), "</span>"
        return f"""<span class="ocr-doubt" {attribute('title', _DOUBT)}>""", "</span>"
    if mark.kind in ACCENTS:   # a short run the PDF prints in a colour: a named accent (transcript.css)
        return f'<span {attribute("class", "ink-", mark.kind)}>', "</span>"
    return f"<{mark.kind}>", f"</{mark.kind}>"


@dataclass
class _Element:
    """One element over text[start:end]: what the emitter opens and closes."""
    start: int
    end: int
    rank: int
    open: str
    close: str


def _emit(text: str, spans: list[Span], marks: list[Mark], lo: int, hi: int, page: int | None,
          auto: bool, display: str | None = None) -> Markup:
    """text[lo:hi] as HTML: spans (an offset range over the whole text) as anchors, marks as
    their elements, and, when `auto`, the URLs, emails and phones of the stretches no span
    covers as links. The elements nest by rank, outermost first (anchor, ins/del, strong, em,
    doubt, sup); where a higher-ranked element starts or ends inside a lower-ranked one, the lower
    is closed and re-opened, so the HTML is always well nested. An anchor is outermost, so it
    is never split. A sup inside a note reference is left out (the reference is already
    raised). The spans and marks fit the text: Node.check_inline holds them to it. `display`,
    the text in sentence case (render.casing), sets each stretch it changes as _cased does."""
    elements: list[_Element] = []
    free: list[tuple[int, int]] = []        # the stretches of [lo, hi) that no span covers
    raised: list[tuple[int, int]] = []      # the note references, set as <sup> already
    at = lo
    for span in sorted(spans, key=lambda sp: (sp.start, sp.end)):
        if span.kind == "noteref":
            raised.append((span.start, span.end))
        start, end = max(span.start, lo), min(span.end, hi)
        if end <= start:
            continue
        free.append((at, start))
        elements.append(_Element(start, end, _ANCHOR_RANK, *anchor_tags(span, page)))
        at = end
    free.append((at, hi))
    if auto:
        for start, end in free:
            for a, b, href in auto_links(text[start:end]):
                elements.append(_Element(start + a, start + b, _ANCHOR_RANK, f"<a {attribute('href', href)}>", "</a>"))
    points: dict[int, int] = {}
    for mark in marks:
        if mark.kind == "sup" and any(a <= mark.start and mark.end <= b for a, b in raised):
            continue
        if mark.kind == "del-unread":
            if lo <= mark.start <= hi:
                points[mark.start] = points.get(mark.start, 0) + 1
            continue
        start, end = max(mark.start, lo), min(mark.end, hi)
        if start < end:
            elements.append(_Element(start, end, MARK_RANK[mark.kind] + 1, *_mark_tags(mark)))
    # A link is set in the link colour; an accent inside one would fight it.
    anchors = [e for e in elements if e.rank == _ANCHOR_RANK]
    elements = [e for e in elements if e.rank != MARK_RANK["red"] + 1
                or not any(e.start < a.end and a.start < e.end for a in anchors)]
    elements.sort(key=lambda e: (e.rank, e.start, -e.end))
    cuts = sorted({lo, hi, *points, *(p for e in elements for p in (e.start, e.end))})
    out: list[str] = []
    stack: list[_Element] = []

    def settle(want: list[_Element]) -> None:
        """Close the open elements down to the ones `want` shares with them, then open the rest."""
        keep = 0
        while keep < min(len(stack), len(want)) and stack[keep] is want[keep]:
            keep += 1
        out.extend(e.close for e in reversed(stack[keep:]))
        out.extend(e.open for e in want[keep:])
        stack[:] = want

    for here, there in zip(cuts, [*cuts[1:], None]):
        if here in points:
            settle([e for e in elements if e.start < here < e.end])
            out.append(_UNREAD * points[here])
        if there is not None:
            settle([e for e in elements if e.start <= here and there <= e.end])
            out.append(str(_cased(text[here:there], display[here:there] if display else None)))
    settle([])
    return Markup("".join(out))


def _cased(text: str, shown: str | None) -> Markup:
    """`text` as printed, with the form to show in its place when that differs (render.casing):
    the stylesheet draws `data-sc` and keeps the printed words for screen readers and copying."""
    if not shown or shown == text:
        return escape(text)
    return Markup(f'<span class="sc" {attribute("data-sc", shown)}><span>{escape(text)}</span></span>')


def inline(node: Node, *, auto: bool = True) -> Markup:
    """The node's text as HTML: its spans (links, note references) and marks (strong, em, OCR
    marks) in place, and links for the URLs, emails and phones they do not cover. `auto=False`
    leaves the rest as plain escaped text."""
    text = node.text or ""
    return _emit(text, node.spans, node.marks, 0, len(text), node.page, auto)


def status(node: Node) -> str:
    """What the page says about the node beside its text (render.annotations): what a status
    icon beside it means, from the page's legend ("Accepting New Patients", see
    reflow.analyze.figures.find_legend), and a calendar day's key labels and list notes. Words
    copied from elsewhere on the page sit in a span that says where from (data-from) and the
    boxes they are printed in (data-bbox), so they are never taken for the cell's own text."""
    out = []
    for a in annotations(node):
        if a.origin == "legend":
            out.append(f' <span class="status">— {escape(a.text)}</span>')
            continue
        kind = "day-status" if a.kind == "status" else "day-note"
        boxes = json.dumps([list(b) for b in a.sources], separators=(",", ":"))
        out.append(f'{SEPARATOR}<span {attribute("class", kind)} '
                   f'{attribute("data-from", a.origin)} '
                   f'{attribute("data-bbox", boxes)}>{escape(a.text)}</span>')
    return "".join(out)


def field_source(detected: str) -> str:
    """How a field that is not in the PDF was found (`data-field-source`): "blank" from the
    printed blank lines, "drawn" from a box drawn on the page, "scan" by reading a scanned page."""
    return {DetectionSource.TEXT: "blank", DetectionSource.DRAWN: "drawn"}.get(detected, "scan")


FIELD_RUN_GAP = 36.0  # boxes this close on one row are one row of a date or a number (pt)


def _same_row_run(a: Node, b: Node) -> bool:
    """b is a text box right after a on the same printed row."""
    return (abs((a.bbox[1] + a.bbox[3]) / 2 - (b.bbox[1] + b.bbox[3]) / 2) <= 3
            and -1 <= b.bbox[0] - a.bbox[2] <= FIELD_RUN_GAP)


def _has_words(name: str, label: str) -> bool:
    """Whether the words of `label` run on in `name`, ignoring case, punctuation and marks
    such as a trailing colon or a leading asterisk. A label with no words is in any name."""
    def words(text: str) -> str:
        return " ".join(re.findall(r"\w+", text.casefold()))
    return not words(label) or f" {words(label)} " in f" {words(name)} "


class NodeRenderer:
    def __init__(self, figures: dict[str, tuple[str, int, int]], labels: dict[int, str],
                 page_of_label: dict[str, int] | None = None, rtl_document: bool = False,
                 ltr_lang: str | None = None, second_lang: str | None = None,
                 doc_lang: str = "en", version: str = "",
                 page_langs: dict[int, str] | None = None,
                 headings: dict[str, str] | None = None, casing: Casing | None = None):
        self.figures = figures  # node id → (filename, width, height)
        self.headings = headings or {}  # figure id → the heading before it (render.alt)
        self.labels = labels
        self.page_of_label = page_of_label or {}  # printed page label → page number
        self.rtl_document = rtl_document  # the whole document is right-to-left
        self.ltr_lang = ltr_lang  # the language of its left-to-right blocks, when clear
        self.second_lang = second_lang
        self.doc_lang = doc_lang
        self.page_langs = page_langs or {}  # page number → the language a whole page is in
        self.version_query = f"?v={version}" if version else ""
        self.casing = casing  # the display case of runs printed in capitals (render.casing)
        self.title_pages: set[int | None] = set()  # the pages a title (an H1) was rendered on
        # What is known of the place a field is in, outermost first: the document, then each
        # section and group being rendered with its heading, and the labels of the fields
        # already written there. It tells the field whose data is asked (analyze.input_purpose).
        self._context: list[list[str]] = [[]]
        self._section_context: dict[str, list[str]] = {}  # section id → its level, kept across pages

    def _display(self, text: str | None, max_words: int) -> str | None:
        return self.casing.display(text or "", max_words) if self.casing else None

    def _lang_dir(self, text: str, page: int | None = None) -> str:
        """The block's direction and language when they differ from the document's."""
        tag = block_language(text, self.doc_lang, self.ltr_lang, self.page_langs.get(page))
        lang = f" {attribute('lang', tag)}" if tag else ""
        if not self.rtl_document:
            return (' dir="rtl"' if is_rtl_text(text) else "") + lang
        if is_ltr_text(text):
            return f' dir="ltr"{lang}'
        return lang

    def _heading_text(self, text: str) -> Markup:
        """Tag a short translated half of a bilingual heading when the block is too short."""
        left, separator, right = text.rpartition(" / ")
        primary = self.doc_lang.split("-", 1)[0].lower()
        right_lang = detect_latin_language([right], min_stopwords=1) if separator else None
        lang = right_lang if right_lang and right_lang != primary else self.second_lang
        primary_words = STOPWORDS.get(primary, set())
        right_words = {word for word in re.findall(r"[^\W\d_]+", right.lower())}
        if (lang and self.second_lang and lang != primary and
                left.strip().casefold() != right.strip().casefold() and
                not (right_words & primary_words)):
            return Markup(f"{escape(left)}{escape(separator)}<span {attribute('lang', lang)}>{escape(right)}</span>")
        return escape(text)

    def render(self, node: Node) -> str:
        html = self._render(node)
        if not is_ai_draft(node):
            return html
        handwritten = node.attrs.get("handwritten") is True
        label = "Handwritten · AI-drafted, not reviewed" if handwritten else "AI-drafted, not reviewed"
        bbox = escape(json.dumps(node.bbox))
        # Decorative art a model chose to leave out leaves its badge out with it.
        hidden = " hidden" if out_of_sight(node) else ""
        return (f'<div class="ai-draft"{hidden} data-provenance="ai-drafted" data-reviewed="false" '
                f'{attribute("data-handwritten", str(handwritten).lower())} '
                f'{attribute("data-bbox", bbox)}>'
                f'<small class="ai-badge" role="note" data-generated>{label}</small>{html}</div>')

    def section(self, heading: Node | None, body: str) -> str:
        """A <section> named by its heading. `heading` is None for the part of a section that
        continues from an earlier page: it has no heading here to name it, so it is a plain
        grouping and not a landmark."""
        label = f" {attribute('aria-labelledby', anchor(heading))}" if heading is not None else ""
        return f"<section{label}>{body}</section>"

    def _render(self, node: Node) -> str:
        role = node.role
        if role in HEADING_LEVEL:
            text = (inline(node, auto=False) if node.spans or node.marks
                    else self._heading_text(node.text or ""))
            shown = self._display(node.text, LABEL_MAX_WORDS)
            if shown and " / " not in (node.text or ""):   # a bilingual heading keeps its lang span
                full = node.text or ""
                text = _emit(full, node.spans, node.marks, 0, len(full), node.page, False, shown)
            caps = in_capitals(node.text or "")   # .caps spaces its letters; the text keeps its case
            if role == Role.H1:
                self.title_pages.add(node.page)
                return (f'<p {attribute("class", "title caps" if caps else "title")}'
                        f'{self._lang_dir(node.text or "", node.page)} '
                        f'{node_attrs(node)}>{text}</p>')
            n = HEADING_LEVEL[role]
            css = f' {attribute("class", "caps")}' if caps else ""
            return (f'<h{n} {attribute("id", anchor(node))}{css}'
                    f'{self._lang_dir(node.text or "", node.page)} '
                    f'{node_attrs(node)}>{text}{status(node)}</h{n}>')
        if role == Role.SECTION:
            heading = heading_of(node)
            return self.section(heading, self.section_items(heading, node.children, self.cell_fields, node))
        if role == Role.LIST:
            return self._list(node)
        if role == Role.FIGURE:
            return self._figure(node)
        if role == Role.TABLE:
            return self._table(node)
        if role == Role.FIELD:
            return self._field(node)
        if role == Role.GROUP:
            return self._group(node)
        if role == Role.BOX:
            return self._box(node)
        if role == Role.CAPTION:
            text = inline(node)
            return (f'<p class="caption"{self._lang_dir(node.text or "", node.page)} '
                    f'{node_attrs(node)}>{text}</p>')
        if role == Role.NOTE and node.attrs.get("marker"):
            return self._footnote(node)
        if role == Role.NOTE:
            text = inline(node)
            return (f'<p class="note"{self._lang_dir(node.text or "", node.page)} '
                    f'{node_attrs(node)}>{text}</p>')
        if node.attrs.get("context"):
            return (f'<p class="continued"{self._lang_dir(node.text or "", node.page)} '
                    f'{node_attrs(node)}>{inline(node, auto=False)}</p>')
        # Linkify paragraphs but not headings
        lines = node.attrs.get("lines")
        shown = None if lines else self._display(node.text, RUN_MAX_WORDS)
        text = (self._lines(node, lines) if lines else
                _emit(node.text or "", node.spans, node.marks, 0, len(node.text or ""), node.page, True, shown)
                if shown else inline(node))
        if is_ai_draft(node) and not (node.spans or node.marks):
            text = mathml.inline(node.text or "") or text   # a model's LaTeX, as math when it reads
        classes = " ".join(name for name, on in (("i", node.attrs.get("italic")),
                                                 ("lead", _bold_lead_in(node))) if on)
        css = f" {attribute('class', classes)}" if classes else ""
        dir_attr = self._lang_dir(node.text or " ".join(lines or []), node.page)
        return f'<p{css}{dir_attr} {node_attrs(node)}>{text}{status(node)}</p>'

    def _lines(self, node: Node, lines: list[str]) -> Markup:
        """A paragraph set line by line (an address): `node.text` is the lines joined by one
        space, so each line takes the spans and marks that fall in its range. When the text
        was edited since and no longer matches the lines, the paragraph is set as one line,
        so a span or mark is not dropped (a missing line break is cosmetic)."""
        text, at, parts = node.text or "", 0, []
        if not (node.spans or node.marks):
            return Markup("<br>").join(linkify(l) for l in lines)
        if text != " ".join(lines):
            return inline(node)
        for line in lines:
            parts.append(_emit(text, node.spans, node.marks, at, at + len(line), node.page, True))
            at += len(line) + 1
        return Markup("<br>").join(parts)

    def _footnote(self, node: Node) -> str:
        """A footnote: DPUB-ARIA doc-footnote, with a backlink to the reference (the noteref
        span on the body text carries id "r-<id>", later ones "r-<id>-2"...; one backlink per reference)."""
        marker = str(node.attrs["marker"])
        page = node.page
        backs = []
        for count in range(1, int(node.attrs.get("refs", 1)) + 1):
            frag = ref_id(node.id, count)
            back = page_link(page, frag) if page is not None else f"#{frag}"
            many = int(node.attrs.get("refs", 1)) > 1
            label = f"Back to reference {count} to note {marker}" if many \
                else f"Back to the reference to note {marker}"
            text = marker if count == 1 else f"\u21a9{count}"
            backs.append(f"""<a role="doc-backlink" {attribute('href', back)} {attribute('aria-label', label)}>{escape(text)}</a>""")
        return (f'<aside class="footnote" role="doc-footnote" {attribute("id", note_id(node.id))}'
                f'{self._lang_dir(node.text or "", node.page)} '
                f'{node_attrs(node)}>{" ".join(backs)} {inline(node)}</aside>')

    def _item(self, node: Node, label: bool = False) -> str:
        # An item may hold a nested list: its sub-entries, or lettered sub-items. In a legal
        # list (see _list) the item shows its printed label as text, so "(ii)" stays "(ii)".
        nested = "".join(self._list(c) for c in node.children if c.role == Role.LIST)
        # Its controls (a direct transcript's "Enter the amount here <input>").
        fields = [c for c in node.children if c.role in (Role.FIELD, Role.GROUP)]
        nested = (self.cell_fields(fields) if fields else "") + nested
        if not node.attrs.get("contents"):
            shown = (f'<span class="marker">{escape(node.attrs.get("marker", ""))}</span> '
                     if label and node.attrs.get("marker") else "")
            return (f'<li{self._lang_dir(node.text or "", node.page)} {node_attrs(node)}>'
                    f'{shown}{inline(node)}{status(node)}{nested}</li>')
        # A contents entry: the title, then its page number (linked when it is a page here).
        ref = node.attrs.get("page_ref", "")
        target = self.page_of_label.get(ref)
        number = f"<a {attribute('href', page_link(target))}>{escape(ref)}</a>" if target else escape(ref)
        return f'<li {node_attrs(node)}>{escape(node.text or "")}{" " + number if ref else ""}{nested}</li>'

    def _list(self, node: Node) -> str:
        if not node.attrs.get("ordered"):
            items = "".join(self._item(c) for c in node.children if c.role == Role.LI)
            return f'<ul {node_attrs(node)}>{items}</ul>'
        markers = [c.attrs.get("marker", "") for c in node.children if c.role == Role.LI]
        if markers and _is_legal_label(markers[0]):
            # A legal list keeps each label as printed, shown as text (see _item): the browser
            # would number "(a)" as "a." and "(1)" as "1.", and would not restart a nested run.
            items = "".join(self._item(c, label=True) for c in node.children if c.role == Role.LI)
            return f'<ol class="legal" role="list" {node_attrs(node)}>{items}</ol>'
        items = "".join(self._item(c) for c in node.children if c.role == Role.LI)
        return f'<ol{_ordered_attrs(markers)} {node_attrs(node)}>{items}</ol>'

    def _table(self, node: Node) -> str:
        caption = next((c for c in node.children if c.role == Role.CAPTION), None)
        rows = [c for c in node.children if c.role == Role.TR]
        header = header_rows(rows)

        def row_html(tr: Node) -> str:
            out = []
            for c in tr.children:
                span = "".join(f" {attribute(k, int(c.attrs[k]))}" for k in ("colspan", "rowspan")
                               if c.attrs.get(k))
                tag = "th" if c.role == Role.TH else "td"
                # A tagged /Scope /Both is a header of its row and its column, which the HTML
                # attribute cannot say (row, col, rowgroup, colgroup): with none, a browser
                # infers the role from the cell's place, so neither axis is lost.
                scope_value = c.attrs.get("scope", "col")
                scope = (f" {attribute('scope', scope_value)}"
                         if tag == "th" and scope_value != "both" else "")
                cell_id = f" {attribute('id', c.attrs['id'])}" if c.attrs.get("id") else ""
                headers = (f" {attribute('headers', ' '.join(c.attrs['headers']))}"
                           if c.attrs.get("headers") else "")
                bar = "".join(self.render(h) for h in c.children if h.role in HEADING_LEVEL)
                fields = bar + self.cell_fields([f for f in c.children
                                                  if f.role in (Role.FIELD, Role.GROUP)])
                trace = f' {node_attrs(c)}' if c.bbox else ""
                out.append(f"<{tag}{scope}{span}{cell_id}{headers}{trace}>{inline(c)}{status(c)}"
                           f"{fields}</{tag}>")
            return f"<tr>{''.join(out)}</tr>"

        label = (caption.text if caption and caption.text
                 else f"Table on page {self.labels.get(node.page or 0, node.page)}")
        cap = f"<caption>{inline(caption)}</caption>" if caption else ""
        head = f"<thead>{''.join(row_html(r) for r in header)}</thead>" if header else ""
        body = f"<tbody>{''.join(row_html(r) for r in rows[len(header):])}</tbody>"
        # The viewer makes this region keyboard-focusable only while the table overflows.
        return (f'<div class="table-wrap" role="region" {attribute("aria-label", label[:120])}>'
                f'<table {attribute("id", anchor(node))} '
                f'{node_attrs(node)}>{cap}{head}{body}</table></div>')

    def _field_name(self, node: Node) -> str:
        """The accessible name of a control with no tooltip, printed label or cell header: its
        PDF field name (last segment, without index brackets), else a generic one. The review
        queue still flags the field; this only keeps the control from being unnamed."""
        raw = str(node.attrs.get("name") or "")
        segment = next((s for s in reversed(raw.split(".")) if re.sub(r"\[\d+\]$", "", s)), "")
        words = re.sub(r"[_\-\s]+", " ", re.sub(r"\[\d+\]$", "", segment)).strip()
        if words and not _JUNK_FIELD_NAME.match(words):
            return words[:160]
        kind = node.attrs.get("kind")
        what = {"checkbox": "Checkbox", "radio": "Option"}.get(kind, "Form field")
        return f"{what} on page {self.labels.get(node.page or 0, node.page)}"

    def cell_fields(self, fields: list, other=None) -> str:
        """Controls in reading order. Text boxes printed side by side (month / day / year) stay
        on one row; everything else stacks. `other` renders what is not a control."""
        other = other or self.render

        def box(f) -> bool:
            return (isinstance(f, Node) and f.role == Role.FIELD
                    and f.attrs.get("kind") in ("text", "combo") and bool(f.bbox))

        out, run = [], []

        def flush() -> None:
            if len(run) > 1:
                out.append('<div class="field-row">' + "".join(self.render(f) for f in run) + "</div>")
            else:
                out.extend(self.render(f) for f in run)
            run.clear()

        for f in fields:
            if box(f) and run and _same_row_run(run[-1], f):
                run.append(f)
                continue
            flush()
            if box(f):
                run.append(f)
            else:
                out.append(other(f))
        flush()
        return "".join(out)

    def _field(self, node: Node) -> str:
        """A form control and its label. Typing is for reading along only: nothing is submitted
        (there is no <form>), and ids are the page and annotation index, unique per document."""
        a = node.attrs
        kind, fid = a.get("kind"), a.get("fid")
        attrs = [attribute("id", fid), node_attrs(node)]
        # Where the control lives in the PDF: the AcroForm field's fully qualified name, and for
        # a checkbox or radio its export value. The viewer's "Download filled PDF" fills the
        # original with these (radios of one group share the field name).
        if a.get("name"):
            attrs.append(attribute('data-pdf-field', a['name']))
            if kind in ("checkbox", "radio") and a.get("export"):
                attrs.append(attribute('data-pdf-value', a['export']))
        name = a.get("aria") or ("" if node.text else self._field_name(node))
        if node.text and a.get("label") == LabelSource.TOOLTIP and not _has_words(name, node.text):
            # A tooltip that does not hold the printed label's words would override what the
            # reader sees (WCAG 2.5.3, Label in Name): the <label> names the control and the
            # tooltip stays as its title, the description. One that does ("Daytime Telephone
            # Area Code" over "DAYTIME TELEPHONE") names it more exactly, so it stays.
            name = ""
        if name:
            attrs.append(attribute('aria-label', name))
        if a.get("label") == LabelSource.ASSIST:
            attrs.append('data-label-source="assist"')   # an AI answer, not a rule's
        if a.get("detected"):   # not in the PDF: found on a scanned page, drawn on the page, or made from a printed blank
            attrs.append(attribute('data-field-source', field_source(a["detected"])))
        tip = a.get("tooltip")
        if tip and tip != name:
            attrs.append(attribute('title', tip))
        if a.get("required"):
            attrs.append("required")
        if a.get("readonly"):
            attrs.append("readonly" if kind == "text" else "disabled")
        width = a.get("width")
        size = f" {attribute('style', '--w:', f'{width:g}', 'pt')}" if width and kind in ("text", "combo", "list") else ""
        joined = " ".join(attrs)
        if kind in ("combo", "list"):
            chosen = set(a.get("selected") or [])
            options = "".join(
                f"<option {attribute('value', o.strip())}{(' selected' if i in chosen else '')}>{escape(o.strip())}</option>" for i, o in enumerate(a.get("options") or []))
            multiple = " multiple" if kind == "list" else ""
            control = f"<select {joined}{multiple}{size}>{options}</select>"
        elif kind in ("checkbox", "radio"):
            name = a.get("group") or fid
            value = escape(a.get("export") or "on")
            checked = " checked" if a.get("checked") else ""
            control = (f"<input {attribute('type', kind)} {attribute('name', name)} {attribute('value', value)} {joined}{checked}>")
        elif a.get("multiline") and not a.get("password"):
            rows = max(2, round(a.get("height", 0) / 12))
            control = (f"<textarea {joined}{size} {attribute('rows', rows)}>{escape(a.get('value', ''))}</textarea>")
        else:
            maxlen = f" {attribute('maxlength', int(a['maxlen']))}" if a.get("maxlen") else ""
            if a.get("password"):  # the stored value is never published, so no value attribute
                control = f'<input type="password" {joined}{maxlen}{size} autocomplete="off">'
            else:
                # What the box asks for (analyze.input_purpose): type, inputmode and the
                # autofill token for the reader's own data. Anything else stays autocomplete="off".
                purpose = input_purpose(node.text, a.get("tooltip"), a.get("aria"), width=a.get("width"),
                                        context=[text for level in self._context for text in level])
                kind_attrs = [attribute("type", (purpose and purpose.type) or "text")]
                if purpose and purpose.inputmode:
                    kind_attrs.append(attribute("inputmode", purpose.inputmode))
                kind_attrs.append(attribute("autocomplete", (purpose and purpose.autocomplete) or "off"))
                control = (f"<input {kind_attrs[0]} {joined}{maxlen}{size} {' '.join(kind_attrs[1:])} "
                           f"{attribute('value', a.get('value', ''))}>")
        self._context[-1].extend(t for t in (node.text, a.get("aria"), a.get("tooltip")) if t)
        label = (f"<label {attribute('for', fid)}{self._lang_dir(node.text or '', node.page)}>{_cased(node.text, self._display(node.text, LABEL_MAX_WORDS))}</label>" if node.text else "")
        check = kind in ("checkbox", "radio")
        css = "field field-check" if check else "field"
        return f"<div {attribute('class', css)}>{(control + label if check else label + control)}</div>"

    def _group(self, node: Node) -> str:
        """Related controls under a question: a fieldset whose legend is the question (hidden
        from sight when the question is printed elsewhere and only names the group)."""
        name = node.text or node.attrs.get("aria")
        self._context.append([name] if name else [])
        try:
            kids = "".join(self.render(c) for c in node.children)
        finally:
            self._context.pop()
        if not name:
            # A fieldset with an empty legend is announced as an unnamed group: leave the
            # controls loose until something names them (the review queue still flags it).
            return kids
        legend = (f"<legend>{_cased(node.text, self._display(node.text, LABEL_MAX_WORDS))}</legend>" if node.text else
                  f'<legend class="sr-only">{escape(name)}</legend>')
        return f'<fieldset class="fields" {node_attrs(node)}>{legend}{kids}</fieldset>'

    def section_items(self, heading: Node | None, items: list, html_of, owner: Node | None = None) -> str:
        """The items of a section, in HTML, with the section's heading (`owner`'s, for the part
        that continues from an earlier page) known to the fields in it: a field under "Agent"
        is not asked of the reader (render.html NodeRenderer._field)."""
        named = heading if heading is not None else heading_of(owner) if owner is not None else None
        level = self._section_context.setdefault(owner.id, []) if owner is not None else []
        if named is not None and named.text and named.text not in level:
            level.insert(0, named.text)
        self._context.append(level)
        try:
            return self._section_items(heading, items, html_of)
        finally:
            self._context.pop()

    def _section_items(self, heading: Node | None, items: list, html_of) -> str:
        """The items of a section, in HTML. The text boxes that follow a heading, from the first
        to the last before any other control (a group, a table or a lone check box), are one
        fieldset named by that heading, when there are two or more (the legend is hidden from
        sight: the heading is on the page right above). Text between them stays inside it."""
        text = squash(heading.text or "") if heading is not None else ""

        def role(item) -> Role | None:
            return item.role if isinstance(item, Node) else None

        def box(item) -> bool:
            return role(item) == Role.FIELD and item.attrs.get("kind") in ("text", "combo", "list")

        start = next((i for i, item in enumerate(items)
                      if not isinstance(item, Node) or item.role in (Role.FIELD, Role.GROUP, Role.BOX, Role.TABLE)),
                     len(items))
        end, count, i = start, 0, start
        while i < len(items) and (box(items[i]) or (role(items[i]) is not None and role(items[i]) not in
                                                    (Role.FIELD, Role.GROUP, Role.BOX, Role.TABLE, Role.SECTION))):
            if box(items[i]):
                count, end = count + 1, i + 1
            i += 1
        if not text or count < 2 or start >= len(items) or not box(items[start]):
            return html_of(items)
        return (html_of(items[:start])
                + f'<fieldset class="fields field-section"><legend class="sr-only">{escape(text)}</legend>'
                + html_of(items[start:end]) + "</fieldset>" + html_of(items[end:]))

    def _box(self, node: Node) -> str:
        """A ruled box of text (analyze.boxes): its blocks in one framed group, named by its
        first line as printed. A box the page tints rather than rules is a callout: an aside when
        it opens with a note, else a group like the framed one."""
        name = node.attrs.get("name")
        label = f" {attribute('aria-label', name)}" if name else ""
        kids = self.cell_fields(node.children)
        if node.attrs.get("shape") == "fill":
            if node.children[0].role == Role.NOTE:
                return f'<aside class="filled-box"{label} {node_attrs(node)}>{kids}</aside>'
            return (f'<section class="filled-box" role="group"{label} '
                    f'{node_attrs(node)}>{kids}</section>')
        return f'<section class="ruled-box" role="group"{label} {node_attrs(node)}>{kids}</section>'

    def _figure(self, node: Node) -> str:
        """A figure shown as its role in the model says (model.figure_role): a
        picture at the size it is printed, a logo or an icon as a compact chip with its
        description, and decorative art out of sight. Its role is in `data-figure`."""
        caption = next((c for c in node.children if c.role == Role.CAPTION), None)
        resolved = figure_alt(node, self.labels.get(node.page or 0, str(node.page)),
                              self.headings.get(node.id))
        alt, role = resolved.text, resolved.role
        img = ""
        if node.id in self.figures:
            name, w, h = self.figures[node.id]
            by = ' data-alt-by="reviewer"' if resolved.source == "reviewer" else ""
            # The printed width, in points, for a picture shown at its own size.
            size = (f" {attribute('style', '--w:', f'{node.bbox[2] - node.bbox[0]:g}', 'pt')}"
                    if role == "content" and node.bbox and node.bbox[2] > node.bbox[0] else "")
            img = (f'<img {attribute("src", f"{FIGURES_DIR}/", name, self.version_query)} '
                   f'{attribute("alt", alt)} {attribute("width", w)} '
                   f'{attribute("height", h)}{size}{by} loading="lazy" decoding="async">')
            if role in ("logo", "icon") and alt and resolved.source in DESCRIBED and not caption:
                # Its description beside the mark, for sighted readers; a screen reader has
                # it once, from the alt. Not page text, so Find skips it. A captioned mark has
                # its caption instead (its alt is then only the kind, render.alt rule 5).
                img += f'<span class="figure-chip-label" aria-hidden="true" data-generated>{escape(alt)}</span>'
        elif alt:
            # No image file (the crop failed): the alt the <img> would carry, as a labelled
            # placeholder, so the figure is not silent.
            # An alt reflow wrote itself (the fallback, its note, the kind) is not page text.
            own = "" if resolved.source in ("author", "reviewer") else " data-generated"
            # A figure with no description says only that its image is missing: the fallback
            # alt ("Picture on page 4; no description yet") is for a screen reader, not the flow.
            shown = ('<span class="figure-alt-label" data-generated>Image not available</span>'
                     if resolved.source == "fallback" else
                     f'<span class="figure-alt-label" data-generated>Image not available: </span>'
                     f'<span{own}>{escape(alt)}</span>')
            img = f'<p class="figure-alt" role="img" {attribute("aria-label", alt)}>{shown}</p>'
        cap = f"<figcaption>{inline(caption)}</figcaption>" if caption else ""
        unread = ""
        if node.attrs.get("unread"):
            # Writing no text transcribes: reflow's note, which Find skips; never OCR's guess.
            # A model's reading that passed its checks follows the crop instead (analyze.assist).
            unread = f""" class="unread" {attribute('data-kind', node.attrs.get('kind'))}"""
            # A signature reads as "Signature": there is no text it leaves out.
            if node.attrs.get("reading") != "drafted" and node.attrs.get("kind") != "signature":
                img += unread_note(node)
        desc = node.attrs.get("description")
        # A model's description is not page text: Find skips it (data-generated).
        generated = " data-generated" if resolved.source == "ai-draft" else ""
        details = (f"<details{generated}><summary>Image description</summary><p>{escape(desc)}</p></details>"
                   if desc and not node.attrs.get("decorative") else "")
        # A chart's labels and figures, folded into the picture: still text for every reader.
        labels = [c for c in node.children if c.attrs.get("figure_label")]
        if labels:
            items = "".join(f"<li>{inline(c)}</li>" for c in labels)
            details += f"<details><summary>Text in the figure</summary><ul>{items}</ul></details>"
        hidden = " hidden" if out_of_sight(node) else ""
        return (f"<figure {attribute('id', anchor(node))}{unread} {attribute('data-figure', role)}"
                f"{hidden} {node_attrs(node)}>{img}{cap}{details}</figure>")


# Where an alt describes the picture, so a chip may show it: not reflow's fallback or the kind.
DESCRIBED = ("author", "reviewer", "ai-draft")


def out_of_sight(node: Node) -> bool:
    """Whether a figure is left out of the reading flow (`hidden`): decorative art with no words
    of the page in it, no caption and no labels. It keeps its node, so it still traces to its box
    and the inspector lists it. Decorative art with a caption shows, for the caption's sake."""
    return (node.role == Role.FIGURE and figure_role(node) == "decorative"
            and not node.attrs.get("unread")
            and not any(c.role == Role.CAPTION or c.attrs.get("figure_label") for c in node.children))


# Why an unread figure stays not transcribed, by what came of a model's reading (its `reading`).
UNREAD_WHY = {None: "no model was asked to read it",
              "rejected": "no model reading passed the checks",
              "unavailable": "the model was unavailable"}


def unread_note(node: Node) -> str:
    why = UNREAD_WHY.get(node.attrs.get("reading"), UNREAD_WHY[None])
    return (f'<p class="unread-note" data-generated>Not transcribed: {why}. '
            'Check the original page.</p>')


def node_attrs(node: Node) -> str:
    """The attributes that trace a block to its node: `data-node`, and `data-source="ocr"` when
    OCR read it, which the React viewer keeps (it reads the page's inner HTML)."""
    source = ' data-source="ocr"' if is_ocr(node) else ""
    return f"{attribute('data-node', node.id)}{source}"
