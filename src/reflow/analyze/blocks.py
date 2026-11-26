"""Blocks: the unit the layout stages work on, and the line-joining and marker
helpers they share.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from reflow.analyze.constants import (
    ALIGN_TOL,
    BULLET_GLYPHS,
    DOT_LEADER,
    HANGING_MIN,
    INDEX_REFERENCE,
    LEGAL_CHAIN,
    LIST_MARKER,
    ROMAN_VALUES,
    TERMINAL,
)
from reflow.analyze.rhythm import Rhythm, line_gap
from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.leaders import is_contents_entry
from reflow.model import BBox


# Address and contact blocks keep their line breaks. Strong cues: a line ending in a
# state + ZIP, a line that is only a formatted phone number, or a contact label.
STATE_ZIP = re.compile(r"\b[A-Z]{2}\s+\d{5}(-\d{4})?$")
PHONE_LINE = re.compile(r"^(\+?1[-.\s])?(\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}$")
CONTACT_LABEL = re.compile(r"^(website|phone|fax|languages?|e-?mail|hours)\s*:", re.I)
# Prose: most lines run (nearly) the full block width and wrap after a lowercase word.
PROSE_WIDTH = 0.85
PROSE_END = re.compile(r"(^|\s)[a-z]+[.,;:]?$")


# Inside an address block a lone lowercase token after a line ending in "." or "/" is the
# rest of a wrapped web address ("Website: www." + "example." + "com"), and a line that
# ends in a unit word continues on the next ("Bldg 2, Ste" + "203").
URL_TAIL = re.compile(r"(^|\s)\S*([a-z0-9-]{3,}\.|/)$")
URL_REST = re.compile(r"^[a-z0-9][a-z0-9./_-]*$")
UNIT_END = re.compile(r"\b(ste|suite|bldg|building|unit|apt|rm|room|fl|floor|#)\.?$", re.I)
UNIT_REST = re.compile(r"^#?[0-9A-Z][0-9A-Za-z-]*$")


MIN_WRAPS = 2  # wrapped lines that must agree on an item text offset at one marker x


@dataclass(frozen=True)
class Marker:
    """A list-marker-looking start of a line. `value` orders numbers and letters, so that
    1./2./3. or a./b./c. in consecutive blocks make a run."""
    kind: str  # "bullet", "dash", "num", "alpha" or "roman"
    text: str  # as printed: "3.", "(b)", "•"
    value: int = 0
    initial: bool = False  # a single letter then a capitalized word: "R. M. Hazen"
    inner: "Marker | None" = None  # the second label of a chain: "(a)(1)" is (a) with inner (1)


def ends_hyphenated(text: str) -> bool:
    """A line that ends in a hyphen after a letter or digit: "ResNet-", "de-". Extraction
    turns a line-end hyphen into a soft hyphen, so both spellings count."""
    t = text.rstrip()
    return len(t) >= 2 and t[-1] in "-" + SOFT_HYPHEN and t[-2].isalnum()


def roman(s: str) -> int:
    vals = [ROMAN_VALUES[c] for c in s]
    return sum(-v if i + 1 < len(vals) and v < vals[i + 1] else v for i, v in enumerate(vals))


def marker_of(text: str, prev: str = "") -> Marker | None:
    """The list marker that starts `text`, or None. `prev` is the text of the line before it:
    a number that finishes a hyphenated word ("ResNet-" / "34. Right") is not a marker."""
    t = text.strip()
    if prev and ends_hyphenated(prev):
        return None
    chain = LEGAL_CHAIN.match(t + " ")
    if chain:
        outer = marker_of(chain[1] + " ")
        inner = marker_of(chain[2] + " ")
        if outer is not None and inner is not None and outer.kind in ("alpha", "num", "roman"):
            if inner.kind == "alpha" and inner.text.strip("()").lower() in ROMAN_VALUES:
                inner = Marker("roman", inner.text, roman(inner.text.strip("()").lower()))
            return replace(outer, text=chain[1] + chain[2], inner=inner)
    m = LIST_MARKER.match(t + " ")
    if not m:
        return None
    token = m[1]
    if token in BULLET_GLYPHS:
        return Marker("bullet", token)
    if token in "-–—*·":
        return Marker("dash", token)
    core = token.strip("().")
    if core.isdigit():
        return Marker("num", token, int(core))
    letters = core.lower()
    # "R. M. Hazen" is an initial; "(A) In General" is a legal label, never an initial.
    initial = not token.startswith("(") and bool(re.match(r"\s+[A-Z]", t[len(token):]))
    if len(letters) == 1:
        return Marker("alpha", token, ord(letters) - ord("a") + 1, initial)
    return Marker("roman", token, roman(letters))


def follows(prev: Marker, cur: Marker) -> bool:
    """Whether `cur` continues the sequence `prev` started: 2. after 1., b. after a. A chained
    label "(a)(1)" opens both its levels, so "(2)" follows it too."""
    if prev.inner is not None and cur.inner is None and follows(prev.inner, cur):
        return True
    if cur.inner is not None:
        cur = replace(cur, inner=None)
    if prev.kind != cur.kind:
        return False
    if cur.kind in ("bullet", "dash"):
        return prev.text == cur.text
    return cur.value == prev.value + 1


class Break(StrEnum):
    """Why a block starts, for the reasons other stages test. Any other reason is free text."""
    BOLD_CHANGES = "bold changes"
    FIRST_LINE_INDENT = "first-line indent"
    LIST_MARKER = "starts with a list marker"
    HYPHEN_COLUMN = "a hyphenated line ends and the next line is in another column"


@dataclass
class Block:
    lines: list[Line] = field(default_factory=list)
    why: str | None = None  # why this block starts where it does (see break_reason)
    marker: Marker | None = None  # the list-marker-looking start of the block, if any
    item: bool = False  # a list item: its marker has list context (see list_items)
    entry: bool = False  # a wrapped index entry and its page number (see join_wrapped_headings)
    label: str | None = None  # a worksheet line number: lines[0] is the number (see worksheet_rows)
    row: bool = False  # a lettered worksheet row, kept whole as a paragraph (see worksheet_rows)
    foot: bool = False  # set in the bottom margin: nothing follows it, so it opens no section
    # `bbox`, `size`, `bold` and `text` are asked for many times per block (heading passes). They are kept with
    # the identity of the lines and the label they were computed from, so a pass that appends
    # to `lines`, replaces one, or sets `label` makes the next read compute them again. A Line's
    # text and box do not change once blocks exist (layout.analyze fixes PUA bullets first).
    _memo: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __getstate__(self) -> dict:
        """The memo travels as plain values (its keys are ids, which mean nothing in another
        process); entries that are stale are left behind."""
        state = self.__dict__.copy()
        key = (self.label, tuple(map(id, self.lines)))
        state["_memo"] = {name: hit[1] for name, hit in self._memo.items() if hit[0] == key}
        return state

    def __setstate__(self, state: dict) -> None:
        memo = state.pop("_memo", {})
        self.__dict__.update(state)
        key = (self.label, tuple(map(id, self.lines)))
        self._memo = {name: (key, value) for name, value in memo.items()}

    def _cached(self, name: str, compute):
        key = (self.label, tuple(map(id, self.lines)))
        hit = self._memo.get(name)
        if hit is None or hit[0] != key:
            hit = self._memo[name] = (key, compute())
        return hit[1]

    @property
    def first(self) -> Line: return self.lines[0]
    @property
    def bbox(self) -> BBox:
        return self._cached("bbox", lambda: (
            min(l.x0 for l in self.lines), min(l.top for l in self.lines),
            max(l.x1 for l in self.lines), max(l.bottom for l in self.lines)))
    @property
    def size(self) -> float:
        return self._cached("size", lambda: Counter(l.size for l in self.lines).most_common(1)[0][0])
    @property
    def bold(self) -> bool: return self._cached("bold", lambda: all(l.bold for l in self.lines))
    @property
    def text(self) -> str:
        return self._cached("text", self._joined)

    def _joined(self) -> str:
        lines = self.lines
        if self.label is not None and self.label.isdigit():
            lines = lines[1:]  # the number is not text
        elif self.label is not None:
            head = lines[0]
            lead = len(head.text) - len(head.text.lstrip())
            first = head.cut(lead + len(self.label) + 1, len(head.text.rstrip()))
            lines = [first, *lines[1:]]  # the sub-item's letter is not text
        return join_lines(_logical_rows(lines))


def reads_rtl(text: str) -> bool:
    counts = Counter(unicodedata.bidirectional(ch) for ch in text) if not text.isascii() else {}
    return counts.get("R", 0) + counts.get("AL", 0) > counts.get("L", 0)


def _logical_rows(lines: Sequence[Line]) -> list[str]:
    """Rows top to bottom, with RTL fragments on each row reading right to left."""
    extraction_order = {id(line): i for i, line in enumerate(lines)}
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: l.top):
        if rows and abs(line.top - rows[-1][0].top) <= 0.25 * min(line.size, rows[-1][0].size):
            rows[-1].append(line)
        else:
            rows.append([line])
    texts = []
    for row in rows:
        if not reads_rtl(" ".join(line.text for line in row)):
            texts.extend(line.text for line in sorted(row, key=lambda line: extraction_order[id(line)]))
            continue
        text = ""
        prev = None
        for line in sorted(row, key=lambda l: -l.x1):
            fragment = line.text.strip()
            orphan_mark = bool(fragment and unicodedata.combining(fragment[0]))
            # A leading combining mark has no base in this extracted fragment.
            while fragment and unicodedata.combining(fragment[0]):
                fragment = fragment[1:]
            continuation = orphan_mark and prev is not None and line.x0 < prev.x0 < line.x1
            separator = " " if prev is not None and not continuation else ""
            text += separator + fragment
            prev = line
        texts.append(text)
    return texts


def join_wrapped(prev: str, nxt: str) -> str:
    """Join two text fragments, handling wrapped URLs/emails without inserting spaces.

    When `prev`'s last token looks URL-like (starts with http(s)://|www. or contains @)
    and ends with special characters (. / - _), and `nxt` starts with a domain-like token,
    join without a space. RTL text keeps a space after a hyphen.
    Otherwise join with a space.
    """
    if not prev or not nxt:
        return f"{prev}{nxt}" if not prev or not nxt else f"{prev} {nxt}"

    if prev.endswith(("-", SOFT_HYPHEN)) and (reads_rtl(prev) or reads_rtl(nxt)):
        return f"{prev} {nxt}"

    # Don't join if nxt starts with uppercase letter (likely a new sentence)
    if nxt and nxt[0].isupper():
        return f"{prev} {nxt}"

    prev_tokens = prev.split()
    nxt_tokens = nxt.split()
    if not prev_tokens or not nxt_tokens:
        return f"{prev} {nxt}"

    prev_last = prev_tokens[-1]
    nxt_first = nxt_tokens[0]

    is_url_like = (
        re.match(r'(https?://|www\.)\S*', prev_last) is not None or
        '@' in prev_last
    )

    if not is_url_like:
        return f"{prev} {nxt}"

    ends_with_relevant = prev_last and prev_last[-1] in './-_'
    if not ends_with_relevant:
        return f"{prev} {nxt}"

    if re.match(r'^[a-z0-9-]+(\.com|\.org|\.gov|\.net|\.edu|/|$)', nxt_first, re.IGNORECASE):
        # More strict: must be mostly lowercase letters/digits/hyphens
        if re.match(r'^[a-z0-9][a-z0-9.-]*[a-z0-9]$|^[a-z0-9]$', nxt_first, re.IGNORECASE):
            return f"{prev}{nxt}"
        # a path-like start: "path/to/resource"
        if nxt_first[0].isalpha() and '/' in nxt:
            return f"{prev}{nxt}"

    tld_pattern = r'^([a-z0-9-]+\.[a-z]{2,}|[a-z0-9-]+(?:/|$)|com|org|gov|net|edu|info|biz)'
    if re.match(tld_pattern, nxt_first, re.IGNORECASE):
        return f"{prev}{nxt}"

    return f"{prev} {nxt}"


def join_lines(texts: list[str]) -> str:
    """Join lines into running text, rejoining words split at a line end.

    A hyphen at a line end (extraction marks it as a soft hyphen) is a word break when the
    next line goes on in lowercase: "de-" + "pendent" is "dependent". Otherwise it is part
    of the word and stays: "ResNet-" + "34." is "ResNet-34.", "well-" + "Known" is
    "well-Known". RTL lines keep their word boundaries. Wrapped URLs and emails go
    through join_wrapped.
    """
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if out and ends_hyphenated(out) and not (reads_rtl(out) or reads_rtl(t)):
            stem = out.rstrip()[:-1]
            if _is_url_like(out.split()[-1]):
                # A wrapped URL keeps its hyphen: a URL is not hyphenated by typesetting
                out = join_wrapped(f"{stem}-", t)
            elif t[0].islower():
                out = stem + t  # the hyphen only broke the word
            elif t[0].isalnum():
                out = f"{stem}-{t}"  # a compound that wrapped at its hyphen
            else:
                out = f"{stem}- {t}"
        else:
            out = join_wrapped(out, t) if out else t
    return out.replace(SOFT_HYPHEN, "")


def _is_url_like(token: str) -> bool:
    return bool(re.match(r"(https?://|www\.)", token, re.I)) or "@" in token


def visual_lines(texts: list[str]) -> list[str]:
    """Lines of an address block as printed, except that a line wrapped mid-token (a URL
    that join_wrapped or URL_REST rejoins, a word split by a soft hyphen) is rejoined with
    the next one, and a suite number wrapped after "Ste" or a value wrapped after its label
    ("Website:") goes back on its line."""
    out: list[str] = []
    for t in (t.strip() for t in texts):
        if not t:
            continue
        prev = out[-1] if out else ""
        if prev.endswith(SOFT_HYPHEN) and not (reads_rtl(prev) or reads_rtl(t)):
            out[-1] = prev[:-len(SOFT_HYPHEN)] + t
        elif prev and (join_wrapped(prev, t) == prev + t
                       or (URL_TAIL.search(prev) and URL_REST.match(t))):
            out[-1] = prev + t
        elif prev and ((UNIT_END.search(prev) and UNIT_REST.match(t))
                       or ((m := CONTACT_LABEL.match(prev)) and m.end() == len(prev))):
            out[-1] = f"{prev} {t}"
        else:
            out.append(t)
    return [t.replace(SOFT_HYPHEN, "") for t in out]


def address_lines(block: Block) -> list[str] | None:
    """The visual lines of an address or contact block, or None for anything else.

    Conservative: two or more lines with a strong cue (state + ZIP, a phone-only line, a
    "Website:"-style label), and not shaped like wrapped prose."""
    if len(block.lines) < 2:
        return None
    texts = [l.text.replace(SOFT_HYPHEN, "").strip() for l in block.lines]
    if not any(STATE_ZIP.search(t) or PHONE_LINE.match(t) or CONTACT_LABEL.match(t)
               for t in texts):
        return None
    x0, _, x1, _ = block.bbox
    width = max(x1 - x0, 1.0)
    # The last line of a paragraph is short, and a "Website: www.example.com" line can be
    # the widest one; judge the shape on the other lines.
    shape = list(zip(block.lines, texts))[:-1]
    prose = sum(1 for l, t in shape if (l.x1 - l.x0) >= PROSE_WIDTH * width
                and PROSE_END.search(t) and not CONTACT_LABEL.match(t))
    if 2 * prose >= len(shape):
        return None
    lines = visual_lines([l.text for l in block.lines])
    return lines if len(lines) >= 2 else None


MIN_SET_LINES = 3  # lines that make a block set line by line (see set_lines)
SET_LINE_ROOM = 2.0  # ems a line leaves free beyond the next line's first word
LEADER_DOTS = re.compile(r"(?:\.\s?){4,}")
SET_LINE_WORDS = 12  # an entry is a short line; a sentence-long line is wrapped prose
# A line that ends on one of these runs on into the next ("… July 1, 2026 and" / "May 31 …").
RUN_ON_END = frozenset("a an and are as at by for from has have in is of on or our that the "
                       "to was were will with your".split())


def set_lines(block: Block) -> list[str] | None:
    """The lines of a block whose author broke every line: a list of names with no markers
    ("ACTEMRA® (tocilizumab) IV & SQ" / "AMJEVITA™ (adalimumab-atto) SQ" …). Wrapped text
    breaks a line because the next word does not fit; here the first word of each line would
    have fit on the line above with room to spare, inside the block's own width. Each line
    starts on the first one's left edge, right under the one before, and holds words: a
    block of table fragments (figures, dot leaders, cells side by side) is not a list.
    None for anything else."""
    lines = block.lines
    if len(lines) < MIN_SET_LINES:
        return None
    width = block.bbox[2] - block.bbox[0]
    widest = max(lines, key=lambda l: l.x1 - l.x0)
    for line in lines:
        text = line.text.strip()
        if (abs(line.x0 - lines[0].x0) > 0.5 * line.size or not any(ch.isalpha() for ch in text)
                or LEADER_DOTS.search(text) or len(text.split()) > SET_LINE_WORDS):
            return None
    for prev, line in zip(lines, lines[1:]):
        text, nxt = prev.text.strip(), line.text.split()
        if (not text or not nxt or text.endswith(("-", SOFT_HYPHEN)) or nxt[0][:1].islower()
                or text.split()[-1].casefold() in RUN_ON_END):
            return None  # a word broken over the line, or a phrase that runs on
        if not 0 < line.top - prev.top <= 2 * prev.height:
            return None  # not the next line down
        if prev is widest:
            continue  # it sets the width, so nothing fits after it: its break tells nothing
        per_char = (prev.x1 - prev.x0) / len(text)
        need = per_char * (1 + len(nxt[0])) + SET_LINE_ROOM * prev.size
        if (prev.x1 - prev.x0) + need > width:
            return None  # the next word would (nearly) fit: the line wrapped
    return [l.text.strip() for l in lines]


def at_text_x(x: float, text_x: float, size: float) -> bool:
    """Whether x sits at a list item's text x (see ALIGN_TOL)."""
    return abs(x - text_x) <= ALIGN_TOL * size


def wrap_offsets(lines: list[Line]) -> Counter[tuple[int, float]]:
    """Hanging indents, counted by (marker x, offset): the distance from a marker line to the
    line set right under it. A paragraph's first line just past a list's last item counts as a
    stray, so no single page is trusted: the document's counts decide (see text_offset)."""
    offsets: Counter[tuple[int, float]] = Counter()
    for prev, nxt in zip(lines, lines[1:]):
        if not prev.text.strip() or not nxt.text.strip() or marker_of(prev.text) is None:
            continue
        d = nxt.x0 - prev.x0
        if (HANGING_MIN * prev.size < d <= 3 * prev.size
                and 0 <= nxt.top - prev.top <= 2 * prev.height):
            offsets[(round(prev.x0), round(d * 2) / 2)] += 1
    return offsets


def text_offset(marker_x: float, counts: Counter[tuple[int, float]]) -> float | None:
    """How far a list item's text starts right of its marker at marker_x: the most common
    hanging indent of items with a marker at that x across the document, when MIN_WRAPS lines
    agree (one list keeps its tab stop); else the most common across all items; None when no
    item wraps anywhere."""
    at: Counter[float] = Counter()
    everyone: Counter[float] = Counter()
    for (x, d), n in counts.items():
        everyone[d] += n
        if x == round(marker_x):
            at[d] += n
    if at and at.most_common(1)[0][1] >= MIN_WRAPS:
        return at.most_common(1)[0][0]
    return everyone.most_common(1)[0][0] if everyone else None


def leaves_item(piece: Block, line: Line, text_x: float | None,
                 lines: Sequence[Line] = (), rhythm: Rhythm | None = None) -> bool:
    """Whether `line` is no longer the text of the list item `piece`. It stays when it sits at
    the item's text x, or when it starts in lowercase directly under the item and in the same
    column (a sentence the layout carried on). A legal item ("(2) at a place") also keeps a line
    that wraps back to the margin, left of its label, when the item's line above ran to the
    column's edge. Anything else (a first-line indent, a line left of the marker) starts the
    next paragraph. On a scan, a line at the page's leading (`rhythm`) is directly under."""
    last = piece.lines[-1]
    if text_x is not None and at_text_x(line.x0, text_x, piece.first.size):
        return False
    gap = line_gap(last, line, rhythm)
    under = (-last.height < gap <= 0.5 * last.size
             and line.x0 < last.x1 and last.x0 < line.x1)
    if line.text.strip()[:1].islower() and under:
        return False
    if (under and lines and legal(piece.marker) and line.x0 < piece.first.x0
            and line_fills(last, lines)):
        return False
    return True


def shares_column(a: Line, b: Line) -> bool:
    """Whether two lines overlap horizontally: the same column of text, not two columns."""
    return min(a.x1, b.x1) - max(a.x0, b.x0) > 0


def wraps_entry(prev: Line, line: Line, lines: Sequence[Line] | None = None) -> bool:
    """Whether the contents entry `line` (a title, its dot leader and a page label) is the rest of
    the title above it: `prev` has no page label of its own and is not a finished line, and
    `line` sits at its hanging indent just under it, in the same column. The title of a long
    index entry wraps onto a leader line ("Nonproliferation, Antiterrorism, Demining, and Related"
    / "    Programs ....777"), where the leader keeps the wrapped word from moving up.

    On a contents page (`lines`, the page's lines, are given) the indented entry under a line
    with no page label may be a group and its first entry instead ("Governmental Fund Financial
    Statements" / "    Balance Sheet ....26"): the title goes on only when `prev` stops where a
    phrase cannot ("... Balance Sheet" / "to the Government-Wide ...", "... Expenditures, and" /
    "Changes in ...") or runs to the edge of its column."""
    if not is_contents_entry(line.text) or is_contents_entry(prev.text):
        return False
    if DOT_LEADER.search(prev.text) or INDEX_REFERENCE.search(prev.text) \
            or prev.text.rstrip().endswith(TERMINAL):
        return False
    gap = line.top - prev.bottom
    if not (HANGING_MIN * prev.size < line.x0 - prev.x0 <= 3 * prev.size
            and -prev.height < gap <= 0.5 * prev.size and shares_column(prev, line)):
        return False
    return (lines is None or line.text.strip()[:1].islower() or ends_mid_phrase(prev.text)
            or line_fills(prev, lines))


WRAP_CONNECTIVES = frozenset({"AND", "OR", "OF", "THE", "FOR", "TO", "IN", "ON", "BY", "WITH",
                              "A", "AN", "AT", "FROM"})


def ends_mid_phrase(text: str) -> bool:
    """Whether a line stops where its phrase cannot: after a comma, semicolon, colon or hyphen,
    or on a connective ("AND", "OF", "THE") that needs a word after it."""
    t = text.rstrip()
    if not t:
        return False
    if t[-1] in ",;:-" + SOFT_HYPHEN:
        return True
    words = t.split()
    return words[-1].strip("()").upper() in WRAP_CONNECTIVES


def column_right(line: Line, lines: Sequence[Line]) -> float:
    """The right edge of the column `line` sits in: the furthest right of the lines beside it."""
    return max([l.x1 for l in lines if l is not line and shares_column(l, line)] or [line.x1])


def line_fills(line: Line, lines: Sequence[Line]) -> bool:
    """Whether a line runs to the edge of its column, so that the text went on to the next line
    rather than ending there."""
    return line.x1 >= column_right(line, lines) - 4 * line.size


def legal(m: Marker | None) -> bool:
    """A legal label, parenthesized as printed: "(a)", "(1)", "(A)", "(ii)". A worksheet line
    item has no marker on its block, so it is not one."""
    return m is not None and m.text.startswith("(") and m.text.endswith(")")
