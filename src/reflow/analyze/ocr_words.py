"""Two narrow OCR slips, and the reading the document's own evidence suggests for each.

OCR's reading stays the text: nothing here changes `node.text`. A suggestion is an `alt` on a
`doubt` mark, which the reader shows as a doubtful word, and a review item, which shows what OCR
read and what is proposed. Two rules, each fed only by what the same document prints:

  * split-word: two adjacent letter tokens neither of which the document prints anywhere else,
    whose join it prints at least twice ("Ge neral" → "General");
  * zip-digit: a letter among the digits of a ZIP code in a printed "City, ST 8011O" address
    slot ("8011O" → "80110").

No dictionary and no model: a word the document does not print is never suggested.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from reflow.extract.pages import PageContent
from reflow import geometry
from reflow.model import Document, Mark, Role, Source, is_ai_draft, mark_order

WORD = re.compile(r"[^\W\d_]+")

# A node text printed identically on this many pages is an undetected running head: it counts
# as one occurrence, not one per page.
REPEATED_PAGES = 3

# The two-letter codes a printed US address may carry: the states, DC and the USPS territories.
STATES = frozenset("""
    AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM
    NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC AS GU MP PR VI UM FM MH PW
""".split())
# "<letters>, <ST> <token>": the city ends in a letter and the comma is printed. The token is
# five digit-like characters, with an optional "-dddd" extension, at the end of its line.
ZIP_SLOT = re.compile(r"(?<=[^\W\d_]), (?P<state>[A-Z]{2}) "
                      r"(?P<zip>[0-9OoIl]{5}(?:-[0-9OoIl]{4})?)(?![\w-])")
DIGIT_FOR = {"O": "0", "o": "0", "I": "1", "l": "1"}
RULE_LABEL = {"zip-digit": "ZIP code digit", "split-word": "split word"}


@dataclass(frozen=True)
class Suggestion:
    start: int      # offsets in node.text
    end: int
    was: str        # node.text[start:end], OCR's reading
    now: str        # the suggested reading
    rule: str       # "zip-digit" | "split-word"


@dataclass
class Lexicon:
    """Where each word (casefolded) occurs: a set of (where, offset), `where` being the node, or
    the text itself for a node repeated on REPEATED_PAGES pages or more (so its copies are one
    occurrence)."""
    places: dict[str, set[tuple[object, int]]]
    repeated: frozenset[str]

    def where(self, node: object) -> object:
        text = getattr(node, "text", None) or ""
        return text if text in self.repeated else id(node)

    def count(self, word: str, *, excluding: tuple[object, int] | None = None) -> int:
        places = self.places.get(word.casefold(), set())
        n = len(places)
        if excluding is not None and (self.where(excluding[0]), excluding[1]) in places:
            n -= 1
        return n


def _evidence(nodes: Iterable) -> Iterable:
    """The nodes whose text is new evidence: not a contents entry, a context crumb or an AI
    draft (each skipped with its children), nor a figure's own text, which describes the figure
    rather than being printed (its children still count)."""
    stack = list(nodes)[::-1]
    while stack:
        node = stack.pop()
        attrs = node.attrs
        if (attrs.get("contents") or attrs.get("context")
                or node.source in (Source.LLM, Source.ASSIST)
                or is_ai_draft(node)):
            continue
        if node.role != Role.FIGURE:
            yield node
        stack.extend(reversed(node.children))


def document_lexicon(nodes: Iterable) -> Lexicon:
    """The word occurrences of `nodes` and their descendants, counted as independent evidence."""
    nodes = list(_evidence(nodes))
    pages: dict[str, set] = defaultdict(set)
    for node in nodes:
        if node.text:
            pages[node.text].add(node.page)
    repeated = frozenset(text for text, on in pages.items() if len(on) >= REPEATED_PAGES)
    lexicon = Lexicon(defaultdict(set), repeated)
    for node in nodes:
        where = lexicon.where(node)
        for m in WORD.finditer(node.text or ""):
            lexicon.places[m.group().casefold()].add((where, m.start()))
    lexicon.places = dict(lexicon.places)
    return lexicon


def _unread_inside(node, start: int, end: int) -> bool:
    return any(m.kind == "del-unread" and start <= m.start <= end for m in node.marks)


def _split_words(node, lexicon: Lexicon) -> list[Suggestion]:
    text = node.text or ""
    words = list(WORD.finditer(text))
    out = []
    for a, b in zip(words, words[1:]):
        if text[a.end():b.start()] != " ":
            continue
        # Whole tokens only: a digit touching either side makes it a code, not a split word.
        if (a.start() and text[a.start() - 1].isalnum()) or (b.end() < len(text)
                                                               and text[b.end()].isalnum()):
            continue
        if (lexicon.count(a.group(), excluding=(node, a.start())) == 0
                and lexicon.count(b.group(), excluding=(node, b.start())) == 0
                and lexicon.count(a.group() + b.group()) >= 2):
            out.append(Suggestion(a.start(), b.end(), text[a.start():b.end()],
                                  a.group() + b.group(), "split-word"))
    return out


def _line_ends(node) -> set[int]:
    """The offsets in node.text where a printed line ends: the text's end, and each line's end
    when the node keeps its lines (an address set line by line)."""
    text = node.text or ""
    ends = {len(text)}
    lines = node.attrs.get("lines")
    if lines and " ".join(lines) == text:
        at = 0
        for line in lines:
            at += len(line)
            ends.add(at)
            at += 1
    return ends


def _zip_digits(node) -> list[Suggestion]:
    text = node.text or ""
    ends = _line_ends(node)
    out = []
    for m in ZIP_SLOT.finditer(text):
        token = m.group("zip")
        if m.group("state") not in STATES or m.end("zip") not in ends:
            continue
        letters = [ch for ch in token if ch in DIGIT_FOR]
        if len(letters) != 1:
            continue
        now = "".join(DIGIT_FOR.get(ch, ch) for ch in token)
        out.append(Suggestion(m.start("zip"), m.end("zip"), token, now, "zip-digit"))
    return out


def suggestions(node, lexicon: Lexicon) -> list[Suggestion]:
    """The suggestions for one OCR node's text, in offset order. A token at a struck word OCR
    could not read (a del-unread mark) is never a candidate: there are no letters to go on."""
    found = _zip_digits(node) + _split_words(node, lexicon)
    found = [s for s in found if not _unread_inside(node, s.start, s.end)]
    kept: list[Suggestion] = []
    for s in sorted(found, key=lambda s: (s.start, -s.end)):
        if not kept or s.start >= kept[-1].end:
            kept.append(s)
    return kept


def _line_box(node, was: str, pages: Sequence[PageContent] | None) -> list[float]:
    """The box of the OCR line that holds `was`: the page's line whose centre is in the node's
    box and whose text contains it; the node's own box when no line does (or there are none)."""
    if pages and node.bbox:
        x0, top, x1, bottom = node.bbox
        for page in pages:
            if page.number != node.page:
                continue
            for line in page.lines:
                cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
                if x0 <= cx <= x1 and top <= cy <= bottom and was in line.text:
                    return geometry.rounded(line.bbox)
    return geometry.rounded(node.bbox) if node.bbox else []


def _place(node, s: Suggestion) -> None:
    """A `doubt` mark over the suggestion with `alt`. A doubt mark already on any of its words is
    widened to it: its part outside the range stays a doubt of its own, so no word is doubted
    twice."""
    text = node.text or ""
    kept, pieces = [], []
    for m in node.marks:
        if m.kind != "doubt" or m.end <= s.start or s.end <= m.start:
            kept.append(m)
            continue
        if m.start < s.start:
            pieces.append(Mark(m.start, s.start, "doubt", text[m.start:s.start]))
        if s.end < m.end:
            pieces.append(Mark(s.end, m.end, "doubt", text[s.end:m.end]))
    node.marks = sorted([*kept, *pieces, Mark(s.start, s.end, "doubt", s.was, s.now)],
                        key=mark_order)


def flag(doc: Document, pages: Sequence[PageContent] | None) -> int:
    """Mark every suggestion on the document's OCR nodes: a `doubt` mark with the suggestion as
    its `alt`, and an "ocr-suggestion" fact for the review queue. The text never changes. The
    pass is idempotent: run again, it finds the same ranges and sets the same marks and facts.
    Returns the number of suggestions."""
    lexicon = document_lexicon(doc.root.children)
    found = 0
    for node in doc.root.walk():
        if node.source != Source.OCR or not node.text:
            continue
        node.facts = [f for f in node.facts if f.code != "ocr-suggestion"]
        for s in suggestions(node, lexicon):
            _place(node, s)
            node.fact("ocr-suggestion", Source.OCR,
                      f"OCR read ‘{s.was}’; the document suggests ‘{s.now}’ ({RULE_LABEL[s.rule]})",
                      was=s.was, now=s.now, rule=s.rule, start=s.start, end=s.end,
                      line_box=_line_box(node, s.was, pages))
            found += 1
    return found
