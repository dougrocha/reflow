"""Text-level checks that a block's words were extracted whole.

Signals the layout engine cannot see from geometry, each recorded as a Fact on the node
so confidence and the review queue treat it like any other doubt:

  * "unfinished-sentence": a prose paragraph or list item whose sentence stops where the next
    block, set right below it, carries on: an unclosed parenthesis the next block closes, or an
    ending no sentence can have followed by a block that starts lowercase;
  * "literal-bullets": text that still carries bullet glyphs the list detection did not turn
    into list items (a box of examples read as one table cell);
  * "split-list": a list whose numbering runs on from the list just above it, with nothing but
    form fields between: "(a)" in one list, "(b)" and "(c)" in the next, so one list became two;
  * "empty-header-row": a table whose first row has no text in any cell, so the headers were
    lost or the table's real first row was taken for rules;
  * "dropped-ligature": a word in which the PDF draws an "f" as wide as a fi or fl ligature but
    the text layer kept only the "f" (reflow.extract measures this per font and size, or
    against the width the font declares). The word and its repairs go in the message. Reported
    once per page, on the first block affected, on every page but those reflow's own OCR wrote.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from reflow.extract.lines import SOFT_HYPHEN
from reflow import geometry
from reflow.analyze.blocks import follows, marker_of
from reflow.extract.ocrwords import GradedWord, PageWords
from reflow.ingest.ocr_trust import Grade
from reflow.model import (FACT_OCR_PAGE, BBox, Document, Fact, HEADING_ROLES, Mark, Node, Role,
                          Source, mark_order)
from reflow.text_quality import squash

BLOCKS = (Role.P, Role.LI)
# Words that cannot end a sentence: articles, prepositions, conjunctions, auxiliaries.
DANGLING = frozenset(
    "a an the of to in on at by for with from and or but nor as if that which is are was were "
    "be been being under per into onto about than see also not your our their its this these "
    "those whose whether between among through during".split())
MIN_WORDS = 5
# Tables and ledgers set in paragraphs: a fragment of one is not a broken sentence.
MAX_DIGIT_SHARE = 0.12
LEADERS = re.compile(r"\.{4,}|(?:\. ){4,}|_{3,}")
CONTINUATION_START = re.compile(r"[a-z]|[)\]”’,;:]")


# Bullet glyphs that start a list item when they open a line or follow a sentence.
BULLET = re.compile(r"(?:^|(?<=\s))[•◦▪●■‣▫○](?=\s+\S)")
BULLET_ROLES = (Role.P, Role.LI, Role.TD, Role.TH)


def literal_bullets(text: str) -> int:
    return len(BULLET.findall(text))


def empty_header_row(table: Node) -> bool:
    """Whether the first row of `table` has two or more cells, none with text, while a later
    row has text: a header row that holds nothing."""
    rows = [r for r in table.children if r.role == Role.TR]
    if len(rows) < 2:
        return False
    first = [c for c in rows[0].children if c.role in (Role.TD, Role.TH)]
    if len(first) < 2 or any((c.text or "").strip() for c in first):
        return False
    return any((c.text or "").strip() for r in rows[1:] for c in r.children)


def _marker(item: Node):
    printed = item.attrs.get("marker")
    return marker_of(printed + " ") if printed else None


def split_list_item(above: Node, below: Node) -> Node | None:
    """The first item of `below` when its numbering runs on from the last item of `above`, two
    ordered lists on one page that are one list, else None."""
    if above.page != below.page or not (above.attrs.get("ordered") and below.attrs.get("ordered")):
        return None
    items = [[i for i in lst.children if i.role == Role.LI] for lst in (above, below)]
    if not all(items):
        return None
    last, first = _marker(items[0][-1]), _marker(items[1][0])
    if last and first and last.kind in ("num", "alpha", "roman") and follows(last, first):
        return items[1][0]
    return None


def table_like(text: str) -> bool:
    """Whether `text` reads as a ledger row or a leader line rather than prose."""
    chars = [c for c in text if c.isalnum()]
    if not chars:
        return True
    if sum(c.isdigit() for c in chars) / len(chars) > MAX_DIGIT_SHARE:
        return True
    return bool(LEADERS.search(text))


def unfinished_reason(text: str, following: str = "") -> str | None:
    """Why `text` reads as cut off where `following` (the next block) carries on, or None."""
    text, following = squash(text), squash(following)
    words = text.split()
    if len(words) < MIN_WORDS or not following or table_like(text) or table_like(following):
        return None
    if text.count("(") > text.count(")"):
        close = following.find(")")
        if close >= 0 and "(" not in following[:close]:
            return "an opened parenthesis is closed only by the next block"
        return None
    last = words[-1]
    if not last.isalpha() or (last.isupper() and len(last) > 1):
        return None
    if words[-2][-1] in ";,:" or last.lower() not in DANGLING:
        return None   # "...withholding; or" opens the next list item, by design
    reason = f'ends on "{last}", which cannot end a sentence'
    if CONTINUATION_START.match(following):
        return reason
    # A cross-reference to a title ("See What Is" / "FATCA Reporting, later") is capitalised
    # on both sides of the break.
    if last[0].isupper() and words[-2][0].isupper() and following[0].isupper():
        return reason
    return None


def ligature_repairs(word: str, index: int | None = None) -> list[str]:
    """Spellings of `word` with the "i" or "l" of a fi or fl restored after an "f"."""
    spots = [index] if index is not None else [i for i, c in enumerate(word) if c == "f"]
    return [word[:i + 1] + letter + word[i + 1:] for i in spots for letter in "il"]


def _suspects(node: Node, pages: Sequence) -> list[str]:
    """The words of `node` that the extract layer measured as missing a ligature letter."""
    text = node.text or ""
    found: list[str] = []
    for page in pages:
        if page.number != node.page:
            continue
        for line in page.lines:
            if not line.ligature_words:
                continue
            if node.bbox:
                cx, cy = (line.x0 + line.x1) / 2, (line.top + line.bottom) / 2
                if not (node.bbox[0] <= cx <= node.bbox[2] and node.bbox[1] <= cy <= node.bbox[3]):
                    continue
            found += [w for w in line.ligature_words
                      if w not in found and re.search(rf"\b{re.escape(w)}\b", text)]
    return found


def flag_text(doc: Document, pages: Sequence | None = None) -> dict[str, int]:
    """Append the facts above to the blocks of `doc`. `pages` are the extract layer's
    PageContents (None: no ligature check). Idempotent. Returns the count per code."""
    counts = {"unfinished-sentence": 0, "dropped-ligature": 0, "literal-bullets": 0,
              "empty-header-row": 0, "split-list": 0}
    for node in doc.root.walk():
        above = None
        for child in node.children:
            if child.role == Role.FIELD:
                continue    # a checkbox set between the lists does not make them two
            if child.role == Role.LIST:
                item = split_list_item(above, child) if above else None
                if item and not any(f.code == "split-list" for f in item.facts):
                    item.facts.append(Fact("split-list", Source.HEURISTIC,
                                           "The numbering runs on from the list above, so this "
                                           "may be one list split in two"))
                    counts["split-list"] += 1
            above = child if child.role == Role.LIST else None
    for node in doc.root.walk():
        have = {f.code for f in node.facts}
        if node.source == Source.OCR or node.attrs.get("figure_label"):
            continue  # a figure's label is its figure's text, not a block of the flow
        if (node.role in BULLET_ROLES and node.text and "literal-bullets" not in have
                and (n := literal_bullets(node.text))):
            node.facts.append(Fact("literal-bullets", Source.HEURISTIC,
                                   f"{n} bullet glyph(s) in the text were not made a list",
                                   {"count": n}))
            counts["literal-bullets"] += 1
        if node.role == Role.TABLE and "empty-header-row" not in have and empty_header_row(node):
            node.facts.append(Fact("empty-header-row", Source.HEURISTIC,
                                   "The table's first row has no text in any cell"))
            counts["empty-header-row"] += 1
    blocks = [n for n in doc.root.walk() if n.role in BLOCKS and n.text and n.text.strip()
              and not n.attrs.get("figure_label")]
    prose = [n for n in blocks if n.source != Source.OCR]
    for i, node in enumerate(prose):
        have = {f.code for f in node.facts}
        following = prose[i + 1] if i + 1 < len(prose) else None
        if following and "unfinished-sentence" not in have and _follows_below(node, following):
            reason = unfinished_reason(node.text or "", following.text or "")
            if reason:
                node.facts.append(Fact("unfinished-sentence", Source.HEURISTIC,
                                       f"The sentence may continue in the next block: {reason}"))
                counts["unfinished-sentence"] += 1
    if not pages:
        return counts
    # The ligature check measures the glyph widths of the file's text layer. An invisible layer
    # someone else's OCR left, which reflow kept, has them too; the layer reflow's own OCR wrote
    # (ocr-page) sets its words in a glyphless font, whose widths measure nothing.
    written = {p.number for p in doc.pages if any(f.code == FACT_OCR_PAGE for f in p.facts)}
    seen_pages: set[int | None] = set()
    for node in blocks:
        if node.page in written:
            continue
        if any(f.code == "dropped-ligature" for f in node.facts):
            seen_pages.add(node.page)
            continue
        words = _suspects(node, pages)
        if words and node.page not in seen_pages:
            seen_pages.add(node.page)
            shown = "; ".join(f"{w} (for {' or '.join(ligature_repairs(w))})" for w in words[:3])
            node.facts.append(Fact("dropped-ligature", Source.HEURISTIC,
                                   f"An fi or fl ligature lost a letter in the text: {shown}",
                                   {"words": words}))
            counts["dropped-ligature"] += 1
    return counts


def _follows_below(node: Node, nxt: Node) -> bool:
    """Whether `nxt` sits on the same page, below `node` in the same column. A block that ends
    its page or its column legitimately continues in the next one."""
    if nxt.page != node.page:
        return False
    if not (node.bbox and nxt.bbox):
        return True
    overlap = min(node.bbox[2], nxt.bbox[2]) - max(node.bbox[0], nxt.bbox[0])
    narrower = min(node.bbox[2] - node.bbox[0], nxt.bbox[2] - nxt.bbox[0])
    return nxt.bbox[1] >= node.bbox[1] and overlap >= 0.5 * narrower


# -- OCR word grades on the tree -----------------------------------------------------------

@dataclass(frozen=True)
class OcrEvidence:
    """What the marks pass knows about each page's OCR, whatever engine built the tree."""
    words: dict[int, PageWords] = field(default_factory=dict)       # pages whose grades bound
    invisible: dict[int, list[BBox]] = field(default_factory=dict)  # invisible-text boxes
    ocr_pages: frozenset[int] = frozenset()                         # pages reflow OCR'd
    missing: dict[int, str] = field(default_factory=dict)           # lost grades: page → why


def ocr_evidence(run, pages: Sequence | None) -> OcrEvidence:
    """The evidence of a conversion: the grades bound in extraction (`pages`, the engine's
    PageContents) or, for an engine that reads no lines (Docling), bound here from the sidecar
    against the working copy's text layer; the invisible-text boxes; the pages with an ocr-page
    fact; and the pages reflow OCR'd with a grading engine whose grades are not usable."""
    from reflow.extract.ocrwords import bind_words, invisible_boxes, missing_grades
    from reflow.ingest.ocr_trust import read_sidecar

    done = list(run.ocr.done)
    read = read_sidecar(run.working) if done else None
    words: dict[int, PageWords] = {}
    invisible: dict[int, list[BBox]] = {}
    if pages is not None:
        for page in pages:
            if page.words is not None:
                words[page.number] = page.words
            invisible[page.number] = [line.bbox for line in page.lines if line.ocr]
    else:
        import pypdfium2 as pdfium
        with run.workers.pdfium:
            pdf = pdfium.PdfDocument(run.working)
            try:
                for i in range(len(pdf)):
                    page = pdf[i]
                    try:
                        if read is not None and (grade := read.pages.get(i + 1)) is not None:
                            words[i + 1] = bind_words(grade, page, None)
                        invisible[i + 1] = invisible_boxes(page)
                    finally:
                        page.close()
            finally:
                pdf.close()
    missing = (missing_grades(read, done) if read is not None and run.ocr.graded else {})
    ocr_pages = frozenset(p.number for p in run.doc.pages
                          if any(f.code == FACT_OCR_PAGE for f in p.facts))
    return OcrEvidence(words, invisible, ocr_pages, missing)


def ocr_origin(node: Node, evidence: OcrEvidence) -> bool:
    """Whether OCR read the node's text. A heuristic node knows: it is Source.OCR exactly when
    it holds an OCR line. Any other node (Docling's, hybrid's) is OCR when its page was OCR'd by
    reflow, or when at least half of its box lies under the page's invisible text (someone
    else's OCR layer)."""
    if node.source == Source.OCR:
        return True
    if node.source == Source.HEURISTIC or node.page is None:
        return False
    if node.page in evidence.ocr_pages:
        return True
    boxes = evidence.invisible.get(node.page) or ()
    if not (node.bbox and boxes and geometry.area(node.bbox)):
        return False
    # The words of a layer do not overlap one another, so their overlaps add up.
    return sum(geometry.overlap(node.bbox, b) for b in boxes) >= 0.5 * geometry.area(node.bbox)


LINE_REACH = 2.5   # how far below a struck line, in word heights, the passage's next line starts


def _within(before: GradedWord, word: GradedWord) -> bool:
    """Whether `word` lies inside `before`'s box on its line: Tesseract's second reading of the
    same ink (legislation p24 reads a struck line as one word and again word by word)."""
    (a0, a1, a2, a3), (b0, b1, b2, b3) = before.bbox, word.bbox
    return b1 < a3 and b3 > a1 and a0 - 1 <= b0 and b2 <= a2 + 1


Spans = dict[int, tuple[float, float]]   # Tesseract's line → how far across the page it runs


def _spans(page: PageWords) -> Spans:
    """Each Tesseract line's extent across the page, from all its words, read or not."""
    out: Spans = {}
    for w in page.words:
        x0, x1 = out.get(w.ocr_line, (w.bbox[0], w.bbox[2]))
        out[w.ocr_line] = (min(x0, w.bbox[0]), max(x1, w.bbox[2]))
    return out


def _continues(before: GradedWord | None, word: GradedWord, spans: Spans) -> bool:
    """Whether struck `word` goes on the passage whose last struck word is `before`: `word`
    lies inside `before`'s box (`_within`), or `before` does not close the passage with its "]"
    (`closes`, the measured bracket OCR misreads) and `word` follows it on its Tesseract line,
    past the words OCR lost, or starts Tesseract's next line, the line below it (a bill is
    double-spaced: legislation p24's lines are two word heights apart) and across the same
    stretch of the page (`spans`): a line of the column beside it does not overlap it.
    Tesseract's lines stop at a column's edge."""
    if before is None:
        return False
    if _within(before, word):
        return True
    if before.closes or before.layer_text.endswith("]"):
        return False
    (_, a1, a2, a3), (b0, b1, _, b3) = before.bbox, word.bbox
    if word.ocr_line == before.ocr_line:
        return b1 < a3 and b3 > a1 and b0 >= a2 - 1
    above, below = spans.get(before.ocr_line), spans.get(word.ocr_line)
    return (word.ocr_line == before.ocr_line + 1
            and 0 <= b1 - a3 <= LINE_REACH * max(a3 - a1, b3 - b1)
            and above is not None and below is not None
            and min(above[1], below[1]) > max(above[0], below[0]))


def _follow(before: GradedWord | None, word: GradedWord) -> GradedWord:
    """The last struck word of a passage after `word`: a second reading inside `before` leaves
    `before`, and its closing "]", in place."""
    return before if before is not None and _within(before, word) else word


def _find(text: str, word: str, start: int, taken: set[int] = frozenset()) -> int:
    """The offset of `word` as a whole token in `text` from `start`, not at a `taken` offset, or
    -1. A word that ends in a hyphen may run on into the next: it ended its line, and the lines
    are joined ("C.14L-" and "61" read back as "C.14L-61")."""
    at = text.find(word, start)
    run_on = word.endswith(("-", SOFT_HYPHEN))
    while at >= 0:
        end = at + len(word)
        if (at not in taken and (at == 0 or not text[at - 1].isalnum())
                and (end == len(text) or run_on or not text[end].isalnum())):
            return at
        at = text.find(word, at + 1)
    return -1


def _printed(word: GradedWord) -> str:
    """The word as the text layer reads it back: a ligature Tesseract wrote ("ﬂ") is its letters."""
    return unicodedata.normalize("NFKC", word.layer_text or word.text)


def _holds(node: Node, word: GradedWord) -> bool:
    """Whether the node prints the word: in its text, or in its printed marker ("(2)")."""
    printed = _printed(word)
    return (_find(node.text or "", printed, 0) >= 0
            or _find(str(node.attrs.get("marker") or ""), printed, 0) >= 0)


def _doubt_marks(node: Node, words: list[GradedWord]) -> tuple[list[Mark], list[str]]:
    """Doubt marks for the node's graded words, aligned to its text in reading order: every
    word kept in the layer advances through the text, so a repeated word is marked where it
    stands. A word of the node's printed marker is not in its text and gets no mark, nor does a
    kept layer's word that is doubtful only as `unconfirmed`: OCR was not unsure of it,
    and the lexical gate judges its line (text_quality.ocr_gate). A doubtful
    word the node does not print (an edit took it) is returned as lost, unless it has no letter
    or digit: extraction folds and drops stray quotes and bars, and nothing of it is shown."""
    text = node.text or ""
    marker = str(node.attrs.get("marker") or "")
    marks, lost, cursor = [], [], 0
    taken: set[int] = set()
    for word in words:
        printed = _printed(word)
        doubt = word.grade is Grade.DOUBT and not word.unconfirmed
        at = _find(text, printed, cursor, taken)
        if at < 0 and doubt:
            at = _find(text, printed, 0, taken)
        if at < 0:
            if (doubt and _find(marker, printed, 0) < 0
                    and any(c.isalnum() for c in printed)):
                lost.append(word.text)
            continue
        cursor = at + len(printed)
        taken.add(at)
        if doubt:
            marks.append(Mark(at, at + len(printed), "doubt", printed))
    return marks, lost


def _owners(nodes: list[Node], page: PageWords) -> dict[int, list[GradedWord]]:
    """id(node) → the words it owns, in page order: the words kept in the text layer and, on a
    tracked-change page, the struck words left out of it. A word belongs to one node: of the
    nodes whose box holds its centre, the smallest that prints it, else the smallest (where it
    is then lost). Boxes of OCR blocks overlap on a busy page, so a box alone does not say.
    On a tracked-change page a struck word in no node's box (a struck line no word of which is
    printed, or its closing "]") belongs to the passage the struck words before it open, which
    runs on, along its line and down to the next (`_continues`), to the next printed word or
    its "]": their node."""
    owned: dict[int, list[GradedWord]] = {}
    spans = _spans(page)
    passage: Node | None = None   # the node of the struck words just before, in page order
    before: GradedWord | None = None   # the last struck word of that passage
    for i, w in enumerate(page.words):
        if not w.struck and w.layer_text:   # a printed word ends the passage
            passage = None
        if not (i in page.matched or (page.tracked and w.struck)):
            continue
        around = sorted((n for n in nodes if geometry.centre_in(w.bbox, n.bbox)), key=lambda n: geometry.area(n.bbox))
        owner = next((n for n in around if _holds(n, w)), around[0] if around else None)
        if owner is None and page.tracked and w.struck:
            if passage is not None and _continues(before, w, spans):
                owner = passage
        elif i not in page.matched and w.layer_text:
            continue   # kept in the layer but bound to no line of it: its own place is unknown
        if w.struck:
            before = _follow(before, w) if owner is not None and owner is passage else w
            passage = owner
        if owner is not None:
            owned.setdefault(id(owner), []).append(w)
    return owned


def _omitted(words: PageWords) -> list[GradedWord]:
    """The page's dropped words but the struck words of a tracked-change page, which the marks
    pass reports itself ("struck-word-unread", "tracked-change-unplaced")."""
    return [w for w in words.omitted if not (words.tracked and w.struck)]


def _omitted_boxes(words: PageWords, omitted: Sequence[GradedWord]) -> list[BBox]:
    """The dropped words `omitted`, those within one read-line height of each other grouped into
    one box, top to bottom."""
    return geometry.group_boxes([w.bbox for w in omitted], words.read_height or 0.0)


# -- tracked changes ----------------------------------------------------------------------
#
# On a tracked-change page (PageWords.tracked) an underlined word is an insertion and a struck
# word a deletion. Words reach nodes by geometry, then one monotone alignment of the node's
# words to its final text says where each one is: a line index (GradedWord.line) names the
# extracted line, which layout may since have joined to a list marker or cut at a form caption.

def _reading(words: list[GradedWord]) -> list[GradedWord]:
    """The words as the text reads them: rows by vertical overlap, top to bottom, and left to
    right in a row."""
    rows: list[list[GradedWord]] = []
    for w in sorted(words, key=lambda w: (w.bbox[1], w.bbox[0])):
        mid = (w.bbox[1] + w.bbox[3]) / 2
        row = next((r for r in rows if r[0].bbox[1] <= mid <= r[0].bbox[3]), None)
        if row is None:
            rows.append([w])
        else:
            row.append(w)
    return [w for row in rows for w in sorted(row, key=lambda w: w.bbox[0])]


def _folded(text: str) -> tuple[str, list[int]]:
    """The non-space characters of `text` in NFKC, lower-cased, each with its index in `text`."""
    chars, origin = [], []
    for i, ch in enumerate(text):
        for low in unicodedata.normalize("NFKC", ch).lower():
            if not low.isspace():
                chars.append(low)
                origin.append(i)
    return "".join(chars), origin


def _align(text: str, words: list[GradedWord]) -> dict[int, list[int | None]]:
    """index in `words` → the offset in `text` of each character of the word's printed text
    (None for one the text does not hold), for the words kept in the layer that the text holds
    at least half of. One alignment of all the words' letters to the text, so a repeated word
    or a repeated "[]" resolves by its place."""
    stream, owner = [], []
    for k, w in enumerate(words):
        if w.layer_text:
            folded, origin = _folded(w.layer_text)
            stream.append(folded)
            owner += [(k, i) for i in origin]
    target, offsets = _folded(text)
    matcher = difflib.SequenceMatcher(None, "".join(stream), target, autojunk=False)
    found: dict[int, list[int | None]] = {}
    for a, b, size in matcher.get_matching_blocks():
        for n in range(size):
            k, i = owner[a + n]
            found.setdefault(k, [None] * len(words[k].layer_text))[i] = offsets[b + n]
    return {k: at for k, at in found.items()
            if sum(o is not None for o in at) * 2 >= len(_folded(words[k].layer_text)[0])}


def _span(at: list[int | None], i: int = 0, j: int | None = None) -> tuple[int, int] | None:
    """The text range of characters [i, j) of a word, from their aligned offsets."""
    held = [o for o in at[i:j] if o is not None]
    return (min(held), max(held) + 1) if held else None


def _runs(text: str, pieces: list[tuple[int, int]], kind: str) -> list[Mark]:
    """One mark per run of pieces with only spaces between them."""
    marks: list[Mark] = []
    for start, end in sorted(pieces):
        if marks and marks[-1].end <= start and not text[marks[-1].end:start].strip():
            start = marks.pop().start
        elif marks and start < marks[-1].end:
            start, end = marks[-1].start, max(end, marks.pop().end)
        marks.append(Mark(start, end, kind, text[start:end]))
    return marks


def _tracked_marks(node: Node, words: list[GradedWord], spans: Spans
                   ) -> tuple[list[Mark], list[GradedWord], list[GradedWord]]:
    """The `ins`, `del` and `del-unread` marks of a node on a tracked-change page, from the
    words it owns; the struck words it does not print, each a `del-unread` point; and every
    word it placed a mark for.

    A struck word whose layer keeps its brackets is a point between them; one with no brackets
    a point at its right-hand neighbour in its row (else after its left-hand one). A struck word
    with neither (a line of a struck passage no word of which is printed, or its lone "]") is
    the point of the struck word before it, when it goes on that word's passage (`_continues`):
    a passage of struck lines is one deletion."""
    text = node.text or ""
    order = _reading(words)
    at = _align(text, order)
    ins: list[tuple[int, int]] = []
    dels: list[tuple[int, int]] = []
    marks: list[Mark] = []
    unread: list[GradedWord] = []
    placed: list[GradedWord] = []
    passage: int | None = None   # the point of the struck word just before, in reading order
    before: GradedWord | None = None   # that struck word
    for k, w in enumerate(order):
        if not w.struck:
            passage = before = None
        printed = bool(w.layer_text) and w.grade is not Grade.UNREAD
        if w.struck and not printed:
            point = None
            if k in at:
                brackets = at[k]
                if w.layer_text.startswith("[") and brackets[0] is not None:
                    point = brackets[0] + 1
                elif w.layer_text.endswith("]") and brackets[-1] is not None:
                    point = brackets[-1]
            elif not w.layer_text:
                row = [j for j, o in enumerate(order)
                       if j in at and o.bbox[1] <= (w.bbox[1] + w.bbox[3]) / 2 <= o.bbox[3]]
                right = next((j for j in row if j > k), None)
                left = next((j for j in reversed(row) if j < k), None)
                if right is not None and (span := _span(at[right])):
                    point = span[0]
                elif left is not None and (span := _span(at[left])):
                    point = span[1]
            if point is not None:
                marks.append(Mark(point, point, "del-unread", ""))
            joined = point is None and passage is not None and _continues(before, w, spans)
            if joined:
                point = passage
            if point is not None:
                unread.append(w)
                placed.append(w)
            passage, before = point, (_follow(before, w) if joined else w)
            continue
        if not (w.struck or w.underlined or w.under) or k not in at:
            continue
        marked = False
        if w.underlined or w.under:
            i, j = w.under if w.under and len(w.layer_text) == len(w.text) else (0, None)
            if span := _span(at[k], i, j):
                ins.append(span)
                marked = True
        if w.struck and (span := _span(at[k])):
            dels.append(span)
            marked = True
        if marked:
            placed.append(w)
    return [*_runs(text, ins, "ins"), *_runs(text, dels, "del"), *marks], unread, placed


def _kind(word: GradedWord) -> str:
    if word.struck:
        return "del" if word.layer_text and word.grade is not Grade.UNREAD else "del-unread"
    return "ins"


MARKS_CODES = ("ocr-doubtful", "ocr-mark-lost", "struck-word-unread")
PAGE_CODES = ("ocr-words-omitted", "ocr-grades-unmatched", "tracked-change-unplaced")
TRACKED = ("ins", "del", "del-unread")


def flag_ocr_marks(doc: Document, evidence: OcrEvidence) -> dict[str, int]:
    """Put the OCR word grades on the tree, for every engine, right after the engine built it
    and before the title, the tags and the outline read it (evidence.witness):

      * `attrs["ocr"]` on every OCR-origin node (`ocr_origin`): "graded" when its page's grades
        bound and none failed to match inside its box, else "ungraded"; and `attrs["ocr_remnant"]`
        when it holds a word left alone on a mostly unreadable OCR line (GradedWord.remnant), and
        `attrs["ocr_unconfirmed"]` when it holds a kept layer's doubtful word that no reading
        confirms (GradedWord.unconfirmed), which takes no mark: node_gate reads its text;
      * a `doubt` mark on each word graded doubtful inside the node's box, with one
        "ocr-doubtful" fact per node; a doubtful word the text does not hold is named in an
        "ocr-mark-lost" fact instead of being dropped;
      * on a tracked-change page (PageWords.tracked), an `ins` mark on each underlined run,
        a `del` mark on each struck word it prints, and a `del-unread` point for each struck
        word it does not, with a "struck-word-unread" fact holding the word's box; and
        `attrs["ocr_struck"]` on any node whose kept words are at least half struck;
      * a heading that fails text_quality.node_gate is demoted to a paragraph (headings.demote):
        Docling's and hybrid's headings never met the gate, and a heuristic heading met it on its
        lines, whose words can differ from the ones its box holds where OCR lines overlap;
      * per page, "ocr-words-omitted" (an error) with the box of every word left out of the text
        layer that no unread figure shows (its centre in the figure's box: the figure's own
        error covers it), and "ocr-grades-unmatched" (a warning) when the grades did not bind
        ("partial"/"misaligned", with the unmatched boxes) or were lost after OCR ("absent",
        "corrupt", "stale", "no-page"); and "tracked-change-unplaced" (a warning) with the box
        and mark kind of every struck or underlined word of a tracked-change page that no node
        placed.

    Idempotent. Returns the count of each fact code added."""
    from reflow.analyze.headings import demote
    from reflow.text_quality import node_gate

    counts = dict.fromkeys(MARKS_CODES + PAGE_CODES + ("demoted",), 0)
    nodes = [n for n in doc.root.walk() if n.text and n.text.strip() and ocr_origin(n, evidence)]
    graded: dict[int, list[Node]] = {}
    for node in nodes:
        node.facts = [f for f in node.facts if f.code not in MARKS_CODES]
        node.marks = [m for m in node.marks
                      if (m.kind != "doubt" or m.alt) and m.kind not in TRACKED]
        page = evidence.words.get(node.page)
        ok = (page is not None and page.status != "misaligned" and node.bbox is not None
              and not any(geometry.intersects(node.bbox, b) for b in page.unmatched))
        node.attrs["ocr"] = "graded" if ok else "ungraded"
        if ok:
            graded.setdefault(node.page, []).append(node)
    owned = {key: words for number, members in graded.items()
             for key, words in _owners(members, evidence.words[number]).items()}
    placed: set[int] = set()
    spans = {number: _spans(evidence.words[number]) for number in graded}
    for node in nodes:
        node.attrs.pop("ocr_remnant", None)
        node.attrs.pop("ocr_struck", None)
        node.attrs.pop("ocr_unconfirmed", None)
        if node.attrs["ocr"] == "graded":
            words = owned.get(id(node), [])
            if any(w.remnant for w in words):
                node.attrs["ocr_remnant"] = True
            kept = [w for w in words if w.layer_text]
            if any(w.unconfirmed and w.grade is Grade.DOUBT for w in kept):
                node.attrs["ocr_unconfirmed"] = True
            if kept and sum(w.struck for w in kept) * 2 >= len(kept):
                node.attrs["ocr_struck"] = True
            if evidence.words[node.page].tracked:
                tracked, unread, done = _tracked_marks(node, words, spans[node.page])
                node.marks = sorted([*node.marks, *tracked], key=mark_order)
                placed.update(id(w) for w in done)
                for w in unread:
                    node.fact("struck-word-unread", Source.OCR,
                              "A struck word OCR could not read is not transcribed; see the "
                              "original page", box=geometry.rounded(w.bbox))
                    counts["struck-word-unread"] += 1
            marks, lost = _doubt_marks(node, kept)
            if marks:
                node.marks = sorted([*node.marks, *marks], key=mark_order)
                words = [m.text for m in marks]
                node.fact("ocr-doubtful", Source.OCR,
                          f"OCR is unsure of {len(words)} word(s): " + ", ".join(words[:5]),
                          words=words)
                counts["ocr-doubtful"] += 1
            if lost:
                node.fact("ocr-mark-lost", Source.OCR,
                          "OCR is unsure of words the text no longer holds: " + ", ".join(lost[:5]),
                          kind="doubt", words=lost)
                counts["ocr-mark-lost"] += 1
        if node.role in HEADING_ROLES and (reason := node_gate(node)):
            demote(node, reason)
            counts["demoted"] += 1
    crops: dict[int, list[BBox]] = {}
    for node in doc.root.walk():
        if node.role == Role.FIGURE and node.attrs.get("unread") and node.bbox is not None:
            crops.setdefault(node.page, []).append(node.bbox)
    for info in doc.pages:
        info.facts = [f for f in info.facts if f.code not in PAGE_CODES]
        page = evidence.words.get(info.number)
        omitted = [w for w in _omitted(page)
                   if not any(geometry.centre_in(w.bbox, box) for box in crops.get(info.number, ()))
                   ] if page is not None else []
        if page is not None and (boxes := _omitted_boxes(page, omitted)):
            count = len(omitted)
            info.facts.append(Fact(
                "ocr-words-omitted", Source.OCR,
                f"{count} word(s) OCR could not read are not shown; see the original page",
                {"count": count, "region_boxes": [v for box in boxes for v in box]}))
            counts["ocr-words-omitted"] += 1
        unplaced = [w for w in (page.words if page is not None and page.tracked else ())
                    if (w.struck or w.underlined or w.under) and id(w) not in placed]
        if unplaced:
            info.facts.append(Fact(
                "tracked-change-unplaced", Source.OCR,
                f"{len(unplaced)} struck or underlined word(s) could not be marked in the text; "
                "see the original page",
                {"region_boxes": [v for w in unplaced for v in geometry.rounded(w.bbox)],
                 "kinds": [_kind(w) for w in unplaced]}))
            counts["tracked-change-unplaced"] += 1
        if page is not None and page.status != "graded":
            info.facts.append(Fact(
                "ocr-grades-unmatched", Source.OCR,
                f"{len(page.unmatched)} OCR word(s) matched no text: the page's OCR text is "
                "checked by spelling only where they lie" if page.status == "partial" else
                "The OCR word grades do not match the page's text: its OCR text is checked by "
                "spelling only",
                {"reason": page.status,
                 "region_boxes": [round(v, 2) for box in page.unmatched for v in box]}))
            counts["ocr-grades-unmatched"] += 1
        elif (why := evidence.missing.get(info.number)) is not None:
            info.facts.append(Fact(
                "ocr-grades-unmatched", Source.OCR,
                f"The OCR word grades of this page are {why.replace('-', ' ')}: its OCR text is "
                "checked by spelling only", {"reason": why}))
            counts["ocr-grades-unmatched"] += 1
    return counts
