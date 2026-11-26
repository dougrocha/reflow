"""Struck and underlined OCR words, their brackets and the tracked-change page gate."""
import shutil
import subprocess
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFont

from ocrfixtures import PRIVATE
from reflow.extract.pages import extract_pages
from reflow.extract.ocrwords import bind_words
from reflow.ingest.ocr_trust import (READ_CONF, Grade, PageGrade, Word, bracket_ends, clean_hocr,
                                     grade_words, ink_mask, parse_hocr, read_evidence, struck,
                                     track_changes, underline, write_sidecar)

FONT = Path("/usr/share/fonts/gsfonts/NimbusMonoPS-Regular.otf")
PLUGIN = Path(__file__).resolve().parents[2] / "src" / "reflow" / "ingest" / "ocr_trust.py"
READ = 40.0   # the page's median READ height, in the synthetic pages' pixels

pytestmark = pytest.mark.skipif(not FONT.exists(), reason="no Nimbus Mono (paru -S gsfonts)")


def font(size=60):
    return ImageFont.truetype(str(FONT), size)


def page(words, marks=(), size=(1800, 240), y=60):
    """A grey page of one typed line: `words` in order, `marks` {word: "strike" | "under"}.
    Returns the image, each word's box, the baseline and the draw handle."""
    f = font()
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    base = y + f.getmetrics()[0]
    boxes, x = {}, 40
    for text in words:
        box = draw.textbbox((x, y), text, font=f)
        draw.text((x, y), text, fill=0, font=f)
        boxes[text] = box
        kind = dict(marks).get(text)
        if kind == "strike":
            # Between the brackets, as a bill strikes "[or]".
            left = draw.textbbox((x, y), text[0], font=f)[2] if text.startswith("[") else box[0]
            right = (draw.textbbox((x, y), text[:-1], font=f)[2] if text.endswith("]")
                     else box[2])
            draw.rectangle((left + 3, base - 20, right - 2, base - 16), fill=0)
        elif kind == "under":
            draw.rectangle((box[0], base + 8, box[2], base + 11), fill=0)
        x = box[2] + 36
    return image, boxes, base, draw


def raw_words(boxes, base, confs=None):
    from reflow.ingest.ocr_trust import RawWord
    return [RawWord(box, text, (confs or {}).get(text, 90), 0, f"w{i}", base)
            for i, (text, box) in enumerate(boxes.items())]


def test_a_strike_and_an_underline_are_found_and_nothing_else():
    image, boxes, base, _ = page(["court,", "a", "[or]", "statutory", "gyp", "-", "—"],
                                 {"[or]": "strike", "a": "under"})
    ink, row = ink_mask(image), list(boxes.values())
    hits = {t: struck(ink, b, row, READ) for t, b in boxes.items()}
    assert hits == {"court,": False, "a": False, "[or]": True, "statutory": False, "gyp": False,
                    "-": False, "—": False}
    unders = {t: underline(ink, b, row, base, READ, len(t)) for t, b in boxes.items()}
    assert unders["a"] == (boxes["a"][0], boxes["a"][2])
    assert [t for t, u in unders.items() if u] == ["a"]   # no descender is an underline


def test_a_rule_through_or_under_a_whole_row_marks_no_word():
    """A table rule runs on past the words: it is not a strike, nor an underline."""
    image, boxes, base, draw = page(["Fund", "balance", "total"])
    draw.rectangle((5, base - 20, 1795, base - 16), fill=0)    # through the row
    draw.rectangle((5, base + 8, 1795, base + 11), fill=0)     # under it
    ink, row = ink_mask(image), list(boxes.values())
    assert not any(struck(ink, b, row, READ) for b in row)
    assert not any(underline(ink, b, row, base, READ, 4) for b in row)


def test_brackets_are_measured_not_read():
    image, boxes, base, _ = page(["[or]", "or"], {"[or]": "strike", "or": "strike"})
    ink = ink_mask(image)
    left, right = bracket_ends(ink, boxes["[or]"])
    assert left is not None and right is not None
    x0, _, x1, _ = boxes["[or]"]
    assert left[2] < x0 + (x1 - x0) / 4 and right[0] > x1 - (x1 - x0) / 4
    assert bracket_ends(ink, boxes["or"]) == (None, None)
    # The reading does not matter: a misread "fe¥|" keeps the same brackets.
    raw = raw_words({"fe¥|": boxes["[or]"], "or": boxes["or"]}, base, {"fe¥|": 49, "or": 90})
    words, tracked = track_changes(grade_words(raw, image), raw, image)
    assert not tracked   # struck words, but no underline on the page
    assert [w.struck for w in words] == [True, True]


def _tracked(words_marks, confs):
    words, marks = zip(*words_marks)
    image, boxes, base, _ = page(list(words), {w: m for w, m in words_marks if m})
    raw = raw_words(boxes, base, confs)
    return track_changes(grade_words(raw, image), raw, image)


def test_the_page_gate():
    # One struck word with brackets and one underline: a tracked-change page.
    words, tracked = _tracked([("a", "under"), ("[or]", "strike"), ("court", None)], {"[or]": 49})
    assert tracked
    # Two struck words without brackets and no underline: not.
    _, tracked = _tracked([("this", "strike"), ("that", "strike"), ("court", None)], {})
    assert not tracked
    # One unbracketed strike and an underline: not (one strike needs its brackets).
    _, tracked = _tracked([("this", "strike"), ("court", "under")], {})
    assert not tracked


def test_a_ruled_table_with_underlined_headers_is_no_tracked_page():
    """Two pages of a ruled table: header rules run under the whole row, row rules through
    blank cells. Each page decides alone, from its words only."""
    for _ in range(2):
        image, boxes, base, draw = page(["Fund", "Amount", "Year"])
        draw.rectangle((5, base + 8, 1795, base + 11), fill=0)
        draw.rectangle((5, base - 20, 1795, base - 16), fill=0)
        raw = raw_words(boxes, base)
        words, tracked = track_changes(grade_words(raw, image), raw, image)
        assert not tracked and not any(w.struck or w.underlined for w in words)


def test_a_struck_word_below_read_keeps_only_its_brackets_on_a_tracked_page():
    words, tracked = _tracked([("a", "under"), ("[or]", "strike"), ("this", "strike"),
                               ("[it]", "strike"), ("court", None)],
                              {"[or]": 49, "this": 67, "[it]": 91})
    assert tracked
    by_text = {w.text: w for w in words}
    assert (by_text["[or]"].grade, by_text["[or]"].layer_text) == (Grade.UNREAD, "[]")
    assert (by_text["this"].grade, by_text["this"].layer_text) == (Grade.UNREAD, "")
    assert (by_text["[it]"].grade, by_text["[it]"].layer_text) == (Grade.READ, "[it]")
    assert by_text["a"].underlined and by_text["court"].layer_text == "court"


def test_a_struck_doubt_on_an_untracked_page_is_graded_as_usual():
    words, tracked = _tracked([("this", "strike"), ("that", "strike")], {"this": 67})
    assert not tracked
    assert words[0].struck and words[0].grade is Grade.DOUBT and words[0].layer_text == "this"


def test_an_underline_under_part_of_a_word_marks_its_punctuation():
    image, boxes, base, draw = page(["court,", "or", "the"])
    comma = draw.textbbox((boxes["court,"][0], 60), "court", font=font())[2]
    draw.rectangle((comma, base + 8, boxes["the"][2], base + 11), fill=0)
    raw = raw_words(boxes, base)
    words, _ = track_changes(grade_words(raw, image), raw, image)
    assert [(w.text, w.underlined, w.under) for w in words] == [
        ("court,", False, (5, 6)), ("or", True, None), ("the", True, None)]


HOCR = """<div class='ocr_page' title='bbox 0 0 600 400'>
  <span class='ocr_line' id='line_1_1' title="bbox 10 10 590 40; baseline 0 -5">
   <span class='ocrx_word' id='w1' title='bbox 10 10 40 40; x_wconf 93'>a</span>
   <span class='ocrx_word' id='w2' title='bbox 50 10 120 40; x_wconf 49'>[e&#165;]</span>
   <span class='ocrx_word' id='w3' title='bbox 130 10 200 40; x_wconf 92'>[it]</span>
   <span class='ocrx_word' id='w4' title='bbox 210 10 260 40; x_wconf 67'>e&#165;</span>
   <span class='ocrx_word' id='w5' title='bbox 270 10 320 40; x_wconf 12'>#&amp;</span>
   <span class='ocrx_word' id='w6' title='bbox 330 10 480 40; x_wconf 91'>statutory</span>
  </span>
</div>"""


def test_clean_hocr_writes_a_struck_words_brackets_and_drops_the_rest():
    raw = parse_hocr(HOCR)
    assert raw[0].base == 35
    graded = grade_words(raw, None)
    struck_words = {"[e¥]": "[]", "[it]": "[it]", "e¥": ""}
    words = [w if w.text not in struck_words else
             Word(w.box, w.text, w.conf, Grade.UNREAD if w.conf < READ_CONF else w.grade, w.pen,
                  w.tall, struck=True, layer_text=struck_words[w.text], line=w.line)
             for w in graded]
    out = clean_hocr(HOCR, words, [w.id for w in raw])
    assert [(w.text, w.box) for w in parse_hocr(out)] == [
        ("a", (10, 10, 40, 40)), ("[]", (50, 10, 120, 40)), ("[it]", (130, 10, 200, 40)),
        ("statutory", (330, 10, 480, 40))]


def test_a_struck_words_brackets_bind_to_their_line(tmp_path):
    """The layer's "[]" is matched like any kept word, though its grade is UNREAD."""
    import pikepdf
    from pikepdf import Dictionary, Name
    pdf = pikepdf.new()
    f = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    pg = pdf.add_blank_page(page_size=(612, 792))
    pg.Resources = Dictionary(Font=Dictionary(F1=f))
    pg.Contents = pdf.make_stream(b"BT 3 Tr /F1 12 Tf 72 680 Td (a [] statutory) Tj ET")
    path = tmp_path / "w.pdf"
    pdf.save(path)
    first = next(extract_pages(path, 1, 1))
    [line] = first.lines
    doc = pdfium.PdfDocument(path)
    try:
        from reflow.extract.ocrwords import layer_words
        layer = layer_words(doc[0])
    finally:
        doc.close()
    px = 300 / 72
    words = []
    for (text, box), (reading, conf, grade, layer_text) in zip(layer, [
            ("a", 93, Grade.READ, "a"), ("[e¥]", 49, Grade.UNREAD, "[]"),
            ("statutory", 91, Grade.READ, "statutory")]):
        words.append(Word(tuple(round(v * px) for v in (box[0], box[1] + 1, box[2], box[3] - 1)),
                          reading, conf, grade, None, 1.0, struck=reading == "[e¥]",
                          layer_text=layer_text))
    write_sidecar(path, [PageGrade(1, "redo", (round(612 * px), round(792 * px)), tuple(words),
                                   tracked=True)])
    bound = next(extract_pages(path, 1, 1))
    assert bound.words.status == "graded" and bound.words.tracked
    assert [w.layer_text for w in bound.lines[0].grades] == ["a", "[]", "statutory"]
    assert bound.words.omitted[0].struck   # its reading is not printed


@pytest.mark.skipif(shutil.which("ocrmypdf") is None, reason="no ocrmypdf binary")
def test_the_plugin_writes_only_the_brackets_of_an_unreadable_struck_word(tmp_path):
    f = font(50)
    image = Image.new("RGB", (2550, 3300), "white")
    draw = ImageDraw.Draw(image)
    y, x = 400, 300
    base = y + f.getmetrics()[0]
    for text, mark in [("court, a ", None), ("[or]", "strike"), (" statutory probate court", None),
                       (", or the business court", "under"), (".", None)]:
        box = draw.textbbox((x, y), text, font=f)
        draw.text((x, y), text, fill="black", font=f)
        if mark == "strike":
            left = draw.textbbox((x, y), "[", font=f)[2]
            right = draw.textbbox((x, y), "[or", font=f)[2]
            draw.rectangle((left + 3, base - 16, right - 1, base - 12), fill="black")
        elif mark == "under":
            draw.rectangle((draw.textbbox((x, y), ",", font=f)[0], base + 6, box[2], base + 9),
                           fill="black")
        x = box[2]
    src, out, evidence = tmp_path / "in.pdf", tmp_path / "out.pdf", tmp_path / "evidence"
    evidence.mkdir()
    image.save(src, resolution=300)
    subprocess.run(["nice", "ocrmypdf", "--force-ocr", "--pdf-renderer", "fpdf2", "--output-type",
                    "pdf", "-l", "eng", "--plugin", str(PLUGIN), "--reflow-evidence",
                    str(evidence), "--reflow-drop", "on", str(src), str(out)],
                   check=True, capture_output=True)
    grade = read_evidence(evidence / "1.json")
    assert grade.tracked
    [gone] = [w for w in grade.words if w.struck]
    assert gone.conf < READ_CONF and (gone.grade, gone.layer_text) == (Grade.UNREAD, "[]")
    doc = pdfium.PdfDocument(out)
    try:
        text = doc[0].get_textpage().get_text_range()
        assert "a [] statutory probate court" in " ".join(text.split())
        bound = bind_words(grade, doc[0], None)
    finally:
        doc.close()
    assert bound.status == "graded" and bound.tracked


P5 = PRIVATE / "docaccess-legislation-5"


@pytest.mark.skipif(not (P5.with_suffix(".png").exists() and P5.with_suffix(".hocr").exists()),
                    reason="the legislation p5 capture is not in the main checkout")
def test_the_three_struck_or_of_legislation_p5_keep_only_their_brackets():
    """Legislation p5 at 300 dpi, captured through OCRmyPDF (tests/ocr/capture.py): the three
    "[or]" Tesseract reads as "[e¥]" at 49, 67 and 79 are struck, with brackets measured at both
    ends; the page is a tracked-change page; and the underlined words are found."""
    raw = parse_hocr(P5.with_suffix(".hocr").read_text(encoding="utf-8"))
    with Image.open(P5.with_suffix(".png")) as image:
        image.load()
        words, tracked = track_changes(grade_words(raw, image), raw, image)
    assert tracked
    assert [(w.text, w.conf, w.layer_text) for w in words if w.struck] == [
        ("[e¥]", 49, "[]"), ("[e¥]", 67, "[]"), ("[e¥]", 79, "[]")]
    under = [w.text if w.underlined else w.text[slice(*w.under)] for w in words
             if (w.underlined or w.under) and w.grade is not Grade.UNREAD]
    # Read off the page image: line 1 "court"; "a" three times; ", or the business court"
    # over lines 11-12; line 19's lone "," and ", or business court"; line 24's ", or business
    # court". The comma of "court," at line 12 is not underlined.
    assert under == ["court", "a", "a", "a", ",", "or", "the", "business", "court",
                     ",", ",", "or", "business", "court", ",", "or", "business", "court"]



def _passage(words, struck, confs, cut=None):
    """A tracked page of one line whose words `struck` one strike runs through, ending at the
    last one's full stop; "court" after them is underlined. `cut` {word: pixels} trims the left
    of a word's box, as Tesseract loses a struck word's first letters."""
    image, boxes, base, draw = page([*words, "court"], {"court": "under"}, size=(3200, 240))
    draw.rectangle((boxes[struck[0]][0] + 3, base - 20, boxes[struck[-1]][2] - 2, base - 16),
                   fill=0)
    for word, pixels in (cut or {}).items():
        x0, y0, x1, y1 = boxes[word]
        boxes[word] = (x0 + pixels, y0, x1, y1)
    raw = raw_words(boxes, base, confs)
    return track_changes(grade_words(raw, image), raw, image)


def test_a_struck_line_whose_first_letters_ocr_lost_is_struck_to_its_full_stop():
    """Legislation p24: Tesseract lost "ad" of a struck "additional", so the strike
    begins before the line's first box, and it ends at a full stop, with no letter above it.
    Each end passes on its own: under letters at the left, within the words at the right."""
    words, tracked = _passage(["additional", "legislative", "appropriations.", "]"],
                              ["additional", "legislative", "appropriations."],
                              {"additional": 30, "legislative": 30, "appropriations.": 30, "]": 60},
                              cut={"additional": 80})
    assert tracked
    assert [w.struck for w in words] == [True, True, True, True, False]
    # The "]" the strike ends at is measured, not read: it belongs to the deletion.
    assert (words[3].grade, words[3].layer_text) == (Grade.UNREAD, "")


def test_a_word_read_of_a_struck_passage_ocr_could_not_read_is_not_printed():
    """Legislation p24: of one strike's words OCR read "1," alone. The words one strike
    runs through are one deletion: "1," is unread with the rest."""
    words, tracked = _passage(["funding", "through", "appropriations.", "[The", "division", "1,",
                               "2026"], ["[The", "division", "1,", "2026"],
                              {"[The": 30, "division": 30, "1,": 92, "2026": 30})
    assert tracked
    assert [w.layer_text for w in words[:3]] == ["funding", "through", "appropriations."]
    assert [w.grade for w in words[3:7]] == [Grade.UNREAD] * 4
    assert words[5].layer_text == ""


def test_a_readable_word_ending_in_a_bracket_beside_a_deletion_keeps_its_letters():
    """Only a lone "]" a strike ends at is measured into the deletion. "law]",
    set close after a struck "[former]", holds letters before its bracket: it is read and
    printed, not struck."""
    image, boxes, base, draw = page(["court", "[former]"], {"[former]": "strike", "court": "under"})
    x, y = boxes["[former]"][2] + 6, 60
    boxes["law]"] = draw.textbbox((x, y), "law]", font=font())
    draw.text((x, y), "law]", fill=0, font=font())
    raw = raw_words(boxes, base, {"law]": 90})
    words, tracked = track_changes(grade_words(raw, image), raw, image)
    assert tracked
    law = words[2]
    assert (law.struck, law.grade, law.layer_text) == (False, Grade.READ, "law]")
