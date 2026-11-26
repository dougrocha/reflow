import math
import struct
from functools import partial

import pikepdf
from pikepdf import Dictionary, Name

from conftest import make_scan_pdf, make_text_pdf
from reflow.extract.chars import Char
from reflow.extract.lines import SOFT_HYPHEN, Line, StyleRun
from reflow.extract.pages import extract_pages
from reflow.extract.rtl import logical_order
from reflow.extract.text import lines_from_chars
from reflow.extract.parallel import extract_document
from reflow import stagecache
from testkit import line_texts


def starts_new_line(cur, c):
    """Whether `c`, coming after the characters `cur` of one line, begins a line of its own."""
    return len(lines_from_chars([*cur, c], 1)) > 1


def one_line(chars):
    [line] = lines_from_chars(chars, 1)
    return line


def lines_of(tmp_path, items):
    [page] = extract_pages(make_text_pdf(tmp_path / "t.pdf", [items]))
    return page.lines


def test_line_text_size_and_style(tmp_path):
    heading, body, note = lines_of(tmp_path, [
        (72, 72, 18, "Annual Report", "B"),
        (72, 110, 10, "The year in review."),
        (72, 130, 10, "See appendix", "I"),
    ])
    assert (heading.text, heading.size, heading.bold) == ("Annual Report", 18, True)
    assert (body.text, body.size, body.bold, body.italic) == ("The year in review.", 10, False, False)
    assert note.italic
    assert heading.page == 1


def test_coordinates_are_top_left(tmp_path):
    [line] = lines_of(tmp_path, [(100, 200, 12, "Hello")])
    assert 98 < line.x0 < 102
    assert 190 < line.top < 210  # loose boxes include ascent, so allow some slack
    assert line.bottom > line.top


def test_columns_side_by_side_are_separate_lines(tmp_path):
    lines = lines_of(tmp_path, [
        (72, 100, 10, "Left column text"),
        (320, 100, 10, "Right column text"),
    ])
    assert sorted(l.text for l in lines) == ["Left column text", "Right column text"]


def test_overlapping_run_that_restarts_left_at_display_size_starts_a_new_line():
    logo = Char("T", 570, 111, 581, 125, 14, True, False, "Univers-Bold",
                 False, False, False, 1)
    title = Char("H", 35, 104, 63, 151, 39, True, False, "Univers-Bold",
                  False, False, False, 2)

    assert starts_new_line([logo], title)


def test_a_colon_or_mark_drawn_against_an_rtl_letter_has_no_space_before_it():
    # az-hca-nondiscrim-multilang p3: pdfium reports "تنبيه :" and "مجان ًا". The colon and the
    # fathatan are runs of their own, drawn against the letter before them.
    def char(ch, x0, x1, run, space=False):
        return Char(ch, x0, 100, x1, 110, 10, False, False, "Arial", space, False, False, run)

    chars = [char("ب", 105, 110, 1), char("ه", 100, 105, 1), char(":", 97, 100.1, 2, True),
             char("ن", 80, 85, 3, True), char("\u064b", 81, 84, 4, True)]
    logical_order(chars)
    assert "".join(c.ch for c in chars) == "به:ن\u064b"
    assert [c.space_before for c in chars] == [False, False, False, True, False]


def _ocr_chars(text, x, top=100.0, step=5.0, invisible=True):
    """Characters of `text`, one `step` wide each, from `x`; invisible ones are OCR."""
    return [Char(ch, x + i * step, top, x + (i + 1) * step, top + 10, 10, False, False, "OCR",
                  False, False, False, 1, invisible=invisible) for i, ch in enumerate(text)]


def test_ocr_text_that_jumps_back_across_the_row_starts_a_new_line():
    cell = _ocr_chars("Length", 400)
    assert starts_new_line(cell, _ocr_chars("T", 134)[0])
    assert not starts_new_line(cell, _ocr_chars("s", 430)[0])  # the next letter


def test_ocr_stray_mark_before_text_is_not_a_cell():
    assert not starts_new_line(_ocr_chars("-", 317), _ocr_chars("C", 148)[0])


def test_ocr_rule_of_underscores_ends_the_cell():
    cell = _ocr_chars("foot ____", 400)
    assert starts_new_line(cell, _ocr_chars("f", 460)[0])
    assert not starts_new_line(cell, _ocr_chars("_", 445)[0])


def test_ocr_jitter_on_the_same_baseline_stays_one_line():
    line = _ocr_chars("Length of the", 400)  # ends at x=465
    assert not starts_new_line(line, _ocr_chars("w", 430)[0])  # lands inside the line


def test_ocr_hanging_indent_on_the_next_row_starts_a_new_line():
    line = _ocr_chars("Length of the bar", 400)
    assert starts_new_line(line, _ocr_chars("x", 420, top=112)[0])


def test_visible_text_is_not_split_by_underscores_or_backsteps():
    cell = _ocr_chars("Name: ____", 400, invisible=False)
    assert not starts_new_line(cell, _ocr_chars("D", 460, invisible=False)[0])
    assert not starts_new_line(_ocr_chars("Length", 400, invisible=False),
                                _ocr_chars("T", 134, invisible=False)[0])


def test_visible_text_that_restarts_left_of_the_line_and_off_its_baseline_starts_a_new_line():
    # a ruled form's boxes: "Fee(s)" at the right edge of one box, then "1 2 3" at the left of another
    cell = _ocr_chars("Fee(s)", 500, invisible=False)
    assert starts_new_line(cell, _ocr_chars("1", 30, top=104.4, invisible=False)[0])
    # on the same baseline it stays one line (text drawn twice, or out of order)
    assert not starts_new_line(cell, _ocr_chars("1", 30, invisible=False)[0])
    # a little left of the line's start is not a restart
    assert not starts_new_line(cell, _ocr_chars("1", 495, top=104.4, invisible=False)[0])
    # right-to-left text runs backwards, and its marks sit above and below the letters
    arabic = _ocr_chars("\u0645\u0631\u062d\u0628\u0627", 500, invisible=False)
    assert not starts_new_line(arabic, _ocr_chars("\u0628", 30, top=104.4, invisible=False)[0])


def test_lines_at_different_heights_split(tmp_path):
    lines = lines_of(tmp_path, [(72, 100, 10, "first"), (72, 114, 10, "second")])
    assert [l.text for l in lines] == ["first", "second"]


def test_line_end_hyphen_becomes_soft_hyphen(tmp_path):
    first, second = lines_of(tmp_path, [(72, 100, 10, "residual learn-"),
                                        (72, 114, 10, "ing framework")])
    assert first.text == "residual learn" + SOFT_HYPHEN
    assert second.text == "ing framework"


def test_page_images(mixed_pdf):
    text, scan, *_ = extract_pages(mixed_pdf)
    assert text.images == []
    assert scan.images == [(0, 0, 612, 792)]
    assert (scan.width, scan.height) == (612, 792)


def test_objects_inside_form_xobjects_use_page_coordinates(tmp_path):
    # An image drawn inside a form XObject that is scaled 2x and moved to (100, 200).
    pdf = pikepdf.new()
    image = pikepdf.Stream(pdf, bytes(4), Type=Name.XObject, Subtype=Name.Image, Width=2,
                           Height=2, ColorSpace=Name.DeviceGray, BitsPerComponent=8)
    form = pikepdf.Stream(pdf, b"q 100 0 0 100 0 0 cm /Im0 Do Q", Type=Name.XObject,
                          Subtype=Name.Form, BBox=[0, 0, 100, 100],
                          Resources=Dictionary(XObject=Dictionary(Im0=image)))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(XObject=Dictionary(Fm0=form))
    page.Contents = pdf.make_stream(b"q 2 0 0 2 100 200 cm /Fm0 Do Q")
    path = tmp_path / "form.pdf"
    pdf.save(path)

    [content] = extract_pages(path)
    # page space x 100..300, y 200..400 → top-left: top = 792 - 400, bottom = 792 - 200
    assert content.images == [(100, 392, 300, 592)]


def test_tiny_images_are_keyed_icons(tmp_path):
    # Two different 2x2 bitmaps; the first drawn twice at icon size, plus a large image.
    pdf = pikepdf.new()

    def image(data):
        return pikepdf.Stream(pdf, data, Type=Name.XObject, Subtype=Name.Image, Width=2,
                              Height=2, ColorSpace=Name.DeviceGray, BitsPerComponent=8)

    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(XObject=Dictionary(Ok=image(bytes(4)),
                                                   No=image(b"\xff\x00\x00\xff")))
    page.Contents = pdf.make_stream(b"q 11 0 0 11 100 700 cm /Ok Do Q "
                                    b"q 11 0 0 11 100 600 cm /Ok Do Q "
                                    b"q 11 0 0 11 100 500 cm /No Do Q "
                                    b"q 200 0 0 200 100 100 cm /Ok Do Q")
    path = tmp_path / "icons.pdf"
    pdf.save(path)

    [content] = extract_pages(path)
    assert len(content.images) == 4
    a, b, c = content.icons  # the large image is not an icon
    assert a.bbox == (100, 81, 111, 92)
    assert a.key == b.key != c.key


def test_font_size_includes_text_matrix_scale(tmp_path):
    # "1 Tf" scaled 10x by Tm, as many report generators write it.
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream(b"BT /F1 1 Tf 10 0 0 10 72 700 Tm (9525 E Old Spanish Trail) Tj ET")
    path = tmp_path / "tm.pdf"
    pdf.save(path)
    [content] = extract_pages(path)
    [line] = content.lines
    assert line.size == 10
    assert line.text == "9525 E Old Spanish Trail"


def test_fill_and_stroke_text_counts_as_bold(tmp_path):
    # Regular Arial drawn with render mode 2 (fill + stroke), as in the AZ Blue directories.
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream(
        b"BT 2 Tr /F1 11 Tf 72 700 Td (Tamburro, Larry S, MD) Tj ET\n"
        b"BT 0 Tr /F1 11 Tf 72 680 Td (4600 E Shea Blvd) Tj ET")
    path = tmp_path / "fakebold.pdf"
    pdf.save(path)
    [content] = extract_pages(path)
    name, address = content.lines
    assert name.bold and not address.bold


def test_sheared_text_is_not_rotated(tmp_path):
    # Fake-italic headings drawn with a skewed text matrix, as in the AZ Blue directories.
    # pdfium reports a char angle for the shear; the lines must still split per row.
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream(
        b"BT /F1 1 Tf 14 0 4.2 14 72 700 Tm (FAMILY NURSE) Tj ET\n"
        b"BT /F1 1 Tf 14 0 4.2 14 72 684 Tm (PRACTITIONER) Tj ET")
    path = tmp_path / "sheared.pdf"
    pdf.save(path)
    [content] = extract_pages(path)
    assert [(l.text, l.rotated) for l in content.lines] == [("FAMILY NURSE", False),
                                                           ("PRACTITIONER", False)]


def _page_with_font(tmp_path, base_font, ops, flags=None, angle=None):
    """One page with font F1 (non-embedded, optionally with a descriptor) and `ops`."""
    pdf = pikepdf.new()
    font = Dictionary(Type=Name.Font, Subtype=Name.TrueType, BaseFont=Name("/" + base_font),
                      Encoding=Name.WinAnsiEncoding)
    if flags is not None:
        font.FontDescriptor = pdf.make_indirect(Dictionary(
            Type=Name.FontDescriptor, FontName=Name("/" + base_font), Flags=flags,
            FontBBox=[0, -200, 1000, 900], ItalicAngle=angle, Ascent=900, Descent=-200,
            CapHeight=700, StemV=80))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=pdf.make_indirect(font)))
    page.Contents = pdf.make_stream(ops)
    path = tmp_path / "font.pdf"
    pdf.save(path)
    [content] = extract_pages(path)
    return content.lines


UPRIGHT = b"BT /F1 11 Tf 72 700 Td (Gokhale, Sankalp S, MD) Tj ET"


def test_italic_flag_with_absurd_angle_is_not_italic(tmp_path):
    # Generated directories set flag 64 and ItalicAngle -120 on upright Arial entries.
    [line] = _page_with_font(tmp_path, "Arial", UPRIGHT, flags=96, angle=-120)
    assert line.text == "Gokhale, Sankalp S, MD"
    assert not line.italic


def test_italic_flag_with_sane_angle_is_italic(tmp_path):
    [line] = _page_with_font(tmp_path, "Arial", UPRIGHT, flags=96, angle=-12)
    assert line.italic


def test_italic_name_is_italic_without_flags(tmp_path):
    [line] = _page_with_font(tmp_path, "Arial-ItalicMT", UPRIGHT)
    assert line.italic


def test_sheared_matrix_is_italic_and_upright_text_is_not(tmp_path):
    # Fake italic: the same face drawn with a 4.2/14 shear, then the same face upright.
    ops = (b"BT /F1 1 Tf 14 0 4.2 14 72 700 Tm (Sheared heading) Tj ET\n"
           b"BT /F1 1 Tf 14 0 0 14 72 680 Tm (Upright heading) Tj ET")
    sheared, upright = _page_with_font(tmp_path, "Arial", ops)
    assert sheared.italic and not upright.italic


def test_drawn_lean_ignores_rotation_and_reads_shear():
    from types import SimpleNamespace as M

    from reflow.extract.fonts import drawn_lean
    assert drawn_lean(M(a=14, b=0, c=0, d=14)) == 0
    assert round(drawn_lean(M(a=14, b=0, c=4.2, d=14)), 1) == 16.7  # leans right
    assert abs(drawn_lean(M(a=0, b=14, c=-14, d=0))) < 1e-9  # rotated 90 degrees, upright


def _truetype(tables):
    """A TrueType header with the given {tag: bytes} tables; enough for table lookups."""
    header = struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0)
    offset = 12 + 16 * len(tables)
    directory, body = b"", b""
    for tag, data in tables.items():
        directory += struct.pack(">4sIII", tag, 0, offset + len(body), len(data))
        body += data
    return header + directory + body


def test_program_style_reads_embedded_weight_and_slant():
    from reflow.extract.fonts import program_style
    os2 = bytearray(96)
    struct.pack_into(">H", os2, 4, 700)  # usWeightClass
    struct.pack_into(">H", os2, 62, 0)   # fsSelection: upright
    head = bytearray(54)
    struct.pack_into(">H", head, 44, 0)  # macStyle
    assert program_style(_truetype({b"OS/2": bytes(os2), b"head": bytes(head)})) == (700, False)
    struct.pack_into(">H", head, 44, 2)  # macStyle italic bit
    assert program_style(_truetype({b"OS/2": bytes(os2), b"head": bytes(head)})) == (700, True)
    assert program_style(b"OTTO-CFF-not-truetype") == (None, False)


def test_tracked_text_is_not_split_into_characters(tmp_path):
    # 16 pt of letter spacing on 8 pt type: every gap is wider than 1.5 font sizes, but the
    # run's own spacing is the norm, so the heading stays one line.
    [line] = _page_with_font(tmp_path, "Helvetica",
                             b"BT /F1 8 Tf 16 Tc 72 700 Td (Schedule A Form 1040) Tj ET")
    assert line.text == "Schedule A Form 1040"


def test_gap_between_separate_text_objects_still_splits(tmp_path):
    # Two separately placed labels, 23 pt apart: tracking only comes from one show operator.
    first, second = _page_with_font(
        tmp_path, "Helvetica",
        b"BT /F1 8 Tf 72 700 Td (0) Tj ET\nBT /F1 8 Tf 100 700 Td (1) Tj ET")
    assert (first.text, second.text) == ("0", "1")


def test_a_word_space_between_runs_of_differing_style_is_kept(tmp_path):
    # "of" and "Loan" are separate text objects 1.1 pt apart: 0.17 em at 6.5 pt, a word space
    # that the run boundary swallowed. The words stay apart.
    assert line_texts(_raw_pdf(tmp_path, [
        "BT /F1 6.5 Tf 72 700 Td (of) Tj ET",
        "BT /F1 6.5 Tf 78.52 700 Td (Loan) Tj ET"])) == ["of Loan"]


def test_runs_set_tight_against_each_other_stay_one_word(tmp_path):
    # The same two runs with no gap between them: one word, as the font sets it.
    assert line_texts(_raw_pdf(tmp_path, [
        "BT /F1 6.5 Tf 72 700 Td (of) Tj ET",
        "BT /F1 6.5 Tf 77.42 700 Td (Loan) Tj ET"])) == ["ofLoan"]


def _runs(pieces, size=6.8, x=72.0, step=4.5):
    """The text of a line of runs, each (text, gap before it in points), its letters `step` wide."""
    chars = []
    for run, (text, gap) in enumerate(pieces):
        x += gap
        for ch in text:
            chars.append(Char(ch, x, 100, x + step, 100 + size, size, False, False, "Arial", False,
                              False, False, run))
            x += step
    return one_line(chars).text


def test_a_word_set_glyph_by_glyph_keeps_its_letters_together():
    # The dental claim form sets "POLICYHOLDER" in runs of a letter or two. Some runs touch, so
    # the letters are placed one by one, and gaps of 0.18 em between the others are the error
    # of the glyph boxes, not word spaces (it read "POL ICY HOL DER").
    assert _runs([("PO", 0), ("L", 0.1), ("IC", 1.2), ("Y", 0.05), ("HO", 1.2), ("L", 0.1),
                  ("DER", 1.2)]) == "POLICYHOLDER"


def test_a_wide_gap_on_a_line_set_glyph_by_glyph_is_still_a_space():
    assert _runs([("PO", 0), ("L", 0.1), ("IC", 0.05), ("Y", 0.1), ("NAME", 2.4)]) == "POLICY NAME"


def test_one_gap_between_runs_is_a_space_when_no_run_breaks_a_word():
    assert _runs([("of", 0), ("Loan", 1.2)]) == "of Loan"


def test_a_spread_run_makes_its_line_judged_glyph_by_glyph():
    assert _runs([("Infor", 0), ("mation", 1.2)]) == "Infor mation"  # no spread run: a space
    chars = [Char(ch, x0 + k * step, 100, x0 + (k + 1) * step, 106.8, 6.8, False, False,
                  "Arial", False, False, False, run, spread=k > 0)
             for run, (text, x0, step) in enumerate([("nfor", 72.0, 4.73), ("mation", 92.1, 1.0)])
             for k, ch in enumerate(text)]
    assert one_line(chars).text == "nformation"


def test_ocr_run_boxes_do_not_add_spaces_inside_words(tmp_path):
    # OCR can position a word's letters in separate invisible runs with inaccurate gaps.
    assert line_texts(_raw_pdf(tmp_path, [
        "72 701 25 1.5 re f",   # the scan's ink the words were read from
        "BT 3 Tr /F1 6.5 Tf 72 700 Td (B) Tj ET",
        "BT 3 Tr /F1 6.5 Tf 77.3755 700 Td (ecause) Tj ET"])) == ["Because"]


def test_a_raised_letter_stays_attached_to_its_abbreviation(tmp_path):
    assert line_texts(_raw_pdf(tmp_path, [
        "BT /F1 9 Tf 72 700 Td (LPG) Tj ET",
        "BT /F1 6 Tf 91.375 703 Td (i) Tj ET"])) == ["LPGi"]


def _raw_pdf(tmp_path, ops):
    """One page with the given content-stream operators, in Helvetica as /F1."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream("\n".join(ops).encode("latin-1"))
    path = tmp_path / "raw.pdf"
    pdf.save(path)
    return path


def test_tiny_hidden_text_is_dropped_next_to_visible_text(tmp_path):
    # p17 page 94 repeats publication numbers at 0.01 pt in white beside the real ones.
    path = _raw_pdf(tmp_path, [
        "BT /F1 10 Tf 72 700 Td (Publication 501) Tj ET",
        "BT /F1 0.01 Tf 1 1 1 rg 258 600 Td (501) Tj ET",
    ])
    assert line_texts(path) == ["Publication 501"]


def test_a_page_of_only_tiny_text_keeps_it(tmp_path):
    path = _raw_pdf(tmp_path, ["BT /F1 0.01 Tf 258 600 Td (501) Tj ET"])
    assert "".join(line_texts(path)).replace(" ", "") == "501"


def test_ocr_text_layer_over_an_image_that_shows_nothing_is_kept(tmp_path):
    # A scan's OCR layer is invisible (render mode 3) and is the only text on the page: when its
    # image renders as nothing (one pdfium cannot draw), the layer stays.
    path = _raw_pdf(tmp_path, ["q 612 0 0 792 0 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \xff EI Q",
                               "BT 3 Tr /F1 10 Tf 72 700 Td (Scanned page text) Tj ET"])
    assert line_texts(path) == ["Scanned page text"]


def test_invisible_text_on_a_page_that_shows_nothing_is_kept(tmp_path):
    # An OCR layer kept without its image is all such a page has (see _unseen).
    path = _raw_pdf(tmp_path, ["BT 3 Tr /F1 10 Tf 72 700 Td (Scanned page text) Tj ET"])
    assert line_texts(path) == ["Scanned page text"]


def test_a_dot_of_ink_keeps_only_the_invisible_word_over_it(tmp_path):
    # Each word is judged on its own: a mark under "Quartz" does not vouch for the rest.
    path = _raw_pdf(tmp_path, [
        "BT /F1 10 Tf 72 700 Td (Visible) Tj ET",
        "74 602 1 1 re f",
        "BT 3 Tr /F1 10 Tf 72 600 Td (Quartz zebra mandolin) Tj ET",
    ])
    assert line_texts(path) == ["Visible", "Quartz"]


def _table_cells(path):
    from reflow.extract.tables.ruled import find_tables

    [table] = find_tables(path, [1])[1]
    return [c for row in table.rows for c in row if c]


_GRID = " ".join(f"{x} 500 m {x} 600 l S" for x in (100, 250, 400)) + " " + \
    " ".join(f"100 {y} m 400 {y} l S" for y in (500, 550, 600))


def test_a_ruled_table_keeps_a_visible_letter_where_a_hidden_one_starts(tmp_path):
    path = _raw_pdf(tmp_path, [
        _GRID, "BT /F1 10 Tf 110 570 Td (Name) Tj ET", "BT /F1 10 Tf 260 570 Td (Value) Tj ET",
        "BT /F1 10 Tf 110 520 Td (Alpha) Tj ET", "BT /F1 10 Tf 260 520 Td (Beta) Tj ET",
        "BT /F1 0.5 Tf 110 520 Td (Quartz) Tj ET"])
    assert "Alpha" in _table_cells(path)


def test_a_ruled_table_leaves_out_hidden_text_under_an_inherited_media_box(tmp_path):
    path = _raw_pdf(tmp_path, [
        _GRID, "BT /F1 10 Tf 110 570 Td (Name) Tj ET", "BT /F1 10 Tf 260 570 Td (Value) Tj ET",
        "BT /F1 10 Tf 110 520 Td (Alpha) Tj ET", "BT /F1 10 Tf 260 520 Td (Beta) Tj ET",
        "BT 3 Tr /F1 10 Tf 300 520 Td (Quartz) Tj ET"])
    for rotate in (0, 90, 180, 270):
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            page = pdf.pages[0].obj
            if "/MediaBox" in page:
                del page["/MediaBox"]
            pdf.Root.Pages.MediaBox = pikepdf.Array([20, 30, 632, 822])
            page.CropBox = pikepdf.Array([20, 30, 600, 780])
            page.Rotate = rotate
            pdf.save(path)
        cells = {"".join(sorted(c)) for c in _table_cells(path)}   # pdfplumber reads 180° backwards
        assert cells == {"".join(sorted(w)) for w in ("Name", "Value", "Alpha", "Beta")}, rotate


def test_a_narrow_white_glyph_on_a_blank_spot_is_dropped(tmp_path):
    path = _raw_pdf(tmp_path, ["BT /F1 10 Tf 72 700 Td (Visible) Tj ET",
                               "BT 1 1 1 rg /F1 2 Tf 72 600 Td (i) Tj ET"])
    assert line_texts(path) == ["Visible"]


def test_a_ruled_table_leaves_out_hidden_text_on_a_turned_page(tmp_path):
    from reflow.extract.tables.ruled import find_tables

    grid = " ".join(f"{x} 500 m {x} 600 l S" for x in (100, 250, 400)) + " " + \
        " ".join(f"100 {y} m 400 {y} l S" for y in (500, 550, 600))
    ops = [grid,
           "BT /F1 10 Tf 110 570 Td (Name) Tj ET", "BT /F1 10 Tf 260 570 Td (Value) Tj ET",
           "BT /F1 10 Tf 110 520 Td (Alpha) Tj ET", "BT /F1 10 Tf 260 520 Td (Beta) Tj ET",
           "BT 3 Tr /F1 10 Tf 300 520 Td (Quartz) Tj ET"]
    for rotate in (0, 90):
        path = _raw_pdf(tmp_path, ops)
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            if rotate:
                pdf.pages[0].obj.Rotate = rotate
            pdf.save(path)
        [table] = find_tables(path, [1])[1]
        cells = [c for row in table.rows for c in row if c]
        assert "Beta" in cells and not any("Quartz" in c for c in cells), (rotate, cells)


def test_invisible_mode_3_text_beside_visible_text_is_kept(tmp_path):
    # Render mode alone is not evidence: an OCR layer over the scan's ink (here a drawn stroke
    # under the words) is the page's text.
    path = _raw_pdf(tmp_path, [
        "BT /F1 10 Tf 72 700 Td (Visible) Tj ET",
        "72 683 50 1.5 re f",
        "BT 3 Tr /F1 10 Tf 72 680 Td (Hidden OCR) Tj ET",
    ])
    assert sorted(line_texts(path)) == ["Hidden OCR", "Visible"]
    [page] = extract_pages(path)
    assert {line.text: line.ocr for line in page.lines} == {"Hidden OCR": True,
                                                         "Visible": False}


def test_invisible_text_over_nothing_is_dropped(tmp_path):
    # #321: a line in render mode 3 on a blank spot of a page that shows other things is text no
    # reader sees (a hidden instruction), not an OCR layer.
    path = _raw_pdf(tmp_path, [
        "BT /F1 10 Tf 72 700 Td (Visible) Tj ET",
        "BT 3 Tr /F1 10 Tf 72 600 Td (Quartz zebra mandolin) Tj ET",
    ])
    assert line_texts(path) == ["Visible"]


def test_white_text_on_white_is_dropped_and_on_a_dark_fill_kept(tmp_path):
    path = _raw_pdf(tmp_path, [
        "BT /F1 10 Tf 72 700 Td (Visible) Tj ET",
        "BT 1 1 1 rg /F1 10 Tf 72 600 Td (Quartz zebra mandolin) Tj ET",
        "0.1 0.2 0.5 rg 70 495 120 20 re f",
        "BT 1 1 1 rg /F1 10 Tf 72 500 Td (Section heading) Tj ET",
    ])
    assert line_texts(path) == ["Visible", "Section heading"]


def test_text_off_the_page_is_dropped(tmp_path):
    # Leftover copies outside the crop box (an azblue form's white headings at top -472).
    path = _raw_pdf(tmp_path, [
        "BT /F1 10 Tf 72 700 Td (Visible) Tj ET",
        "BT 1 1 1 rg /F1 10 Tf 72 1200 Td (Off the page) Tj ET",
    ])
    assert line_texts(path) == ["Visible"]


def test_text_under_a_point_is_dropped_next_to_visible_text(tmp_path):
    path = _raw_pdf(tmp_path, [
        "BT /F1 10 Tf 72 700 Td (Visible) Tj ET",
        "BT /F1 0.5 Tf 72 600 Td (Quartz zebra mandolin) Tj ET",
    ])
    assert line_texts(path) == ["Visible"]


def test_a_scans_ocr_layer_stays_and_a_hidden_line_on_its_blank_spot_goes(tmp_path):
    path = make_scan_pdf(tmp_path / "scan.pdf", [[(100, "Scanned letter of the county"),
                                                  (130, "office the clerk filed")]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].contents_add(pdf.make_stream(
            b"BT 3 Tr /F1 12 Tf 72 400 Td (Quartz zebra mandolin) Tj ET"))
        pdf.save(path)
    assert line_texts(path) == ["Scanned letter of the county", "office the clerk filed"]


def _rotatable_pdf(tmp_path, name, ops, rotate=0):
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    if rotate:
        page.Rotate = rotate
    page.Contents = pdf.make_stream(ops)
    path = tmp_path / name
    pdf.save(path)
    return path


def test_text_on_a_page_rotated_by_rotate_reads_upright_as_displayed(tmp_path):
    # /Rotate 90 turns the page clockwise: text written up the portrait sheet (a 90° baseline) is
    # upright on screen, so it is an ordinary line of the displayed landscape page.
    path = _rotatable_pdf(tmp_path, "rot90.pdf",
                          b"BT /F1 10 Tf 0 1 -1 0 100 600 Tm (Displayed upright) Tj ET", rotate=90)
    [content] = extract_pages(path)
    assert (content.width, content.height) == (792, 612)
    [line] = content.lines
    assert line.text == "Displayed upright" and not line.rotated
    assert 90 <= line.top <= 115 and line.x0 > 150


def test_text_written_up_the_page_is_rotated_rows(tmp_path):
    # Two rows of upward text side by side: each is a line of its own, reading upward (turn 1).
    path = _rotatable_pdf(tmp_path, "sideways.pdf",
                          b"BT /F1 8 Tf 0 1 -1 0 300 100 Tm (first row of words) Tj ET\n"
                          b"BT /F1 8 Tf 0 1 -1 0 310 100 Tm (second row of words) Tj ET")
    [content] = extract_pages(path)
    rows = sorted((l for l in content.lines if l.rotated), key=lambda l: l.x0)
    assert [(l.text, l.turn) for l in rows] == [("first row of words", 1),
                                                ("second row of words", 1)]


def test_a_surrogate_pair_joins_into_one_character():
    from reflow.extract.chars import join_surrogates
    assert chr(join_surrogates(0xD835, 0xDC3A)) == "\U0001D43A"  # MATHEMATICAL ITALIC CAPITAL G
    assert join_surrogates(0xD835, 0x0041) is None  # a lone high surrogate is not a pair
    assert join_surrogates(0x0041, 0xDC3A) is None


def test_page_lines_of_p17_match_the_recorded_baseline():
    """The first 20 pages of Publication 17, as `page_lines` read them before the per-run
    caching of font, angle, size and ink: every field of every line is the same."""
    import dataclasses
    import json
    from pathlib import Path

    import pypdfium2 as pdfium
    import pytest

    from reflow.extract.fonts import GlyphNames
    from reflow.extract.text import page_lines

    root = Path(__file__).parent
    pdf = root.parent / "samples" / "p17.pdf"
    if not pdf.exists():
        pytest.skip("samples/p17.pdf is not present")
    expected = json.loads((root / "fixtures" / "p17-lines-20.json").read_text())
    doc, glyphs, got = pdfium.PdfDocument(pdf), GlyphNames(pdf), []
    try:
        for i in range(20):
            page = doc[i]
            try:
                for line in page_lines(page, i + 1, glyphs):
                    d = dataclasses.asdict(line)
                    assert d.pop("ocr") is False  # the new provenance field is not in the fixture
                    d.pop("sup")                  # nor are the raised runs (tested on their own)
                    d.pop("ligature_words")  # measured after the baseline was recorded
                    d.pop("runs")            # nor are the style runs (tested on their own)
                    assert d.pop("grades") is None   # nor the OCR word grades (no OCR here)
                    assert d.pop("stroke") is None   # nor the stroke weight (not a scan)
                    d["bbox"] = list(d["bbox"])
                    got.append(d)
            finally:
                page.close()
    finally:
        glyphs.close()
        doc.close()
    assert len(got) == len(expected)
    assert [d for d, e in zip(got, expected) if d != e] == []


def _raised_line(text, raised):
    """A line of 10 pt `text` whose characters at the indexes in `raised` are 6 pt and raised."""
    chars = []
    for i, ch in enumerate(text):
        small = i in raised
        chars.append(Char(ch, i * 5.0, 100 - (4 if small else 0), i * 5.0 + 5,
                           110 - (4 if small else 0), 6 if small else 10, False, False, "F",
                           False, False, False, 1))
    return one_line(chars)


def _marked(line):
    return [line.text[a:b] for a, b in line.sup]


def test_a_raised_letter_after_a_space_is_a_possible_marker():
    line = _raised_line("Some wordsa here", {10})
    assert _marked(line) == ["a"]


def test_a_raised_exponent_letter_after_a_digit_or_sign_is_not_a_marker():
    assert _marked(_raised_line("area is 10n units", {10})) == []
    assert _marked(_raised_line("so x=+a here", {5})) == []


def test_an_ordinal_suffix_is_not_a_marker():
    assert _marked(_raised_line("the 1st and 4th", {5, 6, 13, 14})) == []


def test_a_lowered_subscript_is_not_raised():
    # The 2 of H2O is set small and lowered: its bottom sits below the baseline, not above it.
    chars = [Char(ch, i * 5.0, 100 + (3 if ch == "2" else 0), i * 5.0 + 5,
                   110 + (3 if ch == "2" else 0), 6 if ch == "2" else 10, False, False, "F",
                   False, False, False, 1) for i, ch in enumerate("Pure H2O here")]
    assert _marked(one_line(chars)) == []


def test_ocr_form_blank_in_a_sentence_stays_one_line_until_a_gap_of_a_font_size():
    # "Name: ____    on file": 0.7 and 0.9 font sizes after the blank are still prose.
    cell = _ocr_chars("Name: ____", 400)  # ends at x=450, 10 pt type
    assert not starts_new_line(cell, _ocr_chars("o", 452)[0])  # an ordinary word space
    assert not starts_new_line(cell, _ocr_chars("o", 457)[0])
    assert not starts_new_line(cell, _ocr_chars("o", 459)[0])
    assert starts_new_line(cell, _ocr_chars("o", 461)[0])  # a full font size: another cell


def _styled_line(text, bold_from=None):
    """A line of 10 pt `text`; the characters from index `bold_from` on are bold. Spaces are
    set as the gap before the next character, as the extractor does."""
    chars = []
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        chars.append(Char(ch, i * 5.0, 100, i * 5.0 + 5, 110, 10,
                           bold_from is not None and i >= bold_from, False, "F",
                           text[i - 1] == " " if i else False, False, False, 1))
    return one_line(chars)


def test_a_line_of_one_style_has_no_runs():
    line = _styled_line("hearing or denial of the appeal.")

    assert line.runs == ()
    assert line.edge_style("tail") == (False, False)
    assert line.edge_style("head") == (False, False)


def test_a_bold_tail_is_a_second_run():
    text = "appeal. Do not"
    line = _styled_line(text, bold_from=len(text) - 6)

    assert [(r.start, r.end, r.bold) for r in line.runs] == [(0, 8, False), (8, 14, True)]
    assert line.bold is False
    assert line.edge_style("tail") == (True, False)
    assert line.edge_style("head") == (False, False)


def _row(text, x0, runs=(), sup=(), size=10.0, top=100.0):
    return Line(1, (x0, top, x0 + 5.0 * len(text), top + size), text, size, False, False, "F",
                runs=tuple(runs), sup=tuple(sup))


def test_a_list_marker_joined_to_its_text_keeps_the_bold_run():
    from reflow.analyze.lists import merge_list_markers
    text = "Pay the fee now"
    marker = _row("1.", 72.0)
    body = _row(text, 90.0, runs=[StyleRun(0, 8, False, False), StyleRun(8, 15, True, False)])

    [line] = merge_list_markers([marker, body])

    assert line.text == "1. Pay the fee now"
    assert [(r.start, r.end, r.bold) for r in line.runs] == [(0, 11, False), (11, 18, True)]
    assert line.text[11:] == "fee now"


def test_a_caption_cut_keeps_the_bold_word_and_its_run():
    from reflow.analyze.forms import apply_cuts, split_captions
    from reflow.extract.pages import PageContent
    from reflow.extract.forms import Widget
    caption = _row("CITY STATE ZIP", 100.0, top=194.7, size=6.0,
                   runs=[StyleRun(0, 11, False, False), StyleRun(11, 14, True, False)])
    boxes = [Widget(page=1, index=i, kind="text", bbox=(x, 200.9, x + 22.0, 212.3), flags=0)
             for i, x in enumerate((100.0, 125.0, 150.0))]
    page = PageContent(number=1, width=612, height=792, lines=[], images=[], widgets=boxes)

    parts = apply_cuts([caption], split_captions(page, [caption]))

    assert [p.text for p in parts] == ["CITY", "STATE", "ZIP"]
    assert [p.runs for p in parts[:2]] == [(StyleRun(0, 4, False, False),),
                                           (StyleRun(0, 5, False, False),)]
    assert parts[2].runs == (StyleRun(0, 3, True, False),)


def test_a_worksheet_label_cut_from_a_block_keeps_the_bold_run():
    from reflow.analyze import blocks
    line = _row("a. Do not attach evidence", 72.0,
                runs=[StyleRun(0, 6, False, False), StyleRun(6, 25, True, False)])

    assert blocks.Block([line], label="a.").text == "Do not attach evidence"
    cut = line.cut(3, 25)
    assert cut.text == "Do not attach evidence"
    assert [(r.start, r.end, r.bold) for r in cut.runs] == [(0, 3, False), (3, 22, True)]


def test_sup_offsets_survive_a_cut_a_join_and_a_retext():
    line = _row("see note1 and more", 72.0, sup=[(8, 9)])
    assert line.cut(4, 18).sup == ((4, 5),)
    assert line.cut(9, 18).sup == ()
    joined = _row("1.", 50.0).joined(line)
    assert joined.sup == ((11, 12),) and joined.text[11] == "1"
    assert line.retext("see note1 and more").sup == ((8, 9),)
    assert line.retext("see it").sup == () and line.retext("see it").runs == ()


def test_retext_keeps_runs_only_for_text_of_the_same_length():
    line = _row("ab cd", 72.0, runs=[StyleRun(0, 3, False, False), StyleRun(3, 5, True, False)])
    assert line.retext("AB CD").runs == line.runs
    shorter = line.retext("ab c")
    assert shorter.runs == () and (shorter.bold, shorter.italic) == (False, False)


def test_a_symbol_font_run_is_flagged():
    chars = []
    for i, (ch, font) in enumerate([("\u2610", "ABCDEF+Wingdings-Regular"), ("Y", "Arial"),
                                    ("e", "Arial"), ("s", "Arial")]):
        chars.append(Char(ch, i * 5.0, 100, i * 5.0 + 5, 110, 10, False, False, font,
                           False, False, False, 1))
    line = one_line(chars)
    assert [(r.start, r.end, r.symbol) for r in line.runs] == [(0, 1, True), (1, 4, False)]


def _errors_of(text, font="Arial", flagged=()):
    chars = [Char(ch, i * 5.0, 100, i * 5.0 + 5, 110, 10, False, False, font,
                   False, i in flagged, False, 1) for i, ch in enumerate(text)]
    return one_line(chars).unicode_errors


def test_a_bare_replacement_character_counts_as_a_unicode_error():
    # pdfium returns U+FFFD for a lost vowel sign without flagging a map error.
    assert _errors_of("ध्यान द� यद") == 1
    assert _errors_of("Chapter one") == 0


def test_a_replacement_leader_is_not_a_unicode_error():
    assert _errors_of("Housing ���� 12") == 0
    assert _errors_of("Housing ���� 1�2") == 1


def test_a_private_use_reading_in_a_word_counts_as_a_unicode_error_outside_an_icon_font():
    # A /ToUnicode into the Private Use Area maps the text font's letters to nothing (#342).
    assert _errors_of(" W-9") == 4
    assert _errors_of("", flagged=(0,)) == 4   # flagged once, not twice
    assert _errors_of("a") == 1                                  # a letter beside it
    # A lone private-use glyph between words is an icon or a bullet, whatever its font.
    assert _errors_of("Click  to save") == 0
    assert _errors_of(" First item") == 0
    # Symbol, Wingdings and the icon fonts draw pictures from it by design.
    assert _errors_of("", font="ABCDEF+Symbol") == 0
    assert _errors_of("", font="Wingdings-Regular") == 0
    assert _errors_of("", font="MaterialSymbolsOutlined") == 0
    assert _errors_of("", font="FontAwesome6Free-Solid") == 0
    assert _errors_of("") == 2                             # the same in Arial


def test_a_flagged_replacement_character_counts_once():
    assert _errors_of("a�b", flagged={1}) == 1
    assert _errors_of("abc", flagged={1}) == 1


def test_text_in_a_legacy_navajo_font_cannot_be_trusted():
    assert _errors_of("Bilag1ana y1n7[ti", font="YTWIXQ+TimesNewRomanNavajo") == 16
    assert _errors_of("Bilag1ana y1n7[ti", font="YTWIXQ+TimesNewRomanPSMT") == 0


def _fills_of(tmp_path, ops: bytes, cropbox=None):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(ExtGState=Dictionary(Clear=Dictionary(Type=Name.ExtGState, ca=0)))
    if cropbox:
        page.obj.CropBox = cropbox
    page.Contents = pdf.make_stream(ops)
    path = tmp_path / "fills.pdf"
    pdf.save(path)
    [content] = extract_pages(path)
    return content.fills


def test_coloured_fills_are_kept_and_white_or_clear_ones_are_not(tmp_path):
    fills = _fills_of(tmp_path, b"1 0 0 rg 100 700 20 10 re f "
                                b"0 0 1 rg 200 700 20 10 re f "
                                b"1 1 1 rg 300 700 20 10 re f "
                                b"q /Clear gs 0 1 0 rg 400 700 20 10 re f Q")
    assert [(f.rgb, f.bbox, f.shape) for f in fills] == [
        ((255, 0, 0), (100, 82, 120, 92), "box"), ((0, 0, 255), (200, 82, 220, 92), "box")]


def test_a_fill_painted_later_has_a_higher_order(tmp_path):
    red, grey = _fills_of(tmp_path, b"1 0 0 rg 100 700 40 10 re f 0.6 0.6 0.6 rg 110 700 20 10 re f")
    assert red.rgb == (255, 0, 0) and grey.rgb == (153, 153, 153)
    assert red.order < grey.order


def test_fill_boxes_are_in_crop_box_coordinates(tmp_path):
    # The crop box starts 8 pt above the media box's bottom, as the school calendar's does.
    [fill] = _fills_of(tmp_path, b"1 0 0 rg 100 700 20 10 re f", cropbox=[0, 8, 612, 800])
    assert fill.bbox == (100, 90, 120, 100)


def test_an_outline_ring_is_no_fill_and_a_star_is_a_mark(tmp_path):
    points = [(110, 720), (113, 712), (122, 712), (115, 707), (118, 698), (110, 703), (102, 698),
              (105, 707), (98, 712), (107, 712)]
    star = b"%d %d m " % points[0] + b"".join(b"%d %d l " % p for p in points[1:]) + b"h f "
    # A black ring around it, as a star's outline is drawn: an outer and an inner square, even-odd.
    ring = b"0 0 0 rg 90 690 m 130 690 l 130 730 l 90 730 l h 95 695 m 125 695 l 125 725 l 95 725 l h f*"
    fills = _fills_of(tmp_path, b"0 0.76 0.8 rg " + star + ring)
    assert [(f.rgb, f.shape) for f in fills] == [((0, 194, 204), "mark")]


def test_a_triangle_holds_only_its_half_of_its_box(tmp_path):
    [tri] = _fills_of(tmp_path, b"0 1 0 rg 100 700 m 120 700 l 120 720 l h f")
    # page y 700..720 → top-left 72..92; the triangle's corner is at the bottom right
    assert tri.bbox == (100, 72, 120, 92)
    assert tri.holds(118, 90) and not tri.holds(102, 74)


HEAVY_PAGE = [(100, "Conditions Affecting the District", True),
              (130, "Management is unaware of any conditions that could"),
              (146, "significantly impact the financial position of the"),
              (162, "District or any other factors that would matter")]


def test_ocr_lines_of_a_scanned_page_carry_their_stroke_weight(tmp_path):
    [page] = extract_pages(make_scan_pdf(tmp_path / "s.pdf", [HEAVY_PAGE]))
    heavy, *plain = page.lines
    assert all(line.ocr for line in page.lines)
    assert heavy.stroke >= 1.5
    assert all(abs(line.stroke - 1.0) <= 0.1 for line in plain)
    assert [line.stroke for line in page.lines] == list(page.scan.strokes)


def test_a_born_digital_line_has_no_stroke(tmp_path):
    assert lines_of(tmp_path, [(72, 100, 12, "Annual report of the district")])[0].stroke is None


def test_stroke_weights_are_the_same_serial_pooled_and_from_the_stage_cache(tmp_path):
    pdf = make_scan_pdf(tmp_path / "s.pdf", [HEAVY_PAGE, HEAVY_PAGE[::-1]])
    serial, tables = extract_document(pdf, 2, jobs=1)
    pooled, _ = extract_document(pdf, 2, jobs=2, chunk_pages=1)
    strokes = [[line.stroke for line in p.lines] for p in serial]
    assert all(s is not None for page in strokes for s in page)
    assert [[line.stroke for line in p.lines] for p in pooled] == strokes
    build = partial(extract_document, pdf, 2, jobs=1)
    assert stagecache.cached_extract(tmp_path / "cache", pdf, pdf, build)[1] is False
    cached, _ = stagecache.cached_extract(tmp_path / "cache", pdf, pdf, build)[0]
    assert [[line.stroke for line in p.lines] for p in cached] == strokes


def test_a_joined_line_is_no_longer_measured_and_a_cut_one_keeps_its_stroke():
    a = Line(1, (72, 100, 200, 112), "Conditions Affecting", 12, False, False, "OCR", ocr=True,
             stroke=2.1)
    b = Line(1, (210, 100, 300, 112), "Financial Position", 12, False, False, "OCR", ocr=True,
             stroke=1.0)
    assert a.joined(b).stroke is None
    assert a.cut(0, 10).stroke == 2.1
    assert a.retext("Conditions Affecting!").stroke == 2.1


def _actual_text_heading(tmp_path, gap: bytes) -> str:
    """The text of a heading set as the ADA dental claim form sets it: small capitals whose
    /ActualText is the lowercase word, the span's last capital drawn after a jump back to the
    line's start, then the rest of the word `gap` text-space units after it."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream(
        b"BT /F1 1 Tf 7 0 0 7 72 700 Tm (A) Tj ET\n"
        b"BT /Span << /ActualText (treat) >> BDC /F1 1 Tf 7 0 0 7 76.67 700 Tm (TREA) Tj "
        b"7 0 0 7 20 700 Tm 10.763 0 Td (T) Tj EMC ET\n"
        b"BT /F1 1 Tf 7 0 0 7 " + gap + b" 700 Tm (MENT) Tj ET")
    path = tmp_path / "actual.pdf"
    pdf.save(path)
    [content] = extract_pages(path)
    return " ".join(line.text for line in content.lines)


def test_a_glyph_given_to_actual_text_leaves_no_space_inside_its_word(tmp_path):
    # The last T of "TREAT" is drawn at x 95.34 and has no character of its own: pdfium spreads
    # "treat" over "TREA" and generates a space before "MENT", placed back at x 76.67.
    assert _actual_text_heading(tmp_path, b"99.62") == "AtreatMENT"


def test_a_word_gap_after_an_actual_text_span_stays(tmp_path):
    assert _actual_text_heading(tmp_path, b"102.5") == "Atreat MENT"


def test_a_currency_sign_drawn_after_its_figure_is_read_before_it(tmp_path):
    # A spreadsheet export draws each cell's "$" as an object of its own, after the figure on its
    # left, though it prints left of its own figure (audit p24: "8,426,474 $" for "$ 8,426,474").
    lines = lines_of(tmp_path, [(100, 100, 10, "8,426,474"), (86, 100, 10, "$"),
                                (200, 100, 10, "7,346,459"), (186, 100, 10, "$")])
    assert [l.text for l in lines] == ["$ 8,426,474", "$ 7,346,459"]
    assert 85 < lines[0].x0 < 88 and lines[0].x1 < 150


def test_a_currency_sign_ahead_of_the_next_figure_is_not_the_figure_before_it(tmp_path):
    # "124,544" is followed by the "$" of the next cell: the sign is a segment of its own, to be
    # joined to the figure on its right (see join_signs), not glued to the figure it follows.
    lines = lines_of(tmp_path, [(100, 100, 10, "124,544"), (150, 100, 10, "$"),
                                (200, 100, 10, "106,865")])
    assert [l.text for l in lines] == ["124,544", "$", "106,865"]


def test_a_dollar_sign_that_ends_a_line_stays_with_it(tmp_path):
    # A form's "3(a) $" is the line number and the blank's sign: nothing follows to take the sign.
    [line] = lines_of(tmp_path, [(100, 100, 10, "3(a)"), (130, 100, 10, "$")])
    assert line.text == "3(a) $"


def test_figures_drawn_out_of_order_are_lines_of_their_own_with_their_tails_joined(tmp_path):
    # A spreadsheet export draws a row's second figure before its first, and the last digit of a
    # figure after the next cell's objects (audit p75: "6,634,958. 6,854,060.0", "$ 14,215,31 9").
    [first] = lines_of(tmp_path, [(100, 100, 10, "6,854,060.")])
    lines = lines_of(tmp_path, [(200, 100, 10, "6,634,958."), (100, 100, 10, "6,854,060."),
                                (first.x1, 100, 10, "0"), (300, 100, 10, "2,966,949.")])
    assert sorted((round(l.x0), l.text) for l in lines) == [
        (100, "6,854,060.0"), (200, "6,634,958."), (300, "2,966,949.")]


def _run_chars(text, x0, run, space=False):
    return [Char(ch, x0 + 5 * k, 100.0, x0 + 5 * (k + 1), 110.0, 10, False, False, "Arial",
                 space and k == 0, False, False, run) for k, ch in enumerate(text)]


def test_a_break_pdfium_puts_inside_a_row_is_a_space_only_where_the_gap_is_one():
    # fw9 p5 with one Tj per glyph (#324): pdfium breaks the line before the raised "1" of
    # "owner1", which sits against the "r". A break before a word a space away still spaces it.
    owner = _run_chars("owner", 100.0, 1)
    note = Char("1", owner[-1].x1, 97.0, owner[-1].x1 + 3, 104.0, 6, False, False, "Arial",
                False, False, False, 2)
    assert one_line([*owner, None, note]).text == "owner1"
    word = _run_chars("of", owner[-1].x1 + 3, 3)
    assert one_line([*owner, None, *word]).text == "owner of"


def test_a_rotated_row_broken_at_every_glyph_keeps_its_word_spaces():
    # The arXiv margin stamp with one Tj per glyph (#324): pdfium breaks the rotated row after
    # every glyph and gives no space, so "2 Aug 2023" read "2Aug2023".
    def glyphs(text, bottom):  # read upward: each glyph sits 10 pt above the one before
        out = []
        for ch in text:
            if ch == " ":
                bottom -= 5
                continue
            out += [Char(ch, 14.0, bottom - 10, 37.0, bottom, 20, False, False, "Times",
                         False, False, True, None, angle=math.pi / 2), None]
            bottom -= 10
        return out

    assert one_line(glyphs("2 Aug 2023", 300.0)[:-1]).text == "2 Aug 2023"


def test_a_figure_drawn_after_the_na_on_its_right_is_a_cell_of_its_own():
    # Audit p85: the export draws a row's "N/A" (debt per capita) before the population figure on
    # its left, with no line break between them. Read as one line, the two cells were one:
    # "N/A 759,155".
    lines = lines_from_chars([*_run_chars("N/A", 300.0, 1), *_run_chars("759,155", 200.0, 2, True)], 1)
    assert sorted((round(l.x0), l.text) for l in lines) == [(200, "759,155"), (300, "N/A")]


def test_a_word_that_ends_in_na_is_no_cell_to_jump_back_from():
    chars = [*_run_chars("DN/A", 300.0, 1), *_run_chars("759,155", 200.0, 2, True)]
    assert len(lines_from_chars(chars, 1)) == 1


def test_a_digit_after_the_next_cells_figure_joins_the_figure_it_ends(tmp_path):
    [first] = lines_of(tmp_path, [(100, 100, 10, "14,215,31")])
    lines = lines_of(tmp_path, [(100, 100, 10, "14,215,31"), (200, 100, 10, "9,641,176"),
                                (first.x1 + 0.1, 100, 10, "9")])
    assert sorted(l.text for l in lines) == ["14,215,319", "9,641,176"]


def test_a_number_in_a_sentence_is_not_split(tmp_path):
    [line] = lines_of(tmp_path, [(100, 100, 10, "Costs 1,000 and 250 apply")])
    assert line.text == "Costs 1,000 and 250 apply"
