"""OCR word grades bound to the text layer they produced (extract.ocrwords)."""
import pikepdf
import pypdfium2 as pdfium
import pytest
from pikepdf import Array, Dictionary, Name

from reflow.extract.pages import extract_pages
from reflow.extract.ocrwords import bind_words, missing_grades
from reflow.extract.parallel import extract_document
from reflow.ingest.ocr_trust import (Grade, PageGrade, SidecarRead, Word, read_sidecar,
                                     sidecar_path, write_sidecar)

W, H = 612, 792
PX = 300 / 72   # the OCR image's pixels per point
# One word per line (x, top, size, text, grade), as OCR's invisible layer holds them.
WORDS = [(72, 100, 12, "Committee", Grade.READ), (72, 140, 12, "report", Grade.DOUBT),
         (72, 180, 12, "Signature", Grade.READ)]


# What maps the displayed page (bottom-left origin) onto the sheet under /Rotate: OCRmyPDF writes
# its layer to read upright as the page is displayed.
TURN = {0: "1 0 0 1 0 0", 90: f"0 1 -1 0 {W} 0", 180: f"-1 0 0 -1 {W} {H}"}


def invisible_pdf(path, pages, *, rotate=0, crop=None):
    """Pages of invisible (render mode 3) Helvetica words, `pages` a list of WORDS-like lists
    placed on the page as displayed."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    height = W if rotate == 90 else H
    for words in pages:
        page = pdf.add_blank_page(page_size=(W, H))
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        ops = [f"q {TURN[rotate]} cm"] + [
            f"BT 3 Tr /F1 {pt} Tf {x} {height - top - pt} Td ({text}) Tj ET"
            for x, top, pt, text, _ in words] + ["Q"]
        page.Contents = pdf.make_stream("\n".join(ops).encode())
        if rotate:
            page.Rotate = rotate
        if crop:
            page.CropBox = Array(crop)
    pdf.save(path)
    return path


def grade_for(page_no, lines, dropped=(), shift=0.0, size=(W, H)):
    """The PageGrade the plugin would write for these lines (their boxes from extraction), plus
    `dropped` UNREAD words at (x0, top, x1, bottom) points, every box moved down by `shift`."""
    words = []
    for line, (_, _, _, text, grade) in lines:
        x0, top, x1, bottom = line.bbox
        words.append(Word((round(x0 * PX), round((top + 1 + shift) * PX), round(x1 * PX),
                           round((bottom - 1 + shift) * PX)), text, 90 if grade is Grade.READ
                          else 60, grade, None, 1.0, layer_text=text))
    for box in dropped:
        words.append(Word(tuple(round(v * PX) for v in box), "#&%", 12, Grade.UNREAD, None, 1.0))
    return PageGrade(page_no, "redo", (round(size[0] * PX), round(size[1] * PX)), tuple(words))


def _pair(pdf, words=WORDS):
    """The page's extracted lines, each with the word of `words` that made it."""
    page = next(extract_pages(pdf, 1, 1))
    by_text = {w[3]: w for w in words}
    assert sorted(l.text for l in page.lines) == sorted(by_text)
    return [(l, by_text[l.text]) for l in page.lines]


def _graded(tmp_path, *, rotate=0, crop=None, shift=0.0, dropped=(), extra=None):
    pdf = invisible_pdf(tmp_path / "w.pdf", [WORDS], rotate=rotate, crop=crop)
    lines = _pair(pdf)
    size = (crop[2] - crop[0], crop[3] - crop[1]) if crop else (W, H)
    if rotate in (90, 270):
        size = size[::-1]
    grade = grade_for(1, lines, dropped, shift, size)
    if extra:
        grade = PageGrade(1, "redo", grade.image_px, grade.words + (extra,))
    write_sidecar(pdf, [grade])
    return next(extract_pages(pdf, 1, 1))


def test_a_current_sidecar_binds_every_word_to_its_line(tmp_path):
    page = _graded(tmp_path, dropped=[(300, 300, 360, 320)])
    assert page.words.status == "graded" and page.words.unmatched == ()
    assert [len(l.grades) for l in page.lines] == [1, 1, 1]
    assert [l.grades[0].grade for l in page.lines] == [Grade.READ, Grade.DOUBT, Grade.READ]
    (omitted,) = page.words.omitted
    assert omitted.line is None and omitted.bbox == pytest.approx((300, 300, 360, 320), abs=0.3)
    assert page.words.read_height is not None


def test_a_sidecar_shifted_by_30_points_is_misaligned(tmp_path):
    page = _graded(tmp_path, shift=30)
    assert page.words.status == "misaligned" and len(page.words.unmatched) == 3
    assert all(l.grades is None for l in page.lines)


@pytest.mark.parametrize("rotate, crop", [(180, None), (90, None), (0, [36, 36, 576, 756])],
                         ids=["rotate-180", "rotate-90", "crop-box"])
def test_turned_and_cropped_pages_bind_in_the_displayed_frame(tmp_path, rotate, crop):
    page = _graded(tmp_path, rotate=rotate, crop=crop)
    assert page.words.status == "graded"
    assert all([w.text for w in l.grades] == [l.text] for l in page.lines)


def test_a_word_that_matches_no_layer_word_leaves_its_line_ungraded(tmp_path):
    pdf = invisible_pdf(tmp_path / "w.pdf", [WORDS])
    lines = _pair(pdf)
    grade = grade_for(1, lines)
    ghost = Word(grade.words[1].box, "repart", 60, Grade.DOUBT, None, 1.0, layer_text="repart")
    words = (*grade.words[:1], ghost, *grade.words[2:])
    write_sidecar(pdf, [PageGrade(1, "redo", grade.image_px, words)])
    page = next(extract_pages(pdf, 1, 1))
    # One of three kept words unmatched is under 90%: the whole page is misaligned.
    assert page.words.status == "misaligned"
    # With enough matched words around it, only the overlapping line stays ungraded.
    many = [(72, 100 + 20 * i, 10, f"word{i}", Grade.READ) for i in range(12)]
    pdf = invisible_pdf(tmp_path / "m.pdf", [many])
    lines = _pair(pdf, many)
    grade = grade_for(1, lines)
    bad = Word(grade.words[3].box, "wordX", 60, Grade.DOUBT, None, 1.0, layer_text="wordX")
    write_sidecar(pdf, [PageGrade(1, "redo", grade.image_px,
                                  (*grade.words[:3], bad, *grade.words[4:]))])
    page = next(extract_pages(pdf, 1, 1))
    assert page.words.status == "partial" and len(page.words.unmatched) == 1
    assert page.lines[3].grades is None
    assert all(l.grades is not None for i, l in enumerate(page.lines) if i != 3)


def test_no_sidecar_or_one_for_another_pdf_grades_nothing(tmp_path):
    pdf = invisible_pdf(tmp_path / "w.pdf", [WORDS])
    page = next(extract_pages(pdf, 1, 1))
    assert page.words is None and all(l.grades is None for l in page.lines)
    write_sidecar(pdf, [grade_for(1, _pair(pdf))])
    with pikepdf.open(pdf, allow_overwriting_input=True) as doc:   # the PDF changes after
        doc.docinfo["/Title"] = "edited"
        doc.save(pdf)
    assert read_sidecar(pdf).status == "stale"
    page = next(extract_pages(pdf, 1, 1))
    assert page.words is None and all(l.grades is None for l in page.lines)


def test_missing_grades_names_each_expected_page_and_why(tmp_path):
    ok = SidecarRead("ok", {1: grade_for(1, [])})
    assert missing_grades(ok, [1, 2]) == {2: "no-page"}
    for status in ("absent", "corrupt", "stale"):
        assert missing_grades(SidecarRead(status, {}), [1]) == {1: status}
    assert missing_grades(SidecarRead("absent", {}), []) == {}


def test_tracked_is_copied_with_lines_and_without(tmp_path):
    pdf = invisible_pdf(tmp_path / "w.pdf", [WORDS, WORDS])
    page = next(extract_pages(pdf, 1, 1))
    base = grade_for(1, list(zip(page.lines, WORDS)))
    grades = [PageGrade(1, "redo", base.image_px, base.words, tracked=True),
              PageGrade(2, "redo", base.image_px, base.words, tracked=False)]
    write_sidecar(pdf, grades)
    pages = list(extract_pages(pdf))
    assert [p.words.tracked for p in pages] == [True, False]
    doc = pdfium.PdfDocument(pdf)
    try:
        assert [bind_words(g, doc[g.page - 1], None).tracked for g in grades] == [True, False]
    finally:
        doc.close()


def test_serial_and_pool_extraction_bind_the_same_words(tmp_path):
    pdf = invisible_pdf(tmp_path / "w.pdf", [WORDS] * 4)
    first = next(extract_pages(pdf, 1, 1))
    base = grade_for(1, list(zip(first.lines, WORDS)))
    write_sidecar(pdf, [PageGrade(n, "redo", base.image_px, base.words, tracked=n % 2 == 0)
                        for n in range(1, 5)])
    serial, _ = extract_document(pdf, 4, jobs=1)
    pooled, _ = extract_document(pdf, 4, jobs=2, chunk_pages=1)
    assert [p.words for p in serial] == [p.words for p in pooled]
    assert [[l.grades for l in p.lines] for p in serial] == [[l.grades for l in p.lines]
                                                            for p in pooled]
    assert all(p.words.status == "graded" for p in serial)
    assert sidecar_path(pdf).exists()


def test_same_place_takes_a_short_word_above_its_font_box_and_refuses_a_shift():
    from reflow.extract.ocrwords import same_place
    assert same_place((480, 195, 499, 204), (480, 201, 499, 205))     # '4': the font box sits low
    assert not same_place((480, 165, 499, 174), (480, 201, 499, 205))  # 30 pt off
    assert not same_place((440, 201, 459, 205), (480, 201, 499, 205))  # beside it


def test_a_ligature_in_the_graded_word_matches_the_letters_in_the_layer(tmp_path):
    # Tesseract writes "\ufb03" where the text layer reads "ffi".
    pdf = invisible_pdf(tmp_path / "w.pdf", [[(72, 100, 12, "Official", Grade.READ)]])
    [(line, _)] = _pair(pdf, [(72, 100, 12, "Official", Grade.READ)])
    write_sidecar(pdf, [grade_for(1, [(line, (72, 100, 12, "O\ufb03cial", Grade.READ))])])
    page = next(extract_pages(pdf, 1, 1))
    assert page.words.status == "graded" and page.words.unmatched == ()
    assert [w.text for w in page.lines[0].grades] == ["O\ufb03cial"]


def test_a_word_left_on_a_mostly_dropped_line_is_a_remnant(tmp_path):
    from dataclasses import replace
    pdf = invisible_pdf(tmp_path / "w.pdf", [WORDS])
    lines = _pair(pdf)
    base = grade_for(1, lines)
    words = [replace(w, line=i) for i, w in enumerate(base.words)]
    # Line 2 ("Signature") lost a word with letters, line 1 only a stray mark.
    words += [Word((1300, 740, 1500, 790), "ﬂu‘#mw/", 48, Grade.UNREAD, None, 4.1, line=2),
              Word((1300, 570, 1320, 620), "’", 1, Grade.UNREAD, None, 0.2, line=1)]
    with pdfium.PdfDocument(pdf) as doc:
        bound = bind_words(PageGrade(1, "redo", base.image_px, tuple(words)), doc[0], None)
    by_text = {w.text: w.remnant for w in bound.words}
    assert by_text == {"Committee": False, "report": False, "Signature": True,
                       "ﬂu‘#mw/": False, "’": False}


def test_a_kept_layers_unread_word_is_not_read_and_a_layer_its_grades_miss_is_ungraded(tmp_path):
    # A kept OCR layer's grades are of its words in order; extraction leaves out the
    # characters of the ones graded unread. Grades that no longer read as the layer does (a
    # review) are not its grades: the page is misaligned, which every engine's binding reports
    # (bind_words) and review warns of, not graded with none.
    from dataclasses import replace
    pdf = invisible_pdf(tmp_path / "k.pdf", [WORDS])
    lines = _pair(pdf, WORDS)
    base = grade_for(1, lines)
    unread = replace(base.words[1], grade=Grade.UNREAD, layer_text="")
    words = (base.words[0], unread, base.words[2])
    write_sidecar(pdf, [PageGrade(1, "keep", base.image_px, words)])
    page = next(extract_pages(pdf, 1, 1))
    assert [l.text for l in page.lines] == ["Committee", "Signature"]
    assert page.words is not None and page.words.status == "graded"
    write_sidecar(pdf, [PageGrade(1, "keep", base.image_px,
                                  (base.words[0], replace(unread, text="repart"), base.words[2]))])
    page = next(extract_pages(pdf, 1, 1))
    assert [l.text for l in page.lines] == ["Committee", "report", "Signature"]
    assert page.words.status == "misaligned" and all(l.grades is None for l in page.lines)
    with pdfium.PdfDocument(str(pdf)) as doc:
        assert bind_words(read_sidecar(pdf).pages[1], doc[0], None).status == "misaligned"


def test_same_place_takes_a_word_whose_font_box_sits_below_it_and_still_refuses_a_line_off():
    from reflow.extract.ocrwords import same_place
    # SHELL on a handwritten line of docaccess-permit page 6: OCRmyPDF 17.14 sets the line's
    # boxes 14 pt lower than 17.13 did, so the word's own box meets only the top of its font box.
    assert same_place((267.12, 462.96, 308.52, 473.4), (267.09, 470.53, 308.74, 488.22))
    assert not same_place((267.12, 462.96, 308.52, 473.4), (267.09, 473.5, 308.74, 488.22))
