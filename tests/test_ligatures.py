"""Ligatures in the text layer: Unicode ligature code points, glyphs named by the font, and
the stray space pdfium reports inside a ligature's advance."""

import pikepdf
from pikepdf import Dictionary, Name

from reflow.extract.fonts import ligature_letters
from testkit import line_texts

# Codes in the test font: 1 is a ligature, 2 and 3 are letters, 32 is a space.
WIDTHS = [680, 500, 500] + [0] * 28 + [278]


def _cmap(pairs):
    """A ToUnicode CMap mapping each (code, UTF-16 hex) pair."""
    body = "".join(f"<{code:02X}> <{text}>\n" for code, text in pairs)
    return (f"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            f"1 begincodespacerange <00> <FF> endcodespacerange\n"
            f"{len(pairs)} beginbfchar\n{body}endbfchar\nendcmap end end").encode()


def _pdf(tmp_path, ops, to_unicode=None, encoding=None, widths=WIDTHS):
    """One page with a non-embedded Helvetica as /F1, with the ligature widths above."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                      FirstChar=1, LastChar=32, Widths=widths)
    if to_unicode is not None:
        font.ToUnicode = pdf.make_stream(to_unicode)
    if encoding is not None:
        font.Encoding = encoding
    page.Resources = Dictionary(Font=Dictionary(F1=pdf.make_indirect(font)))
    page.Contents = pdf.make_stream(ops)
    path = tmp_path / "lig.pdf"
    pdf.save(path)
    return path


def test_glyph_names_of_ligatures_read_as_their_letters():
    assert ligature_letters("f_i") == "fi"
    assert ligature_letters("f_f.liga") == "ff"
    assert ligature_letters("f_f_l") == "ffl"
    assert ligature_letters("s_t") == "st"
    assert ligature_letters("nine.sup") is None
    assert ligature_letters("f") is None


def test_a_unicode_ligature_code_point_is_written_as_its_letters(tmp_path):
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x01\x02) Tj ET",
                to_unicode=_cmap([(1, "FB01"), (2, "0061")]))
    assert line_texts(path) == ["fia"]


def test_a_stray_space_inside_a_ligature_is_dropped(tmp_path):
    # The ligature glyph is 6.8 pt wide; the next glyph is moved back so the space starts
    # 3.42 pt into it, as in a font whose "fi" advance is narrower than its box.
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td [(\x01) 338 ( ) 0 (\x02\x03)] TJ ET",
                to_unicode=_cmap([(1, "00660069"), (2, "0061"), (3, "0062")]))
    assert line_texts(path) == ["fiab"]


def test_a_word_space_after_a_ligature_is_kept(tmp_path):
    # The space starts where the ligature's box ends, so it is a word gap.
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x02\x01) Tj ( ) Tj (\x03\x02) Tj ET",
                to_unicode=_cmap([(1, "00660069"), (2, "0061"), (3, "0062")]))
    assert line_texts(path) == ["afi ba"]


def test_an_unmapped_glyph_the_font_names_as_a_ligature_reads_as_its_letters(tmp_path):
    # pdfium cannot map code 1 to Unicode; the font's /Differences names it f_f.
    encoding = Dictionary(Type=Name.Encoding, Differences=[1, Name("/f_f"), Name("/i")])
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x02\x01) Tj ET", encoding=encoding)
    assert line_texts(path) == ["iff"]


def test_a_ligature_pdfium_reports_as_a_control_character_is_not_a_space(tmp_path):
    # Code 31 is named f_i; pdfium reports it as U+001F, which reads as a space.
    encoding = Dictionary(Type=Name.Encoding, Differences=[31, Name("/f_i"), Name("/x")])
    widths = [0] * 30 + [680, 278]  # code 31 is 6.8 pt wide, code 32 is 2.78 pt
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x1f\x20) Tj ET", encoding=encoding,
                widths=widths)
    assert line_texts(path) == ["fix"]


def test_a_ligature_named_in_a_form_xobject_font_is_read(tmp_path):
    # The font is in the resources of a form the page draws, not of the page itself.
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, FirstChar=1,
                      LastChar=32, Widths=WIDTHS,
                      Encoding=Dictionary(Type=Name.Encoding,
                                          Differences=[1, Name("/f_f"), Name("/i")]))
    form = pdf.make_stream(b"BT /F9 10 Tf 72 700 Td (\x02\x01) Tj ET")
    form.Type = Name.XObject
    form.Subtype = Name.Form
    form.BBox = [0, 0, 612, 792]
    form.Resources = Dictionary(Font=Dictionary(F9=pdf.make_indirect(font)))
    page.Resources = Dictionary(XObject=Dictionary(Fm0=pdf.make_indirect(form)))
    page.Contents = pdf.make_stream(b"/Fm0 Do")
    path = tmp_path / "form.pdf"
    pdf.save(path)
    assert line_texts(path) == ["iff"]


# A page whose only glyph is one code draws nothing in pdfium's text layer, so these tests add a
# Latin "a" after the Arabic glyph.


def test_a_lam_alef_in_one_box_reads_in_logical_order(tmp_path):
    # The font maps the ligature to logical "لا" (lam, alef), one box. pdfium reports the
    # letters in drawn order (alef first), so they must be put back.
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x01\x02) Tj ET",
                to_unicode=_cmap([(1, "06440627"), (2, "0061")]))
    assert line_texts(path) == ["لاa"]


def test_a_joined_pair_in_one_box_reads_in_logical_order(tmp_path):
    # Lam then alef-maksura ("لى") in one box, as in the Arabic Hepatitis B handout.
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x01\x02) Tj ET",
                to_unicode=_cmap([(1, "06440649"), (2, "0061")]))
    assert line_texts(path) == ["لىa"]


def test_a_lam_alef_presentation_form_reads_as_its_logical_letters(tmp_path):
    # U+FEFB is the lam-alef presentation form; NFKC gives "لا", which is already logical.
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x01\x02) Tj ET",
                to_unicode=_cmap([(1, "FEFB"), (2, "0061")]))
    assert line_texts(path) == ["لاa"]


def test_letters_in_separate_boxes_are_not_reordered_by_the_joined_glyph_rule(tmp_path):
    # Each letter has its own box; the order is pdfium's, which reads this run as "لا".
    path = _pdf(tmp_path, b"BT /F1 10 Tf 72 700 Td (\x02\x03) Tj ET",
                to_unicode=_cmap([(2, "0627"), (3, "0644")]))
    assert line_texts(path) == ["لا"]


def test_rtl_letters_drawn_in_logical_pieces_read_in_logical_order(tmp_path):
    # InDesign's Arabic taglines (az-hca-nondiscrim-multilang p3): "إذا" is drawn glyph by glyph
    # in logical order, each moved left by a kern, then "با" as one piece drawn left to right
    # after a kerned word gap. pdfium reverses the whole object ("بااذإ"); the letters read
    # right to left on the line.
    widths = [680, 500, 500, 500, 500] + [0] * 26 + [278]
    path = _pdf(tmp_path, b"BT /F1 10 Tf 200 700 Td [(\x02) 1000 (\x03) 1000 (\x04) 1800 (\x04\x05)] TJ ET",
                to_unicode=_cmap([(2, "0625"), (3, "0630"), (4, "0627"), (5, "0628")]),
                widths=widths)
    assert line_texts(path) == ["إذا با"]
