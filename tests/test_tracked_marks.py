"""Insertions and deletions on a tracked-change page: text_evidence.flag_ocr_marks places
the struck and underlined OCR words on the final text by geometry, whatever layout did to the
lines; and the review queue publishes every box it could not place."""

from dataclasses import replace

import pytest

from reflow.analyze import review
from reflow.analyze.lists import merge_list_markers
from reflow.analyze.text_evidence import OcrEvidence, flag_ocr_marks
from reflow.extract.lines import Line
from reflow.extract.ocrwords import GradedWord, PageWords
from reflow.ingest.ocr_trust import Grade
from reflow.model import Document, Mark, Node, PageInfo, Role, Source


def word(text, x, top=0.0, *, grade=Grade.READ, struck=False, under=False, part=None,
         layer=None, line=0, closes=False):
    """A graded word at x (10 points a letter, one row 20 high at `top`), kept in the layer
    unless UNREAD; `layer` overrides what the layer holds (a struck word's "[]"). `line` is both its
    text-layer line and its Tesseract line."""
    kept = text if grade is not Grade.UNREAD else ""
    return GradedWord((x, top, x + 10 * len(text), top + 20), text,
                      kept if layer is None else layer,
                      {Grade.READ: 90, Grade.DOUBT: 60, Grade.UNREAD: 30}[grade], grade, None,
                      1.0, struck, under, line, line, under=part, closes=closes)


def bill(*rows, tracked=True):
    """PageWords of rows of words, `rows` a list of [(text, options)], one row 30 points below
    the last, each word one space after the last. Returns the page and the words in order."""
    words = []
    for r, items in enumerate(rows):
        x = 0.0
        for text, opts in items:
            words.append(word(text, x, 30.0 * r, line=r, **opts))
            x += 10 * (len(text) + 1)
    page = PageWords("graded", tuple(words), 20.0, tracked=tracked,
                     matched=frozenset(i for i, w in enumerate(words) if w.layer_text))
    return page, words


def node(text, box, role=Role.P, source=Source.OCR):
    return Node(role, page=1, bbox=box, text=text, source=source)


def doc_of(*nodes):
    return Document(root=Node(Role.DOC, children=list(nodes)), pages=[PageInfo(1, 612, 792)],
                    meta={"lang": "en"})


def run(doc, page):
    return flag_ocr_marks(doc, OcrEvidence({1: page}, {}, frozenset({1}), {}))


def marks(n):
    """Each mark's kind, text and the six characters before it."""
    return [(m.kind, m.text, n.text[:m.start][-6:]) for m in n.marks]


STRUCK = {"grade": Grade.UNREAD, "struck": True, "layer": "[]"}


def test_a_struck_word_ocr_could_not_read_is_a_point_between_its_brackets():
    page, words = bill([("a", {}), ("[e¥]", STRUCK), ("statutory", {}), ("court", {})])
    para = node("a [] statutory court", (0, 0, 400, 20))
    doc = doc_of(para)
    counts = run(doc, page)
    assert [(m.start, m.end, m.kind) for m in para.marks] == [(3, 3, "del-unread")]
    [fact] = [f for f in para.facts if f.code == "struck-word-unread"]
    assert fact.data["box"] == list(words[1].bbox) and counts["struck-word-unread"] == 1
    assert "e¥" not in fact.detail
    assert not doc.pages[0].facts   # nothing omitted twice, nothing unplaced


def test_repeated_brackets_and_words_are_placed_by_position():
    page, _ = bill([("court,", {}), ("[e¥]", STRUCK), ("court", {}), ("[e¥]", STRUCK),
                    ("court", {"under": True}), ("court", {})])
    para = node("court, [] court [] court court", (0, 0, 600, 20))
    run(doc_of(para), page)
    assert [(m.kind, m.start, m.end) for m in para.marks] == [
        ("del-unread", 8, 8), ("del-unread", 17, 17), ("ins", 19, 24)]


def test_marks_follow_the_words_through_the_real_line_rewrites():
    """A list marker joined to its mate (lists.merge_list_markers) and a line cut at a form
    caption (Line.cut, as forms.split_captions does) leave every GradedWord.line stale; the
    words still reach the right node and offset by their boxes."""
    def line(x0, top, text):
        return Line(1, (x0, top, x0 + 10 * len(text), top + 20), text, 10, False, False, "C",
                    ocr=True)
    marker = line(0, 0, "1.")
    mate = line(30, 0, "court, a [] statutory county court, or business court")
    [joined] = merge_list_markers([marker, mate])
    caption = line(0, 60, "court [] court, a court")
    left, right = caption.cut(0, 8, strip=True), caption.cut(9, len(caption.text), strip=True)
    left = replace(left, bbox=(0, 60, 80, 80))
    right = replace(right, bbox=(90, 60, 230, 80))
    item = node(joined.text, joined.bbox, Role.LI)
    first, second = node(left.text, left.bbox), node(right.text, right.bbox)
    page, _ = bill(
        [("1.", {}), ("court,", {}), ("a", {}), ("[e¥]", STRUCK), ("statutory", {}),
         ("county", {}), ("court,", {"under": True, "part": (5, 6)}), ("or", {"under": True}),
         ("business", {"under": True}), ("court", {"under": True})],
        [],
        [("court", {}), ("[e¥]", STRUCK), ("court,", {}), ("a", {}), ("court", {"under": True})])
    # The bill() rows put row 0 at x 0 (the marker) and row 2 at top 60, as the lines are.
    page = replace(page, words=tuple(replace(w, line=7) for w in page.words))   # all stale
    doc = doc_of(item, first, second)
    run(doc, page)
    assert item.text == "1. court, a [] statutory county court, or business court"
    assert marks(item) == [("del-unread", "", "t, a ["), ("ins", ", or business court", " court")]
    assert marks(first) == [("del-unread", "", "ourt [")]
    assert marks(second) == [("ins", "court", "rt, a ")]
    assert not doc.pages[0].facts


def test_a_word_split_at_a_line_end_and_two_lines_joined_keep_their_marks():
    page, _ = bill([("district", {}), ("statu-", {"under": True})],
                   [("tory", {"under": True}), ("[e¥]", STRUCK), ("court", {})])
    para = node("district statutory [] court", (0, 0, 200, 50))
    run(doc_of(para), page)
    assert [(m.kind, m.text) for m in para.marks] == [("ins", "statutory"), ("del-unread", "")]
    assert para.marks[1].start == para.text.index("[") + 1


def test_a_struck_word_without_brackets_sits_by_its_right_hand_neighbour():
    page, words = bill([("court,", {}), ("e¥", {"grade": Grade.UNREAD, "struck": True}),
                        ("statutory", {}), ("court", {})])
    para = node("court, statutory court", (0, 0, 300, 20))
    doc = doc_of(para)
    run(doc, page)
    assert [(m.kind, m.start) for m in para.marks] == [("del-unread", 7)]
    assert [f.code for f in doc.pages[0].facts] == []   # not also an omitted word


def test_a_struck_word_read_confidently_is_printed_inside_a_deletion():
    page, _ = bill([("the", {}), ("former", {"struck": True}), ("law", {"struck": True}),
                    ("applies", {})])
    para = node("the former law applies", (0, 0, 300, 20))
    run(doc_of(para), page)
    assert [(m.kind, m.text) for m in para.marks] == [("del", "former law")]


def test_an_inserted_doubtful_word_is_inserted_and_doubtful():
    page, _ = bill([("the", {}), ("buisness", {"under": True, "grade": Grade.DOUBT}),
                    ("court", {"under": True})])
    para = node("the buisness court", (0, 0, 300, 20))
    run(doc_of(para), page)
    assert [(m.kind, m.text) for m in para.marks] == [("ins", "buisness court"),
                                                      ("doubt", "buisness")]


def test_a_page_that_fails_the_gate_gets_no_insertion_or_deletion():
    page, _ = bill([("a", {"under": True}), ("[e¥]", {"grade": Grade.DOUBT, "struck": True}),
                    ("court", {})], tracked=False)
    para = node("a [e¥] court", (0, 0, 300, 20))
    doc = doc_of(para)
    run(doc, page)
    assert [m.kind for m in para.marks] == ["doubt"]
    assert not [f for f in para.facts if f.code == "struck-word-unread"] and not doc.pages[0].facts


def test_the_same_text_marks_alike_on_a_heuristic_and_a_docling_node():
    page, _ = bill([("a", {"under": True}), ("[e¥]", STRUCK), ("court", {})])
    heuristic = node("a [] court", (0, 0, 300, 20))
    docling = node("a [] court", (0, 0, 300, 20), source=Source.DOCLING)
    run(doc_of(heuristic), page)
    run(doc_of(docling), page)
    assert heuristic.marks == docling.marks == [Mark(0, 1, "ins", "a"),
                                                Mark(3, 3, "del-unread", "")]


def test_running_twice_gives_the_same_marks_and_facts():
    page, _ = bill([("a", {"under": True}), ("[e¥]", STRUCK), ("court", {})])
    para = node("a [] court", (0, 0, 300, 20))
    doc = doc_of(para)
    run(doc, page)
    first = (list(para.marks), list(para.facts))
    run(doc, page)
    assert (para.marks, para.facts) == first


def test_an_edit_keeps_a_mapped_mark_and_reports_a_lost_one():
    page, _ = bill([("or", {"under": True}), ("the", {"under": True}), ("court", {"struck": True})])
    para = node("or  the court", (0, 0, 300, 20))
    run(doc_of(para), page)
    para.set_text("or the court")          # a join folds the double space inside the insertion
    assert [(m.kind, m.text) for m in para.marks] == [("ins", "or the"), ("del", "court")]
    assert not [f for f in para.facts if f.code == "ocr-mark-lost"]
    para.set_text("or the")                # an edit takes the whole deletion
    [lost] = [f for f in para.facts if f.code == "ocr-mark-lost"]
    assert (lost.data["kind"], lost.data["text"]) == ("del", "court")


def test_a_struck_or_underlined_word_no_node_places_is_a_page_target():
    """Outside every node, unmatched in the layer, or dropped with no neighbour: each box is a
    "tracked-change-unplaced" target of the page item, beside the omitted words' targets."""
    page, words = bill([("a", {"under": True}), ("[e¥]", STRUCK), ("court", {})],
                       [("xq#", {"grade": Grade.UNREAD})],
                       [("e¥", {"grade": Grade.UNREAD, "struck": True})])
    kept_struck = word("former", 400, 200, struck=True)
    page = replace(page, words=page.words + (kept_struck,), unmatched=(kept_struck.bbox,),
                   status="partial")
    para = node("a [] court", (0, 0, 300, 20))
    doc = doc_of(para)
    run(doc, page)
    [unplaced] = [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    assert unplaced.data["kinds"] == ["del-unread", "del"]
    items = review.queue(doc)
    [item] = [i for i in items if i["role"] == "page"]
    codes = [t["code"] for t in item["targets"]]
    assert codes.count("tracked-change-unplaced") == 2 and "ocr-words-omitted" in codes
    assert [t["box"] for t in item["targets"] if t["code"] == "tracked-change-unplaced"] == [
        list(words[4].bbox), list(kept_struck.bbox)]


def test_an_underlined_comma_the_text_lost_is_unplaced():
    page, words = bill([("court,", {"part": (5, 6)}), ("a", {})])
    para = node("court a", (0, 0, 300, 20))
    doc = doc_of(para)
    run(doc, page)
    assert not para.marks
    [unplaced] = [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    assert unplaced.data["kinds"] == ["ins"]


def test_every_deletion_box_is_a_target_of_its_node_also_once_collapsed():
    page, words = bill(
        [("a", {}), ("[e¥]", STRUCK), ("b", {}), ("[e¥]", STRUCK), ("c", {}), ("[e¥]", STRUCK),
         ("dcourt", {"grade": Grade.DOUBT}), ("x", {"under": True})],
        [("d", {}), ("[e¥]", STRUCK), ("e", {}), ("x", {"under": True})])
    one = node("a [] b [] c [] dcourt x", (0, 0, 600, 20))
    two = node("d [] e x", (0, 30, 600, 50))
    doc = doc_of(one, two)
    run(doc, page)
    struck = [list(w.bbox) for w in words if w.struck]
    first = review.item_for(one, [612, 792], one.facts[0], [])
    assert [t["box"] for t in first["targets"] if t["code"] == "struck-word-unread"] == struck[:3]
    assert "ocr-doubtful" in [f.code for f in one.facts]
    items = review.queue(doc)
    boxes = [t["box"] for i in items for t in i["targets"] if t["code"] == "struck-word-unread"]
    assert boxes == struck


def test_a_passage_of_struck_lines_is_one_deletion_at_its_bracket():
    """Legislation p24: a struck passage runs on over two lines no word of which is
    printed, to its lone "]". Every word of it sits at the point its "[" opens: no other mark,
    and nothing unplaced; each box OCR could not read stays a review target of the node."""
    gone = {"grade": Grade.UNREAD, "struck": True}
    page, words = bill([("appropriations.", {}), ("[The", {**gone, "layer": "["}),
                        ("division", gone)],
                       [("unless", gone), ("reauthorized", gone)],
                       [("additional", gone), ("]", {**gone, "layer": "]"})])
    para = node("appropriations. [", (0, 0, 300, 20))
    doc = doc_of(para)
    counts = run(doc, page)
    assert {(m.kind, m.start) for m in para.marks} == {("del-unread", 17)}
    assert not [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    boxes = [f.data["box"] for f in para.facts if f.code == "struck-word-unread"]
    assert boxes == [list(w.bbox) for w in words[1:]] and counts["struck-word-unread"] == 6


@pytest.mark.parametrize("x, top, line", [(450.0, 0.0, 9), (450.0, 30.0, 9), (450.0, 30.0, 1),
                                          (450.0, 600.0, 9)],
                         ids=["same-row-other-column", "next-row-other-column",
                              "next-tesseract-line-other-column", "rows-below"])
def test_a_struck_word_far_from_a_passage_is_not_part_of_it(x, top, line):
    """A struck passage runs on along its line, past a few lost words, and down to
    the next line in its column. An unread struck word across a gutter on its row, on the next
    row in another column (on Tesseract's next line too: its counter runs across the page), or
    rows below, belongs to no deletion and stays unplaced."""
    gone = {"grade": Grade.UNREAD, "struck": True}
    page, words = bill([("a", {}), ("[", {**gone, "layer": "["})])
    far = word("division", x, top, line=line, **gone)
    page = PageWords("graded", (*words, far), 20.0, tracked=True, matched=page.matched)
    para = node("a [", (0, 0, 100, 20))
    doc = doc_of(para)
    run(doc, page)
    [fact] = [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    assert fact.data["region_boxes"] == list(far.bbox)


def test_a_deletion_after_a_closed_passage_is_not_part_of_it():
    """A passage ends at its "]". A struck word after it, with no point of its own,
    is not placed at that passage's point."""
    gone = {"grade": Grade.UNREAD, "struck": True}
    page, words = bill([("a", {}), ("[x]", {**gone, "layer": "[]"})],
                   [("division", gone)])
    para = node("a []", (0, 0, 300, 20))
    doc = doc_of(para)
    run(doc, page)
    assert [m.start for m in para.marks] == [3]
    [fact] = [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    assert fact.data["region_boxes"] == list(words[2].bbox)


def test_a_second_reading_inside_a_struck_word_is_part_of_its_passage():
    """Legislation p24: Tesseract read a struck line twice, once as one word across it
    whose "]" closes the passage, and again word by word inside that box. The words inside it
    are the same ink: they are placed with the passage, not left unplaced, and they leave it
    closed."""
    gone = {"grade": Grade.UNREAD, "struck": True}
    page, words = bill([("a", {}), ("[x", {**gone, "layer": "["})],
                       [("fundedthrough]", {**gone, "layer": "]"})])
    inner = word("through]", 60.0, 30.0, line=1, **gone)
    # The second reading leaves the passage closed: a deletion on the next row is its own.
    after = word("other", 0.0, 60.0, line=2, **gone)
    page = PageWords("graded", (*words, inner, after), 20.0, tracked=True, matched=page.matched)
    para = node("a [", (0, 0, 100, 20))
    doc = doc_of(para)
    run(doc, page)
    [fact] = [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    assert fact.data["region_boxes"] == list(after.bbox)


def test_a_measured_closing_bracket_closes_its_passage():
    """The "]" a passage ends at keeps no text (OCR misreads it "R"); its measured
    flag closes the passage. It is placed with the passage, and a deletion on the row after it
    is its own."""
    gone = {"grade": Grade.UNREAD, "struck": True}
    page, words = bill([("a", {}), ("[x", {**gone, "layer": "["})],
                       [("R", {**gone, "layer": "", "closes": True})],
                       [("other", gone)])
    para = node("a [", (0, 0, 100, 20))
    doc = doc_of(para)
    run(doc, page)
    [fact] = [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    assert fact.data["region_boxes"] == list(words[3].bbox)


def test_a_passage_wraps_to_the_start_of_its_next_line():
    """A passage opened at the end of a short line runs on at the left margin of
    Tesseract's next line, left of its node's box: that line runs under the one above."""
    gone = {"grade": Grade.UNREAD, "struck": True}
    opener = [word("a", 200.0), word("[x", 220.0, **gone, layer="[")]
    more = [word("more", 0.0, 30.0, line=1, **gone), word("words", 210.0, 30.0, line=1, **gone)]
    page = PageWords("graded", (*opener, *more), 20.0, tracked=True, matched=frozenset({0, 1}))
    para = node("a [", (200, 0, 260, 20))
    doc = doc_of(para)
    run(doc, page)
    assert not [f for f in doc.pages[0].facts if f.code == "tracked-change-unplaced"]
    assert [m.kind for m in para.marks] == ["del-unread"]


def test_a_doubtful_word_that_ended_its_line_in_a_hyphen_is_marked_where_the_lines_joined():
    page, _ = bill([("Soun", {}), ("C.14L-", {"grade": Grade.DOUBT})], [("61", {}), ("x", {})])
    para = node("Soun C.14L-61 x", (0, 0, 400, 60))
    run(doc_of(para), page)
    assert [(m.kind, m.start, m.end) for m in para.marks] == [("doubt", 5, 11)]


def test_a_doubtful_word_is_not_marked_inside_a_longer_word():
    page, _ = bill([("Soun", {}), ("C.14L", {"grade": Grade.DOUBT}), ("x", {})])
    para = node("Soun C.14L61 x", (0, 0, 400, 30))
    run(doc_of(para), page)
    assert not [m for m in para.marks if m.kind == "doubt"]
