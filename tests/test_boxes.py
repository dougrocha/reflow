from reflow.analyze.layout import analyze
from reflow.extract.drawings import Drawing, Fill
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import Node, Role
from reflow.render.html import NodeRenderer


def _line(x0, top, text, size=10.0, bold=False):
    return Line(1, (x0, top, x0 + 5 * len(text), top + size * 1.2), text, size, bold, False,
                "Helvetica")


def _intro():
    """A form's left column: four lines of running text, then its first section heading
    printed lower than the box beside it."""
    return [_line(36, 20 + 14 * k, f"This form asks for the facts of the appeal, line {k}.")
            for k in range(4)] + [_line(36, 140, "1. APPLICANT INFORMATION", 14, True)]


def _band():
    """A mail-to panel at the top right, between two thick rules of one span."""
    lines = [_line(310, 20, "MAIL TO: APPEALS BOARD CLERK"), _line(310, 34, "1055 MONTEREY ST"),
             _line(310, 58, "FEE: $65.75 PER PARCEL")]
    rules = [Drawing((305, 10, 578, 13), 1, 1, strokes=1), Drawing((305, 94, 578, 97), 1, 1, strokes=1)]
    return lines, rules


def _page(lines, drawings, body=()):
    # Full-width text under it, as the form's fields run under the title and the panel.
    filler = [_line(36, 300 + 14 * k, f"Body text of the form runs across the whole page here, "
                                      f"from one margin to the other margin, line number {k}.")
              for k in range(12)]
    return PageContent(1, 612, 792, [*lines, *body, *filler], [], drawings)


def _flow(nodes):
    return [n for n in nodes if n.role != Role.ARTIFACT]


def test_band_of_text_becomes_one_named_box_before_the_heading_below_it():
    lines, rules = _band()
    nodes = _flow(analyze([_page(lines, rules, _intro())]))
    boxes = [n for n in nodes if n.role == Role.BOX]
    assert len(boxes) == 1
    box = boxes[0]
    assert box.attrs["shape"] == "band"
    assert box.attrs["name"] == "MAIL TO: APPEALS BOARD CLERK"
    assert box.bbox == (305, 10, 578, 97) and box.page == 1
    assert " ".join(c.text for c in box.children).split() == \
        "MAIL TO: APPEALS BOARD CLERK 1055 MONTEREY ST FEE: $65.75 PER PARCEL".split()
    heading = next(i for i, n in enumerate(nodes) if n.role in (Role.H1, Role.H2, Role.H3))
    assert nodes.index(box) < heading


def test_frame_of_text_becomes_a_box():
    lines = [_line(100, 110, "Key points"), _line(100, 124, "Bring your card to every visit.")]
    frame = Drawing((90, 100, 400, 150), 1, 0, strokes=1, outlines=1)
    nodes = _flow(analyze([_page(lines, [frame])]))
    box = next(n for n in nodes if n.role == Role.BOX)
    assert box.attrs["shape"] == "frame" and box.attrs["name"] == "Key points"


def test_a_box_holding_another_drawing_or_one_line_is_left_alone():
    lines = [_line(100, 110, "Key points"), _line(100, 124, "Bring your card to every visit.")]
    frame = Drawing((90, 100, 400, 150), 1, 0, strokes=1, outlines=1)
    tick = Drawing((300, 110, 308, 118), 1, 0, strokes=1, outlines=1)
    assert not any(n.role == Role.BOX for n in analyze([_page(lines, [frame, tick])]))
    assert not any(n.role == Role.BOX for n in analyze([_page(lines[:1], [frame])]))


def test_a_line_across_the_edge_sends_the_box_to_review():
    lines = [_line(100, 110, "Key points"),
             _line(100, 124, "Bring your card to every visit and keep the receipt for later.")]
    frame = Drawing((90, 100, 300, 150), 1, 0, strokes=1, outlines=1)
    nodes = analyze([_page(lines, [frame])])
    assert not any(n.role == Role.BOX for n in nodes)
    doubts = [f for n in nodes for f in n.facts if f.code == "ruled-box-doubt"]
    assert len(doubts) == 1 and "crosses the edge" in doubts[0].detail
    assert doubts[0].data["bbox"] == [90, 100, 300, 150]


def test_box_renders_as_a_named_group_that_traces_to_its_box():
    box = Node(Role.BOX, page=1, bbox=(305, 10, 578, 97),
               attrs={"shape": "band", "name": "MAIL TO: <CLERK>"},
               children=[Node(Role.P, page=1, bbox=(310, 20, 450, 32), text="MAIL TO: <CLERK>")])
    html = NodeRenderer({}, {1: "1"}).render(box)
    assert html.startswith('<section class="ruled-box" role="group" '
                           'aria-label="MAIL TO: &lt;CLERK&gt;" data-node="')
    assert f'data-node="{box.id}"' in html and "<p " in html and html.endswith("</section>")


def test_rules_that_underline_text_are_no_band():
    """Two links underlined at one span are underlines, not the edges of a box."""
    lines = [_line(310, 0, "Available online at the agency"), _line(310, 20, "MAIL TO: APPEALS BOARD"),
             _line(310, 34, "1055 MONTEREY ST"), _line(310, 58, "FEE: $65.75 PER PARCEL")]
    rules = [Drawing((305, 13, 578, 14), 1, 1, strokes=1), Drawing((305, 94, 578, 95), 1, 1, strokes=1)]
    nodes = analyze([_page(lines, rules, _intro())])
    assert not any(n.role == Role.BOX for n in nodes)
    assert not any(f.code == "ruled-box-doubt" for n in nodes for f in n.facts)


def test_a_span_ruled_three_times_is_no_band():
    lines, rules = _band()
    third = Drawing((305, 200, 578, 201), 1, 1, strokes=1)
    assert not any(n.role == Role.BOX for n in analyze([_page(lines, [*rules, third], _intro())]))


def test_band_from_the_scan_rules_of_a_scanned_page():
    """The page scan rounds a rule's box to its 8 pt grid; the band is found all the same."""
    from PIL import Image, ImageDraw

    from reflow.extract.scan import scan_image
    image = Image.new("RGB", (612, 792), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((305, 11, 578, 12), fill="black")
    draw.rectangle((305, 95, 578, 96), fill="black")
    scan = scan_image(image, [], None, [(0, 0, 612, 792)], [])
    assert [r.kind for r in scan.regions] == ["rule", "rule"]
    lines, _ = _band()
    page = _page(lines, [], _intro())
    page.scan = scan
    box = next(n for n in analyze([page]) if n.role == Role.BOX)
    assert box.attrs["shape"] == "band" and box.attrs["name"] == "MAIL TO: APPEALS BOARD CLERK"


TINT = (209, 234, 247)


def _fill(box, rgb=TINT):
    x0, y0, x1, y1 = box
    return Fill(box, rgb, "box", 1, ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)))


def _callout():
    """A label over a sentence of two lines, on a tinted rectangle at the page's right."""
    lines = [_line(425, 180, "REMEMBER", 14, True), _line(425, 200, "Claims must be sent"),
             _line(425, 213, "within one year.")]
    return lines, Drawing((416, 170, 568, 260), 1, 0, fills=1)


def _tinted(lines, drawing, fills, body=()):
    page = _page(lines, [drawing], body)
    page.fills = fills
    return page


def test_text_on_a_tinted_rectangle_becomes_one_filled_box():
    lines, drawing = _callout()
    nodes = _flow(analyze([_tinted(lines, drawing, [_fill(drawing.bbox)])]))
    box = next(n for n in nodes if n.role == Role.BOX)
    assert box.attrs["shape"] == "fill" and box.attrs["name"] == "REMEMBER"
    assert box.bbox == drawing.bbox
    assert " ".join(c.text for c in box.children).split() == \
        "REMEMBER Claims must be sent within one year.".split()


def test_a_tinted_rectangle_drawn_as_glyph_outlines_is_one_box_all_the_same():
    """A page whose text is outlines: the fill and the letters on it are one drawing."""
    lines, _ = _callout()
    drawing = Drawing((416, 170, 568, 260), 40, 0, fills=40)
    nodes = _flow(analyze([_tinted(lines, drawing, [_fill(drawing.bbox)])]))
    assert any(n.role == Role.BOX and n.attrs["shape"] == "fill" for n in nodes)


def test_stripes_a_backdrop_and_a_one_line_tint_are_no_box():
    lines, drawing = _callout()
    stripes = [_fill((416, y, 568, y + 20)) for y in (170, 210, 250)]
    assert not any(n.role == Role.BOX for n in analyze([_tinted(lines, drawing, stripes)]))
    backdrop = _fill((0, 0, 612, 792))
    assert not any(n.role == Role.BOX for n in analyze([_tinted(lines, drawing, [backdrop])]))
    one = _tinted(lines[:1], Drawing((416, 170, 568, 200), 1, 0, fills=1), [_fill((416, 170, 568, 200))])
    assert not any(n.role == Role.BOX for n in analyze([one]))


def test_a_tinted_rectangle_with_a_picture_or_a_rounded_shape_is_no_box():
    lines, drawing = _callout()
    icon = Drawing((540, 185, 600, 205), 1, 0, strokes=1, outlines=1)
    page = _tinted(lines, drawing, [_fill(drawing.bbox)])
    page.drawings.append(icon)
    assert not any(n.role == Role.BOX for n in analyze([page]))
    rounded = Fill(drawing.bbox, TINT, "mark", 1)
    assert not any(n.role == Role.BOX for n in analyze([_tinted(lines, drawing, [rounded])]))
    # An icon wholly inside the tint is a picture too, not a rule of the box.
    page = _tinted(lines, drawing, [_fill(drawing.bbox)])
    page.drawings.append(Drawing((540, 230, 560, 250), 1, 0, strokes=1, outlines=1))
    assert not any(n.role == Role.BOX for n in analyze([page]))
    # A curved fill has no corner points to check: it is not a plain rectangle.
    oval = Fill(drawing.bbox, TINT, "box", 1)
    assert not any(n.role == Role.BOX for n in analyze([_tinted(lines, drawing, [oval])]))


def test_a_filled_box_renders_as_an_aside_when_it_opens_with_a_note_else_a_group():
    kids = [Node(Role.NOTE, page=1, bbox=(425, 180, 480, 194), text="REMEMBER"),
            Node(Role.P, page=1, bbox=(425, 200, 540, 226), text="Claims must be sent.")]
    attrs = {"shape": "fill", "name": "REMEMBER"}
    box = Node(Role.BOX, page=1, bbox=(416, 170, 568, 260), attrs=attrs, children=kids)
    html = NodeRenderer({}, {1: "1"}).render(box)
    assert html.startswith('<aside class="filled-box" aria-label="REMEMBER" data-node="')
    assert html.endswith("</aside>")
    plain = Node(Role.BOX, page=1, bbox=(416, 170, 568, 260), attrs=attrs,
                 children=[kids[1], kids[1]])
    assert NodeRenderer({}, {1: "1"}).render(plain).startswith(
        '<section class="filled-box" role="group" aria-label="REMEMBER" data-node="')


def test_a_tinted_continued_banner_is_no_box():
    """A directory tints the "(continued)" banner at the top of each page: furniture, not a callout."""
    lines = [_line(425, 180, "ORTHOPEDIC (continued)", 12, True), _line(425, 200, "Maricopa County")]
    drawing = Drawing((416, 170, 568, 230), 1, 0, fills=1)
    nodes = analyze([_tinted(lines, drawing, [_fill(drawing.bbox)])])
    assert not any(n.role == Role.BOX for n in nodes)
    assert any(n.attrs.get("context") for n in nodes)


def test_a_tinted_paragraph_alone_is_a_banner_not_a_callout():
    lines = [_line(425, 180, "PSYCHOLOGY, ADULT, ADOLESCENT", 12, True), _line(425, 194, "AND CHILD", 12, True)]
    drawing = Drawing((416, 170, 568, 215), 1, 0, fills=1)
    assert not any(n.role == Role.BOX for n in analyze([_tinted(lines, drawing, [_fill(drawing.bbox)])]))
