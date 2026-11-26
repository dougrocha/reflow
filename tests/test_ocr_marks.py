"""OCR word grades on the tree and in the outputs: text_evidence.flag_ocr_marks, the review
items of its page facts, the page note, data-source, the passages' doubtful ranges, the title."""

import json
import re

from reflow.analyze import review, title
from reflow.analyze.confidence import CODES
from reflow.analyze.sections import nest_sections
from reflow.analyze.text_evidence import OcrEvidence, flag_ocr_marks
from reflow.extract.ocrwords import GradedWord, PageWords
from reflow.ingest.ocr_trust import Grade
from reflow.model import Document, Fact, Node, PageInfo, Role, Source
from reflow.render import html as render
from reflow.render.chunks import ocr_note
from reflow.render.passages import passages
from reflow.render.site import render_html


def word(text, box, grade=Grade.READ, kept=True):
    return GradedWord(box, text, text if kept and grade is not Grade.UNREAD else "", 90 if grade
                      is Grade.READ else 60 if grade is Grade.DOUBT else 20, grade, None, 1.0)


def page_words(*words, status="graded", unmatched=()):
    return PageWords(status, words, 10.0, unmatched,
                     matched=frozenset(i for i, w in enumerate(words) if w.grade is not Grade.UNREAD))


def node(text, box, role=Role.P, source=Source.HEURISTIC, page=1, **attrs):
    return Node(role, page=page, bbox=box, text=text, attrs=attrs, source=source)


def doc_of(*nodes, pages=1, facts=()):
    infos = [PageInfo(n, 612, 792) for n in range(1, pages + 1)]
    infos[0].facts.extend(facts)
    return Document(root=Node(Role.DOC, children=list(nodes)), pages=infos, meta={"lang": "en"})


def evidence(words=None, ocr_pages=(1,), missing=None):
    return OcrEvidence(words or {}, {}, frozenset(ocr_pages), missing or {})


def test_a_doubtful_word_is_marked_where_it_stands_among_repeats():
    para = node("the fee is the fee", (0, 0, 300, 20), source=Source.OCR)
    words = page_words(word("the", (0, 0, 20, 20)), word("fee", (25, 0, 45, 20)),
                       word("is", (50, 0, 60, 20)), word("the", (65, 0, 85, 20)),
                       word("fee", (90, 0, 110, 20), Grade.DOUBT))
    doc = doc_of(para)
    flag_ocr_marks(doc, evidence({1: words}))
    assert [(m.start, m.end, m.kind) for m in para.marks] == [(15, 18, "doubt")]
    assert para.attrs["ocr"] == "graded"
    assert [f.code for f in para.facts] == ["ocr-doubtful"]


def test_a_word_belongs_to_the_smallest_node_that_prints_it():
    """OCR blocks overlap on a busy page: the wide one does not lose the narrow one's word."""
    wide = node("7 Floyel", (0, 0, 600, 40), source=Source.OCR)
    narrow = node("FAM,", (400, 0, 440, 20), source=Source.OCR)
    words = page_words(word("7", (0, 20, 10, 40), Grade.DOUBT), word("Floyel", (20, 20, 80, 40)),
                       word("FAM,", (405, 0, 435, 20), Grade.DOUBT))
    doc = doc_of(wide, narrow)
    flag_ocr_marks(doc, evidence({1: words}))
    assert [m.text for m in wide.marks] == ["7"] and [m.text for m in narrow.marks] == ["FAM,"]
    assert not [f for n in (wide, narrow) for f in n.facts if f.code == "ocr-mark-lost"]


def test_a_marker_and_stray_punctuation_are_not_lost_a_word_is():
    item = node("a judge of a county court;", (0, 0, 300, 20), Role.LI, Source.OCR, marker="(2)")
    gone = node("Family Residence", (0, 30, 300, 50), source=Source.OCR)
    words = page_words(word("(2)", (0, 0, 15, 20), Grade.DOUBT), word("judge", (30, 0, 60, 20)),
                       word("’", (0, 30, 5, 50), Grade.DOUBT),
                       word("Famity", (10, 30, 60, 50), Grade.DOUBT))
    doc = doc_of(item, gone)
    flag_ocr_marks(doc, evidence({1: words}))
    assert not item.marks and not item.facts
    [lost] = [f for f in gone.facts if f.code == "ocr-mark-lost"]
    assert lost.data["words"] == ["Famity"]


def test_docling_and_heuristic_headings_with_a_doubtful_word_become_paragraphs():
    docling = node("Ge neral Manager", (0, 0, 300, 20), Role.H2, Source.DOCLING, style_level=2)
    heuristic = node("SINGLE Famity", (0, 40, 300, 60), Role.H2, Source.OCR)
    clean = node("Budget Summary", (0, 80, 300, 100), Role.H2, Source.OCR)
    words = page_words(word("Ge", (0, 0, 20, 20), Grade.DOUBT), word("neral", (25, 0, 60, 20)),
                       word("Manager", (65, 0, 120, 20)), word("SINGLE", (0, 40, 60, 60)),
                       word("Famity", (70, 40, 130, 60), Grade.DOUBT),
                       word("Budget", (0, 80, 60, 100)), word("Summary", (70, 80, 140, 100)))
    doc = doc_of(docling, heuristic, clean)
    counts = flag_ocr_marks(doc, evidence({1: words}))
    assert (docling.role, heuristic.role, clean.role) == (Role.P, Role.P, Role.H2)
    assert "style_level" not in docling.attrs and counts["demoted"] == 2
    assert docling.attrs["why"][-1] == "paragraph: OCR is unsure of the word ‘Ge’"


def test_an_ungraded_docling_heading_meets_the_lexical_gate():
    junk = node("b LR SR S", (0, 0, 300, 20), Role.H2, Source.DOCLING)
    fine = node("Part A", (0, 40, 300, 60), Role.H2, Source.DOCLING)
    born = node("b LR SR S", (0, 0, 300, 20), Role.H2, Source.DOCLING, page=2)
    doc = doc_of(junk, fine, born, pages=2)
    flag_ocr_marks(doc, evidence(ocr_pages=(1,)))
    assert junk.attrs["ocr"] == "ungraded" and junk.role == Role.P
    assert fine.role == Role.H2 and born.role == Role.H2 and "ocr" not in born.attrs


def test_page_facts_for_omitted_words_and_unmatched_grades_and_idempotence():
    para = node("kept", (0, 0, 300, 20), source=Source.OCR)
    words = page_words(word("kept", (0, 0, 30, 20)), word("Sara", (0, 100, 40, 120), Grade.UNREAD),
                       word("Diwiw", (45, 100, 90, 125), Grade.UNREAD),
                       word("far", (400, 600, 430, 620), Grade.UNREAD),
                       status="partial", unmatched=((200, 200, 240, 220),))
    doc = doc_of(para, pages=2)
    ev = evidence({1: words}, ocr_pages=(1, 2), missing={2: "stale"})
    flag_ocr_marks(doc, ev)
    flag_ocr_marks(doc, ev)
    facts = {f.code: f for f in doc.pages[0].facts}
    assert [f.code for f in doc.pages[0].facts] == ["ocr-words-omitted", "ocr-grades-unmatched"]
    assert facts["ocr-words-omitted"].data == {
        "count": 3, "region_boxes": [0, 100, 90, 125, 400, 600, 430, 620]}
    assert facts["ocr-grades-unmatched"].data["reason"] == "partial"
    [stale] = doc.pages[1].facts
    assert (stale.code, stale.data) == ("ocr-grades-unmatched", {"reason": "stale"})


def test_review_gathers_every_box_of_the_page_facts():
    page = PageInfo(1, 612, 792, facts=[
        Fact("ocr-words-omitted", Source.OCR, "", {"count": 2, "region_boxes": [0, 0, 10, 10,
                                                                                20, 20, 30, 30]}),
        Fact("ocr-grades-unmatched", Source.OCR, "", {"reason": "partial",
                                                      "region_boxes": [40, 40, 50, 50]})])
    doc = Document(root=Node(Role.DOC, children=[node("x", (0, 0, 10, 10))]), pages=[page])
    [item] = [i for i in review.queue(doc) if i.get("kind") == "page" or i.get("node") is None]
    assert [t["code"] for t in item["targets"]] == ["ocr-words-omitted", "ocr-words-omitted",
                                                    "ocr-grades-unmatched"]
    assert item["bbox"] == [0, 0, 50, 50]
    absent = Document(root=Node(Role.DOC, children=[node("x", (0, 0, 10, 10))]), pages=[
        PageInfo(1, 612, 792, facts=[Fact("ocr-grades-unmatched", Source.OCR, "",
                                          {"reason": "absent"})])])
    [item] = [i for i in review.queue(absent) if i.get("node") is None]
    assert item["bbox"] is None


def test_the_codes_are_registered_with_their_scope():
    assert CODES["ocr-doubtful"].severity == "warning"
    assert (CODES["ocr-words-omitted"].severity, CODES["ocr-words-omitted"].scope) == ("error", "page")
    assert CODES["ocr-grades-unmatched"].scope == "page"
    assert "ocr-mark-lost" in CODES


def test_ocr_text_is_traced_and_born_digital_text_is_not():
    renderer = render.NodeRenderer({}, {})
    ocr = node("Hello", (0, 0, 10, 10), ocr="graded")
    born = node("Hello", (0, 0, 10, 10))
    assert 'data-source="ocr"' in str(renderer.render(ocr))
    assert "data-source" not in str(renderer.render(born))
    heading = node("Budget", (0, 0, 10, 10), Role.H2, Source.OCR)
    assert 'data-source="ocr"' in str(renderer.render(heading))


def test_the_page_note_says_what_ocr_did():
    ocr_page = Fact("ocr-page", Source.OCR, "", {"reason": "unreadable-text"})
    band = Fact("ocr-page", Source.OCR, "", {"reason": "textless-band"})
    omitted = Fact("ocr-words-omitted", Source.OCR, "", {"count": 3, "region_boxes": []})
    one = Fact("ocr-words-omitted", Source.OCR, "", {"count": 1, "region_boxes": []})
    note = ocr_note(PageInfo(1, 1, 1, facts=[ocr_page, omitted]), True)
    assert note == ('<p class="ocr-note" role="note" data-generated>Text on this page was read '
                    'by OCR. 3 words it could not read are not shown; see the original page.</p>')
    assert "1 word it could not read is not shown" in ocr_note(
        PageInfo(1, 1, 1, facts=[ocr_page, one]), True)
    assert "Part of the text" in ocr_note(PageInfo(1, 1, 1, facts=[band]), True)
    assert "comes from an OCR layer in the PDF and was not checked" in ocr_note(
        PageInfo(1, 1, 1), True)
    assert ocr_note(PageInfo(1, 1, 1), False) == ""


def test_the_note_follows_the_page_label(tmp_path):
    doc = Document(root=Node(Role.DOC, children=nest_sections([
        node("Read by OCR", (72, 100, 300, 112), source=Source.OCR, ocr="graded")])),
        pages=[PageInfo(1, 612, 792, facts=[Fact("ocr-page", Source.OCR, "", {})])],
        meta={"lang": "en"})
    render_html(doc, "T", tmp_path, page_widths=(800,))
    chunk = (tmp_path / "chunks/0001.html").read_text()
    label = chunk.index('class="pg-label"')
    note = chunk.index('class="ocr-note"')
    assert label < note < chunk.index("Read by OCR")
    assert re.search(r'<p class="ocr-note" role="note" data-generated>', chunk)


def test_a_passage_carries_its_doubtful_ranges():
    para = node("The fee is $25", (0, 0, 300, 20), source=Source.OCR, ocr="graded")
    clean = node("No doubt here", (0, 30, 300, 50), source=Source.OCR, ocr="graded")
    doc = doc_of(para, clean)
    words = page_words(word("The", (0, 0, 20, 20)), word("fee", (25, 0, 45, 20), Grade.DOUBT),
                       word("No", (0, 30, 20, 50)))
    flag_ocr_marks(doc, evidence({1: words}))
    first, second = passages(doc)
    assert first.doubt == ((4, 7),) and first.text[4:7] == "fee"
    assert json.loads(first.to_line())["u"] == [[4, 7]]
    assert "u" not in json.loads(second.to_line())


def test_a_doubtful_lead_is_no_title_and_an_ocr_lead_makes_the_candidate_ocr():
    lead = node("Report for", (0, 0, 300, 20), source=Source.OCR, ocr="graded")
    heading = node("Annual Budget", (0, 30, 300, 60), Role.H1, ocr="graded", font_size=24)
    doc = doc_of(lead, heading)
    words = page_words(word("Report", (0, 0, 60, 20)), word("for", (65, 0, 90, 20), Grade.DOUBT))
    flag_ocr_marks(doc, evidence({1: words}))
    joined, alone = [c for c in title.candidates(doc.root.children, None, None, "doc")
                     if c.origin == "heading"]
    assert joined.ocr and joined.doubtful == "OCR is unsure of the word ‘for’"
    assert title.score(joined, [])[1] is not None
    assert alone.ocr and alone.doubtful is None


def test_a_ligature_ocr_wrote_is_marked_on_its_letters():
    para = node("Hflvéflfmfifi", (0, 0, 300, 20), source=Source.OCR)
    doc = doc_of(para)
    flag_ocr_marks(doc, evidence({1: page_words(word("Hﬂvéﬂfmﬁﬁ", (0, 0, 100, 20), Grade.DOUBT))}))
    assert [m.text for m in para.marks] == ["Hflvéflfmfifi"] and not [
        f for f in para.facts if f.code == "ocr-mark-lost"]


def test_a_heading_that_holds_a_remnant_word_becomes_a_paragraph():
    from dataclasses import replace
    signature = node("—JohnSon", (0, 0, 300, 20), Role.H2, Source.DOCLING)
    words = page_words(replace(word("—JohnSon", (0, 0, 80, 20)), remnant=True))
    doc = doc_of(signature)
    flag_ocr_marks(doc, evidence({1: words}))
    assert signature.role == Role.P and signature.attrs["ocr_remnant"] is True
    assert signature.attrs["why"][-1] == "paragraph: most of its OCR line could not be read"


def test_an_omitted_word_a_crop_shows_leaves_the_page_error_and_one_outside_stays():
    # Every omitted word is either in an unread figure (whose own error covers it) or listed in
    # its page's ocr-words-omitted, never neither.
    crop = Node(Role.FIGURE, page=1, bbox=(0, 90, 100, 130), attrs={"unread": True})
    words = page_words(word("Sara", (0, 100, 40, 120), Grade.UNREAD),
                       word("far", (400, 600, 430, 620), Grade.UNREAD))
    doc = doc_of(crop)
    flag_ocr_marks(doc, evidence({1: words}))
    [omitted] = [f for f in doc.pages[0].facts if f.code == "ocr-words-omitted"]
    assert omitted.data == {"count": 1, "region_boxes": [400, 600, 430, 620]}
    crop.attrs["unread"] = False   # any other figure covers nothing
    flag_ocr_marks(doc, evidence({1: words}))
    [omitted] = [f for f in doc.pages[0].facts if f.code == "ocr-words-omitted"]
    assert omitted.data["count"] == 2


def test_a_heading_of_kept_layer_words_nothing_confirms_goes_through_the_lexical_gate():
    # A kept layer's unconfirmed words get no doubt mark, so a heading made
    # of them ("El B o") passed node_gate on any engine that hands the tree nodes. Its text is
    # judged as an ungraded layer's was; a heading of real words keeps its role.
    from dataclasses import replace
    junk = node("El B o", (0, 0, 300, 20), Role.H2, Source.DOCLING)
    fine = node("Operating Budget", (0, 40, 300, 60), Role.H2, Source.DOCLING)
    words = page_words(*(replace(word(t, (40 * i, 0, 40 * i + 30, 20), Grade.DOUBT), unconfirmed=True)
                         for i, t in enumerate(("El", "B", "o"))),
                       *(replace(word(t, (100 * i, 40, 100 * i + 90, 60), Grade.DOUBT), unconfirmed=True)
                         for i, t in enumerate(("Operating", "Budget"))))
    doc = doc_of(junk, fine)
    flag_ocr_marks(doc, evidence({1: words}))
    assert junk.role == Role.P and junk.attrs["ocr_unconfirmed"] is True and not junk.marks
    assert fine.role == Role.H2
