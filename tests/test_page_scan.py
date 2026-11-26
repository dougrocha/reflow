"""The page scan (extract/scan.py) on synthetic pages: pictures, pen ink, rules and the image
objects' kinds, and the figure rule (PageScan.pictures_for) decided from its stored values."""

import random
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from reflow import geometry
from reflow.extract.lines import Line
from reflow.extract.ocrwords import GradedWord, PageWords
from reflow.extract.scan import (EMPTY_INK, InkRegion, PageScan, ScannedObject, pen_share,
                                 scan_image, scan_page, stroke_ratios)
from reflow.ingest.ocr_trust import Grade

W, H = 612, 792
PAGE = (0.0, 0.0, float(W), float(H))
AREA = W * H


def line(bbox, text="The annual report of the district for the year", ocr=False, size=10.0):
    return Line(1, bbox, text, size, False, False, "OCR" if ocr else "Times", ocr=ocr)


def page(colour=255, mode="L"):
    return Image.new(mode, (W, H), colour)


def typeset(image, lines):
    """Ink inside each line's box, as glyphs would leave it."""
    draw = ImageDraw.Draw(image)
    for l in lines:
        x0, top, x1, bottom = l.bbox
        for x in range(int(x0), int(x1) - 2, 6):
            draw.rectangle((x, top + 1, x + 3, bottom - 1), fill=0)


def texture(image, box, seed=0):
    rng = random.Random(seed)
    x0, top, x1, bottom = box
    for y in range(top, bottom):
        for x in range(x0, x1):
            image.putpixel((x, y), rng.randrange(40, 200))


TEXT = [line((72, 80 + i * 16, 540, 92 + i * 16)) for i in range(3)]


def test_a_textured_rectangle_beside_text_is_one_picture():
    image = page()
    typeset(image, TEXT)
    texture(image, (100, 300, 300, 450))
    result = scan_image(image, TEXT, None, [PAGE], [])
    assert result.status == "scanned"
    [picture] = [r for r in result.regions if r.kind == "picture"]
    assert all(abs(a - b) <= 4 for a, b in zip(picture.bbox, (100, 300, 300, 450)))
    assert result.pen == ()


def test_text_lines_alone_give_no_region():
    image = page()
    typeset(image, TEXT)
    result = scan_image(image, TEXT, None, [PAGE], [])
    assert result.status == "scanned"
    assert result.regions == ()
    assert [o.kind for o in result.objects] == ["text"]
    assert result.pictures_for(AREA) == []


def test_a_page_with_little_image_is_skipped_without_rendering():
    class Page:
        def get_size(self):
            return W, H

        def render(self, **kwargs):
            raise AssertionError("a skipped page is not rendered")

    result = scan_page(Page(), TEXT, None, [(72, 72, 200, 200)], [])
    assert result == PageScan(status="skipped")


def test_a_long_thin_rule_is_a_rule():
    image = page()
    ImageDraw.Draw(image).line((56, 103, 552, 103), fill=0, width=1)
    result = scan_image(image, [], None, [PAGE], [])
    assert [r.kind for r in result.regions] == ["rule"]
    assert result.pen == ()


def test_purple_strokes_beside_black_text_are_one_pen_region():
    image = page(mode="RGB", colour=(255, 255, 255))
    typeset(image, TEXT)
    draw = ImageDraw.Draw(image)
    for i in range(5):   # a signature: five strokes, one pixel wide
        x = 300 + i * 22
        draw.line([(x, 236), (x + 6, 200), (x + 12, 236), (x + 18, 212)], fill=(110, 30, 140), width=1)
    result = scan_image(image, TEXT, None, [PAGE], [])
    assert [r.kind for r in result.regions] == ["pen"]
    [pen] = result.pen
    assert pen.pen_share is not None and pen.pen_share >= 0.9
    assert geometry_inside(pen.bbox, (280, 184, 432, 248))


def geometry_inside(inner, outer):
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def test_pen_share_is_none_on_a_grey_page():
    image = page()
    ImageDraw.Draw(image).line((300, 200, 390, 230), fill=0, width=2)
    assert pen_share(image, (290, 190, 400, 240)) is None


def test_grain_over_most_of_the_page_is_noisy_and_still_classifies_objects():
    image = page()
    texture(image, (0, 0, W, int(H * 0.7)), seed=1)
    strip = (5.0, 700.0, 607.0, 760.0)
    lines = [line((72, 20, 300, 32))]
    result = scan_image(image, lines, None, [PAGE, strip], [])
    assert result.status == "noisy"
    assert result.regions == ()
    assert [o.kind for o in result.objects] == ["text", "empty"]


def test_a_big_glyph_overshooting_its_box_leaves_nothing():
    title = line((100, 100, 400, 130), "Annual Report", size=30)
    image = page()
    ImageDraw.Draw(image).rectangle((97, 97, 403, 133), fill=0)   # 3 pt past every side
    result = scan_image(image, [title], None, [PAGE], [])
    assert result.regions == ()
    assert result.objects[0].residual_share < EMPTY_INK


FOOTER = (5.0, 597.0, 616.0, 792.0)
FOLIO = line((302.07, 761.98, 309.93, 772.82), "iii", size=6)


def footer_page(folio):
    image = page()
    draw = ImageDraw.Draw(image)
    draw.line((40, 775, 584, 775), fill=100, width=1)
    if folio:
        draw.rectangle((303, 763, 309, 771), fill=0)
    return image


def test_a_strip_holding_only_a_rule_is_empty():
    upper = (5.0, 0.0, 616.0, 597.0)
    result = scan_image(footer_page(False), [], None, [upper, FOOTER], [])
    assert result.objects[1].kind == "empty"
    assert result.pictures_for(AREA) == []


def test_a_strip_with_a_rule_and_a_live_folio_is_text_and_no_figure():
    upper = (5.0, 0.0, 616.0, 597.0)
    result = scan_image(footer_page(True), [FOLIO], None, [upper, FOOTER], [])
    strip = result.objects[1]
    assert strip.kind == "text" and strip.lines == (0,)
    assert strip.residual_share < EMPTY_INK
    assert result.pictures_for(AREA) == []


def test_an_image_holding_one_short_line_is_text():
    box = (72.0, 72.0, 540.0, 400.0)
    short = line((100, 200, 330, 212), "Board of Directors of the District, 2024")
    assert len(short.text) == 40
    result = scan_image(page(), [short], None, [box], [])
    assert result.objects[0].kind == "text"
    # Not an image of text, though: a scanned cover with one title line keeps the title's
    # confidence (ocr-image-text marks only the text read inside a text image).
    assert result.text_images == ()


def test_a_page_of_text_is_a_text_image():
    lines = [line((72, 80 + i * 16, 540, 92 + i * 16)) for i in range(4)]   # 188 characters
    image = page()
    typeset(image, lines)
    result = scan_image(image, lines, None, [PAGE], [])
    assert result.objects[0].text_image and result.text_images == (PAGE,)


def smooth_photo():
    image = page()
    draw = ImageDraw.Draw(image)
    for x in range(72, 540):
        draw.line((x, 72, x, 400), fill=60 + (x - 72) * 120 // 468)
    return image


def test_a_smooth_photo_under_a_live_title_is_text_with_ink_and_still_a_figure():
    box = (72.0, 72.0, 540.0, 400.0)
    title = line((150, 90, 450, 110), "Wastewater District", size=18)
    result = scan_image(smooth_photo(), [title], None, [box], [])
    [photo] = result.objects
    assert photo.kind == "text" and photo.residual_share >= EMPTY_INK
    assert [r for r in result.regions if r.kind == "picture"] == []
    # Decided from the stored PageScan alone: no image goes in.
    stored = PageScan(result.status, result.objects, result.regions)
    assert stored.pictures_for(AREA) == [box]


def test_a_title_band_of_white_words_on_flat_colour_is_not_a_figure():
    # docaccess-carmel p1 after redo OCR: the title's own image, white type on the cover's blue,
    # stays an image object under its live lines. Its ink beyond them is the band's ground.
    box = (149.0, 81.0, 462.0, 156.0)
    title = [line((170, 88, 440, 108), "Carmel Area Wastewater District", size=18),
             line((240, 114, 370, 130), "Final Budget", size=14),
             line((200, 136, 410, 150), "July 1, 2024 - June 30, 2025", size=12)]
    image = page()
    draw = ImageDraw.Draw(image)
    draw.rectangle(box, fill=70)
    for l in title:
        x0, top, x1, bottom = l.bbox
        for x in range(int(x0), int(x1) - 2, 6):
            draw.rectangle((x, top + 1, x + 3, bottom - 1), fill=255)
    result = scan_image(image, title, None, [PAGE, box], [])
    band = result.objects[1]
    assert band.kind == "text" and band.residual_share >= EMPTY_INK and band.text_cover >= 0.5
    assert PageScan(result.status, result.objects, result.regions).pictures_for(AREA) == []


def test_the_residual_exception_is_off_on_a_noisy_page():
    photo = ScannedObject((72.0, 72.0, 540.0, 400.0), "text", (0,), 0.2)
    footer = ScannedObject(FOOTER, "text", (1,), 0.001)
    assert PageScan("scanned", (photo,)).pictures_for(AREA) == [photo.bbox]
    assert PageScan("noisy", (photo,)).pictures_for(AREA) == []
    for status in ("scanned", "noisy"):
        assert PageScan(status, (footer,)).pictures_for(AREA) == []


@pytest.mark.parametrize("status", ["scanned", "noisy"])
def test_a_page_size_text_object_yields_only_its_pictures(status):
    scan = PageScan(status, (ScannedObject(PAGE, "text", (0, 1, 2), 0.3),),
                    (InkRegion((100.0, 300.0, 300.0, 450.0), "picture"),) if status == "scanned" else ())
    assert scan.pictures_for(AREA) == ([(100.0, 300.0, 300.0, 450.0)] if status == "scanned" else [])


def test_a_title_bar_under_a_photo_is_not_part_of_the_picture():
    # A photo, a 16 pt white gap, then a light title bar with a darker border and its title (the
    # Carmel p10 timeline heading). Closing bridges the gap; the blank rows cut it again, so the
    # title's centre is not inside the picture and its words stay in the flow.
    image = page()
    texture(image, (100, 200, 500, 400))
    ImageDraw.Draw(image).rectangle((100, 416, 500, 436), fill=225, outline=120)
    title = line((220, 420, 380, 432), "Carmel Area Wastewater District Timeline")
    typeset(image, [title])
    result = scan_image(image, [title], None, [PAGE], [])
    [picture] = [r for r in result.regions if r.kind == "picture"]
    assert all(abs(a - b) <= 4 for a, b in zip(picture.bbox, (100, 200, 500, 400)))
    assert not geometry.centre_in(title.bbox, picture.bbox)


@pytest.mark.parametrize("photo", [False, True])
def test_tinted_paper_is_neither_ink_nor_a_photo(photo):
    # A buff report cover scanned to grey 175 (naca p1 is 182): its tone is paper, not ink under the
    # text, so a ruled table of text on it leaves no ink and no picture, while a photo on it is
    # still a picture.
    image = page(175)
    typeset(image, TEXT)
    draw = ImageDraw.Draw(image)
    for top in (200, 230, 260, 290):
        draw.line((100, top, 500, top), fill=60)
    rows = [line((110, top + 8, 490, top + 20)) for top in (200, 230, 260)]
    typeset(image, rows)
    if photo:
        texture(image, (100, 400, 300, 550))
    result = scan_image(image, TEXT + rows, None, [PAGE], [])
    assert result.status == "scanned"
    assert [r.kind for r in result.regions if r.kind != "rule"] == ["picture"] * int(photo)
    if not photo:
        assert result.objects[0].residual_share < EMPTY_INK


# -- the mask by word -------------------------------------------------------------------

def graded(box, text, grade=Grade.READ, line=0):
    kept = grade is not Grade.UNREAD
    return GradedWord(box, text, text if kept else "", 90 if kept else 20, grade, None, 1.0,
                      line=line if kept else None)


def scrawl(image, box):
    """Pen strokes filling the box, as a signature or a handwritten answer leaves them."""
    x0, top, x1, bottom = box
    draw = ImageDraw.Draw(image)
    for x in range(int(x0), int(x1) - 12, 14):
        draw.line([(x, bottom - 2), (x + 5, top + 2), (x + 10, bottom - 2), (x + 13, top + 8)],
                  fill=0, width=1)


def ocr_line(bbox, text, words):
    """An OCR line whose kept words are `words`, graded as extract.ocrwords binds them."""
    out = line(bbox, text, ocr=True)
    out.grades = tuple(w for w in words if w.grade is not Grade.UNREAD)
    return out


def test_script_beside_print_on_one_line_is_pen_and_the_print_is_masked():
    signed, by = graded((72, 212, 120, 224), "Signed"), graded((124, 212, 140, 224), "by")
    script = graded((300, 196, 420, 240), "Sxa~", Grade.UNREAD)
    row = ocr_line((72, 196, 420, 240), "Signed by", (signed, by))
    image = page()
    typeset(image, [line(signed.bbox), line(by.bbox)])
    scrawl(image, script.bbox)
    words = PageWords("graded", (signed, by, script), 12.0)
    result = scan_image(image, [row], words, [PAGE], [])
    [pen] = result.pen
    assert geometry_inside(pen.bbox, (288, 184, 432, 248))
    assert result.regions == (pen,)
    # The same line ungraded masks its whole box, the script with it.
    row.grades = None
    assert scan_image(image, [row], None, [PAGE], []).pen == ()


def test_a_small_omitted_word_between_read_words_is_no_pen():
    a, b = graded((72, 212, 130, 224), "annual"), graded((150, 212, 200, 224), "report")
    small = graded((134, 212, 146, 224), "~", Grade.UNREAD)
    row = ocr_line((72, 212, 200, 224), "annual report", (a, b))
    image = page()
    typeset(image, [line(a.bbox), line(b.bbox), line(small.bbox)])
    words = PageWords("graded", (a, small, b), 12.0)
    assert scan_image(image, [row], words, [PAGE], []).pen == ()
    assert words.omitted == (small,)


# Words of one piece of writing sit up to a line's height apart (#257): chem-math p1's first answer.
@pytest.mark.parametrize("gap, regions", [(60, 2), (30, 1), (4, 1)])
def test_omitted_word_clusters_a_line_height_apart_are_two_regions_and_nearer_ones_one(gap, regions):
    first = graded((100, 300, 200, 340), "Ixq", Grade.UNREAD)
    second = graded((200 + gap, 300, 300 + gap, 340), "qwz", Grade.UNREAD)
    image = page()
    scrawl(image, first.bbox)
    scrawl(image, second.bbox)
    result = scan_image(image, [], PageWords("graded", (first, second), 12.0), [PAGE], [])
    assert len(result.pen) == regions


@pytest.mark.parametrize("left, regions", [(100, 1), (160, 2)])
def test_touching_lines_of_writing_are_one_region_when_they_start_at_one_margin(left, regions):
    first = graded((100, 300, 200, 340), "Ixq", Grade.UNREAD)
    second = graded((left, 348, left + 100, 388), "qwz", Grade.UNREAD)
    image = page()
    scrawl(image, first.bbox)
    scrawl(image, second.bbox)
    result = scan_image(image, [], PageWords("graded", (first, second), 12.0), [PAGE], [])
    assert len(result.pen) == regions


def test_a_kept_layer_word_nothing_confirms_masks_its_ink_only_when_its_text_is_reliable():
    # docaccess-permit p2: the file's own OCR layer reads handwriting as "s,1.._,..,,1,1", and
    # Tesseract does not confirm it. Like an ungraded line, it explains its ink only when
    # its text is mostly letters and digits; junk leaves the script pen.
    junk = replace(graded((300, 196, 420, 240), "s,1.._,..,,1", Grade.DOUBT), unconfirmed=True)
    row = ocr_line((300, 196, 420, 240), junk.text, (junk,))
    image = page()
    scrawl(image, junk.bbox)
    [pen] = scan_image(image, [row], PageWords("graded", (junk,), 12.0), [PAGE], []).pen
    assert geometry_inside(pen.bbox, (288, 184, 432, 248))
    word = replace(junk, text="Matthews", layer_text="Matthews")
    row = ocr_line(word.bbox, word.text, (word,))
    assert scan_image(image, [row], PageWords("graded", (word,), 12.0), [PAGE], []).pen == ()


def test_a_misaligned_page_keeps_the_line_mask():
    signed = graded((72, 212, 120, 224), "Signed")
    script = graded((300, 196, 420, 240), "Sxa~", Grade.UNREAD)
    row = ocr_line((72, 196, 420, 240), "Signed by", (signed,))
    image = page()
    typeset(image, [line(signed.bbox)])
    scrawl(image, script.bbox)
    assert scan_image(image, [row], PageWords("misaligned", (signed, script), 12.0),
                      [PAGE], []).pen == ()


def test_a_ledger_of_script_under_printed_lines_is_pen_not_a_map():
    # docaccess-permit p3: a handwritten ledger whose printed column words OCR reads. Three
    # lines of plain words inside a cluster would make it a printed map, but most of its ink
    # lies in the words OCR left out: it is writing.
    printed = [graded((110, 312 + 40 * i, 200, 324 + 40 * i), "Single family", line=i)
               for i in range(3)]
    rows = [ocr_line(w.bbox, w.text, (w,)) for w in printed]
    script = [graded((210, 300 + 40 * i, 400, 336 + 40 * i), "a/@/??m", Grade.UNREAD)
              for i in range(3)]
    image = page()
    typeset(image, [line(w.bbox) for w in printed])
    for w in script:
        scrawl(image, w.bbox)
    draw = ImageDraw.Draw(image)
    draw.line((104, 296, 104, 424), fill=0, width=1)   # the ledger's rules join its rows
    for i in range(3):
        draw.line((104, 340 + 40 * i, 400, 340 + 40 * i), fill=0, width=1)
    result = scan_image(image, rows, PageWords("graded", (*printed, *script), 12.0), [PAGE], [])
    assert [r.kind for r in result.regions] == ["pen"]


def test_a_scanned_table_grid_is_not_pen():
    # naca p2: the rules of a table drawn in the page image, not as vector rulings.
    image = page()
    draw = ImageDraw.Draw(image)
    for y in (200, 230, 260, 290):
        draw.line((100, y, 300, y), fill=0, width=1)
    for x in (100, 170, 240, 300):
        draw.line((x, 200, x, 290), fill=0, width=1)
    assert scan_image(image, [], None, [PAGE], []).pen == ()


def test_a_typed_line_with_leaders_masks_its_ink():
    leader = line((134, 231, 300, 241), "Length ______________", ocr=True)
    image = page()
    typeset(image, [leader])
    scrawl(image, leader.bbox)
    assert scan_image(image, [leader], None, [PAGE], []).regions == ()


def bars(image, box, width):
    """Vertical strokes `width` px wide across the box, as glyph stems cut by every pixel row."""
    draw = ImageDraw.Draw(image)
    x0, top, x1, bottom = box
    for x in range(int(x0), int(x1) - width, 6):
        draw.rectangle((x, top + 1, x + width - 1, bottom - 1), fill=0)


def test_stroke_ratios_weigh_each_ocr_line_against_the_page_median():
    regular = [line((72, 80 + i * 16, 540, 92 + i * 16), ocr=True) for i in range(3)]
    heavy = line((72, 160, 540, 172), ocr=True)
    short = line((72, 200, 140, 212), text="Ab cd", ocr=True)
    printed = line((72, 240, 540, 252))
    image = page()
    for l in regular + [short, printed]:
        bars(image, l.bbox, 1)
    bars(image, heavy.bbox, 2)
    ratios = stroke_ratios(image, regular + [heavy, short, printed])
    assert ratios[:3] == (1.0, 1.0, 1.0)
    assert ratios[3] >= 1.8
    assert ratios[4:] == (None, None)   # under 6 characters; born-digital


def test_scan_image_keeps_the_strokes_and_a_born_digital_page_has_none():
    lines = [line((72, 80 + i * 16, 540, 92 + i * 16), ocr=True) for i in range(3)]
    image = page()
    for l in lines:
        bars(image, l.bbox, 1)
    assert scan_image(image, lines, None, [PAGE], []).strokes == (1.0, 1.0, 1.0)
    assert scan_image(image, lines, None, [], []).strokes == ()


def test_signatures_joined_across_their_lines_are_cut_along_them():
    """Two signatures on lines one under the other, a stroke of the upper one crossing its line
    into the lower one: one pen region above each line, and each line a rule."""
    image = page()
    draw = ImageDraw.Draw(image)
    for rule in (300, 360):
        draw.line((100, rule, 300, rule), fill=0, width=1)
        for i in range(6):   # a signature on the line: six strokes
            x = 110 + i * 26
            draw.line([(x, rule - 4), (x + 8, rule - 36), (x + 16, rule - 4), (x + 22, rule - 24)],
                      fill=0, width=1)
    draw.line([(180, 270), (186, 340)], fill=0, width=1)   # the upper one's descender
    result = scan_image(image, [], None, [PAGE], [])
    rules = [r.bbox for r in result.regions if r.kind == "rule"]
    assert [round(r[1]) for r in rules] == [300, 360]
    pens = sorted(r.bbox for r in result.pen)
    assert len(pens) == 2, pens
    assert [p[3] for p in pens] == [304, 360] and pens[1][1] >= 296, pens


def test_writing_across_a_printed_forms_cells_is_not_cut_along_their_borders():
    """Claim-form booklet p3: a cell's border meets a vertical rule at both ends. A cluster
    holding one is a grid, not signature lines: it stays one region."""
    image = page()
    draw = ImageDraw.Draw(image)
    for rule in (300, 340):
        draw.line((100, rule, 300, rule), fill=0, width=1)
    for x in (100, 300):
        draw.line((x, 292, x, 348), fill=0, width=1)   # the cells' sides, under 60 pt
    for i in range(8):   # strokes across both cells
        x = 110 + i * 22
        draw.line([(x, 270), (x + 8, 330), (x + 16, 280), (x + 22, 360)], fill=0, width=1)
    result = scan_image(image, [], None, [PAGE], [])
    assert len(result.pen) == 1, [r.bbox for r in result.pen]
