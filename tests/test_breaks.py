"""Block breaks: where the weight of a line changes and where it does not."""

from dataclasses import replace

from reflow.analyze.blocks import Break
from reflow.analyze.breaks import break_reason
from reflow.extract.lines import Line, StyleRun


def line(top, text, *, bold=False, runs=()):
    return Line(page=1, bbox=(72.0, top, 272.0, top + 10), text=text, size=8.5, bold=bold,
                italic=False, font="Helvetica", runs=runs)


def test_a_bold_sentence_that_wraps_stays_in_one_paragraph():
    # Assessment appeal p1: "... denial of the appeal. **Do not** / **attach hearing evidence**"
    first = line(100, "denial of the appeal. Do not",
                 runs=(StyleRun(0, 22, False, False), StyleRun(22, 28, True, False)))
    second = line(110, "attach hearing evidence to this application.", bold=True)

    assert break_reason(first, second) is None


def test_a_bold_heading_over_plain_text_is_still_a_block():
    heading = line(100, "Mailing address", bold=True)
    body = line(116, "Send the form to the clerk.")

    assert break_reason(heading, body) == Break.BOLD_CHANGES


def test_a_bold_heading_set_solid_under_a_paragraph_ending_in_bold_is_still_a_block():
    # Assessment appeal p4: "... the tax bill." (bold tail) then "SECTION 6." set 2.8 pt below
    para = line(100, "your appeal. It is required that you attach the tax bill.",
                runs=(StyleRun(0, 13, False, False), StyleRun(13, 57, True, False)))
    heading = line(112.8, "SECTION 6.", bold=True)

    assert break_reason(para, heading) == Break.BOLD_CHANGES


def test_banners_of_two_columns_on_one_row_are_two_blocks():
    # directory p2: the "Broomfield" and "Delta" banners, each alone in its ruled box
    from reflow.analyze.blocks import Block
    from reflow.analyze.breaks import split_banners
    left = Line(page=1, bbox=(125.6, 436.9, 202.7, 453.7), text="Broomfield", size=14.0,
                bold=True, italic=False, font="Helvetica")
    right = Line(page=1, bbox=(437.3, 438.1, 474.5, 454.9), text="Delta", size=14.0,
                 bold=True, italic=False, font="Helvetica")
    boxes = [(22.0, 430.0, 290.0, 460.0), (314.0, 430.0, 580.0, 460.0)]
    blocks = split_banners([Block([left, right])], boxes, [left, right])
    assert [b.text for b in blocks] == ["Broomfield", "Delta"]
    # with no ruled boxes around them the two lines stay one block (a bold label row)
    assert len(split_banners([Block([left, right])], [], [left, right])) == 1


def test_a_short_last_line_set_flush_right_continues_an_rtl_paragraph():
    # az-hca-nondiscrim-multilang p3: the Arabic tagline wraps its "(711" onto a short line at
    # the right margin. Under left-to-right text the same line is a first-line indent.
    def at(x0, x1, top, text):
        return Line(page=1, bbox=(x0, top, x1, top + 14), text=text, size=12.0, bold=False,
                    italic=False, font="ArialMT")

    tail = at(552.0, 576.0, 437.0, "(711")
    assert break_reason(at(88.0, 576.0, 423.0, "اتصل على 1-800-322-8670 (ھاتف نصي:"), tail) is None
    assert break_reason(at(36.0, 560.0, 423.0, "Call 1-800-322-8670 (TTY:"), tail) == Break.FIRST_LINE_INDENT


# da-legislation p5, a double-spaced scan: baselines 23 pt apart, boxes 11.4 pt high, text from
# x 80 to 547, the "(1)" items indented to x 160 and their wrapped lines back at the margin.
LEGISLATION_PITCH = 23.0


def scan_line(top, text, *, x0=80.0, x1=547.0):
    return Line(page=5, bbox=(x0, top, x1, top + 11.4), text=text, size=11.0, bold=False,
                italic=False, font="GlyphLessFont", ocr=True)


def _rhythm():
    from reflow.analyze.rhythm import Rhythm
    return Rhythm(LEGISLATION_PITCH)


def test_a_line_at_the_page_pitch_does_not_break_and_one_at_twice_it_does():
    prev = scan_line(280.0, "court, each party or the party's attorney shall include in its")
    at_pitch = scan_line(280.0 + LEGISLATION_PITCH, "initial pleading of the party in the case")
    at_twice = scan_line(280.0 + 2 * LEGISLATION_PITCH, "initial pleading of the party in the case")

    assert break_reason(prev, at_pitch, rhythm=_rhythm()) is None
    assert break_reason(prev, at_twice, rhythm=_rhythm()).startswith("gap of")
    assert break_reason(prev, at_pitch).startswith("gap of")  # no rhythm: today's rule


def test_an_indented_line_at_the_pitch_still_breaks():
    prev = scan_line(582.6, "current residence or business address.", x1=344.0)
    section = scan_line(582.6 + LEGISLATION_PITCH, "SECTION 7. Section 30.016(a), Civil Practice",
                        x0=124.6)

    assert break_reason(prev, section, rhythm=_rhythm()) == Break.FIRST_LINE_INDENT


def _legislation_lines():
    tops = [326.3 + LEGISLATION_PITCH * i for i in range(7)]
    return [
        scan_line(tops[0], "(1) the last three numbers of the party's driver's", x0=160.0),
        scan_line(tops[1], "license number, if the party has been issued a driver's license; and"),
        scan_line(tops[2], "(2) the last three numbers of the party's social", x0=160.0),
        scan_line(tops[3], "security number, if the party has been issued a social security"),
        scan_line(tops[4], "number.", x1=128.0),
        scan_line(tops[5], "SECTION 7. Section 30.016(a), Civil Practice and Remedies", x0=124.6),
        scan_line(tops[6], "Code, is amended to read as follows:", x1=325.0),
    ]


def test_a_list_item_wrapped_at_the_page_pitch_is_one_item():
    from reflow.analyze.breaks import make_blocks

    blocks = make_blocks(_legislation_lines(), rhythm=_rhythm())

    texts = [(b.item, b.text) for b in blocks]
    assert texts == [
        (True, "(1) the last three numbers of the party's driver's license number, if the party "
               "has been issued a driver's license; and"),
        (True, "(2) the last three numbers of the party's social security number, if the party "
               "has been issued a social security number."),
        (False, "SECTION 7. Section 30.016(a), Civil Practice and Remedies Code, is amended to "
                "read as follows:"),
    ]


def test_without_a_rhythm_the_same_lines_keep_todays_blocks():
    from reflow.analyze.breaks import make_blocks

    blocks = make_blocks(_legislation_lines())

    assert len(blocks) == 7
    assert [b.item for b in blocks][:3] == [True, False, True]


def test_a_lowercase_line_at_the_pitch_stays_in_its_item():
    # leaves_item on its own: a numbered item ("1.", not a legal label, so no legal-wrap join)
    # whose sentence wraps back to the margin at the page's pitch.
    from reflow.analyze.blocks import Block, leaves_item, marker_of

    first = scan_line(326.3, "1. the last three numbers of the party's driver's", x0=160.0)
    wrap = scan_line(326.3 + LEGISLATION_PITCH, "license number, if the party has a license; and")
    item = Block([first], marker=marker_of(first.text, ""), item=True)

    assert leaves_item(item, wrap, None) is True
    assert leaves_item(item, wrap, None, rhythm=_rhythm()) is False


PX = 300 / 72   # the OCR image's pixels per point
ROWS = [(100, "Challenges in front of us"), (116, "We have multiple challenges in front"),
        (132, "of us and a few that stand out")]


def _underlined_scan(tmp_path, rule_to=None):
    """A page OCR'd by reflow: invisible words over nothing, and the grades its plugin would
    write from an image of those words with a line under the first row, which ends with the row's
    words, or runs on to `rule_to` points like a rule. Returns the page's lines, graded."""
    import pikepdf
    import pypdfium2 as pdfium
    from pikepdf import Dictionary, Name
    from PIL import Image, ImageDraw

    from reflow.extract.pages import extract_pages
    from reflow.extract.ocrwords import layer_words
    from reflow.ingest.ocr_trust import (PageGrade, RawWord, grade_words, track_changes,
                                         write_sidecar)

    path = tmp_path / "u.pdf"
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream("\n".join(
        f"BT 3 Tr /F1 12 Tf 72 {792 - top - 12} Td ({text}) Tj ET" for top, text in ROWS).encode())
    pdf.save(path)
    doc = pdfium.PdfDocument(path)
    try:
        words = layer_words(doc[0])
    finally:
        doc.close()
    image = Image.new("L", (round(612 * PX), round(792 * PX)), 255)
    draw = ImageDraw.Draw(image)
    raw = []
    for text, (x0, top, x1, bottom) in words:
        box = tuple(round(v * PX) for v in (x0, top, x1, bottom))
        row = next(i for i, (t, _) in enumerate(ROWS) if abs(t - top) < 8)
        base = box[1] + round(0.78 * (box[3] - box[1]))   # the loose box holds the descenders
        for x in range(box[0], box[2] - 4, 10):   # the letters' stems, x-height to baseline
            draw.rectangle((x, box[1] + round(0.3 * (box[3] - box[1])), x + 4, base), fill=0)
        raw.append(RawWord(box, text, 95, row, base=base))
    first = [w for w in raw if w.line == 0]
    y = first[0].base + 10   # in the band under the baseline that the probe reads
    draw.rectangle((first[0].box[0], y, round(rule_to * PX) if rule_to else first[-1].box[2],
                    y + 3), fill=0)
    graded = [replace(w, layer_text=w.text)
              for w in track_changes(grade_words(raw, image), raw, image)[0]]
    write_sidecar(path, [PageGrade(1, "redo", image.size, tuple(graded))])
    [content] = extract_pages(path)
    assert all(line.grades for line in content.lines)
    return content.lines


def test_an_underlined_ocr_line_is_a_block_of_its_own(tmp_path):
    from reflow.analyze.breaks import make_blocks
    from reflow.analyze.headings import underlined
    lines = _underlined_scan(tmp_path)
    assert [underlined(line) for line in lines] == [True, False, False]
    blocks = make_blocks(lines)
    assert [b.text for b in blocks] == [ROWS[0][1], f"{ROWS[1][1]} {ROWS[2][1]}"]
    assert blocks[1].why == "underlined line ends"


def test_a_rule_running_on_past_the_line_is_no_underline(tmp_path):
    from reflow.analyze.breaks import make_blocks
    lines = _underlined_scan(tmp_path, rule_to=560)
    assert [len(b.lines) for b in make_blocks(lines)] == [3]


def test_an_underlined_insertion_that_carries_its_sentence_on_stays_in_its_paragraph():
    # Legislation p28: an inserted line of a tracked change, underlined in full.
    from test_headings import underlined_line
    inserted = underlined_line([(w, True) for w in "action was served with process".split()])
    ends = underlined_line([(w, True) for w in "served with process by the court.".split()])
    heading = underlined_line([(w, True) for w in "Challenges in front of us".split()])
    def under(text):
        return replace(line(112, text), size=10.0, bbox=(72.0, 112.0, 244.0, 122.0))
    assert break_reason(inserted, under("the supreme court; or")) is None
    assert break_reason(ends, under("The clerk shall file it.")) is None
    assert break_reason(heading, under("We have multiple challenges")) == "underlined line ends"


def at(top, text, x0, x1, *, bold=False):
    return Line(page=1, bbox=(x0, top, x1, top + 10), text=text, size=8.5, bold=bold,
                italic=False, font="Helvetica")


def test_a_column_label_over_a_line_that_ends_before_it_begins_is_a_block_of_its_own():
    # da-financial-audit-comprehensive p3: "Page", set over the page numbers at the right edge,
    # was joined to the section title under it ("Page INTRODUCTORY SECTION").
    label = at(100, "Page", 540, 574, bold=True)
    section = at(111, "INTRODUCTORY SECTION", 50, 169, bold=True)

    assert break_reason(label, section) == (
        "the line above sits wholly to the right of this one (a column label)")


def test_a_wrapped_line_that_shares_a_column_with_the_one_above_stays_in_the_block():
    first = at(100, "The title of the section goes on", 50, 250)
    second = at(111, "and wraps here", 50, 130)

    assert break_reason(first, second) is None


def test_a_group_over_its_first_entry_on_a_contents_page_is_two_blocks():
    # da-financial-audit-comprehensive p3: "Governmental Fund Financial Statements" over the
    # indented "Balance Sheet ........ 26". A title that wraps stops where a phrase cannot, or
    # the next line goes on in lowercase.
    leader = "." * 60
    page = [at(100, "Governmental Fund Financial Statements", 77, 280),
            at(111, "Balance Sheet" + leader + " 26", 86, 561)]
    assert break_reason(page[0], page[1], page_lines=page) == "contents entry"
    page = [at(100, "Reconciliation of the Governmental Funds Balance Sheet", 86, 361),
            at(111, "to the Government-Wide Statement of Net Position" + leader + " 27", 95, 561)]
    assert break_reason(page[0], page[1], page_lines=page) is None
    page = [at(100, "Reconciliation of the Funds Statement of Revenues, Expenditures, and", 86, 480),
            at(111, "Changes in Fund Balances" + leader + " 29", 95, 561)]
    assert break_reason(page[0], page[1], page_lines=page) is None


def test_ocr_lines_and_lines_on_one_row_are_not_split_by_the_column_label_rule():
    label = at(100, "Page", 540, 574, bold=True)
    side = at(100, "SECTION", 50, 169, bold=True)             # the same row: the reader joins them
    assert break_reason(label, side) != (
        "the line above sits wholly to the right of this one (a column label)")
    scanned = at(111, "INTRODUCTORY SECTION", 50, 169, bold=True)
    scanned.ocr = True
    assert break_reason(label, scanned) != (
        "the line above sits wholly to the right of this one (a column label)")
