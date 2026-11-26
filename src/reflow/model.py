"""The document model: one normalized tree that every pipeline stage reads and writes.

Coordinates are PDF points with a top-left origin, as (x0, top, x1, bottom), matching
pdfplumber. Pages are 1-based.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 1

type BBox = tuple[float, float, float, float]

# What a Fact's data values may hold: JSON scalars, or a list of them (no tuples or dicts,
# which would not survive a JSON round-trip unchanged).
_JSON_SCALARS = (str, int, float, bool, type(None))


class Role(StrEnum):
    DOC = "doc"
    SECTION = "section"
    H1 = "h1"
    H2 = "h2"
    H3 = "h3"
    H4 = "h4"
    H5 = "h5"
    H6 = "h6"
    P = "p"
    LIST = "list"
    LI = "li"
    TABLE = "table"
    TR = "tr"
    TH = "th"
    TD = "td"
    FIGURE = "figure"
    CAPTION = "caption"
    LINK = "link"
    NOTE = "note"
    FORM = "form"
    FIELD = "field"  # a fillable form control (attrs: kind, name, label, ...)
    GROUP = "group"  # a fieldset: related controls under a legend (the node text)
    BOX = "box"  # a ruled box of text: its blocks, framed as one group (analyze.boxes)
    ARTIFACT = "artifact"  # headers, footers, page numbers: not part of the content


HEADING_BY_LEVEL = (Role.H1, Role.H2, Role.H3, Role.H4, Role.H5, Role.H6)  # index = level - 1
HEADING_ROLES = frozenset(HEADING_BY_LEVEL)
HEADING_LEVEL = {role: level for level, role in enumerate(HEADING_BY_LEVEL, 1)}
SUBHEADING_ROLES = HEADING_ROLES - {Role.H1}  # the title (h1) is not a section heading


class Source(StrEnum):
    HEURISTIC = "heuristic"
    TAGS = "tags"  # the PDF's own tag tree, bookmarks and link annotations
    DOCLING = "docling"
    OCR = "ocr"
    LLM = "llm"
    ASSIST = "assist"  # an opt-in AI answer to a narrow question, checked against the page
    HUMAN = "human"


# Codes of the page facts that one pass writes and a later one reads by name.
FACT_OCR_PAGE = "ocr-page"          # OCR read the page (ingest.ocr)
FACT_OCR_MISSING = "ocr-missing"    # an image-only page nobody read
FACT_SCAN_NOISY = "scan-noisy"      # a grainy scan no figure was looked for in (page_integrity)
FACT_SCAN_FIELDS = "scan-fields"    # fields found on a scan by reading its image (scan_fields)


class LabelSource(StrEnum):
    """Where a form control's accessible name came from (`attrs["label"]` of a FIELD or GROUP)."""
    PRINTED = "printed"  # the printed label it is bound to
    PRINTED_NEARBY = "printed-nearby"  # a printed caption near it: a guess
    TOOLTIP = "tooltip"  # the author's tooltip
    CELL_HEADER = "cell-header"  # the header of its table cell: a guess
    NONE = "none"
    ASSIST = "assist"  # a caption the AI chose among the candidates


class PageKind(StrEnum):
    BORN_DIGITAL = "born_digital"
    SCANNED = "scanned"
    MIXED = "mixed"
    EMPTY = "empty"


@dataclass(frozen=True)
class Fact:
    """One piece of evidence about a node, a page or the document.

    A fact says *who* found *what*; it never says what it is worth. The code's meaning
    (label, severity, effect on confidence) lives once, in reflow.analyze.confidence.CODES,
    so a new kind of evidence is one registry entry, not an edit in every consumer.

    code:    stable identifier, e.g. "order-disagreement", "alt-missing", "ocr-page"
    source:  the engine or stage that produced it
    detail:  the human reason a reviewer sees ("Docling reads blocks 3-7 before 1-2")
    data:    measurements for tests and the inspector ({"tau": 0.41}); JSON-safe scalars
    """
    code: str
    source: Source
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (not isinstance(self.code, str) or not self.code or self.code != self.code.lower()
                or any(c.isspace() for c in self.code)):
            raise ValueError(f"fact code must be lower-case with no spaces: {self.code!r}")
        for key, value in self.data.items():
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, _JSON_SCALARS):
                    raise TypeError(f"fact data {key!r} must be a JSON scalar or a list of "
                                    f"scalars, not {type(item).__name__}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "source": self.source.value}
        if self.detail:
            d["detail"] = self.detail
        if self.data:
            d["data"] = self.data
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fact:
        return cls(code=d["code"], source=Source(d["source"]), detail=d.get("detail", ""),
                   data=d.get("data", {}))


@dataclass(frozen=True)
class Span:
    """An inline annotation over `node.text[start:end]`: a link or a footnote reference.

    Links and note references do not split a node's text into child nodes (every stage
    reads `node.text` as one string), they annotate offsets of it. The renderer turns them
    into <a href> and <a role="doc-noteref">. `kind` is "link" or "noteref"; `target` is a
    URL, "#p12" for a page, "#<anchor>" for a heading or table, or a NOTE node's id.
    """
    start: int
    end: int
    kind: str
    target: str

    def __post_init__(self) -> None:
        if self.kind not in ("link", "noteref"):
            raise ValueError(f"span kind must be 'link' or 'noteref': {self.kind!r}")
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"span needs 0 <= start < end: {self.start}:{self.end}")

    def to_dict(self) -> list[Any]:
        return [self.start, self.end, self.kind, self.target]

    @classmethod
    def from_dict(cls, d: list[Any]) -> Span:
        start, end, kind, target = d
        return cls(int(start), int(end), str(kind), str(target))


# The inline marks (design: the one layer for emphasis, raised text, OCR doubt and OCR
# insertions and deletions). Span stays for links and note references; every other word-level
# annotation is a Mark, so a bold word inside a link, or a doubtful word inside an insertion,
# nests. "fix" is a word a reviewer corrected (docs/review-decisions.md): its `alt` is what OCR
# read. "sup" is a run the PDF sets small and raised (extract.Line.sup) that no note claimed.
# "red" and "green" are a short run the PDF prints in that colour (analyze.marks): the
# named accent, never the PDF's own value, so the viewer can keep its contrast in either theme.
ACCENTS = ("red", "green")
MARK_KINDS = ("strong", "em", "ins", "del", "del-unread", "doubt", "fix", "sup", *ACCENTS)
# Marks that carry a reading of the page and are never dropped silently: losing one adds an
# "ocr-mark-lost" fact. A lost strong or em is only cosmetic.
SEMANTIC_MARKS = frozenset({"ins", "del", "del-unread", "doubt", "fix"})
# Nesting order for marks that start at the same place: the outermost kind first.
MARK_RANK = {"ins": 0, "del": 0, "del-unread": 0, "strong": 1, "em": 2, "fix": 3, "doubt": 4,
             "sup": 5, "red": 6, "green": 6}


def mark_order(mark: Mark) -> tuple[int, int, int]:
    """Sort key: by start, the longer first, then the outer kind first."""
    return (mark.start, -mark.end, MARK_RANK[mark.kind])


def _ops_of(edits: list[tuple[int, int, int]], length: int) -> list[tuple[str, int, int, int, int]]:
    """SequenceMatcher opcodes for known edits: equal stretches between replaced ranges."""
    ops, at, shift = [], 0, 0
    for start, end, size in sorted(edits):
        if start < at:
            raise ValueError(f"edits overlap at {start}")
        if at < start:
            ops.append(("equal", at, start, at + shift, start + shift))
        ops.append(("replace", start, end, start + shift, start + shift + size))
        shift += size - (end - start)
        at = end
    if at < length:
        ops.append(("equal", at, length, at + shift, length + shift))
    return ops


@dataclass(frozen=True)
class Mark:
    """An inline annotation over `node.text[start:end]`: emphasis or an OCR reading.

    `text` is `node.text[start:end]` when the mark was made or last rebased; Node.check_inline
    rejects a mark whose text is no longer what the node has at its range.
    A "del-unread" mark is a point (start == end, no text): a struck word the OCR could not
    read, shown at its offset. `alt` is a reading the document's own evidence suggests, for a
    "doubt"; for a "fix" it is what OCR read before a reviewer corrected the word, and it is
    required.
    """
    start: int
    end: int
    kind: str
    text: str
    alt: str = ""

    def __post_init__(self) -> None:
        if self.kind not in MARK_KINDS:
            raise ValueError(f"mark kind must be one of {MARK_KINDS}: {self.kind!r}")
        if self.kind == "del-unread":
            if self.start < 0 or self.end != self.start or self.text:
                raise ValueError("a del-unread mark is a point with no text: "
                                 f"{self.start}:{self.end} {self.text!r}")
        elif self.start < 0 or self.end <= self.start:
            raise ValueError(f"mark needs 0 <= start < end: {self.start}:{self.end}")
        if len(self.text) != self.end - self.start:
            raise ValueError(f"mark text {self.text!r} does not fill {self.start}:{self.end}")
        if self.alt and self.kind not in ("doubt", "fix"):
            raise ValueError(f"only a doubt or fix mark carries alt: {self.kind!r}")
        if self.kind == "fix" and not self.alt:
            raise ValueError("a fix mark carries the reading it replaced, in alt")

    def to_dict(self) -> list[Any]:
        d: list[Any] = [self.start, self.end, self.kind, self.text]
        if self.alt:
            d.append(self.alt)
        return d

    @classmethod
    def from_dict(cls, d: list[Any]) -> Mark:
        start, end, kind, text, *rest = d
        return cls(int(start), int(end), str(kind), str(text), str(rest[0]) if rest else "")


def make_id(page: int | None, bbox: BBox | None, text: str | None) -> str:
    """Stable node ID, so review overrides survive re-runs.

    The bbox is rounded to whole points to absorb float noise between extractor runs.
    Role is deliberately excluded: re-classifying a node (p -> h2) keeps its ID.
    """
    rounded = tuple(round(v) for v in bbox) if bbox else None
    digest = hashlib.sha1(repr((rounded, text or "")).encode()).hexdigest()[:12]
    return f"{page if page is not None else 0}:{digest}"


# What a figure is for, set on every figure by the figure pass (figure_role, below): a picture
# that carries the page's meaning, art that carries none, a mark repeated as a letterhead, or a
# glyph too small to describe. The transcript shows each kind its own way.
type FigureRole = Literal["content", "decorative", "logo", "icon"]
FIGURE_ROLES: tuple[FigureRole, ...] = ("content", "decorative", "logo", "icon")


def figure_role(node: Node) -> FigureRole:
    """What the figure is for: the role the figure pass stored, else what its alt says (a
    decorative figure is decorative, a figure whose kind is logo is a logo). An unread crop
    is content: it stands for words a reader needs."""
    stored = node.attrs.get("figure_role")
    if stored in FIGURE_ROLES:
        return stored
    if node.attrs.get("unread"):
        return "content"
    if node.attrs.get("decorative"):
        return "decorative"
    if node.attrs.get("kind") == "logo":
        return "logo"
    return "content"


# A node a model wrote or changed carries attrs["provenance"] AI_DRAFTED and attrs["reviewed"]
# False until a reviewer settles it (decisions then set "reviewer" and True), and an AI_DRAFTED
# fact that review shows as a warning.
AI_DRAFTED = "ai-drafted"


def ai_draft_attrs() -> dict[str, Any]:
    return {"provenance": AI_DRAFTED, "reviewed": False}


def mark_ai_draft(node: Node, detail: str = "", **data: Any) -> None:
    """Label `node` as a model's draft, with its AI_DRAFTED fact unless it has one."""
    node.attrs.update(ai_draft_attrs())
    if not any(f.code == AI_DRAFTED for f in node.facts):
        node.fact(AI_DRAFTED, Source.LLM, detail, **data)


def is_ai_draft(node: Node) -> bool:
    return node.attrs.get("provenance") == AI_DRAFTED


def is_model_text(node: Node) -> bool:
    """Whether the text is a model's, not the document's own: a draft, or anything marked
    unreviewed."""
    return is_ai_draft(node) or node.attrs.get("reviewed") is False


def is_ocr(node: Node) -> bool:
    """Whether OCR read the node's text: text_evidence.flag_ocr_marks writes `attrs["ocr"]` on
    every OCR-origin node, whatever its engine, and a heuristic OCR node is Source.OCR as well."""
    return "ocr" in node.attrs or node.source == Source.OCR


class Why(str):
    """One entry of `attrs["why"]`: the prose a reviewer reads, and the `code` of the rule that
    wrote it when a later pass branches on the reason (a pass branches on the code, never on the
    words). Everywhere else it is its prose: it compares, joins and serializes as the str, so
    document.json holds the words only. A node read back from JSON has the words without the
    code; the passes that read codes run on the tree the converter has just built."""
    code: str | None

    def __new__(cls, text: str, code: str | None = None) -> Why:
        self = super().__new__(cls, text)
        self.code = code
        return self

    def __reduce__(self) -> tuple:
        return Why, (str(self), self.code)


def code_of(reason: str) -> str | None:
    """The code of a `why` entry, None for plain prose."""
    return reason.code if isinstance(reason, Why) else None


NodeAttrs = TypedDict("NodeAttrs", {
    "alt": str,
    "alt-source": str,
    "anchor": str,
    "aria": str,
    "assist_for": str,
    "assist_reason": str,
    "cell": list[float],
    "crop_sha": str,
    "checked": bool,
    "colspan": int,
    "columns": int,
    "comb": bool,
    "contents": bool,
    "context": bool,
    "day": dict[str, Any],
    "decorative": bool,
    "description": str,
    "detected": str,
    "export": str,
    "fid": str,
    "figure_label": bool,
    "figure_role": FigureRole,
    "font_size": float,
    "form_text": str,
    "format": str,
    "generic": bool,
    "group": str,
    "handwritten": bool,
    "header_rows": int,
    "headers": list[str],
    "height": float,
    "href": str,
    "id": str,
    "index": bool,
    "index_letter": bool,
    "italic": bool,
    "kind": str,
    "label": str,
    "label_caps": bool,
    "layer_text": str,
    "level": int,
    "lines": list[str],
    "marker": str,
    "maxlen": int,
    "method": str,
    "multiline": bool,
    "name": str,
    "needs": str,
    "ocr": str,
    "ocr_guess": str,
    "ocr_remnant": bool,
    "ocr_struck": bool,
    "options": list[str],
    "ordered": bool,
    "outline": dict[str, Any],
    "page_ref": str,
    "pages": list[int],
    "parent": str | None,
    "password": bool,
    "provenance": str,
    "readonly": bool,
    "refs": int,
    "required": bool,
    "reading": str,
    "reviewed": bool,
    "rows": int,
    "rowspan": int,
    "scope": str,
    "selected": list[int],
    "set_apart": str,
    "signs": str,
    "size": float,
    "status": str,
    "style_level": int,
    "title": str,
    "title_evidence": dict[str, Any],
    "undecided": dict[str, Any],
    "undecided_title": str,
    "toc": str,
    "tooltip": str,
    "trail": list[str],
    "unicode_errors": int,
    "unread": bool,
    "value": str,
    "warn": str,
    "why": list[str],
    "width": float,
    "words": list[str],
    "x": float,
}, total=False)
"""Optional attributes shared by node kinds; absent keys stay absent in JSON.

Nested evidence records retain their evolving JSON schema. ``fid`` is the stable
form control id, distinct from the source node id.
"""


@dataclass(init=False)
class Node:
    role: Role
    id: str
    page: int | None
    bbox: BBox | None
    # The text, read through `text`. It changes only through set_text and join_inline, which
    # keep spans and marks on their words, and restore_text, which puts a saved state back;
    # there is no setter.
    _text: str | None
    attrs: NodeAttrs
    children: list[Node]
    confidence: float
    source: Source
    # Evidence about this node (see Fact). The engines append; reflow.analyze.confidence is
    # the one writer of `confidence` from it, and reflow.analyze.review the one reader for
    # review.json.
    facts: list[Fact]
    # Inline links and note references over `text` (see Span), in offset order.
    spans: list[Span]
    # Inline emphasis and OCR marks over `text` (see Mark), sorted by (start, -end, rank).
    marks: list[Mark]

    def __init__(self, role: Role, id: str = "", page: int | None = None,
                 bbox: BBox | None = None, text: str | None = None,
                 attrs: NodeAttrs | None = None, children: list[Node] | None = None,
                 confidence: float = 1.0, source: Source = Source.HEURISTIC,
                 facts: list[Fact] | None = None, spans: list[Span] | None = None,
                 marks: list[Mark] | None = None) -> None:
        self.role = role
        self.page = page
        self.bbox = bbox
        self._text = text
        self.attrs = {} if attrs is None else attrs
        self.children = [] if children is None else children
        self.confidence = confidence
        self.source = source
        self.facts = [] if facts is None else facts
        self.spans = [] if spans is None else spans
        self.marks = [] if marks is None else marks
        self.id = id or make_id(page, bbox, text)

    @property
    def text(self) -> str | None:
        return self._text

    def fact(self, code: str, source: Source, detail: str = "", **data: Any) -> Fact:
        """Append a Fact about this node and return it (saves each engine a line)."""
        fact = Fact(code, source, detail, dict(data))
        self.facts.append(fact)
        return fact

    def set_text(self, new: str | None, edits: list[tuple[int, int, int]] | None = None) -> None:
        """Replace `text`, moving every span and mark with the words it covers. None (a node
        with no text, such as a figure) is diffed as an empty text; a caller that turns a node
        into one with no text clears its spans and marks first.

        The old and new text are diffed; a boundary in an unchanged stretch moves by that
        stretch's shift, a start inside a changed stretch moves to its new start and an end to
        its new end. A span or mark left empty is lost; a lost semantic mark adds an
        "ocr-mark-lost" fact so a reviewer sees it. A mark over an edited word stays on it, with
        its `text` updated.

        `edits` names the changes exactly, as disjoint (start, end, new length) ranges of the old
        text in order, when the caller knows them (a reviewer's correction): then nothing outside
        them moves, which a diff of two texts with repeated words can't promise.
        """
        old = self._text or ""
        if new == self._text or not (self.spans or self.marks):
            self._text = new
            return
        stored, new = new, new or ""
        ops = (_ops_of(edits, len(old)) if edits is not None
               else difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes())

        def start_of(p: int) -> int:
            for tag, i1, i2, j1, _ in ops:
                if i1 <= p < i2:
                    return j1 + (p - i1) if tag == "equal" else j1
            return len(new)

        def end_of(p: int) -> int:
            for tag, i1, i2, j1, j2 in ops:
                if i1 < p <= i2:
                    return j1 + (p - i1) if tag == "equal" else j2
            return 0

        def deleted(i: int) -> bool:
            return any(tag != "equal" and i1 <= i < i2 for tag, i1, i2, _, _ in ops)

        spans = []
        for span in self.spans:
            start, end = start_of(span.start), end_of(span.end)
            if start < end:
                spans.append(replace(span, start=start, end=end))
        marks = []
        for mark in self.marks:
            if mark.kind == "del-unread":
                p = mark.start
                if 0 < p < len(old) and deleted(p - 1) and deleted(p):
                    self._lose(mark)
                else:
                    at = start_of(p) if p < len(old) else len(new)
                    marks.append(replace(mark, start=at, end=at))
                continue
            start, end = start_of(mark.start), end_of(mark.end)
            if start >= end:
                self._lose(mark)
                continue
            text = new[start:end]
            if mark.kind == "fix":
                if text != mark.text:   # a correction is kept only while its word is untouched
                    self._lose(mark)
                    continue
                marks.append(replace(mark, start=start, end=end))
                continue
            alt = "" if text == mark.alt else mark.alt
            marks.append(replace(mark, start=start, end=end, text=text, alt=alt))
        self._text = stored
        self.spans = sorted(spans, key=lambda s: (s.start, -s.end))
        self.marks = sorted(marks, key=mark_order)

    def restore_text(self, text: str | None, spans: list[Span], marks: list[Mark]) -> None:
        """Put back a text with the spans and marks it had, as they were: no rebasing, no loss
        facts. For `analyze.decisions_apply.revert`, which returns a node to its saved state."""
        self._text, self.spans, self.marks = text, spans, marks

    def join_inline(self, parts: Sequence[Node], sep: str = " ") -> None:
        """Set this node's text to the parts' texts joined by `sep`, with every span and mark
        shifted to its new offset. `parts` may include this node.

        A caller that then normalizes the joined text (a removed hyphen) calls `set_text`,
        which moves the offsets again.
        """
        texts: list[str] = []
        spans: list[Span] = []
        marks: list[Mark] = []
        at = 0
        for part in parts:
            text = part.text or ""
            texts.append(text)
            spans += [replace(s, start=s.start + at, end=s.end + at) for s in part.spans]
            marks += [replace(m, start=m.start + at, end=m.end + at) for m in part.marks]
            at += len(text) + len(sep)
        self._text, self.spans, self.marks = sep.join(texts), spans, sorted(marks, key=mark_order)

    def _lose(self, mark: Mark) -> None:
        """Record that a semantic mark could not be placed (a lost strong or em is cosmetic)."""
        if mark.kind in SEMANTIC_MARKS:
            self.fact("ocr-mark-lost", Source.OCR, f"A {mark.kind} mark could not be placed",
                      kind=mark.kind, text=mark.text)

    def check_inline(self) -> None:
        """Raise ValueError, naming this node, unless every span and mark fits its text: in
        range, a mark's `text` what the node has at its range, and no span over another (an
        anchor never nests). Marks may cross each other and a span; the renderer splits the
        lower-ranked element. The load and the end of analysis call this, so the renderer and
        the saved model never meet a bad one."""
        if not (self.spans or self.marks):
            return
        if self.text is None:
            raise ValueError(f"node {self.id}: spans or marks but no text")
        size, last = len(self.text), 0
        for span in sorted(self.spans, key=lambda s: (s.start, s.end)):
            if span.end > size:
                raise ValueError(f"node {self.id}: span {span.start}:{span.end} runs past the "
                                 f"end of its text ({size})")
            if span.start < last:
                raise ValueError(f"node {self.id}: span {span.start}:{span.end} overlaps the "
                                 f"span that ends at {last}")
            last = span.end
        for mark in self.marks:
            if mark.end > size:
                raise ValueError(f"node {self.id}: {mark.kind} mark {mark.start}:{mark.end} runs "
                                 f"past the end of its text ({size})")
            if self.text[mark.start:mark.end] != mark.text:
                raise ValueError(f"node {self.id}: {mark.kind} mark {mark.text!r} is not the text "
                                 f"at {mark.start}:{mark.end} "
                                 f"({self.text[mark.start:mark.end]!r})")

    def walk(self) -> Iterator[Node]:
        """Depth-first, document order."""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "role": self.role.value}
        if self.page is not None:
            d["page"] = self.page
        if self.bbox is not None:
            d["bbox"] = list(self.bbox)
        if self.text is not None:
            d["text"] = self.text
        if self.attrs:
            d["attrs"] = self.attrs
        d["confidence"] = self.confidence
        d["source"] = self.source.value
        if self.facts:
            d["facts"] = [f.to_dict() for f in self.facts]
        if self.spans:
            d["spans"] = [s.to_dict() for s in self.spans]
        if self.marks:
            d["marks"] = [m.to_dict() for m in self.marks]
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Node:
        """The node `to_dict` wrote. Raises ValueError, naming the node, for a bad span or mark
        (an unknown kind, an empty or reversed range, or one check_inline rejects)."""
        try:
            spans = [Span.from_dict(s) for s in d.get("spans", [])]
            marks = [Mark.from_dict(m) for m in d.get("marks", [])]
        except ValueError as error:
            raise ValueError(f"node {d['id']}: {error}") from None
        node = cls(
            id=d["id"],
            role=Role(d["role"]),
            page=d.get("page"),
            bbox=tuple(d["bbox"]) if "bbox" in d else None,
            text=d.get("text"),
            attrs=d.get("attrs", {}),
            children=[cls.from_dict(c) for c in d.get("children", [])],
            confidence=d.get("confidence", 1.0),
            source=Source(d.get("source", Source.HEURISTIC)),
            facts=[Fact.from_dict(f) for f in d.get("facts", [])],
            spans=spans,
            marks=marks,
        )
        node.check_inline()
        return node


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    rotation: int = 0
    kind: PageKind = PageKind.BORN_DIGITAL
    char_count: int = 0
    image_coverage: float = 0.0  # fraction of the page area covered by images, 0..1
    # The printed page label ("iv", "10"): what the file declares, recorded by ingest, then
    # completed by analyze.page_numbers.fill_labels. Serialized only when set.
    label: str = ""
    # Evidence about the page as a whole: "ocr-page", "ocr-missing", "order-disagreement".
    facts: list[Fact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "number": self.number,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "kind": self.kind.value,
            "char_count": self.char_count,
            "image_coverage": self.image_coverage,
        }
        if self.label:
            d["label"] = self.label
        if self.facts:
            d["facts"] = [f.to_dict() for f in self.facts]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageInfo:
        return cls(**{**d, "kind": PageKind(d["kind"]),
                      "facts": [Fact.from_dict(f) for f in d.get("facts", [])]})


@dataclass
class Document:
    root: Node
    pages: list[PageInfo] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)  # title, lang, sha256, producer...

    @property
    def decisions_ledger(self) -> dict | None:
        """What a reviewer's decisions did (applied, dismissed, orphaned), or None when the
        conversion was given no decisions file (written by analyze.decisions_apply)."""
        return (self.meta.get("decisions") or {}).get("ledger")

    def ensure_unique_ids(self) -> None:
        """Disambiguate the rare collision (same text, same rounded bbox, same page)."""
        seen: dict[str, int] = {}
        for node in self.root.walk():
            n = seen.get(node.id, 0)
            seen[node.id] = n + 1
            if n:
                node.id = f"{node.id}~{n}"

    def flow(self) -> Iterator[Node]:
        """The content nodes in reading order, with SECTION wrappers opened transparently.

        Once reflow.analyze.sections nests headings into SECTION nodes, `root.children` is no
        longer the flat reading order; every consumer that walked it (chunking, the contents,
        page text sizes, figure contexts, review) walks this instead. A SECTION yields its
        children, not itself; every other top-level node is yielded whole (lists and tables
        keep their own children).
        """
        stack = list(reversed(self.root.children))
        while stack:
            node = stack.pop()
            if node.role == Role.SECTION:
                stack.extend(reversed(node.children))
            else:
                yield node

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "meta": self.meta,
            "pages": [p.to_dict() for p in self.pages],
            "root": self.root.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Document:
        if d.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {d.get('schema')!r}")
        return cls(
            root=Node.from_dict(d["root"]),
            pages=[PageInfo.from_dict(p) for p in d["pages"]],
            meta=d.get("meta", {}),
        )

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> Document:
        return cls.from_dict(json.loads(s))
