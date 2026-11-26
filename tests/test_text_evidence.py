from reflow.analyze.review import queue
from reflow.analyze.text_evidence import (flag_text, ligature_repairs, table_like,
                                          unfinished_reason)
from reflow.extract.chars import Char
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.text import _dropped_ligature_words, _wide_f
from reflow.model import Document, Fact, Node, PageInfo, Role, Source


def _doc(*blocks: Node) -> Document:
    doc = Document(Node(Role.DOC), pages=[PageInfo(1, 612, 792), PageInfo(2, 612, 792)])
    doc.meta["lang"] = "en"
    doc.root.children = list(blocks)
    return doc


def _p(text: str, role: Role = Role.P, **kw) -> Node:
    return Node(role, page=kw.pop("page", 1), text=text, confidence=0.7, **kw)


def test_open_parenthesis_continued_by_the_next_block():
    assert unfinished_reason("You do not certify your TIN when required (see the instructions for",
                             "Part II for details);")
    assert unfinished_reason("You do not certify your TIN when required (see the instructions for",
                             "Part II (again) for details") is None


def test_dangling_ending_needs_a_continuation_start():
    assert unfinished_reason("The partnership must withhold tax on income of the", "foreign partner.")
    assert unfinished_reason("The partnership must withhold tax on income of the",
                             "Payments are due.") is None
    # a cross-reference title is capitalised on both sides of the break
    assert unfinished_reason("Certify that the code is correct. See What Is",
                             "FATCA Reporting, later, for further information.")


def test_finished_or_short_endings_are_left_alone():
    assert unfinished_reason("Certify that you are not subject to backup withholding; or",
                             "claim exemption") is None
    assert unfinished_reason("Send it to the IRS", "and wait") is None
    assert unfinished_reason("Name of the", "applicant") is None   # four words: a label


def test_table_like_numeric_fragments_are_not_broken_sentences():
    row = "Salaries and expenses 1,204 3,455 (12) 4,601 6,002 for the"
    assert table_like(row)
    assert unfinished_reason(row, "of the Department of Commerce") is None
    assert table_like("Administrative provisions ........ 15 and the")
    assert not table_like("The partnership must withhold tax on income of the")


def test_only_prose_blocks_are_checked():
    heading = _p("Section 12 of the Internal Revenue Code and the", Role.H3)
    after = _p("foreign partner pays.")
    assert flag_text(_doc(heading, after))["unfinished-sentence"] == 0


def test_unfinished_block_is_a_review_item_only_when_it_continues_below_in_its_column():
    broken = _p("Claim it when required (see the instructions for", Role.LI,
                bbox=(50, 100, 300, 112))
    rest = _p("Part II for details);", bbox=(50, 114, 300, 126))
    wrap = _p("Payments are due in the next calendar year of the", bbox=(50, 700, 280, 720))
    column = _p("year after filing.", bbox=(320, 90, 560, 110))
    doc = _doc(broken, rest, wrap, column)
    counts = flag_text(doc)
    assert (counts["unfinished-sentence"], counts["dropped-ligature"]) == (1, 0)
    assert flag_text(doc)["unfinished-sentence"] == 0   # idempotent
    items = queue(doc)
    assert [(i["warn"], i["id"] == broken.id) for i in items] == [("unfinished-sentence", True)]


def _chars(text: str, widths: dict[str, float], run: int | None = 1) -> list[Char]:
    """Characters of one text object (`run`), 9 pt Serif, 3 pt wide unless `widths` says."""
    out, x = [], 0.0
    for i, ch in enumerate(text):
        w = widths.get(ch, 3.0)
        out.append(Char(ch, x, 0, x + w, 9, 9.0, False, False, "Serif", ch == " ", False,
                         False, run))
        x += w
    return out


def test_wide_f_marks_only_an_f_wider_than_the_fonts_usual_f():
    usual = _chars("of if off often after of fee ", {"f": 3.6})
    chars = usual + _chars("specifcally", {"f": 5.0})
    wide = _wide_f(chars)
    assert len(wide) == 1
    line = [c for c in chars if c.ch != " "][-11:]
    line[0].space_before = True
    assert _dropped_ligature_words(line, wide) == ("specifcally",)


def test_an_intact_fl_ligature_is_not_a_dropped_one():
    # pdfium reads an intact ligature as two characters in the glyph's one box: the "l" of
    # "flexibility" is in the wide "f"'s box. A dropped "i" leaves the next letter in its own box.
    usual = _chars("of if off often after of fee ", {"f": 3.6})
    intact = _chars("flexibility", {"f": 6.0})
    intact[1].x0, intact[1].x1 = intact[0].x0, intact[0].x1
    dropped = _chars("flled", {"f": 6.0})
    chars = usual + intact + dropped
    chars[len(usual)].space_before = chars[len(usual) + len(intact)].space_before = True
    wide = _wide_f(chars)
    assert len(wide) == 2
    assert _dropped_ligature_words(chars[len(usual):len(usual) + len(intact)], wide) == ()
    assert _dropped_ligature_words(chars[len(usual) + len(intact):], wide) == ("flled",)


DECLARED = {"f": 278.0, **{ch: 333.0 for ch in "ocedlh"}}   # per 1000 em: at 9 pt, 2.5 and 3.0


def _declared(font: str) -> dict[str, float] | None:
    return DECLARED if font == "Serif" else None


def test_a_font_with_too_few_fs_is_measured_against_the_width_it_declares():
    # Two plain f's and two ligature glyphs read as "f": no usual width to learn (#342). The
    # font declares 278 per 1000 em for "f" (Helvetica); at 9 pt that is 2.5 pt. Its other
    # letters (eight or more: CALIBRATE_MIN) are drawn as declared, 3.0 pt.
    chars = (_chars("of ", {"f": 2.5}) + _chars("offce ", {"f": 2.5}) + _chars("fled ", {"f": 4.5})
             + _chars("held", {}))
    chars[2 + 2].x0, chars[2 + 2].x1 = chars[2 + 1].x1, chars[2 + 1].x1 + 4.5   # the fi of "offce"
    assert _wide_f(chars) == set()                                     # nothing declared
    wide = _wide_f(chars, _declared)
    assert [c.ch for c in chars if id(c) in wide] == ["f", "f"]
    assert all(c.x1 - c.x0 == 4.5 for c in chars if id(c) in wide)
    assert _wide_f(chars, lambda font: None) == set()
    # An f a quarter wider than declared is an overhang, not a ligature (fw9's italic f).
    assert _wide_f(_chars("of held deed", {"f": 3.2}), _declared) == set()
    # An italic f's hook overhangs its advance: italics are never measured against it.
    for c in chars:
        c.italic = True
    assert _wide_f(chars, _declared) == set()


def test_declared_widths_are_measured_at_the_scale_the_fonts_letters_are_drawn():
    # Horizontal scaling (Tz 250) widens every glyph's drawn box and not its size: the f's
    # are 2.5 times the declared width, and so is every other letter, so none is wide.
    chars = _chars("of the deed held", {"f": 2.5 * 2.5})
    for c in chars:
        c.x1 = c.x0 + (c.x1 - c.x0) * (1.0 if c.ch == "f" else 2.5)
    assert _wide_f(chars, _declared) == set()
    # Too few letters to fix the scale: nothing is measured against a declared width.
    assert _wide_f(_chars("of fled", {"f": 4.5}), _declared) == set()
    # One text object set with its own Tz beside the page's text at the same drawn size: its
    # letters are not at the page's scale, so its f is not measured (a second case).
    page = _chars("the deed held the clerk", {})
    stretched = _chars("of", {"f": 2.5 * 2.5}, run=2)
    stretched[0].x1 = stretched[0].x0 + 3.0 * 2.5
    assert _wide_f(page + stretched, _declared) == set()
    # A glyph shown by an operator of its own says nothing about its scale either.
    alone = _chars("f", {"f": 4.5}, run=3)
    assert _wide_f(page + alone, _declared) == set()
    # The f's own object at the page's scale, with a real ligature glyph: found.
    assert len(_wide_f(page + _chars("fled", {"f": 4.5}, run=4), _declared)) == 1


def test_a_horizontally_scaled_page_raises_no_dropped_ligature(tmp_path):
    # a reproduction on the first draft of #342: Helvetica at 12 pt with 250 Tz, where
    # "of" measured against the declared "f" alone reads as a dropped ligature.
    import pikepdf
    from pikepdf import Dictionary, Name

    from reflow.extract.pages import extract_pages

    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))))
    page.Contents = pdf.make_stream(b"BT /F1 12 Tf 250 Tz 72 700 Td (of the deed of the clerk) Tj ET")
    pdf.save(tmp_path / "wide.pdf")
    (content,) = extract_pages(tmp_path / "wide.pdf")
    assert [line.ligature_words for line in content.lines] == [()]
    # a second case: ordinary text at 19 pt, and "of" at 12 pt with 250 Tz, which pdfium
    # sizes the same, so the page's letters would calibrate the stretched f.
    page.Contents = pdf.make_stream(b"BT /F1 19 Tf 72 700 Td (the deed held by the clerk) Tj ET "
                                    b"BT /F1 12 Tf 250 Tz 72 650 Td (of) Tj ET")
    pdf.save(tmp_path / "mixed.pdf")
    (content,) = extract_pages(tmp_path / "mixed.pdf")
    assert [line.ligature_words for line in content.lines] == [(), ()]


def test_a_kept_ocr_layer_is_checked_for_ligatures_and_the_layer_reflow_wrote_is_not():
    kept = _p("The clerk fled the deed", source=Source.OCR, bbox=(50, 100, 300, 112))
    written = _p("The clerk fled the deed", page=2, source=Source.OCR, bbox=(50, 100, 300, 112))
    doc = _doc(kept, written)
    doc.pages[1].facts.append(Fact("ocr-page", Source.OCR, "text recognized by ocrmypdf",
                                   {"reason": "no-text", "mode": "redo"}))
    line = Line(1, (50, 100, 300, 112), "", 9.0, False, False, "Helvetica", ocr=True,
                ligature_words=("fled",))
    pages = [PageContent(1, 612, 792, [line], []),
             PageContent(2, 612, 792, [Line(2, (50, 100, 300, 112), "", 9.0, False, False,
                                            "GlyphLessFont", ocr=True, ligature_words=("fled",))], [])]
    assert flag_text(doc, pages)["dropped-ligature"] == 1
    assert [f.code for f in kept.facts] == ["dropped-ligature"] and written.facts == []


def test_a_ligature_glyph_on_a_page_of_two_fs_is_found_from_the_fonts_metrics(tmp_path):
    import random

    from conftest import make_text_pdf
    from robustness.kinds import KINDS

    from reflow.extract.fonts import GlyphNames
    from reflow.extract.pages import extract_pages

    src = make_text_pdf(tmp_path / "clean.pdf",
                        [[(72, 72, 12, "The clerk filed the deed of the office in March")]])
    dst = tmp_path / "ligatures.pdf"
    next(k for k in KINDS if k.id == "broken-ligature").transform(src, dst, random.Random(1))
    declared = GlyphNames(src).widths(1, "Helvetica")
    assert declared["f"] == 278.0 and declared["o"] == 556.0 and "1" in declared
    (page,) = extract_pages(dst)
    assert [line.ligature_words for line in page.lines] == [("fled", "offce")]
    (clean,) = extract_pages(src)
    assert [line.ligature_words for line in clean.lines] == [()]


def test_ordinary_words_with_an_f_pair_are_never_suspects():
    for word in ("halfback", "Afghanistan", "dwarfs", "https://golf.com/fn", "office"):
        chars = _chars(word, {"f": 3.6})
        assert _dropped_ligature_words(chars, _wide_f(chars)) == ()


def test_dropped_ligature_is_reported_once_per_page_with_the_repairs():
    a = _p("The rule has specifcally been confned.", bbox=(50, 100, 300, 112))
    b = _p("A second specifc note.", bbox=(50, 120, 300, 132))
    c = _p("Another confned note.", page=2)
    ocr = _p("Scanned confned text", page=2, source=Source.OCR)
    doc = _doc(a, b, c, ocr)

    def line(page, words, box):
        return Line(page, box, "", 9.0, False, False, "Serif", ligature_words=words)

    pages = [PageContent(1, 612, 792, [line(1, ("specifcally", "confned"), (50, 100, 300, 112)),
                                       line(1, ("specifc",), (50, 120, 300, 132))], []),
             PageContent(2, 612, 792, [line(2, ("confned",), (50, 100, 300, 112))], [])]
    assert flag_text(doc, pages)["dropped-ligature"] == 2
    assert [f.code for f in a.facts] == ["dropped-ligature"]
    assert b.facts == [] and ocr.facts == []
    assert "specifcally (for specifically or speciflcally)" in a.facts[0].detail
    assert {(i["severity"], i["label"]) for i in queue(doc)} == {
        ("error", "Words missing the letters of a fi ligature")}
    assert ligature_repairs("confned") == ["confined", "conflned"]


def test_without_measured_geometry_no_word_is_called_a_dropped_ligature():
    node = _p("Afghanistan halfback dwarfs specifcally")
    assert flag_text(_doc(node), [])["dropped-ligature"] == 0


def _table(*rows: list[str]) -> Node:
    return Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, children=[Node(Role.TD, page=1, text=t) for t in row])
        for row in rows])


def test_literal_bullets_count_only_glyphs_that_open_an_item():
    from reflow.analyze.text_evidence import literal_bullets
    assert literal_bullets("Incorrect examples: \u2022 Contracted with BCBS. \u2022 We are Blue.") == 2
    assert literal_bullets("\u2022 Single item") == 1
    assert literal_bullets("No bullets here, only a dot. And a point.") == 0
    assert literal_bullets("trailing bullet \u2022") == 0


def test_literal_bullets_in_cells_and_paragraphs_are_flagged_but_lists_are_not():
    cell = _p("Correct examples: \u2022 one \u2022 two", Role.TD)
    clean = _p("Correct examples", Role.TD)
    item = _p("Contracted with Blue Cross", Role.LI)
    doc = _doc(_p("Intro text here."), Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, children=[cell, clean])]), item)
    assert flag_text(doc)["literal-bullets"] == 1
    assert [f.code for f in cell.facts] == ["literal-bullets"] and item.facts == []


def test_empty_header_row_needs_a_whole_row_of_empty_cells_above_content():
    from reflow.analyze.text_evidence import empty_header_row
    assert empty_header_row(_table(["", "", ""], ["Correct", "x", "y"]))
    assert not empty_header_row(_table(["", "Name"], ["a", "b"]))   # a blank corner is fine
    assert not empty_header_row(_table(["", ""], ["", ""]))          # nothing to head
    assert not empty_header_row(_table(["", ""]))                    # one row
    doc = _doc(_table(["", ""], ["a", "b"]))
    assert flag_text(doc)["empty-header-row"] == 1
    assert [i["warn"] for i in queue(doc)] == ["empty-header-row"]


def _list(*markers: str, page: int = 1) -> Node:
    return Node(Role.LIST, page=page, attrs={"ordered": True}, children=[
        Node(Role.LI, page=page, text=f"item {m}", attrs={"marker": m}) for m in markers])


def test_a_list_whose_numbering_runs_on_from_the_list_above_is_flagged_once():
    first = _list("(a)")
    second = _list("(b)", "(c)")
    field = Node(Role.FIELD, page=1)
    doc = _doc(first, field, second)
    assert flag_text(doc)["split-list"] == 1
    assert flag_text(doc)["split-list"] == 0   # idempotent
    assert [f.code for f in second.children[0].facts] == ["split-list"]
    assert second.children[1].facts == [] and first.children[0].facts == []
    assert [i["warn"] for i in queue(doc)] == ["split-list"]


def test_lists_that_start_over_or_sit_apart_are_not_one_split_list():
    assert flag_text(_doc(_list("(a)", "(b)"), _list("(a)")))["split-list"] == 0   # starts over
    assert flag_text(_doc(_list("1."), _list("3.")))["split-list"] == 0            # a gap
    assert flag_text(_doc(_list("1."), _p("Notes, then more steps."), _list("2.")))["split-list"] == 0
    assert flag_text(_doc(_list("1."), _list("2.", page=2)))["split-list"] == 0    # next page
    bullets = Node(Role.LIST, page=1, attrs={"ordered": False}, children=[
        Node(Role.LI, page=1, text="x", attrs={"marker": "•"})])
    assert flag_text(_doc(bullets, bullets))["split-list"] == 0
