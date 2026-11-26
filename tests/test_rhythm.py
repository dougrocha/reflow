"""Page rhythm and OCR size snapping, on lines shaped like a double-spaced legislation scan
(da-legislation p5: baseline pitch 23 pt, boxes 11.4 pt high, OCR sizes 9.7 to 11.7 pt)."""

from reflow.analyze.rhythm import Rhythm, line_gap, ocr_page, snap_ocr_sizes
from reflow.extract.lines import Line

SIZES = [11.0, 9.7, 11.7, 10.4, 11.2, 10.9, 11.5, 10.1, 11.0, 10.6]


def line(top, *, x0=108.0, x1=540.0, size=11.0, height=11.4, ocr=True,
         text="court, each party or the party's attorney shall include"):
    return Line(page=5, bbox=(x0, top, x1, top + height), text=text, size=size, bold=False,
                italic=False, font="GlyphLessFont", ocr=ocr)


def column(n, *, pitch=23.0, x0=108.0, x1=540.0, ocr=True, start=300.0, size=None):
    return [line(start + i * pitch, x0=x0, x1=x1, ocr=ocr,
                 size=size if size is not None else SIZES[i % len(SIZES)]) for i in range(n)]


def test_the_pitch_of_a_double_spaced_scan_is_found():
    rhythm = Rhythm.of(column(10))

    assert rhythm is not None and rhythm.pitch == 23


def test_seven_lines_have_no_rhythm():
    assert Rhythm.of(column(7)) is None


def test_text_layer_lines_have_no_rhythm_whatever_their_spacing():
    lines = column(10, ocr=False)

    assert not ocr_page(lines)
    assert Rhythm.of(lines) is None
    assert line_gap(lines[0], lines[1], Rhythm.of(lines)) == lines[1].top - lines[0].bottom


def test_two_columns_give_one_pitch_and_no_sample_across_the_column_change():
    # Left column, then the right column from the top: the jump back up gives no sample, and
    # neither does a pair whose lines are in different columns.
    left = column(6, x0=72.0, x1=290.0)
    right = column(6, x0=320.0, x1=540.0, start=290.0)

    rhythm = Rhythm.of(left + right)

    assert rhythm is not None and rhythm.pitch == 23
    assert not rhythm.is_leading(left[-1], right[0])


def test_a_single_spaced_scan_has_a_pitch_of_about_its_leading():
    lines = column(10, pitch=13.0, size=11.0)

    rhythm = Rhythm.of(lines)

    assert rhythm is not None and rhythm.pitch == 13
    para = line(lines[-1].top + 26.0)  # a blank line between two paragraphs: 2× the pitch
    assert not rhythm.is_leading(lines[-1], para)
    assert line_gap(lines[-1], para, rhythm) == para.top - lines[-1].bottom


def test_a_pair_at_the_pitch_has_no_gap():
    lines = column(10)
    rhythm = Rhythm.of(lines)

    assert line_gap(lines[0], lines[1], rhythm) == 0.0
    assert line_gap(lines[0], lines[1], None) == lines[1].top - lines[0].bottom


def test_ocr_sizes_snap_to_the_page_size():
    lines = column(10, size=11.0)
    tall = line(600.0, size=14.2)                    # p5: a body line read at 14.2 pt
    boxed = line(623.0, size=18.7, height=11.3)      # p13: 18.7 pt in an 11.3 pt box
    big = line(646.0, size=21.6, height=21.6)        # p15: a 21.6 pt box
    page = lines + [tall, boxed, big]

    snap_ocr_sizes(page)

    assert tall.size == 11 and boxed.size == 11
    assert big.size == 21.6


def test_text_layer_lines_are_not_snapped():
    lines = column(10, size=11.0, ocr=False)
    tall = line(600.0, size=14.2, ocr=False)

    snap_ocr_sizes(lines + [tall])

    assert tall.size == 14.2
    assert [l.size for l in lines] == [11.0] * 10


def test_a_body_line_read_at_a_larger_size_is_not_a_heading():
    # p5: "(a) In this section, ..." came out of OCR at 14.2 pt among 11 pt lines.
    from reflow.analyze.layout import analyze
    from reflow.extract.pages import PageContent
    from reflow.model import Role

    lines = column(10, size=11.0)
    lines[4].size = 14.2
    nodes = analyze([PageContent(5, 612, 1008, lines, [])])

    assert not any(n.role.value.startswith("h") for parent in nodes for n in parent.walk())
    assert lines[4].size == 11 and Role.P in {n.role for parent in nodes for n in parent.walk()}


def test_a_title_page_with_no_clear_size_is_not_snapped():
    # naca p1: a dozen title lines in as many sizes; the 15.6 pt title is not jitter.
    sizes = [12.4, 20.3, 20.6, 12.4, 15.6, 15.6, 8.8, 8.8, 6.5, 6.8]
    lines = [line(100.0 + 30 * i, size=s, height=s) for i, s in enumerate(sizes)]

    snap_ocr_sizes(lines)

    assert [l.size for l in lines] == sizes


def test_seven_lines_are_not_snapped():
    lines = column(6, size=11.0)
    tall = line(600.0, size=14.2)

    snap_ocr_sizes(lines + [tall])

    assert tall.size == 14.2


def test_a_bold_ocr_line_keeps_its_size():
    # naca p4: "TECHNICAL COMMITTEES" in Times-Bold at 9.8 pt over names at 7.3 to 8.4 pt.
    lines = column(10, size=8.0, pitch=10.0)
    title = line(600.0, size=9.8, height=11.2, text="TECHNICAL COMMITTEES")
    title.bold = True

    snap_ocr_sizes(lines + [title])

    assert title.size == 9.8
