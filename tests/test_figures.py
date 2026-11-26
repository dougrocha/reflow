"""Vector drawings count as figures only when they are pictures, not banners behind text.

The tests draw real PDFs (filled rectangles, outlines, arrows, text) and run extraction and
layout on them, so the drawing clusters come from pdfium, not from hand-made boxes.
"""

import math

import pikepdf
from pikepdf import Dictionary, Name

from reflow.analyze.figures import figure_boxes, in_columns
from reflow.analyze.layout import analyze
from reflow.extract.drawings import Drawing
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent, extract_pages
from reflow.extract.tables.cells import TableData
from reflow.analyze.tables.vetoes import chart_grid
from reflow.extract.tables.ruled import find_tables
from reflow.model import Role

H = 792.0  # LETTER height: PDF y is measured from the bottom


def _rect(x0, top, x1, bottom, gray=None, outline=False):
    """A filled rectangle (gray 0..1), optionally with a black outline."""
    fill = f"{gray} g " if gray is not None else ""
    op = "B" if outline and gray is not None else ("S" if outline else "f")
    stroke = "0 G " if outline else ""
    return f"{stroke}{fill}{x0} {H - bottom} {x1 - x0} {bottom - top} re {op}"


def _line(x0, top0, x1, top1):
    return f"0 G {x0} {H - top0} m {x1} {H - top1} l S"


def _text(x, top, text, pt=10.0):
    escaped = text.replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {pt} Tf {x} {H - top - pt} Td ({escaped}) Tj ET"


def _pdf_page(tmp_path, ops):
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica,
                                        Encoding=Name.WinAnsiEncoding))
    page = pdf.add_blank_page(page_size=(612, H))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream("\n".join(ops).encode("cp1252"))
    path = tmp_path / "drawn.pdf"
    pdf.save(path)
    (extracted,) = list(extract_pages(path))
    return extracted


def _labels(figure):
    """The texts of a figure's label nodes, in their order."""
    return [c.text for c in figure.children if c.attrs.get("figure_label")]


def _all_text(nodes):
    texts = []

    def walk(node):
        if node.text:
            texts.append(node.text)
        for child in node.children:
            walk(child)

    for node in nodes:
        walk(node)
    return " ".join(texts)


def _side_panel_ops(tab_x0, tab_x1, banner_x0, banner_x1):
    """A category tab on one page edge, two banners beside it (4 pt from the tab, as on the
    pima directory), and provider entries in two columns."""
    ops = [_rect(tab_x0, 67, tab_x1, 396, gray=0.0),
           _rect(banner_x0, 67, banner_x1, 102, gray=0.66),
           _rect(banner_x0, 108, banner_x1, 158, gray=0.0),
           # Rules under the banners, 4 pt below them: with the tab they made eight paths.
           _line(banner_x0, 162, banner_x1, 162), _line(banner_x0, 168, banner_x1, 168),
           _line(banner_x0, 199, banner_x1, 199), _line(banner_x0, 209, banner_x1, 209),
           _line(banner_x0, 262, banner_x1, 262)]
    ops += [_text(banner_x0, 170 + 14 * i, f"Gil {i} MD (F)") for i in range(8)]
    ops += [_text(banner_x0 + 260, 170 + 14 * i, f"Diaz {i} DO") for i in range(8)]
    return ops


def test_left_tab_and_banners_are_separate_drawings_and_no_figure(tmp_path):
    page = _pdf_page(tmp_path, _side_panel_ops(0, 36, 40, 214))

    assert all(not (d.bbox[0] < 1 and d.bbox[2] > 100) for d in page.drawings)  # tab alone
    nodes = analyze([page])
    assert [n for n in nodes if n.role == Role.FIGURE] == []
    text = _all_text(nodes)
    assert "Gil 7 MD (F)" in text and "Diaz 7 DO" in text


def test_right_tab_and_banners_are_separate_drawings_and_no_figure(tmp_path):
    page = _pdf_page(tmp_path, _side_panel_ops(576, 612, 400, 572))

    assert all(not (d.bbox[0] < 500 and d.bbox[2] > 575) for d in page.drawings)
    nodes = analyze([page])
    assert [n for n in nodes if n.role == Role.FIGURE] == []
    text = _all_text(nodes)
    assert "Gil 7 MD (F)" in text and "Diaz 7 DO" in text


def test_a_solid_backdrop_with_text_on_it_is_not_a_figure(tmp_path):
    # Eight adjoining grey stripes make one solid panel (no outlines) with two-word lines on it.
    ops = [_rect(100, 100 + 20 * i, 400, 120 + 20 * i, gray=0.85) for i in range(8)]
    ops += [_text(110, 110 + 20 * i, f"Provider {i} Name") for i in range(6)]
    page = _pdf_page(tmp_path, ops)

    nodes = analyze([page])

    assert [n for n in nodes if n.role == Role.FIGURE] == []
    text = _all_text(nodes)
    assert all(f"Provider {i} Name" in text for i in range(6))


def test_a_labeled_diagram_with_outlines_and_arrows_stays_a_figure(tmp_path):
    # Boxes with outlines, joined by arrows that touch the boxes (as in resnet's Figure 3).
    ops = []
    for i in range(6):
        top = 120 + 50 * i
        ops.append(_rect(150, top, 270, top + 30, gray=0.92, outline=True))
        ops.append(_text(160, top + 10, f"3x3 conv, {64 * (i + 1)}"))
        if i:
            ops.append(_line(210, top - 20, 210, top))
    page = _pdf_page(tmp_path, ops)

    nodes = analyze([page])

    (figure,) = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["why"][0].startswith("figure: vector drawing of ")
    assert "with outlines" in figure.attrs["why"][0]


def test_a_chart_drawing_is_still_a_figure_and_says_why():
    chart = Drawing((100.0, 100.0, 400.0, 300.0), paths=12, rules=0, fills=0, strokes=12,
                    outlines=12)
    labels = [Line(page=1, bbox=(80.0, 150.0, 110.0, 160.0), text="Axis", size=10.0,
                   bold=False, italic=False, font="Helvetica")]
    page = PageContent(number=1, width=612, height=792, lines=labels, images=[],
                       drawings=[chart])

    nodes = analyze([page])

    (figure,) = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["why"][0].startswith("figure: vector drawing of 12 paths")
    assert "with outlines" in figure.attrs["why"][0]


def test_figure_boxes_gives_each_figure_its_reason_and_source():
    chart = Drawing((100.0, 100.0, 400.0, 300.0), paths=12, rules=0, fills=0, strokes=12,
                    outlines=12)
    page = PageContent(number=1, width=612, height=792, lines=[], images=[],
                       drawings=[chart])

    [found] = figure_boxes(page, [], 10.0)

    assert found.why.startswith("vector drawing of 12 paths")
    assert found.source == "drawing"


def test_a_plain_drawing_with_no_text_takes_no_labels_and_loses_to_its_ruled_table(tmp_path):
    # Outlined boxes with no text left in them: the candidate's labels are None (no entry), not
    # (), so the ruled table that the drawing draws vetoes the figure and keeps its cells.
    box = (100.0, 100.0, 400.0, 300.0)
    drawn = Drawing(box, paths=12, rules=0, fills=0, strokes=12, outlines=12)
    page = PageContent(number=1, width=612, height=792, lines=[], images=[], drawings=[drawn])
    [found] = figure_boxes(page, [], 10.0)
    assert found.labels is None

    table = TableData(1, box, [["Name", "Total"], ["Alpha", "12"]], method="ruled")
    nodes = analyze([page], {1: [table]})

    assert [n for n in nodes if n.role == Role.TABLE]
    assert not [n for n in nodes if n.role == Role.FIGURE]


def _wedge(cx, cy_top, r, a0, a1):
    """A filled pie wedge from the centre, its arc one Bezier curve (angles in degrees)."""
    cy = H - cy_top
    k = 4 / 3 * math.tan(math.radians(a1 - a0) / 4) * r
    p0 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
    p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
    c1 = (p0[0] - k * math.sin(math.radians(a0)), p0[1] + k * math.cos(math.radians(a0)))
    c2 = (p1[0] + k * math.sin(math.radians(a1)), p1[1] - k * math.cos(math.radians(a1)))
    pts = [cx, cy, *p0, *c1, *c2, *p1, cx, cy]
    return f"{' '.join(f'{v:.2f}' for v in pts[:2])} m {' '.join(f'{v:.2f}' for v in pts[2:4])} l " \
           f"{' '.join(f'{v:.2f}' for v in pts[4:10])} c {cx} {cy} l h 0.5 g f"


def test_a_pie_chart_is_a_figure_and_keeps_its_labels_on_it(tmp_path):
    # Eight wedges share the centre (300, 400); their labels sit on the pie, two words each.
    ops = [_wedge(300, 400, 100, 45 * i, 45 * i + 45) for i in range(8)]
    labels = ["Health care", "Chronic conditions", "Mortality rate"]
    ops += [_text(250, 330 + 25 * i, name) for i, name in enumerate(labels)]
    ops += [_text(200, 510, "Figure 2. Causes of death")]
    page = _pdf_page(tmp_path, ops)

    nodes = analyze([page])

    (figure,) = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["why"][0].startswith("figure: chart: 8 wedges sharing a centre")
    assert _labels(figure) == labels
    assert "Figure 2. Causes of death" in _all_text(nodes)
    assert "Figure 2. Causes of death" not in _labels(figure)


def test_a_bar_chart_keeps_its_caption_and_notes_and_folds_its_labels(tmp_path):
    # Six bars of one height and six lengths, each named on the bar; the caption and the source
    # sit inside the frame rule at the bottom.
    widths = [300, 240, 180, 150, 120, 90]
    names = ["Heart disease", "Cancer deaths", "Stroke events", "Lung disease",
             "Accidents injury", "Diabetes cases"]
    ops = [_rect(100, 140 + 20 * i, 100 + w, 154 + 20 * i, gray=0.4) for i, w in enumerate(widths)]
    ops += [_text(104, 141 + 20 * i, name, pt=9) for i, name in enumerate(names)]
    ops += [_line(100, 136, 100, 300),  # the value axis joins the bars into one drawing
            _line(100, 300, 400, 300),
            _text(100, 262, "Figure 1. Deaths by cause, 2020"),
            _text(100, 280, "Source: National Center for Health Statistics", pt=9)]
    page = _pdf_page(tmp_path, ops)

    nodes = analyze([page])

    (figure,) = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["why"][0].startswith("figure: chart: 6 repeated bars")
    assert _labels(figure) == names
    text = _all_text(nodes)
    assert "Figure 1. Deaths by cause, 2020" in text
    assert "Source: National Center for Health Statistics" in text
    assert not any(t.startswith(("Figure 1", "Source")) for t in _labels(figure))


def test_a_banner_whose_bands_have_two_lengths_is_still_a_backdrop(tmp_path):
    # Eight adjoining solid bands of two lengths (the near-miss of a chart: too few lengths for
    # bars), two-word lines on them. No chart marks, so the solid backdrop gate still rejects it.
    ops = [_rect(100, 100 + 20 * i, 100 + (300 if i % 2 else 240), 120 + 20 * i, gray=0.85)
           for i in range(8)]
    ops += [_text(108, 104 + 20 * i, f"Panel {i} Heading") for i in range(8)]
    page = _pdf_page(tmp_path, ops)

    assert all(not d.marks for d in page.drawings)
    assert figure_boxes(page, page.lines, body=10.0) == []


def test_a_panel_of_tiled_cells_with_words_is_not_a_figure(tmp_path):
    # Four rows of three cells that tile each row end to end, a word in each cell: table cells,
    # which must not be read as bars even though they have three lengths and repeat.
    widths = [30, 22, 38]
    ops = [_rect(100 + sum(widths[:c]), 300 + 14 * r, 100 + sum(widths[:c + 1]), 314 + 14 * r, gray=0.9)
           for r in range(4) for c in range(3)]
    ops += ["0 g"]   # black words: the cells' grey would leave them unseen
    ops += [_text(102 + sum(widths[:c]), 302 + 14 * r, f"Cell {r} {c}", pt=6)
            for r in range(4) for c in range(3)]
    page = _pdf_page(tmp_path, ops)

    assert all(not d.marks for d in page.drawings)
    assert figure_boxes(page, page.lines, body=10.0) == []


def test_a_chart_with_gridlines_is_a_figure_and_its_sparse_grid_is_no_table(tmp_path):
    # A bar chart whose gridlines pdfplumber took for a ruled table: most of the grid's cells are
    # empty (the chart's labels sit outside them) and it is coloured (`coloured`, counted from the
    # page's objects by find_tables). The table is dropped before the figure is weighed, so the
    # chart stands as a figure and its labels move into it.
    widths = [300, 240, 180, 150, 120, 90]
    names = ["Heart disease", "Cancer deaths", "Stroke events", "Lung disease",
             "Accidents injury", "Diabetes cases"]
    ops = [_rect(100, 140 + 20 * i, 100 + w, 154 + 20 * i, gray=0.4) for i, w in enumerate(widths)]
    ops += [_text(104, 141 + 20 * i, name, pt=9) for i, name in enumerate(names)]
    ops += [_line(100, 136, 100, 300), _line(100, 300, 400, 300)]
    ops += [_line(100, top, 400, top) for top in (150, 190, 230, 270)]  # gridlines
    ops += [_text(100, 312, "Figure 1. Deaths by cause, 2020"),
            _text(100, 330, "Source: National Center for Health Statistics", pt=9)]
    page = _pdf_page(tmp_path, ops)
    rows = [["", ""], ["", ""], ["", "5"], ["", ""]]
    table = TableData(page.number, (95.0, 130.0, 405.0, 300.0), rows, method="ruled", coloured=12)

    nodes = analyze([page], {page.number: [table]})

    assert not [n for n in nodes if n.role == Role.TABLE]
    (figure,) = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["why"][0].startswith("figure: chart: 6 repeated bars")
    assert _labels(figure) == names
    assert "Figure 1. Deaths by cause, 2020" in _all_text(nodes)


BLUE = "0 0.4 0.8"  # a saturated colour, as a chart's series and gridlines are drawn


def _line_chart_with_grid(tmp_path):
    """A line chart over a ruled grid (its gridlines are rules to pdfplumber), with ticks on the
    axis. Every stroke is blue, and three labels sit in the grid's cells."""
    ops = [f"{BLUE} RG {BLUE} rg 0.8 w"]
    ops += [f"100 {H - top} m 400 {H - top} l S" for top in (130, 170, 210, 250, 290)]
    ops += [f"{x} {H - 130} m {x} {H - 290} l S" for x in (100, 160, 220, 280, 340, 400)]
    ops += [f"{x} {H - 290} m {x} {H - 294} l S" for x in range(110, 400, 25)]  # 12 ticks
    points = [(110, 240), (160, 220), (220, 200), (280, 180), (340, 160), (400, 150)]
    ops += [f"{points[0][0]} {H - points[0][1]} m"]
    ops += [f"{x} {H - top} l" for x, top in points[1:]] + ["S"]  # the series
    ops += [_text(104, 134, "Percent", pt=8), _text(164, 262, "2020", pt=8),
            _text(104, 254, "10", pt=8)]
    return _pdf_page(tmp_path, ops)


def test_a_line_chart_with_gridlines_is_a_figure_not_a_table(tmp_path):
    # The real extraction: pdfplumber finds the chart's grid as a ruled table whose few filled
    # cells sit among blue strokes (`coloured`, counted by find_tables). The chart stands as a figure.
    page = _line_chart_with_grid(tmp_path)
    tables = find_tables(tmp_path / "drawn.pdf", [page.number])
    (table,) = tables[page.number]
    assert table.coloured >= 10

    nodes = analyze([page], tables)

    assert not [n for n in nodes if n.role == Role.TABLE]
    (figure,) = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["why"][0].startswith("figure: vector drawing of 24 paths")
    # The grid's own text (in its cells) moves into the figure: no text is left only in a picture.
    assert {"Percent", "2020", "10"} <= set(_labels(figure))


def test_a_full_grid_of_axis_values_with_coloured_curves_is_a_figure_not_a_table(tmp_path):
    # A chart over a grid whose cells are all full of axis values (resnet p5 Figure 4, p8 Figure 6):
    # the cells are not sparse and the drawing has ticks but no dots, yet four coloured data
    # curves run through it. A table has no coloured curves, so the grid is the chart's.
    ops = [f"{BLUE} RG {BLUE} rg 0.8 w"]
    tops, xs = (130, 150, 170, 190), (100, 160, 220, 280, 340, 400)
    ops += [f"100 {H - top} m 400 {H - top} l S" for top in tops]
    ops += [f"{x} {H - 130} m {x} {H - 190} l S" for x in xs]
    for r, top in enumerate(tops[:-1]):
        for c, x in enumerate(xs[:-1]):
            ops.append(_text(x + 4, top + 5, str(10 * (r + c + 1)), pt=7))
    ops += [f"{x} {H - 190} m {x} {H - 194} l S" for x in range(110, 400, 25)]  # axis ticks
    for k in range(4):  # four data series of 25 points each, kept inside the grid's 130..190 band
        pts = [(110 + 12 * i, 172 - 5 * (i % 6) - 3 * k) for i in range(25)]
        ops.append(f"{pts[0][0]} {H - pts[0][1]} m")
        ops += [f"{x} {H - y} l" for x, y in pts[1:]]
        ops.append("S")
    ops += [_text(100, 212, "Figure 1. Error on the test set, by epoch", pt=9)]
    page = _pdf_page(tmp_path, ops)
    tables = find_tables(tmp_path / "drawn.pdf", [page.number])
    assert tables.get(page.number), "the grid is read as a ruled table"

    nodes = analyze([page], tables)

    assert not [n for n in nodes if n.role == Role.TABLE]
    (figure,) = [n for n in nodes if n.role == Role.FIGURE]
    assert "10" in _labels(figure)
    assert "Figure 1. Error on the test set, by epoch" in _all_text(nodes)


def test_a_black_and_white_ruled_table_is_kept_with_its_text(tmp_path):
    # A real table: a full grid of black rules, every cell with text. Not coloured, not sparse.
    ops = ["0 G 0.8 w"]
    ops += [f"100 {H - top} m 400 {H - top} l S" for top in (130, 150, 170, 190)]
    ops += [f"{x} {H - 130} m {x} {H - 190} l S" for x in (100, 200, 300, 400)]
    cells = [[f"Item {r}", f"{r * 3}.5", f"{r * 7}.0"] for r in range(3)]
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            ops.append(_text(104 + 100 * c, 134 + 20 * r, text, pt=9))
    page = _pdf_page(tmp_path, ops)
    tables = find_tables(tmp_path / "drawn.pdf", [page.number])
    (table,) = tables[page.number]
    assert table.coloured == 0
    assert chart_grid(table, page.drawings) is None

    nodes = analyze([page], tables)

    (kept,) = [n for n in nodes if n.role == Role.TABLE]
    assert "Item 1" in _all_text([kept]) and "7.0" in _all_text([kept])
    assert not [n for n in nodes if n.role == Role.FIGURE]


def test_live_text_over_a_photo_is_kept_in_the_transcript():
    # A section divider: a photo covering most of the page with its title laid over it as real
    # text. The title is content, so the figure must not swallow it.
    title = [Line(1, (104, 355, 508, 422), "2 | CHOOSING", 56, False, False, "Lato"),
             Line(1, (81, 433, 531, 500), "THE RIGHT PLAN", 56, False, False, "Lato")]
    page = PageContent(1, 612, 792, title, [(20.0, 61.0, 592.0, 787.0)])
    nodes = analyze([page])
    assert "CHOOSING" in _all_text(nodes) and "THE RIGHT PLAN" in _all_text(nodes)
    assert any(n.role == Role.FIGURE for n in nodes)  # the photo is still a figure


def test_drawn_panel_text_reads_column_by_column():
    """Three callouts set side by side read one whole callout at a time, not row by row."""
    def at(x, top, text):
        return Line(1, (x, top, x + 90, top + 12), text, 10, False, False, "Lato")
    lines = [at(x, top, f"{name} {row}")
             for row, top in enumerate((100, 114, 128))
             for x, name in ((50, "left"), (200, "middle"), (350, "right"))]
    assert [l.text for l in in_columns(lines)] == [
        "left 0", "left 1", "left 2", "middle 0", "middle 1", "middle 2",
        "right 0", "right 1", "right 2"]


def _banner(tmp_path, text, pt=14.0):
    # A grey bar made of stripes with no outlines, a short label on it.
    ops = [_rect(100, 100 + 10 * i, 400, 110 + 10 * i, gray=0.85) for i in range(9)]
    ops.append(_text(110, 115, text, pt=pt))
    return _pdf_page(tmp_path, ops)


def test_a_label_banner_is_text_not_a_figure(tmp_path):
    nodes = analyze([_banner(tmp_path, "Baca")])

    assert [n for n in nodes if n.role == Role.FIGURE] == []
    assert "Baca" in _all_text(nodes)


def test_a_banner_with_tiny_lettering_is_still_a_figure(tmp_path):
    page = _banner(tmp_path, "Baca", pt=5.0)
    (line,) = page.lines

    assert figure_boxes(page, page.lines, body=line.size + 5.0) != []


def test_a_banner_with_chart_marks_is_still_a_figure():
    chart = Drawing((100.0, 100.0, 400.0, 150.0), paths=9, rules=0, fills=9, strokes=0,
                    outlines=0, marks=(("bars", 6),))
    label = Line(page=1, bbox=(110.0, 110.0, 150.0, 124.0), text="Baca", size=14.0,
                 bold=False, italic=False, font="Helvetica")
    page = PageContent(number=1, width=612, height=792, lines=[label], images=[],
                       drawings=[chart])

    assert figure_boxes(page, page.lines, body=10.0) != []


def test_a_banner_holding_one_ocr_line_is_still_a_figure():
    banner = Drawing((100.0, 100.0, 400.0, 150.0), paths=9, rules=0, fills=9, strokes=0,
                     outlines=0)
    label = Line(page=1, bbox=(110.0, 110.0, 150.0, 124.0), text="Baca", size=14.0,
                 bold=False, italic=False, font="Helvetica", ocr=True)
    page = PageContent(number=1, width=612, height=792, lines=[label], images=[],
                       drawings=[banner])

    assert figure_boxes(page, page.lines, body=10.0) != []


# The raster branch on scanned pages: the figures are the page scan's (PageScan.pictures_for).
SCAN_PAGE = (0.0, 0.0, 612.0, 792.0)
PHOTO = (100.0, 300.0, 400.0, 500.0)


def _ocr(top, text, x0=72.0, x1=400.0):
    return Line(1, (x0, top, x1, top + 12), text, 10, False, False, "GlyphLessFont", ocr=True)


def _scanned_page(lines, objects, regions=(), status="scanned"):
    from reflow.extract.scan import PageScan
    return PageContent(1, 612, 792, lines, [o.bbox for o in objects],
                       scan=PageScan(status, tuple(objects), tuple(regions)))


def _object(box, kind, residual=0.0):
    from reflow.extract.scan import ScannedObject
    return ScannedObject(box, kind, (), residual)


def _picture(box):
    from reflow.extract.scan import InkRegion
    return InkRegion(box, "picture")


def test_a_scan_page_with_a_picture_gives_the_picture_and_its_ocr_lines_as_labels():
    lines = [_ocr(100, "A scanned letter to the board of directors."),
             _ocr(350, "T2 — -", 150, 250), _ocr(600, "Yours sincerely, the district.")]
    page = _scanned_page(lines, [_object(SCAN_PAGE, "text", 0.05)], [_picture(PHOTO)])

    [found] = figure_boxes(page, page.lines, 10.0)

    assert (found.bbox, found.source, found.labels) == (PHOTO, "picture", (1,))


def test_a_scan_page_without_a_picture_gives_no_figure():
    lines = [_ocr(100 + 20 * i, f"Line {i} of a typed letter to the board.") for i in range(3)]
    page = _scanned_page(lines, [_object(SCAN_PAGE, "text", 0.05)])
    assert figure_boxes(page, page.lines, 10.0) == []


def test_an_empty_strip_on_a_scanned_page_is_no_figure():
    # A footer band holding only a rule: the object rule (1% of the page) would publish it.
    page = _scanned_page([], [_object((5.0, 597.0, 616.0, 792.0), "empty", 0.001)])
    assert figure_boxes(page, [], 10.0) == []


def test_a_noisy_page_finds_no_picture_in_its_scan_but_keeps_a_plain_photo_object():
    grainy = _scanned_page([_ocr(100, "Grainy page text.")],
                           [_object(SCAN_PAGE, "text", 0.3)], status="noisy")
    assert figure_boxes(grainy, grainy.lines, 10.0) == []
    small = (60.0, 60.0, 260.0, 210.0)
    with_photo = _scanned_page([], [_object((0.0, 0.0, 612.0, 400.0), "text", 0.3),
                                    _object(small, "plain", 0.2)], status="noisy")
    assert [(f.bbox, f.source, f.labels) for f in figure_boxes(with_photo, [], 10.0)] == [
        (small, "object", None)]


def test_a_small_text_free_photo_object_is_still_its_own_figure():
    page = _scanned_page([], [_object(SCAN_PAGE, "text", 0.01), _object(PHOTO, "plain", 0.4)])
    assert [f.bbox for f in figure_boxes(page, [], 10.0)] == [PHOTO]


def test_a_photo_over_a_page_background_is_one_figure_and_a_text_mask_none():
    # Under redo OCR: the page background (100%), a photo object over it whose picture region
    # the background also yields, and the alpha mask of a logo's lettering (text, no ink left).
    lines = [_ocr(120, "DISTRICT", 360, 480)]
    mask = (350.0, 100.0, 500.0, 180.0)
    page = _scanned_page(lines, [_object(SCAN_PAGE, "text", 0.05), _object(PHOTO, "plain", 0.4),
                                 _object(mask, "text", 0.001)],
                         [_picture((98.0, 302.0, 402.0, 497.0))])

    assert [f.bbox for f in figure_boxes(page, page.lines, 10.0)] == [PHOTO]


def test_two_abutting_pieces_of_one_photo_on_a_scanned_page_are_one_figure():
    left, right = (100.0, 100.0, 250.0, 300.0), (250.0, 100.0, 400.0, 300.0)
    page = _scanned_page([], [_object(left, "plain", 0.4), _object(right, "plain", 0.4)])
    assert [(f.bbox, f.labels) for f in figure_boxes(page, [], 10.0)] == [
        ((100.0, 100.0, 400.0, 300.0), None)]


def test_a_page_the_scan_skipped_keeps_the_object_rule():
    # Images under a fifth of the page: nothing was rendered, and each image object between 1%
    # and 90% of the page is a figure, as before the scan.
    photo, icon = (72.0, 72.0, 272.0, 222.0), (300.0, 72.0, 320.0, 92.0)
    page = PageContent(1, 612, 792, [], [photo, icon])
    assert page.scan.status == "skipped"
    assert [(f.bbox, f.source, f.labels) for f in figure_boxes(page, [], 10.0)] == [
        (photo, "object", None)]


def test_a_title_line_centred_on_the_edge_of_a_picture_stays_in_the_flow():
    # Brochure p2: the second line of the title over a map has its centre 0.2 pt inside the
    # picture's top edge, which the scan knows only to a tile; the map's labels are well inside.
    title = _ocr(0, "CEMETERY TOUR", 376, 584)
    title.bbox = (376.0, 75.3, 583.7, 101.0)
    street = _ocr(330, "CROSBY", 236, 277)
    page = _scanned_page([title, street], [_object(SCAN_PAGE, "text", 0.05)],
                         [_picture((80.0, 88.0, 520.0, 696.0))])
    [found] = figure_boxes(page, page.lines, 10.0)
    assert found.labels == (1,)


# Unread figures: the page scan's pen regions, as candidates.
def _pen(box, share=None):
    from reflow.extract.scan import InkRegion
    return InkRegion(box, "pen", share)


def _omitted(box, text):
    from reflow.extract.ocrwords import GradedWord
    from reflow.ingest.ocr_trust import Grade
    return GradedWord(box, text, "", 20, Grade.UNREAD, None, 1.0)


def test_a_pen_region_on_a_scan_page_is_one_unread_figure_with_its_omitted_words():
    from reflow.extract.ocrwords import PageWords
    region = (300.0, 200.0, 420.0, 240.0)
    inside = [_omitted((305, 205, 350, 235), "Sxa"), _omitted((355, 205, 415, 235), "~mth")]
    outside = _omitted((72, 600, 90, 612), "x")
    page = _scanned_page([_ocr(100, "A typed letter to the board.")],
                         [_object(SCAN_PAGE, "text", 0.05)], [_pen(region, 0.95)])
    page.words = PageWords("graded", (*inside, outside), 12.0)

    [found] = figure_boxes(page, page.lines, 10.0)

    assert (found.bbox, found.source, found.labels) == (region, "pen", ())
    assert found.unread.words == tuple(inside) and found.unread.ocr_guess == "Sxa ~mth"
    assert found.unread.pen_share == 0.95


def test_pen_regions_inside_a_ledger_strip_turn_that_object_unread():
    # docaccess-permit p1: the handwritten ledger is two image objects, 96%x59% and 97%x24% of
    # the page. Every pen region inside one makes it unread: one figure, not two.
    from reflow.extract.scan import PageScan
    strip = (16.0, 58.0, 982.0, 420.0)
    lines = [_ocr(30, "BUILDING PERMIT APPLICATIONS", 300, 700)]
    for regions in ([_pen((16.0, 56.0, 984.0, 420.0))],
                    [_pen((20.0, 60.0, 980.0, 240.0)), _pen((20.0, 244.0, 980.0, 418.0))]):
        page = PageContent(1, 1008, 612, lines, [strip],
                           scan=PageScan("scanned", (_object(strip, "plain", 0.3),), tuple(regions)))
        [found] = figure_boxes(page, page.lines, 10.0)
        assert (found.bbox, found.source, found.labels) == (strip, "object", None)
        assert found.unread is not None


def test_a_small_pen_region_in_a_graphic_is_the_graphics_own_ink():
    # Carmel p10: two timeline labels OCR could not read, inside the timeline's image object.
    graphic = (92.0, 385.0, 497.0, 474.0)
    page = _scanned_page([_ocr(100, "A typed letter to the board.")],
                         [_object(SCAN_PAGE, "text", 0.05), _object(graphic, "plain", 0.3)],
                         [_pen((296.0, 392.0, 344.0, 408.0), 0.0)])
    assert [(f.bbox, f.unread) for f in figure_boxes(page, page.lines, 10.0)] == [(graphic, None)]


def test_a_pen_region_spanning_two_strips_turns_both_unread():
    from reflow.extract.scan import PageScan
    upper, lower = (16.0, 58.0, 982.0, 420.0), (16.0, 422.0, 996.0, 570.0)
    page = PageContent(1, 1008, 612, [_ocr(30, "BUILDING PERMIT APPLICATIONS", 300, 700)],
                       [upper, lower],
                       scan=PageScan("scanned", (_object(upper, "plain", 0.3),
                                                 _object(lower, "plain", 0.3)),
                                     (_pen((16.0, 56.0, 984.0, 576.0), 0.0),)))
    found = figure_boxes(page, page.lines, 10.0)
    assert [(f.bbox, f.unread is not None) for f in found] == [(upper, True), (lower, True)]


def test_a_pen_region_over_a_picture_is_the_pictures_and_one_beside_it_is_its_own():
    over = (110.0, 310.0, 390.0, 480.0)     # IoU with PHOTO 0.79: the photo's own ink
    beside = (420.0, 320.0, 520.0, 380.0)
    page = _scanned_page([], [_object(SCAN_PAGE, "text", 0.05)],
                         [_picture(PHOTO), _pen(over), _pen(beside)])
    found = figure_boxes(page, [], 10.0)
    assert [(f.bbox, f.source, f.unread is not None) for f in found] == [
        (PHOTO, "picture", False), (beside, "pen", True)]


def test_a_born_digital_page_with_a_photo_makes_no_unread_figure():
    live = Line(1, (72, 100, 400, 112), "A born-digital report.", 10, False, False, "Times")
    page = _scanned_page([live], [_object(SCAN_PAGE, "text", 0.05)],
                         [_pen((300.0, 200.0, 420.0, 240.0))])
    assert figure_boxes(page, page.lines, 10.0) == []


def _circle(cx, cy, r, closed=True):
    """A stroked circle of four Béziers about (cx, cy) with the page's top-down y; an open one
    stops after three quarters."""
    k = 0.5523 * r
    y = H - cy
    parts = [f"{cx + r} {y} m",
             f"{cx + r} {y + k} {cx + k} {y + r} {cx} {y + r} c",
             f"{cx - k} {y + r} {cx - r} {y + k} {cx - r} {y} c",
             f"{cx - r} {y - k} {cx - k} {y - r} {cx} {y - r} c"]
    if closed:
        parts.append(f"{cx + k} {y - r} {cx + r} {y - k} {cx + r} {y} c")
    return "0 G " + " ".join(parts) + " S"


def test_a_closed_drawn_circle_is_an_oval_and_an_open_curve_is_not(tmp_path):
    page = _pdf_page(tmp_path, [_circle(30, 100, 5), _circle(30, 130, 5, closed=False),
                                              _text(45, 95, "Yes")])
    (oval,) = page.ovals   # the open one, three quarters of a circle, is left out
    assert abs((oval[1] + oval[3]) / 2 - 100) < 1
