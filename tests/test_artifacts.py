"""Which margin lines are page furniture (artifact_reasons), built from synthetic lines."""

import math
from dataclasses import replace

from reflow.analyze.artifacts import artifact_reasons
from reflow.extract.drawings import Drawing
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent

WIDTH, HEIGHT = 612.0, 792.0
BODY = 10.0


def line(page, top, text, *, x0=72.0, width=200.0, size=BODY, bold=False):
    return Line(page=page, bbox=(x0, top, x0 + width, top + size * 1.2), text=text, size=size,
                bold=bold, italic=False, font="Helvetica")


def document(n_pages, build):
    """build(n) → the lines of page n (1-based); every page is HEIGHT points tall."""
    return [PageContent(number=n, width=WIDTH, height=HEIGHT, lines=build(n), images=[])
            for n in range(1, n_pages + 1)]


def reason_of(pages, text, page_number=None):
    reasons = artifact_reasons(pages, body=BODY)
    return [reasons.get(id(l)) for p in pages for l in p.lines
            if l.text == text and (page_number is None or p.number == page_number)]


def test_column_top_address_is_content_not_running_header():
    # The clinic address heads the second column on 30 of 40 pages, in the top band, and the
    # listing continues directly under it. Its count alone must not make it a header.
    def build(n):
        lines = [line(n, 300, "Body text of the listing.")]
        if n <= 30:
            lines.append(line(n, 68, "20950 N Tatum Blvd, Ste 190", x0=221, width=145, size=11))
            lines.append(line(n, 80.6, "Phoenix, AZ 85050", x0=221, width=145, size=11))
        return lines
    pages = document(40, build)
    assert reason_of(pages, "20950 N Tatum Blvd, Ste 190") == [None] * 30


def test_table_row_in_the_bottom_band_is_content():
    # Rows of a table in the bottom band, 8 points apart. The middle row repeats on 30 of
    # 40 pages at the same height, but the rows above and below it are text of the same stack.
    def build(n):
        lines = [line(n, 300, "Body text of the report.")]
        if n <= 30:
            lines.append(line(n, 716, "Budget authority, gross", size=7))
            lines.append(line(n, 724, "Outlays from discretionary balances", size=7))
            lines.append(line(n, 732, "Outlays, net", size=7))
        return lines
    pages = document(40, build)
    assert reason_of(pages, "Outlays from discretionary balances") == [None] * 30
    assert reason_of(pages, "Budget authority, gross") == [None] * 30


def test_a_bold_title_block_of_three_rows_on_every_page_is_a_running_head():
    """The charter prints its name on two rows and its date on a third at the top of every
    page. The first page keeps them; the others drop them."""
    def build(n):
        return [line(n, 34, "BLUE CROSS AND BLUE SHIELD OF ARIZONA, INC.", x0=168, bold=True),
                line(n, 47, "AUDIT, COMPLIANCE, AND RISK COMMITTEE CHARTER", x0=156, bold=True),
                line(n, 60, "MARCH 2026", x0=271, width=70, bold=True),
                line(n, 300, "Body text of the charter.")]
    pages = document(4, build)
    for text in ("BLUE CROSS AND BLUE SHIELD OF ARIZONA, INC.", "MARCH 2026"):
        assert reason_of(pages, text) == [None] + ["running header or footer, repeats on 4 pages"] * 3


def test_true_running_header_is_dropped():
    def build(n):
        return [line(n, 20, "Annual Report 2025", size=9), line(n, 300, "Body text of the report.")]
    pages = document(40, build)
    assert reason_of(pages, "Annual Report 2025") == [
        "running header or footer, repeats on 40 pages"] * 40


def test_running_footer_a_few_points_under_body_text_is_dropped():
    # A footer printed close under the last body line is still furniture: the gap is wider
    # than half a font size, and the body above is not in the footer's column.
    def build(n):
        return [line(n, 300, "Body text of the report."),
                line(n, 690, "The last line of the body text.", size=8),
                line(n, 750, "Publication 17 (2025)", size=8)]
    pages = document(40, build)
    assert reason_of(pages, "Publication 17 (2025)") == [
        "running header or footer, repeats on 40 pages"] * 40


def test_true_page_number_is_dropped():
    # A folio alone on its row, at the bottom of each page, following the page index.
    def build(n):
        return [line(n, 300, "Body text of the report."), line(n, 760, str(n), size=8)]
    pages = document(40, build)
    assert reason_of(pages, "7") == ["page number"]
    assert reason_of(pages, "40") == ["page number"]


def test_number_in_the_text_is_content():
    # "501" sits in the bottom band of one page only, in a row of its own: a publication
    # number, not a folio. It does not follow the page index, so it stays.
    def build(n):
        lines = [line(n, 300, "Body text of the report."), line(n, 760, str(n), size=8)]
        if n == 20:
            lines.append(line(n, 704, "501", x0=300, size=8))
        return lines
    pages = document(40, build)
    assert reason_of(pages, "501", page_number=20) == [None]


def test_table_row_figure_in_the_bottom_band_is_content():
    # A budget row in the bottom band: its figure equals the page number and follows the page
    # index on the neighbouring pages, but the row also carries a label, so it is a figure.
    def build(n):
        label = "4000 Budget authority, line " + "".join("abcdefghij"[int(d)] for d in str(n))
        return [line(n, 300, "Body text of the report."),
                line(n, 723, label, x0=36, width=180, size=7),
                line(n, 723, str(n), x0=573, width=12, size=7)]
    pages = document(40, build)
    figures = [l for p in pages for l in p.lines if l.text.isdigit() and l.top == 723]
    reasons = artifact_reasons(pages, body=BODY)
    assert [reasons.get(id(l)) for l in figures] == [None] * 40


def test_folio_beside_a_running_footer_is_dropped():
    # The footer's text repeats in place, so it is furniture: the folio beside it is alone on
    # its row and still a page number.
    def build(n):
        return [line(n, 300, "Body text of the report."),
                line(n, 760, "Publication 17 (2025)", x0=72, width=150, size=8),
                line(n, 760, str(n), x0=540, width=12, size=8)]
    pages = document(40, build)
    assert reason_of(pages, "7") == ["page number"]
    assert reason_of(pages, "Publication 17 (2025)") == [
        "running header or footer, repeats on 40 pages"] * 40


def test_folio_beside_a_words_only_running_head_is_dropped():
    # A page's own running head ("Federal Funds—Continued", its words vary from page to page)
    # sits on the folio's row: words without figures are furniture, not a table row.
    def build(n):
        head = "Section " + "".join("abcdefghij"[int(d)] for d in str(n)) + " continued"
        return [line(n, 300, "Body text of the report."),
                line(n, 34, head, x0=36, width=200, size=9),
                line(n, 34, str(n), x0=573, width=12, size=11)]
    pages = document(40, build)
    assert reason_of(pages, "7") == ["page number"]


WATERMARK = "Page Proof Pending Publication"


def drawn(page, top, text, *, x0=150.0, width=300.0, size=21.7, ink=1.0, angle=0.0):
    """A line with the text's drawn look: ink (1 black, 0.15 a transparent watermark) and the
    baseline angle in radians. rotated means the angle is off the horizontal."""
    rotated = min(angle, 2 * math.pi - angle) > 0.05
    return Line(page=page, bbox=(x0, top, x0 + width, top + size * 1.2), text=text, size=size,
                bold=False, italic=False, font="Helvetica", rotated=rotated, angle=angle,
                ink=ink)


def test_faint_watermark_repeated_on_every_page_is_an_artifact():
    def build(n):
        return [line(n, 300, "Body text of the opinion."),
                drawn(n, 380, WATERMARK, ink=0.15)]
    pages = document(20, build)
    assert reason_of(pages, WATERMARK) == ["watermark: faint text repeated on 20 pages"] * 20
    assert reason_of(pages, "Body text of the opinion.") == [None] * 20


def test_diagonal_watermark_repeated_on_every_page_is_an_artifact():
    def build(n):
        return [line(n, 300, "Body text of the opinion."),
                drawn(n, 380, WATERMARK, x0=200, width=250, angle=math.radians(40))]
    pages = document(20, build)
    assert reason_of(pages, WATERMARK) == ["watermark: diagonal text repeated on 20 pages"] * 20
    assert reason_of(pages, "Body text of the opinion.") == [None] * 20


def test_single_diagonal_line_on_one_page_is_content():
    def build(n):
        lines = [line(n, 300, "Body text of the opinion.")]
        if n == 5:
            lines.append(drawn(n, 380, WATERMARK, x0=200, width=250, angle=math.radians(40)))
        return lines
    pages = document(20, build)
    assert reason_of(pages, WATERMARK) == [None]


def test_faint_label_at_varying_positions_is_content():
    # A pale "Website:" label that moves from page to page is text, not a watermark.
    def build(n):
        return [line(n, 300, "Body text of the listing."),
                drawn(n, 340 + 9 * (n % 7), "Website: example.org", ink=0.15, width=150)]
    pages = document(20, build)
    assert reason_of(pages, "Website: example.org") == [None] * 20


def test_sideways_table_headers_in_a_block_are_content():
    # Three diagonal headers side by side on every page form a block: content, not a stamp.
    def build(n):
        return [line(n, 300, "Body text of the table."),
                drawn(n, 120, "Total revenue", x0=100, width=80, angle=math.radians(45)),
                drawn(n, 124, "Net income", x0=130, width=80, angle=math.radians(45)),
                drawn(n, 128, "Budget", x0=160, width=60, angle=math.radians(45))]
    pages = document(20, build)
    assert reason_of(pages, "Total revenue") == [None] * 20


def test_pale_text_on_a_filled_banner_is_content():
    # White text on a dark banner reads as faint ink; the banner makes it a label, not a watermark.
    def build(n):
        return [line(n, 300, "Body text of the opinion."),
                drawn(n, 110, "Summary of the case", x0=80, width=200, size=12, ink=0.0)]
    pages = document(20, build)
    banner = Drawing(bbox=(72.0, 100.0, 540.0, 130.0), paths=4, rules=0, fills=4)
    for page in pages:
        page.drawings = [banner]
    assert reason_of(pages, "Summary of the case") == [None] * 20


def numbered_page(*, right=False, x0=None, table=False):
    x0 = (570 if right else 36) if x0 is None else x0
    labels = ["1", "2", "N", "4", "5", "6", "O", "8", "9", "10", "11", "18"]
    numbers = [line(1, 120 + i * 40, text, x0=x0, width=12) for i, text in enumerate(labels)]
    body = [line(1, 120 + i * 40, "The legislative text on this line.",
                 x0=90, width=430) for i in range(len(labels))]
    page = PageContent(1, WIDTH, HEIGHT, numbers + body, [])
    if table:
        page.drawings = [Drawing((30, 110, 600, 600), paths=4, rules=4)]
    return page, numbers, body


def test_margin_line_numbers_and_ocr_single_glyphs_are_artifacts_on_either_edge():
    for right in (False, True):
        page, numbers, body = numbered_page(right=right)
        reasons = artifact_reasons([page], BODY)
        assert [reasons.get(id(l)) for l in numbers] == ["margin line numbering"] * 12
        assert [reasons.get(id(l)) for l in body] == [None] * 12


def test_rotated_ocr_fragments_in_a_numbering_column_are_artifacts():
    page, numbers, _ = numbered_page()
    fragment = drawn(1, 210, "W N", x0=36, width=12, size=8, angle=math.pi / 2)
    fragment.ocr = True
    page.lines.append(fragment)
    assert reason_of([page], "W N") == ["margin line numbering"]


def test_an_ocr_margin_column_can_be_entirely_misread_as_rotated_letters():
    page, numbers, body = numbered_page()
    for i, number in enumerate(numbers):
        number.text = ["N", "O", "W N", "gl"][i % 4]
        number.ocr = True
        number.rotated = True
    reasons = artifact_reasons([page], BODY)
    assert [reasons.get(id(l)) for l in numbers] == ["margin line numbering"] * 12
    assert [reasons.get(id(l)) for l in body] == [None] * 12


def test_short_numbers_inside_the_body_are_content():
    page, numbers, _ = numbered_page(x0=300)
    reasons = artifact_reasons([page], BODY)
    assert [reasons.get(id(l)) for l in numbers] == [None] * 12


def test_list_labels_close_to_their_text_are_content():
    page, numbers, body = numbered_page(x0=72)
    reasons = artifact_reasons([page], BODY)
    assert [reasons.get(id(l)) for l in numbers + body] == [None] * 24


def test_a_numbered_table_at_the_page_edge_is_content():
    page, numbers, _ = numbered_page(table=True)
    reasons = artifact_reasons([page], BODY)
    assert [reasons.get(id(l)) for l in numbers] == [None] * 12


def test_nonconsecutive_numeric_values_at_the_edge_are_content():
    page, numbers, _ = numbered_page()
    for i, number in enumerate(numbers):
        number.text = str(100 + 5 * i)
    reasons = artifact_reasons([page], BODY)
    assert [reasons.get(id(l)) for l in numbers] == [None] * 12


def test_a_short_number_run_does_not_make_an_artifact_column():
    page, numbers, body = numbered_page()
    page.lines = numbers[:4] + body
    reasons = artifact_reasons([page], BODY)
    assert [reasons.get(id(l)) for l in numbers[:4]] == [None] * 4


def test_a_margin_line_of_unknown_size_is_not_tight_against_body_text():
    # A NaN size makes every gap unknown, so such a line is never right against body text and
    # stays furniture; the same head with a known size, 3 points above body text, is its text.
    def head_over_body(size):
        return document(10, lambda n: [line(n, 70, "Annual Report 2025", size=size),
                                       line(n, 86, f"Body text of page {n}.")])

    assert set(reason_of(head_over_body(BODY), "Annual Report 2025")) == {None}
    assert None not in reason_of(head_over_body(math.nan), "Annual Report 2025")


def test_a_running_head_of_two_rows_is_dropped_though_one_row_is_two_lines():
    # "CICP Provider Directory" over "Revised:" and "January 11, 2024" side by side: three lines
    # but two rows, so a running head, not a text block.
    def build(n):
        return [line(n, 20, "Provider Directory", x0=380, width=208, size=9),
                line(n, 33, "Revised:", x0=411, width=40, size=9),
                line(n, 33, "January 11, 2024", x0=455, width=108, size=9),
                line(n, 300, "Body text of the listing.")]
    pages = document(40, build)
    head = ["running header or footer, repeats on 40 pages"] * 40
    assert reason_of(pages, "Provider Directory") == head
    assert reason_of(pages, "Revised:") == head
    assert reason_of(pages, "January 11, 2024") == head


def test_a_stack_of_three_rows_in_the_top_margin_stays_text():
    def build(n):
        return [line(n, 20, "Provider Directory", x0=380, width=208, size=9),
                line(n, 33, "Revised: January 11, 2024", x0=380, width=208, size=9),
                line(n, 46, "Page header third row", x0=380, width=208, size=9),
                line(n, 300, "Body text of the listing.")]
    pages = document(40, build)
    assert reason_of(pages, "Provider Directory") == [None] * 40


def test_a_title_running_on_most_pages_is_dropped_on_the_first_too():
    # "CICP Provider Directory" is heading-sized and repeats on 18 of 20 pages: the document's
    # title, not a section start, so its first appearance is furniture as well.
    def build(n):
        head = [line(n, 20, "Provider Directory", size=13)] if n <= 18 else []
        return head + [line(n, 300, "Body text of the listing.")]
    pages = document(20, build)
    assert reason_of(pages, "Provider Directory") == [
        "running header or footer, repeats on 18 pages"] * 18


def test_a_chapter_header_keeps_its_first_appearance_as_the_section_title():
    # A heading-sized header repeating over one chapter (8 of 40 pages) is dropped after the
    # first page, which stays content: that is where the chapter starts.
    def build(n):
        head = [line(n, 20, "Chapter Two", size=13)] if 5 <= n <= 12 else []
        return head + [line(n, 300, "Body text of the report.")]
    pages = document(40, build)
    assert reason_of(pages, "Chapter Two") == [None] + [
        "running header or footer, repeats on 8 pages"] * 7


def test_a_bold_form_title_on_every_page_keeps_its_first_appearance():
    # A form's title block is bold and heading-sized on every page; on page 1 it is the title.
    def build(n):
        return [line(n, 38, "Practitioner Data Form", size=14, bold=True),
                line(n, 300, "Body text of the form.")]
    pages = document(12, build)
    assert reason_of(pages, "Practitioner Data Form") == [None] + [
        "running header or footer, repeats on 12 pages"] * 11


# da-legislation pp4-12: a legal-size scan whose text area starts a quarter of the way down,
# and whose registration moves the running head by up to 3.5 pt from page to page.
SCAN_HEAD_TOPS = [257.2, 260.7, 258.0, 260.7, 257.2]


def scan_page(n, *, ocr=True, title_top=None):
    """A double-spaced legal-size page: "H.B. No. 40" heads its text, body lines follow at a
    23 pt pitch, "89R34727 AMF-D" ends it."""
    def at(top, text, width=432.0):
        return Line(page=n, bbox=(108.0, top, 108.0 + width, top + 11.4), text=text, size=11.0,
                    bold=False, italic=False, font="GlyphLessFont", ocr=ocr)
    head = SCAN_HEAD_TOPS[(n - 1) % len(SCAN_HEAD_TOPS)]
    lines = [at(head, "H.B. No. 40", width=60.0)]
    lines += [at(350.0 + 23.0 * i, "court, each party or the party's attorney shall include")
              for i in range(24)]
    if title_top is not None:
        lines.append(at(title_top, "SECTION 6. Civil Practice and Remedies Code", width=250.0))
    lines.append(at(933.0 - (head - 257.2), "89R34727 AMF-D", width=90.0))
    return PageContent(number=n, width=612.0, height=1008.0, lines=lines, images=[])


def test_a_scans_running_head_a_quarter_down_the_page_is_dropped():
    pages = [scan_page(n) for n in range(1, 6)]
    reasons = artifact_reasons(pages, body=11.0)
    for text in ("H.B. No. 40", "89R34727 AMF-D"):
        assert [reasons.get(id(l)) for p in pages for l in p.lines if l.text == text] == [
            "running header or footer, repeats on 5 pages"] * 5


def test_an_ocr_footer_on_a_page_with_its_own_text_keeps_the_scan_tolerance():
    # da-legislation p3: OCR read the page image's body and footer under the file's own title.
    first = scan_page(1, ocr=False)
    first.lines = [replace(l, ocr=True) if l.text == "89R34727 AMF-D" else l for l in first.lines]
    pages = [first] + [scan_page(n) for n in range(2, 6)]
    reasons = artifact_reasons(pages, body=11.0)
    assert [reasons.get(id(l)) for p in pages for l in p.lines if l.text == "89R34727 AMF-D"] == [
        "running header or footer, repeats on 5 pages"] * 5


def test_the_same_running_head_on_text_layer_pages_stays_text():
    pages = [scan_page(n, ocr=False) for n in range(1, 6)]
    reasons = artifact_reasons(pages, body=11.0)
    assert [reasons.get(id(l)) for p in pages for l in p.lines if l.text == "H.B. No. 40"] == [
        None] * 5


def test_a_scans_section_title_repeating_at_different_heights_stays_text():
    # The title opens the text on each page (so it is in the scan's margin band), but at a
    # different height each time: a repeated title, not a running head.
    pages = [scan_page(n, title_top=270.0 + 10.0 * n) for n in range(1, 6)]
    reasons = artifact_reasons(pages, body=11.0)
    title = "SECTION 6. Civil Practice and Remedies Code"
    assert [reasons.get(id(l)) for p in pages for l in p.lines if l.text == title] == [None] * 5
