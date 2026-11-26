"""Assertion checks for page checks.

A page check is a converted page of HTML (the `<div class="pg" data-page=N>` element
written by reflow) plus the page's text layer from the PDF. Checks are deliberately
about structure and words, never about exact markup, so a legitimate improvement only
breaks the page checks that describe what was wrong before.

The HTML is parsed with html.parser into a small tree of Node objects. The module imports only the
standard library (scripts/affected imports assertion_kind); one doc check imports reflow's fact
codes when it runs, to tell which facts are review items.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Iterator

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}
# Inline elements do not separate words; every other element does.
INLINE = {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn", "em", "i",
          "ins", "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup",
          "time", "u", "var"}
# Elements that count as one "block" for same_block: the words of one cell, line or
# paragraph. Rows (tr) are checked separately so a table row is one block too.
BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "caption", "figcaption",
         "dt", "dd"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    def walk(self) -> Iterator["Node"]:
        """Every element below this one, in document order."""
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.walk()

    def find_all(self, *tags: str) -> list["Node"]:
        return [n for n in self.walk() if n.tag in tags]

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # An end tag with no open element is ignored, as browsers do.

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def norm(s: str) -> str:
    """Whitespace-collapsed, quote-folded form used for every text comparison."""
    s = unicodedata.normalize("NFC", s).translate(_QUOTES)
    return re.sub(r"\s+", " ", s).strip()


def text_of(node: Node, skip: Callable[[Node], bool] | None = None) -> str:
    """Words of a node. Block elements are separated by spaces, inline ones are not."""
    pieces: list[str] = []

    def walk(n: Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                pieces.append(c)
            elif skip is None or not skip(c):
                if c.tag in INLINE:
                    walk(c)
                else:
                    pieces.append(" ")
                    walk(c)
                    pieces.append(" ")

    walk(node)
    return norm("".join(pieces))


# Spans of words a cell copies from elsewhere on the page (reflow.render.annotations).
COPIED = ("day-status", "day-note")


def own_text(cell: Node) -> str:
    """A cell's own words: its text without the spans of copied words, and without the " — "
    separators before them."""
    text = text_of(cell, skip=lambda n: any(n.has_class(c) for c in COPIED))
    return norm(re.sub(r"(\s*—)+$", "", text))


def _is_label(n: Node) -> bool:
    return n.has_class("pg-label")


def page_text(pg: Node) -> str:
    return text_of(pg, skip=_is_label)


def page_tokens(text: str) -> list[str]:
    """Word tokens, as used for word recall: NFKC, soft hyphens and line-end hyphens removed."""
    s = unicodedata.normalize("NFKC", text).replace("­", "")
    # A word hyphenated at a line end is one word; pdfium writes CRLF line ends.
    s = re.sub(r"-\r?\n", "", s).replace("\r", "\n")
    return re.findall(r"[^\W_]+", s.lower())


def recall(pdf_text: str, html_text: str) -> float:
    """Share of the PDF's words that the page transcript contains (multiset overlap)."""
    from collections import Counter

    pt = page_tokens(pdf_text)
    if not pt:
        raise CheckFailed("no source text on this page")
    hc = Counter(page_tokens(html_text))
    pc = Counter(pt)
    return round(sum((pc & hc).values()) / len(pt) * 100, 2)


def _block_contains(block: Node, *needles: str) -> bool:
    t = text_of(block, skip=_is_label)
    return all(n in t for n in needles)


@dataclass
class Assertion:
    """One check from a spec file. `xfail` holds the reason when the check is a known open
    defect; the runner then expects it to fail (strict xfail), so a fix shows as XPASS."""

    check: str
    value: object
    xfail: str | None = None
    why: str | None = None
    index: int = 0
    spec_id: str = ""


# The keys of an assertion entry in a spec file that are not its check: {"links": [...], "xfail": "why"}.
ASSERTION_NOTES = ("xfail", "xfail_without_ocr", "why")


def assertion_kind(entry: dict) -> str:
    """The check an assertion entry of a spec file names."""
    return next(k for k in entry if k not in ASSERTION_NOTES)


class CheckFailed(AssertionError):
    pass


def _fail(msg: str) -> None:
    raise CheckFailed(msg)


def run_check(a: Assertion, pg: Node, pdf_text: Callable[[], str], doc=None,
              page: int | None = None) -> None:
    """Raise CheckFailed (an AssertionError) unless the assertion holds on the page, on the
    document's ledger (document.json) for a doc check, or on the document shell (index.html, which
    holds the title header) for a shell check. `doc` is the DocOutput and `page` the source page."""
    if a.check in DOC_CHECKS or a.check in SHELL_CHECKS:
        if doc is None or page is None:
            raise ValueError(f"{a.check} needs the converted document and the page")
        if a.check in DOC_CHECKS:
            DOC_CHECKS[a.check](a.value, doc, page)
        else:
            SHELL_CHECKS[a.check](a.value, doc.shell())
        return
    fn = CHECKS.get(a.check)
    if fn is None:
        raise ValueError(f"unknown check {a.check!r}")
    fn(a.value, pg, pdf_text)


def _min_recall(value, pg, pdf_text):
    floor = value["floor"] if isinstance(value, dict) else value
    got = recall(pdf_text(), page_text(pg))
    if got < float(floor):
        _fail(f"word recall {got} < floor {floor}")


def _contains(values, pg, pdf_text):
    t = page_text(pg)
    for s in values:
        if norm(s) not in t:
            _fail(f"text not on page: {s!r}")


def _contains_in_order(values, pg, pdf_text):
    t = page_text(pg)
    pos = 0
    prev = None
    for s in values:
        i = t.find(norm(s), pos)
        if i < 0:
            if t.find(norm(s)) >= 0:
                _fail(f"out of order: {s!r} is on the page only before {prev!r}")
            _fail(f"text not on page: {s!r}" + (f" (after {prev!r})" if prev else ""))
        pos = i + len(norm(s))
        prev = s


def _no_text(values, pg, pdf_text):
    t = page_text(pg)
    for s in values:
        if norm(s) in t:
            i = t.find(norm(s))
            _fail(f"forbidden text {s!r} at: ...{t[max(0, i - 40):i + len(s) + 40]}...")


def _no_blank_runs(value, pg, pdf_text):
    """No run of three or more underscores is left on the page: each printed blank is a field
    (or a control's own box), not text. `value` is true."""
    runs = re.findall(r"_{3,}", page_text(pg))
    if value and runs:
        _fail(f"{len(runs)} blank lines left as underscores, e.g. {runs[0][:12]!r}")


def _same_block(pairs, pg, pdf_text):
    blocks = [n for n in pg.walk() if n.tag in BLOCK or n.tag == "tr"]
    for a, b in pairs:
        a, b = norm(a), norm(b)
        if not any(_block_contains(n, a, b) for n in blocks):
            where = [n for n in blocks if _block_contains(n, a)]
            hint = (f"{a!r} is in {len(where)} block(s) but none holds {b!r} too"
                    if where else f"{a!r} is not in any block")
            _fail(f"not in one block/row: {a!r} / {b!r} ({hint})")


def _same_item(pairs, pg, pdf_text):
    """One list item holds both texts in its own words, not in a list nested inside it."""
    items = [text_of(n, skip=lambda c: _is_label(c) or c.tag in ("ul", "ol"))
             for n in pg.find_all("li")]
    for a, b in pairs:
        a, b = norm(a), norm(b)
        if not any(a in t and b in t for t in map(norm, items)):
            where = sum(a in t for t in map(norm, items))
            _fail(f"not in one list item: {a!r} / {b!r} ({a!r} is in {where} item(s))")


def _same_entry(pairs, pg, pdf_text):
    """A heading that holds `name` is followed, before the next heading, by `text`. This is
    the "same entry" test for a directory listing: the provider's name (a heading) and its
    address (the paragraphs under it) belong together, even though they are separate blocks."""
    flow = [n for n in pg.walk() if n.tag in BLOCK or n.tag == "tr"]
    sections: list[tuple[str, list[str]]] = []
    for n in flow:
        if n.tag in HEADINGS:
            sections.append((norm(text_of(n, _is_label)), []))
        elif sections:
            sections[-1][1].append(text_of(n, _is_label))
    for name, text in pairs:
        name, text = norm(name), norm(text)
        heads = [s for s in sections if name in s[0]]
        if not heads:
            _fail(f"no heading holds {name!r}")
        if not any(text in " ".join(body) for _, body in heads):
            _fail(f"{text!r} does not follow the heading {name!r} before the next heading")


def _separate_blocks(pairs, pg, pdf_text):
    blocks = [n for n in pg.walk() if n.tag in BLOCK or n.tag == "tr"]
    for a, b in pairs:
        a, b = norm(a), norm(b)
        if any(_block_contains(n, a, b) for n in blocks):
            _fail(f"{a!r} and {b!r} share one block, but they are separate entries")


def _heading(spec, pg, pdf_text):
    """A heading whose text equals `text` (or contains it, with "contains": true), at a level
    in `levels`."""
    text = norm(spec["text"])
    lo, hi = spec.get("levels", [1, 6])
    if spec.get("contains"):
        hits = [n for n in pg.find_all(*HEADINGS) if text in norm(text_of(n, _is_label))]
    else:
        hits = [n for n in pg.find_all(*HEADINGS) if norm(text_of(n, _is_label)) == text]
    if not hits:
        _fail(f"no heading with text {text!r}")
    levels = [int(n.tag[1]) for n in hits]
    if not any(lo <= lv <= hi for lv in levels):
        _fail(f"heading {text!r} is at level {levels}, expected {lo}-{hi}")


def _not_heading(values, pg, pdf_text):
    heads = {norm(text_of(n, _is_label)) for n in pg.find_all(*HEADINGS)}
    for s in values:
        if norm(s) in heads:
            _fail(f"{norm(s)!r} is a heading but must be body text")


def _body_blocks(pg: Node) -> list[Node]:
    """The block elements of the page outside every figure (a figure's labels are its own)."""
    out: list[Node] = []

    def walk(n: Node) -> None:
        for c in n.children:
            if isinstance(c, Node) and c.tag != "figure":
                if c.tag in BLOCK:
                    out.append(c)
                walk(c)

    walk(pg)
    return out


def _no_block(values, pg, pdf_text):
    """No body block is just one of these texts: a chart's labels set as running text."""
    blocks = {norm(text_of(b, _is_label)) for b in _body_blocks(pg)}
    for s in values:
        if norm(s) in blocks:
            _fail(f"{norm(s)!r} is a body block, expected it inside its figure")


def _in_figure(values, pg, pdf_text):
    """Each text is a whole run of words inside some figure (its labels or its caption)."""
    text = " ".join(text_of(f, _is_label) for f in pg.find_all("figure"))
    for s in values:
        if not re.search(r"(?<!\S)" + re.escape(norm(s)) + r"(?!\S)", text):
            _fail(f"{norm(s)!r} is not in any figure on the page")


def _max_headings(value, pg, pdf_text):
    n = len(pg.find_all(*HEADINGS))
    if n > int(value):
        _fail(f"{n} headings, at most {value} expected")


def _max_blocks(value, pg, pdf_text):
    """At most `value` text blocks (paragraphs, list items, headings, cells) on the page."""
    n = sum(1 for b in pg.walk() if b.tag in BLOCK and not _is_label(b))
    if n > int(value):
        _fail(f"{n} text blocks, at most {value} expected")


def _count(tag: str, label: str):
    def check(value, pg, pdf_text):
        n = len(pg.find_all(tag))
        if n != int(value):
            _fail(f"{n} {label} on the page, expected {value}")
    return check


def _ins_texts(spec, pg, pdf_text):
    """The text of each <ins> on the page, in order: `{"exactly": [...]}` is all of them,
    `{"includes": [...]}` some of them."""
    have = [norm(text_of(n, _is_label)) for n in pg.find_all("ins")]
    if "exactly" in spec:
        if have != [norm(t) for t in spec["exactly"]]:
            _fail(f"the insertions are {have}, expected {spec['exactly']}")
        return
    missing = [t for t in spec["includes"] if norm(t) not in have]
    if missing:
        _fail(f"no insertion {missing}; the page has {have}")


def _unmarked(values, pg, pdf_text):
    """Each phrase is on the page outside every <ins> and <del>: the text around a tracked
    change is not marked with it."""
    pieces: list[str] = []

    def walk(n: Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                pieces.append(c)
            elif c.tag in ("ins", "del"):
                pieces.append("\x00")
            elif not _is_label(c):
                spaced = c.tag not in INLINE
                pieces.append(" " if spaced else "")
                walk(c)
                pieces.append(" " if spaced else "")

    walk(pg)
    outside = [norm(p) for p in "".join(pieces).split("\x00")]
    for phrase in values:
        if not any(norm(phrase) in part for part in outside):
            _fail(f"{phrase!r} is not on the page outside the insertions and deletions")


def _cells_of(tr: Node) -> list[Node]:
    return [c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]


def _span(cell: Node, attr: str) -> int:
    try:
        return max(1, int(cell.attrs.get(attr) or 1))
    except ValueError:
        return 1


def _logical_rows(trs: list[Node]) -> list[dict[int, Node]]:
    """Cells indexed by logical column, including colspan and rowspan coverage."""
    taken: dict[tuple[int, int], Node] = {}
    rows = []
    for r, tr in enumerate(trs):
        c = 0
        for cell in _cells_of(tr):
            while (r, c) in taken:
                c += 1
            for dr in range(_span(cell, "rowspan")):
                for dc in range(_span(cell, "colspan")):
                    taken[r + dr, c + dc] = cell
            c += _span(cell, "colspan")
        rows.append({cc: cell for (rr, cc), cell in taken.items() if rr == r})
    return rows


def _is_col_header(tr: Node, text: str) -> bool:
    """Whether the row has a cell with this text that is a th[scope=col]."""
    return any(c.tag == "th" and c.attrs.get("scope") == "col" and norm(text_of(c)) == norm(text)
               for c in _cells_of(tr))


def _table(spec, pg, pdf_text):
    """`rows` and `cols` are minimums: some table has at least that many rows and cols. With
    "exact": true they are equalities on the logical grid: the table has exactly `rows` rows and
    every row covers exactly `cols` columns, counting colspan and rowspan. `row_headers` requires
    every tbody row to start with a th[scope=row]. `col_headers` lists cell texts that head the
    table's columns: each a th[scope=col] in the table's first row, which is in a thead."""
    rows_min, cols_min = spec.get("rows", 1), spec.get("cols", 1)
    exact = bool(spec.get("exact", False))
    cells = [norm(c) for c in spec.get("cells", [])]
    tables = pg.find_all("table")
    if not tables:
        _fail("no table on the page")
    best = None
    for t in tables:
        trs = t.find_all("tr")
        logical = _logical_rows(trs)
        widths = [max(row, default=-1) + 1 for row in logical] if exact else None
        if exact:
            cols = max(widths, default=0)
            ok_shape = (len(trs) == rows_min and bool(widths)
                        and all(w == cols_min for w in widths))
        else:
            cols = max((len(_cells_of(tr)) for tr in trs), default=0)
            ok_shape = len(trs) >= rows_min and cols >= cols_min
        texts = {norm(text_of(c)) for c in t.find_all("td", "th")}
        missing = [c for c in cells if c not in texts]
        for check in spec.get("cell_contains", []):
            anchor = check["row"]
            if not any(anchor["col"] in row
                       and norm(text_of(row[anchor["col"]])) == norm(anchor["text"])
                       and any(col in row and norm(check["text"]) in norm(text_of(row[col]))
                               for col in check["cols"]) for row in logical):
                missing.append(f"cell_contains {check!r}")
        body_rows = [tr for body in t.find_all("tbody") for tr in body.find_all("tr")]
        row_headers = not spec.get("row_headers") or (bool(body_rows) and all(
            (cs := _cells_of(tr)) and cs[0].tag == "th" and cs[0].attrs.get("scope") == "row"
            for tr in body_rows))
        head_trs = [tr for head in t.find_all("thead") for tr in head.find_all("tr")]
        col_headers = not spec.get("col_headers") or (
            bool(trs) and any(trs[0] is tr for tr in head_trs)
            and all(_is_col_header(trs[0], name) for name in spec["col_headers"]))
        if ok_shape and not missing and row_headers and col_headers:
            return
        if best is None or len(missing) < len(best[2]):
            best = (len(trs), cols, missing, widths)
    rows, cols, missing, widths = best
    shape = f"{rows_min}x{cols_min}" + (" exactly" if exact else "")
    msg = (f"no table with {shape} holding all cells; closest: {rows} rows, "
           f"{cols} cols, missing cells {missing}")
    if widths is not None:
        msg += f", row widths {widths}"
    if spec.get("row_headers"):
        msg += "; requires tbody rows whose first cell is th[scope=row]"
    if spec.get("col_headers"):
        msg += (f"; requires {spec['col_headers']} as th[scope=col] cells of the first row, "
                "which is in a thead")
    _fail(msg)


def _max_table_cols(value, pg, pdf_text):
    """No table is wider than `value` logical columns (colspan and rowspan counted). A printed
    form that is ruled into boxes can come out as one table of its whole page, thirty columns
    across, wider than the page and clipped on the right: its boxes are not columns, and no real
    table of a form's page (a record of services, ten columns) comes near that."""
    for i, t in enumerate(pg.find_all("table")):
        widest = max((max(row, default=-1) + 1 for row in _logical_rows(t.find_all("tr"))), default=0)
        if widest > int(value):
            _fail(f"table {i + 1} is {widest} columns wide, at most {value} expected")


def _list_items(spec, pg, pdf_text):
    items = [norm(s) for s in spec["items"]]
    want = spec.get("ordered")
    for lst in pg.find_all("ul", "ol"):
        if want is not None and (lst.tag == "ol") != bool(want):
            continue
        lis = [norm(text_of(li)) for li in lst.children if isinstance(li, Node) and li.tag == "li"]
        if _is_subsequence(items, lis):
            return
    _fail(f"no list with items {items} in order")


def _nested_list(spec, pg, pdf_text):
    parent, items = norm(spec["parent"]), [norm(s) for s in spec["items"]]
    for li in pg.find_all("li"):
        own = text_of(li, skip=lambda c: c.tag in ("ul", "ol"))
        if not own.startswith(parent):
            continue
        for sub in li.children:
            if isinstance(sub, Node) and sub.tag in ("ul", "ol"):
                lis = [norm(text_of(x)) for x in sub.children
                       if isinstance(x, Node) and x.tag == "li"]
                if _is_subsequence(items, lis):
                    return
    _fail(f"no list item starting {parent!r} with nested items {items}")


def _is_subsequence(want: list[str], have: list[str]) -> bool:
    it = iter(have)
    return all(any(h == w for h in it) for w in want)


def _figures(value, pg, pdf_text):
    _count("figure", "figures")(value, pg, pdf_text)


def _unread_figures(value, pg, pdf_text):
    """The page has this many unread figures (figure.unread): a count, or {"min", "max"}."""
    n = sum(f.has_class("unread") for f in pg.find_all("figure"))
    low, high = ((value.get("min", 0), value.get("max", n)) if isinstance(value, dict)
                 else (value, value))
    if not low <= n <= high:
        _fail(f"{n} unread figures on the page, expected {value}")


def _fragment(href: str) -> str:
    return href.partition("#")[2]


def _footnotes(value, pg, pdf_text):
    n = len([a for a in pg.find_all("aside") if a.attrs.get("role") == "doc-footnote"])
    if n != int(value):
        _fail(f"{n} footnotes (role=doc-footnote) on the page, expected {value}")


def _noterefs(value, pg, pdf_text):
    """`value` references (role=doc-noteref), each pointing at a footnote on the page whose
    backlink points back at it."""
    refs = [a for a in pg.find_all("a") if a.attrs.get("role") == "doc-noteref"]
    if len(refs) != int(value):
        _fail(f"{len(refs)} note references (role=doc-noteref) on the page, expected {value}")
    ids = [n.attrs["id"] for n in pg.walk() if n.attrs.get("id")]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        _fail(f"duplicate ids on the page: {dupes}")
    notes = {a.attrs.get("id"): a for a in pg.find_all("aside")
             if a.attrs.get("role") == "doc-footnote"}
    for ref in refs:
        note = notes.get(_fragment(ref.attrs.get("href", "")))
        if note is None:
            _fail(f"note reference {text_of(ref)!r} points at no footnote on the page")
        back = [a for a in note.find_all("a") if a.attrs.get("role") == "doc-backlink"]
        if not any(_fragment(a.attrs.get("href", "")) == ref.attrs.get("id") for a in back):
            _fail(f"footnote {text_of(note)[:40]!r} has no backlink to its reference")


def _links(values, pg, pdf_text):
    """Each entry names a link on the page: an <a> whose text is `text` (after normalising)
    and whose href is `href` exactly, points at page `page` (a chunk file's "#pN"), or, with
    `"internal": true`, points at some page of the document. Use `internal` where the page
    number depends on the conversion: a --quick excerpt renumbers the pages it keeps."""
    anchors = [(text_of(a), a.attrs.get("href", "")) for a in pg.find_all("a")]
    for want in values:
        text = norm(want["text"])
        if want.get("internal"):
            hits = [h for t, h in anchors if t == text and re.search(r"#p\d+$", h)]
            target = "a page of the document"
        elif "page" in want:
            target = f"#p{want['page']}"
            hits = [h for t, h in anchors if t == text and h.endswith(target)]
            target = f"page {want['page']}"
        else:
            target = want["href"]
            hits = [h for t, h in anchors if t == text and h == target]
        if not hits:
            seen = [h for t, h in anchors if t == text]
            _fail(f"no link {text!r} to {target}" + (f" (it goes to {seen})" if seen else ""))


def _emphasis(values, pg, pdf_text):
    """Each entry `{"text": ..., "kind": "strong" | "em" | "red" | "green"}` names
    emphasis the page prints: some <strong> (or <em>) element, or a span of the named accent
    colour (`ink-red`), whose text, normalised, is exactly `text`. One entry may be given
    without the list."""
    for want in [values] if isinstance(values, dict) else values:
        kind = want["kind"]
        accent = kind in ("red", "green")
        if kind not in ("strong", "em") and not accent:
            raise ValueError(f"emphasis kind must be strong, em, red or green: {kind!r}")
        text = norm(want["text"])
        found = ([n for n in pg.find_all("span") if n.has_class(f"ink-{kind}")] if accent
                 else pg.find_all(kind))
        seen = [norm(text_of(n, _is_label)) for n in found]
        if text not in seen:
            _fail(f"no {'<' + kind + '>' if not accent else kind + ' span'} with the text {text!r}"
                  + (f" (the page has {seen[:5]})" if seen else ""))


def _raised(n: Node, pieces: list[str]) -> None:
    """The words of `n` with each <sup>'s text between "^(" and ")"."""
    for c in n.children:
        if isinstance(c, str):
            pieces.append(c)
            continue
        if c.tag == "sup":
            pieces.append("^(")
        elif c.tag not in INLINE:
            pieces.append(" ")
        _raised(c, pieces)
        pieces.append(")" if c.tag == "sup" else "" if c.tag in INLINE else " ")


def _superscripts(values, pg, pdf_text):
    """Each entry `{"text": "1", "after": "each year."}` names a superscript the page prints: a
    <sup> whose text is `text`, right after the words `after` in one block (so "each year.1"
    with the 1 on the line fails). A <sup> around a note reference counts."""
    blocks = []
    for n in pg.walk():
        if n.tag in BLOCK:
            pieces: list[str] = []
            _raised(n, pieces)
            blocks.append(norm("".join(pieces)))
    for want in [values] if isinstance(values, dict) else values:
        needle = f"{norm(want['after'])}^({norm(want['text'])})"
        if not any(needle in b for b in blocks):
            near = [b for b in blocks if norm(want["after"]) in b]
            _fail(f"no superscript {want['text']!r} after {want['after']!r}"
                  + (f" (the block reads {near[0][:120]!r})" if near else ""))


def _controls(pg: Node) -> dict[str, int]:
    """Form controls on the page by kind: text (inputs and textareas), select, checkbox, radio."""
    counts = {"text": 0, "select": 0, "checkbox": 0, "radio": 0}
    for n in pg.walk():
        if n.tag == "input":
            kind = n.attrs.get("type", "text")
            if kind in counts:
                counts[kind] += 1
            elif kind in ("email", "tel"):   # typed text boxes
                counts["text"] += 1
        elif n.tag == "textarea":
            counts["text"] += 1
        elif n.tag == "select":
            counts["select"] += 1
    return counts


def _fields(spec, pg, pdf_text):
    """The page has exactly these controls: {"text": n, "select": n, "checkbox": n, "radio": n}
    (kinds left out must be absent)."""
    got = _controls(pg)
    want = {k: int(spec.get(k, 0)) for k in got}
    if got != want:
        _fail(f"controls {got}, expected {want}")


def _field_group(spec, pg, pdf_text):
    """A fieldset whose legend is `legend` holds checkboxes/radios labelled `options`, in order,
    each label bound with for= to a control inside the fieldset. With `type` ("radio",
    "checkbox", "text"), every control in it is of that type; radios must also share one name
    (one group to a keyboard and a screen reader) and each name a PDF field to fill."""
    for fs in pg.find_all("fieldset"):
        legend = next((n for n in fs.find_all("legend")), None)
        if legend is None or text_of(legend) != norm(spec["legend"]):
            continue
        ids = {n.attrs.get("id") for n in fs.find_all("input")}
        labels = fs.find_all("label")
        got = [text_of(l) for l in labels]
        if got != [norm(o) for o in spec["options"]]:
            _fail(f"group {spec['legend']!r} has options {got}")
        if any(l.attrs.get("for") not in ids for l in labels):
            _fail(f"group {spec['legend']!r}: a label is not bound to a control inside it")
        if "type" in spec:
            inputs = fs.find_all("input")
            # An email or telephone box is a text box with a typed keyboard.
            types = sorted({"text" if n.attrs.get("type", "text") in ("email", "tel") else n.attrs.get("type", "text")
                            for n in inputs})
            if types != [spec["type"]]:
                _fail(f"group {spec['legend']!r} holds controls of type {types}, expected {spec['type']!r}")
            if spec["type"] == "radio":
                names = {n.attrs.get("name") for n in inputs}
                if len(names) != 1 or None in names:
                    _fail(f"group {spec['legend']!r}: its radios have the names {sorted(map(str, names))}, expected one")
                # A radio drawn on a page with no form (data-field-source="drawn") has no PDF field.
                if any(not n.attrs.get("data-pdf-field") and n.attrs.get("data-field-source") != "drawn"
                       for n in inputs):
                    _fail(f"group {spec['legend']!r}: a radio does not name its PDF field")
        return
    _fail(f"no fieldset with legend {spec['legend']!r}")


def _fieldsets_named(value, pg, pdf_text):
    """Every fieldset has a legend with words in it: an empty legend is announced as an unnamed
    group. A legend hidden from sight (sr-only) names a group that the page names in view as
    well, so its words must be printed again outside it (the heading right above the group)."""
    text = page_text(pg)
    for fs in pg.find_all("fieldset"):
        legend = next((n for n in fs.find_all("legend")), None)
        name = text_of(legend) if legend is not None else ""
        if not name:
            _fail("a fieldset has no legend words")
        if legend.has_class("sr-only") and text.count(name) < 2:
            _fail(f"the hidden legend {name!r} is not printed anywhere else on the page")


def _ruled_box(spec, pg, pdf_text):
    """A framed group (section.ruled-box, role group) named `name` (aria-label; null for none)
    holds the `texts`, in order, and nothing before the first of them. With `"filled": true`
    the box is a tinted callout (.filled-box) instead: an <aside> when `"tag": "aside"`, else a
    group."""
    name = spec.get("name")
    filled = spec.get("filled", False)
    tag = spec.get("tag", "section")
    for box in pg.find_all(tag):
        if not box.has_class("filled-box" if filled else "ruled-box"):
            continue
        label = box.attrs.get("aria-label")
        if (norm(label) if label is not None else None) != (norm(name) if name else None):
            continue
        if tag == "section" and box.attrs.get("role") != "group":
            _fail(f"box {name!r} is not a group")
        text, pos = text_of(box), 0
        for s in spec["texts"]:
            i = text.find(norm(s), pos)
            if i < 0 or (pos == 0 and i != 0):
                _fail(f"box {name!r} holds {text!r}, not {s!r} in its place")
            pos = i + len(norm(s))
        return
    _fail(f"no {'filled' if filled else 'ruled'} {tag} box named {name!r}")


def _labelled_once(values, pg, pdf_text):
    """Each text is the label of exactly one control (bound with for= to an existing id) and
    is on the page exactly once: the printed label became the label, not a second copy."""
    ids = {n.attrs.get("id") for n in pg.walk() if n.tag in ("input", "select", "textarea")}
    t = page_text(pg)
    for s in values:
        want = norm(s)
        labels = [l for l in pg.find_all("label") if text_of(l) == want]
        if len(labels) != 1 or labels[0].attrs.get("for") not in ids:
            _fail(f"{s!r}: {len(labels)} labels bound to a control, expected 1")
        if t.count(want) != 1:
            _fail(f"{s!r} is on the page {t.count(want)} times, expected 1")


def _field_labels(spec, pg, pdf_text):
    """Each key is the text of exactly `n` labels (the value), each bound with for= to a control
    on the page: neighbouring captions are separate labels, one per control."""
    ids = {n.attrs.get("id") for n in pg.walk() if n.tag in ("input", "select", "textarea")}
    for text, n in spec.items():
        want = norm(text)
        got = [l for l in pg.find_all("label") if text_of(l) == want and l.attrs.get("for") in ids]
        if len(got) != int(n):
            _fail(f"{len(got)} labels {text!r} bound to a control, expected {n}")


def _input_attrs(entries, pg, pdf_text):
    """Each entry names a text box by its printed label (`label`, and `nth` when several carry
    it, counting from 0 in reading order) and gives the attributes it must have: `type`,
    `inputmode`, `autocomplete`. A value of null means the attribute is absent. `type` is
    "text" when it is not written. The attributes are the box's own, so a box that fills a PDF
    field with what the reader typed keeps no `type="date"`."""
    boxes = {n.attrs.get("id"): n for n in pg.walk() if n.tag == "input"}
    for entry in entries:
        want = norm(entry["label"])
        labels = [l for l in pg.find_all("label") if text_of(l) == want and l.attrs.get("for") in boxes]
        nth = int(entry.get("nth", 0))
        if nth >= len(labels):
            _fail(f"{entry['label']!r}: {len(labels)} labels bound to an input, wanted number {nth}")
        box = boxes[labels[nth].attrs["for"]]
        for name in ("type", "inputmode", "autocomplete"):
            if name not in entry:
                continue
            got = box.attrs.get(name, "text" if name == "type" else None)
            if got != entry[name]:
                _fail(f"{entry['label']!r} #{nth}: {name} is {got!r}, expected {entry[name]!r}")


def _control_name(n: Node, pg: Node) -> str:
    """A control's accessible name: its aria-label, else the text of the label bound to it."""
    if n.attrs.get("aria-label"):
        return norm(n.attrs["aria-label"])
    return next((text_of(l) for l in pg.find_all("label") if l.attrs.get("for") == n.attrs.get("id")), "")


def _control_names(values, pg, pdf_text):
    """Each text is the accessible name of at least one control (aria-label, else its bound label)."""
    names = {_control_name(n, pg) for n in pg.walk() if n.tag in ("input", "select", "textarea")}
    for s in values:
        if norm(s) not in names:
            _fail(f"no control is named {s!r}")


def _control_order(values, pg, pdf_text):
    """These texts are the accessible names of controls in this order (other controls may come
    between them). A name is looked up in document order, so a control that took its neighbour's
    label, or a row of boxes that share one name, fails."""
    names = [_control_name(n, pg) for n in pg.walk() if n.tag in ("input", "select", "textarea")]
    at = 0
    for s in values:
        want = norm(s)
        try:
            at = names.index(want, at) + 1
        except ValueError:
            _fail(f"no control named {s!r} after the one before it (names in order: {names[:60]})")


def _labels_in_names(value, pg, pdf_text):
    """A control's printed label is in its accessible name (WCAG 2.5.3): where a control has both a
    bound label and an aria-label, the label's words are the name's, or the name's the label's.
    A label printed for the box above or below it fails."""
    if not value:
        return
    for n in pg.walk():
        if n.tag not in ("input", "select", "textarea") or not n.attrs.get("aria-label"):
            continue
        label = next((text_of(l) for l in pg.find_all("label") if l.attrs.get("for") == n.attrs.get("id")), "")
        # A two-line box's "[1]", "[2]" and a trailing colon or full stop are not words.
        def words(t: str) -> str:
            return re.sub(r"\s*\[\d+\]$", "", norm(t).lower()).rstrip(":. ")
        name = words(n.attrs["aria-label"])
        if label and words(label) not in name and name not in words(label):
            _fail(f"the control named {name!r} is labelled {label!r}")


def _no_control_names(values, pg, pdf_text):
    """No control is named any of these texts (an English tooltip or field name, say)."""
    names = {_control_name(n, pg) for n in pg.walk() if n.tag in ("input", "select", "textarea")}
    for s in values:
        if norm(s) in names:
            _fail(f"a control is named {s!r}")


def _field_rows(values, pg, pdf_text):
    """Each entry lists the names of controls that share one row (a div.field-row), in order."""
    rows = [[_control_name(n, pg) for n in row.walk() if n.tag == "input"]
            for row in pg.walk() if row.tag == "div" and row.has_class("field-row")]
    for want in values:
        if [norm(w) for w in want] not in rows:
            _fail(f"no row of controls named {want}; rows are {rows}")


def _no_empty_field_cells(value, pg, pdf_text):
    """No table row that holds a form control has an empty cell: a row label beside the rows of
    its fields spans them (rowspan), so a field's row has no blank where that label stands."""
    controls = ("input", "select", "textarea")
    for tr in pg.find_all("tr"):
        cells = _cells_of(tr)
        if not any(n.tag in controls for c in cells for n in c.walk()):
            continue
        for k, c in enumerate(cells):
            if not text_of(c) and not any(n.tag in controls for n in c.walk()):
                _fail(f"an empty cell (cell {k + 1}) in a row of fields: {text_of(tr)[:80]!r}")


def _shell_contains(values, shell: Node) -> None:
    """Each text is in the document shell: the header and the first chunk's pages, not one page.
    The renderer prints the title once in the shell header, so a page-level check cannot see it."""
    t = text_of(shell, skip=_is_label)
    for s in values:
        if norm(s) not in t:
            _fail(f"text not in the document shell: {s!r}")


def _doc_lang(value, shell: Node) -> None:
    """The document's `<html lang>` is this tag, exactly."""
    html = next((n for n in [shell, *shell.walk()] if n.tag == "html"), None)
    got = html.attrs.get("lang") if html else None
    if got != value:
        _fail(f"<html lang> is {got!r}, expected {value!r}")


def _doc_title(value, shell: Node) -> None:
    """The shell's `<title>` text is this title, exactly (the published title, not a file name)."""
    node = next((n for n in [shell, *shell.walk()] if n.tag == "title"), None)
    got = text_of(node).strip() if node else None
    if got != value:
        _fail(f"<title> is {got!r}, expected {value!r}")


def _block_lang(values, pg, pdf_text):
    """Each snippet is read in the requested language, respecting nested overrides."""
    runs: list[tuple[str, str | None]] = []

    def visit(node: Node, inherited: str | None):
        lang = node.attrs.get("lang", inherited)
        for c in node.children:
            if isinstance(c, str):
                if runs and runs[-1][1] == lang:
                    runs[-1] = (runs[-1][0] + c, lang)
                else:
                    runs.append((c, lang))
            else:
                if c.tag not in INLINE:
                    runs.append((" ", lang))
                visit(c, lang)
                if c.tag not in INLINE:
                    runs.append((" ", lang))

    visit(pg, None)
    found: list[tuple[str, str | None]] = []
    for text, lang in runs:
        if found and found[-1][1] == lang:
            found[-1] = (found[-1][0] + text, lang)
        else:
            found.append((text, lang))
    found = [(norm(text), lang) for text, lang in found]
    for want in values:
        text = norm(want["text"])
        langs = {lang for t, lang in found if text in t}
        if want["lang"] not in langs:
            _fail(f"no element holding {want['text']!r} is in language {want['lang']!r}; "
                  f"it is in {sorted(map(str, langs))}")


def _month(spec, pg, pdf_text):
    """A month grid: exactly one table on the page has a caption equal to `caption`; its first
    row is 7 th; its body cells, read row by row, give non-empty digit texts whose integer
    values are 1 to N for `year` and `month`, each in its weekday's column (Sunday first). The
    weekdays come from Python's calendar, never from reflow. A cell's text is its own
    (`own_text`): the words it copies from the key or a list are not the printed date.

    Optional, by date: `status` and `notes` are the exact texts of the cell's day-status and
    day-note spans, in order ({"2": ["Closed / Holiday"]}); `plain` lists dates whose cells
    copy no words at all. Each copied span must say where it is from (data-from) and where it
    is printed (data-bbox)."""
    import calendar
    import datetime

    caption = norm(spec["caption"])
    year, month = int(spec["year"]), int(spec["month"])
    found = [t for t in pg.find_all("table")
             if (cap := next(iter(t.find_all("caption")), None)) and norm(text_of(cap)) == caption]
    if len(found) != 1:
        _fail(f"{len(found)} tables captioned {caption!r}, expected 1")
    rows = found[0].find_all("tr")
    if not rows or [c.tag for c in _cells_of(rows[0])] != ["th"] * 7:
        _fail(f"{caption}: the first row is not 7 th")
    expected = 1
    by_date: dict[int, Node] = {}
    for r, tr in enumerate(rows[1:], start=1):
        cells = _cells_of(tr)
        if len(cells) != 7:
            _fail(f"{caption}: row {r} has {len(cells)} cells, expected 7")
        for c, cell in enumerate(cells):
            text = own_text(cell)
            if not text:
                continue
            if not text.isdigit():
                _fail(f"{caption}: row {r}, column {c + 1} holds {text!r}, not a date")
            if int(text) != expected:
                _fail(f"{caption}: expected date {expected}, found {text} (row {r}, column {c + 1})")
            if expected > calendar.monthrange(year, month)[1]:
                _fail(f"{caption}: date {text} is past the end of the month")
            column = (datetime.date(year, month, expected).weekday() + 1) % 7
            if c != column:
                _fail(f"{caption}: date {expected} is in column {c + 1}, expected {column + 1}")
            by_date[expected] = cell
            expected += 1
    last = calendar.monthrange(year, month)[1]
    if expected != last + 1:
        _fail(f"{caption}: the dates stop at {expected - 1}, expected {last}")
    for day, cell in by_date.items():
        for span in (n for n in cell.walk() if any(n.has_class(c) for c in COPIED)):
            if not span.attrs.get("data-from") or not span.attrs.get("data-bbox", "").startswith("[["):
                _fail(f"{caption}: date {day} copies {text_of(span)!r} without its source")
    for key, kind in (("status", "day-status"), ("notes", "day-note")):
        for day, want in spec.get(key, {}).items():
            got = [text_of(n) for n in by_date[int(day)].walk() if n.has_class(kind)]
            if got != [norm(w) for w in want]:
                _fail(f"{caption}: date {day} has {key} {got}, expected {want}")
    for day in spec.get("plain", []):
        copied = [text_of(n) for n in by_date[day].walk() if any(n.has_class(c) for c in COPIED)]
        if copied:
            _fail(f"{caption}: date {day} should copy no words, it has {copied}")


def _walk_json(node: dict) -> Iterator[dict]:
    yield node
    for child in node.get("children", []):
        yield from _walk_json(child)


def _page_nodes(doc, page: int) -> list[dict]:
    number = doc.excerpt_page(page)
    return [n for n in _walk_json(doc.document()["root"]) if n.get("page") == number]


def _ledger_codes(doc, page: int) -> set[str]:
    """The codes of the facts with a severity (CODES) on the page itself or on any node of the
    page, children included. Read from document.json, not from review.json, whose items name
    only the first error and the first warning of a node."""
    from reflow.analyze.confidence import CODES

    document = doc.document()
    number = doc.excerpt_page(page)
    found: set[str] = set()

    def facts(items) -> None:
        found.update(f["code"] for f in items if CODES[f["code"]].severity)

    for p in document.get("pages", []):
        if p["number"] == number:
            facts(p.get("facts", []))
    stack = [document["root"]]
    while stack:
        node = stack.pop()
        if node.get("page") == number:
            facts(node.get("facts", []))
        stack.extend(node.get("children", []))
    return found


def _review_has(values, doc, page):
    have = _ledger_codes(doc, page)
    missing = [c for c in values if c not in have]
    if missing:
        _fail(f"no review fact {missing} on page {page}; it has {sorted(have)}")


def _review_lacks(values, doc, page):
    present = sorted(set(values) & _ledger_codes(doc, page))
    if present:
        _fail(f"review fact {present} on page {page}")


def _title_suspect(value: bool, doc, page):
    """The document's title is (true) or is not (false) sent to review: a `title-suspect` fact on
    the root of document.json. Document-wide, like the title."""
    facts = doc.document()["root"].get("facts", [])
    have = any(f["code"] == "title-suspect" for f in facts)
    if have != value:
        why = next((f.get("message") for f in facts if f["code"] == "title-suspect"), None)
        _fail(f"title-suspect is {have}, expected {value}" + (f": {why}" if why else ""))


def _toc_lacks(texts, doc, page):
    """None of the texts is an entry of the Contents. Document-global: a quick excerpt's
    Contents holds only the pages it converted."""
    entries = {norm(e.get("text", "")) for e in doc.manifest.get("toc", [])}
    for text in texts:
        if norm(text) in entries:
            _fail(f"{norm(text)!r} is an entry of the Contents")


def _toc_on_page(texts, doc, page):
    """The Contents entries that point at the page are exactly these, in order."""
    number = doc.excerpt_page(page)
    have = [norm(e.get("text", "")) for e in doc.manifest.get("toc", []) if e.get("page") == number]
    if have != [norm(t) for t in texts]:
        _fail(f"the Contents entries of page {page} are {have}, expected {texts}")


def _figure_nodes(doc, page: int) -> list[dict]:
    return [n for n in _page_nodes(doc, page) if n.get("role") == "figure"]


def _unread_over_text(values, doc, page):
    """No unread figure of the page overlaps a block whose text holds one of the printed
    strings: a crop never lies over typed text."""
    nodes = _page_nodes(doc, page)
    crops = [n["bbox"] for n in nodes if n.get("role") == "figure"
             and (n.get("attrs") or {}).get("unread") and n.get("bbox")]
    for n in nodes:
        text = norm(n.get("text") or "")
        hit = next((s for s in values if norm(s) in text), None)
        if hit is None or not n.get("bbox"):
            continue
        x0, top, x1, bottom = n["bbox"]
        for c in crops:
            if min(x1, c[2]) > max(x0, c[0]) and min(bottom, c[3]) > max(top, c[1]):
                _fail(f"an unread figure {c} lies over the block holding {hit!r}")


def _max_figure_share(value, doc, page):
    number = doc.excerpt_page(page)
    info = next(p for p in doc.document()["pages"] if p["number"] == number)
    area = info["width"] * info["height"]
    for n in _figure_nodes(doc, page):
        if not n.get("bbox"):
            continue
        x0, top, x1, bottom = n["bbox"]
        share = max(0.0, x1 - x0) * max(0.0, bottom - top) / area
        if share > float(value):
            _fail(f"a figure {n['bbox']} covers {share:.0%} of the page, at most {float(value):.0%}")


def _min_figures(value, doc, page):
    n = len(_figure_nodes(doc, page))
    if n < int(value):
        _fail(f"{n} figures on the page, at least {value} expected")


def _box_on_page(spec, doc, page):
    """A BOX node of the page named `name` traces back to its box: its bbox is `bbox` (within
    `tol` points, default 2), and every block it holds lies inside it."""
    tol = spec.get("tol", 2.0)
    boxes = [n for n in _page_nodes(doc, page)
             if n["role"] == "box" and n.get("attrs", {}).get("name") == spec["name"]]
    if len(boxes) != 1:
        _fail(f"{len(boxes)} boxes named {spec['name']!r} on the page")
    box = boxes[0]["bbox"]
    if any(abs(a - b) > tol for a, b in zip(box, spec["bbox"])):
        _fail(f"box {spec['name']!r} is at {box}, not {spec['bbox']}")
    for child in boxes[0]["children"]:
        b = child.get("bbox")
        if b is None or not (b[0] >= box[0] - tol and b[1] >= box[1] - tol
                             and b[2] <= box[2] + tol and b[3] <= box[3] + tol):
            _fail(f"block {child.get('text')!r} at {b} is outside its box {box}")


def _max_review_items(value, doc, page):
    number = doc.excerpt_page(page)
    items = [i for i in doc.review() if i.get("page") == number]
    if len(items) > int(value):
        _fail(f"{len(items)} review items on the page, at most {value}")


def _signature_items(value, doc, page):
    """The page has exactly this many review items that point at a signature (unread-signature)."""
    number = doc.excerpt_page(page)
    n = sum(1 for i in doc.review() if i.get("page") == number
            and any(t["code"] == "unread-signature" for t in i.get("targets", [])))
    if n != int(value):
        _fail(f"{n} signature review items on the page, expected {value}")


SIGNATURE = "<figure kind=signature>"


def _figure_order(values, doc, page):
    """The entries come in this order among the page's nodes (document.json, in document order,
    a figure's children skipped): a text entry is a node whose text holds it, SIGNATURE a
    signature figure. A signature followed by a text entry must be directly before its node."""
    nodes, skip = [], set()
    for n in _page_nodes(doc, page):
        if id(n) in skip:
            continue
        nodes.append(n)
        if n.get("role") == "figure":
            skip.update(id(c) for c in _walk_json(n) if c is not n)

    def matches(entry: str, n: dict) -> bool:
        if entry == SIGNATURE:
            return n.get("role") == "figure" and (n.get("attrs") or {}).get("kind") == "signature"
        return norm(entry) in norm(n.get("text") or "")

    at = 0
    for k, entry in enumerate(values):
        following = values[k + 1] if k + 1 < len(values) else None
        found = next((i for i in range(at, len(nodes)) if matches(entry, nodes[i]) and (
            entry != SIGNATURE or following in (None, SIGNATURE)
            or (i + 1 < len(nodes) and matches(following, nodes[i + 1])))), None)
        if found is None:
            order = [SIGNATURE if matches(SIGNATURE, n) else (n.get("text") or n.get("role"))[:40]
                     for n in nodes]
            _fail(f"{entry!r} not found in order after {values[:k]}; the page reads {order}")
        at = found + 1


def _alt_matches(pattern: str, alt: str) -> bool:
    """A substring of the alt, or a regular expression when the pattern starts with `^`."""
    return bool(re.search(pattern, alt)) if pattern.startswith("^") else norm(pattern) in alt


def _alts(pg: Node) -> list[str]:
    return [norm(n.attrs.get("alt", "")) for n in pg.find_all("img")]


def _alt_has(values, pg, pdf_text):
    """Each pattern matches the alt of at least one `<img>` on the page."""
    alts = _alts(pg)
    if not alts:
        _fail("no <img> on the page")
    for pattern in values:
        if not any(_alt_matches(pattern, alt) for alt in alts):
            _fail(f"no <img alt> matches {pattern!r}; the alts are {alts}")


def _alt_not(values, pg, pdf_text):
    """No `<img alt>` on the page matches any pattern. A spec that uses it also counts the
    page's figures (see specs.load_specs), so a page that lost every picture cannot pass."""
    for alt in _alts(pg):
        for pattern in values:
            if _alt_matches(pattern, alt):
                _fail(f"<img alt> {alt!r} matches {pattern!r}")


# Checks that read the converted document (corpus.DocOutput) at a source page: the ledger of
# document.json, the Contents in the manifest and the review queue. `page` is the source page;
# DocOutput.excerpt_page turns it into the page of a quick excerpt.
DOC_CHECKS: dict[str, Callable] = {
    "review_has": _review_has,
    "review_lacks": _review_lacks,
    "title_suspect": _title_suspect,
    "toc_lacks": _toc_lacks,
    "toc_on_page": _toc_on_page,
    "max_figure_share": _max_figure_share,
    "min_figures": _min_figures,
    "max_review_items": _max_review_items,
    "unread_over_text": _unread_over_text,
    "signature_items": _signature_items,
    "figure_order": _figure_order,
    "box_on_page": _box_on_page,
}

# Checks that read the document shell (index.html) instead of one page.
SHELL_CHECKS: dict[str, Callable] = {
    "shell_contains": _shell_contains,
    "doc_lang": _doc_lang,
    "doc_title": _doc_title,
}

CHECKS: dict[str, Callable] = {
    "min_recall": _min_recall,
    "contains": _contains,
    "contains_in_order": _contains_in_order,
    "no_text": _no_text,
    "no_blank_runs": _no_blank_runs,
    "same_block": _same_block,
    "same_item": _same_item,
    "separate_blocks": _separate_blocks,
    "same_entry": _same_entry,
    "heading": _heading,
    "not_heading": _not_heading,
    "no_block": _no_block,
    "in_figure": _in_figure,
    "max_headings": _max_headings,
    "max_blocks": _max_blocks,
    "table": _table,
    "max_table_cols": _max_table_cols,
    "list_items": _list_items,
    "nested_list": _nested_list,
    "figures": _figures,
    "unread_figures": _unread_figures,
    "fields": _fields,
    "input_attrs": _input_attrs,
    "field_group": _field_group,
    "fieldsets_named": _fieldsets_named,
    "ruled_box": _ruled_box,
    "labelled_once": _labelled_once,
    "field_labels": _field_labels,
    "control_names": _control_names,
    "control_order": _control_order,
    "labels_in_names": _labels_in_names,
    "no_control_names": _no_control_names,
    "field_rows": _field_rows,
    "no_empty_field_cells": _no_empty_field_cells,
    "tables": _count("table", "tables"),
    "ins_count": _count("ins", "insertions"),
    "del_count": _count("del", "deletions"),
    "ins_texts": _ins_texts,
    "unmarked": _unmarked,
    "calendar_month": _month,
    "footnotes": _footnotes,
    "noterefs": _noterefs,
    "links": _links,
    "emphasis": _emphasis,
    "superscripts": _superscripts,
    "block_lang": _block_lang,
    "alt_has": _alt_has,
    "alt_not": _alt_not,
}
