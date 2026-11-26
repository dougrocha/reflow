"""The grading policy of reflow.ingest.ocr_trust, on words OCRmyPDF itself read.

The naca and scan fixtures are committed; the DocAccess ones are captured from the gitignored
samples into the main checkout (tests/ocr/capture.py) and are skipped where they are absent."""
import pytest

from ocrfixtures import available, by_text, graded
from reflow.ingest.ocr_trust import (READ_CONF, SCRIPT_HEIGHT, Grade, RawWord, Word,
                                     grade_words)


def _fixture(name):
    if not available(name):
        pytest.skip(f"no fixture {name}: run tests/ocr/capture.py")
    return graded(name)


def _one(words, text) -> Word:
    found = by_text(words, text)
    assert found, f"{text!r} not in the fixture"
    return found[0]


def test_budget_signature_page():
    words = _fixture("docaccess-budget-4")
    assert _one(words, "Signed").grade is Grade.READ and _one(words, "by:").grade is Grade.READ
    president, attest = _one(words, "President"), _one(words, "ATTEST:")
    assert (president.conf, president.grade) == (78, Grade.DOUBT)
    assert (attest.conf, attest.grade) == (66, Grade.DOUBT)
    diwiw, secretary, sara = _one(words, "Diwiw"), _one(words, "Secretaﬂr"), _one(words, "Sara")
    assert diwiw.grade is Grade.UNREAD and diwiw.tall > 2
    assert secretary.grade is Grade.UNREAD and secretary.tall > SCRIPT_HEIGHT
    assert sara.conf == 77 and sara.tall >= SCRIPT_HEIGHT and sara.grade is Grade.UNREAD
    assert president.tall < SCRIPT_HEIGHT
    assert _one(words, "4™").grade is Grade.DOUBT


def test_chem_math_worksheet():
    words = _fixture("docaccess-chem-math-1")
    assert _one(words, "copper(ll)").grade is Grade.DOUBT
    assert _one(words, "Zgﬁf)}wﬁq").grade is Grade.UNREAD
    six = by_text(words, "6S512")
    assert all(w.grade is Grade.UNREAD for w in six)
    # The purple working: every word read in pen colour without confidence is dropped.
    pen = [w for w in words if w.pen is not None and w.pen >= 0.9 and w.conf < READ_CONF]
    assert pen and all(w.grade is Grade.UNREAD for w in pen)
    typed = [w for w in words if w.conf >= READ_CONF and (w.pen or 0) < 0.1]
    assert typed and all(w.grade is Grade.READ for w in typed
                         if w.text not in ("for", "a", "of"))   # rule 4 strays, see below


def test_naca_cover_keeps_confident_coloured_print():
    words = _fixture("naca-1")
    for text in ("NATIONAL", "ADVISORY", "COMMITTEE"):
        assert _one(words, text).grade is Grade.READ
    coloured = [w for w in words if w.conf >= READ_CONF and w.pen is not None and w.pen >= 0.5]
    assert coloured and all(w.grade is Grade.READ for w in coloured)
    copy = _one(words, "Copy")
    assert copy.pen is not None and copy.pen >= 0.9
    assert copy.grade is (Grade.READ if copy.conf >= READ_CONF else Grade.DOUBT)


def test_permit_ledger_has_no_read_word_under_read_conf():
    words = _fixture("docaccess-permit-3")
    assert all(w.grade is not Grade.READ for w in words if w.conf < READ_CONF)
    assert sum(w.grade is Grade.UNREAD for w in words) > len(words) / 2


def test_brochure_map_letters_are_unread():
    words = _fixture("docaccess-brochure-2")
    line = [w for w in words if w.text in ("L", "I", "P", "e", "S")]
    assert line and all(w.grade is Grade.UNREAD for w in line)


def test_legislation_bracketed_slips_are_not_read():
    words = _fixture("docaccess-legislation-5")
    brackets = by_text(words, "[e¥]")
    assert sorted(w.conf for w in brackets) == [49, 67, 79]
    assert all(w.grade is not Grade.READ for w in brackets)


def _raw(text, conf, box=(0, 0, 40, 20), line=0):
    return RawWord(box, text, conf, line)


@pytest.mark.parametrize("text", ["CROSBY", "UtaScore", "CHARLES"])
def test_garbage_capitals_have_no_exception(text):
    assert grade_words([_raw(text, 45)], None)[0].grade is Grade.DOUBT
    assert grade_words([_raw(text, 39)], None)[0].grade is Grade.UNREAD


def test_the_rules_in_order():
    words = grade_words([_raw("typed", 92), _raw("6S512", 55), _raw("|87)88-HY", 59),
                         _raw("owner's", 51), _raw("maybe", 41), _raw("gone", 39)], None)
    assert [w.grade for w in words] == [Grade.READ, Grade.UNREAD, Grade.UNREAD, Grade.DOUBT,
                                        Grade.DOUBT, Grade.UNREAD]
    assert [w.layer_text for w in words] == ["typed", "", "", "owner's", "maybe", ""]


def test_tall_doubtful_word_is_script():
    body = [_raw("word", 90, (0, 0, 40, 20)) for _ in range(3)]
    words = grade_words([*body, _raw("Sara", 77, (50, 0, 90, 31), 1),
                         _raw("Heading", 77, (50, 40, 90, 66), 2)], None)
    assert words[3].tall == pytest.approx(1.55) and words[3].grade is Grade.UNREAD
    assert words[4].tall == pytest.approx(1.3) and words[4].grade is Grade.DOUBT


def test_stray_confident_letters_of_a_scrawl_are_unread():
    scrawl = [_raw("el", 90, line=1), _raw("#&w", 20, line=1), _raw("frrr", 0, line=1),
              _raw("Mx", 30, line=1)]
    words = grade_words([*scrawl, _raw("ok", 90, line=2), _raw("zz", 20, line=2),
                         _raw("fine", 90, line=2)], None)
    assert words[0].grade is Grade.UNREAD           # 3 of 4 unread: "el" is a stray
    assert words[4].grade is Grade.READ             # 1 of 3 unread: "ok" stays


def test_large_type_read_on_its_line_keeps_a_tall_doubtful_word():
    body = [_raw("word", 90, (0, 0, 40, 20)) for _ in range(3)]
    street = [_raw("CHAMBERLIN", 90, (0, 30, 80, 70), 1), _raw("AVENUE", 51, (90, 30, 150, 70), 1)]
    stray = [_raw("J", 95, (0, 80, 20, 120), 2), _raw("Sxmth", 70, (30, 80, 90, 120), 2)]
    words = grade_words([*body, *street, *stray], None)
    assert words[4].tall == pytest.approx(2.0) and words[4].grade is Grade.DOUBT
    assert words[6].grade is Grade.UNREAD           # a stray letter is not proof of print


def test_brochure_street_name_under_the_map_is_kept():
    words = _fixture("docaccess-brochure-3")
    street = next(w for w in words if w.text == "CHAMBERLIN" and w.tall >= SCRIPT_HEIGHT)
    avenue = [w for w in words if w.line == street.line and w.text == "AVENUE"]
    assert street.grade is Grade.READ
    assert avenue and avenue[0].tall >= SCRIPT_HEIGHT and avenue[0].grade is Grade.DOUBT


def test_print_with_a_confident_neighbour_of_its_stroke_is_doubtful_not_unread():
    words = _fixture("scan-1")
    whats, new = _one(words, "What’s"), _one(words, "New")
    assert whats.tall >= SCRIPT_HEIGHT and new.grade is Grade.READ
    assert whats.grade is Grade.DOUBT          # the bold heading: "New" is as bold
    url = _one(words, "www.rs.gov/FormW9.")
    assert url.conf < 60 and url.grade is Grade.DOUBT   # the URL in a line of its print


def test_a_kept_layer_word_is_read_where_tesseract_reads_it_alike_confidently_and_doubtful_elsewhere():
    from PIL import Image

    from reflow.ingest.ocr_trust import grade_layer
    image = Image.new("RGB", (400, 100), "white")
    read = grade_words([_raw("Unit", 16, (10, 10, 50, 30), 1), _raw("budget", 92, (60, 10, 120, 30), 1),
                        _raw("$215,711;", 0, (130, 10, 175, 30), 1), _raw("and", 95, (178, 10, 192, 30), 1),
                        _raw("Length______", 0, (200, 10, 290, 30), 2)], image)
    layer = [("Unit", (10, 10, 50, 30)),             # Tesseract agrees, unsure: doubtful, not 16
             ("budget", (61, 11, 119, 29)),
             ("$215,711; and", (130, 10, 192, 30)),  # two words under it: doubtful, not 0
             ("Length", (200, 10, 240, 30)),         # read otherwise: doubtful, not 0
             ("6'17", (300, 60, 340, 80))]           # Tesseract read nothing there: doubtful
    words = grade_layer(layer, read, image)
    assert [w.text for w in words] == [t for t, _ in layer]
    assert [w.conf for w in words] == [READ_CONF - 1, 92, READ_CONF - 1, READ_CONF - 1, READ_CONF - 1]
    assert [w.grade for w in words] == [Grade.DOUBT, Grade.READ, Grade.DOUBT, Grade.DOUBT,
                                        Grade.DOUBT]
    assert words[1].line == 1 and words[4].line not in {w.line for w in read}
    assert [w.unconfirmed for w in words] == [True, False, True, True, True]



def test_a_tall_kept_layer_word_tesseract_reads_alike_is_print_not_writing():
    # docaccess-budget p5: "County of Jefferson", set larger than the body, which Tesseract reads
    # alike at 78 and 73. Two readings that agree on its letters are print, whatever its height;
    # a reading that differs ("Secretaﬂr" under the signature read "UtaScore") is not.
    from PIL import Image

    from reflow.ingest.ocr_trust import grade_layer
    image = Image.new("RGB", (600, 200), "white")
    body = [_raw(t, 92, (10 + 60 * i, 10, 60 + 60 * i, 30), 1)
            for i, t in enumerate(("the", "above", "named", "notary"))]
    read = grade_words([*body, _raw("County", 78, (10, 60, 90, 90), 2),
                        _raw("Jefferson!", 73, (100, 60, 220, 90), 2),
                        _raw("LEVY2", 70, (230, 60, 290, 90), 2),      # p11: one letter off
                        _raw("Secretafr", 53, (300, 60, 420, 90), 3)], image)
    layer = [(w.text, w.box) for w in body] + [
        ("County", (10, 60, 90, 90)), ("Jefferson", (100, 60, 220, 90)),
        ("LEVY?", (230, 60, 290, 90)), ("UtaScore", (300, 60, 420, 90))]
    words = grade_layer(layer, read, image)
    assert [w.grade for w in words[4:]] == [Grade.DOUBT, Grade.DOUBT, Grade.DOUBT, Grade.UNREAD]
    # Kept by agreement alone, a tall word is doubtful in its own right: it is
    # marked and reviewed, not left to the lexical gate, which a handwritten name would pass.
    assert [w.unconfirmed for w in words[4:7]] == [False, False, False]

def test_pen_colour_says_nothing_on_a_page_whose_type_is_tinted_too():
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (300, 60), (230, 190, 150))     # brown paper
    draw = ImageDraw.Draw(image)
    for x in (10, 110):
        draw.rectangle((x, 10, x + 80, 30), fill=(120, 60, 20))   # type, tinted by the cast
    draw.rectangle((210, 10, 290, 30), fill=(20, 40, 160))        # blue ink
    raw = [_raw("printed", 92, (10, 10, 90, 30), 0), _raw("words", 91, (110, 10, 190, 30), 0),
           _raw("Tirne", 70, (210, 10, 290, 30), 1)]
    words = grade_words(raw, image)
    assert words[0].pen >= 0.5 and words[2].pen >= 0.5
    assert words[2].grade is Grade.DOUBT              # pen would make it unread on black type
    black = image.copy()
    ImageDraw.Draw(black).rectangle((10, 10, 190, 30), fill=(0, 0, 0))
    assert grade_words(raw, black)[2].grade is Grade.UNREAD


def test_a_title_word_tesseract_missed_takes_the_line_of_the_word_after_it():
    # A title's first word, read by no Tesseract word, started a line of its
    # own, so its height alone made it unread, though the confirmed word beside it is as tall.
    from PIL import Image

    from reflow.ingest.ocr_trust import grade_layer
    image = Image.new("RGB", (600, 200), "white")
    body = [_raw(t, 92, (10 + 60 * i, 100, 60 + 60 * i, 120), 2)
            for i, t in enumerate(("the", "body", "text", "here"))]
    read = grade_words([_raw("TITLE", 92, (130, 10, 230, 50), 1), *body], image)
    layer = [("FIRST", (10, 10, 120, 50)), ("TITLE", (130, 10, 230, 50))] + [
        (w.text, w.box) for w in body]
    words = grade_layer(layer, read, image)
    assert words[0].line == words[1].line == 1
    assert [w.grade for w in words[:2]] == [Grade.DOUBT, Grade.READ]


def test_a_run_of_kept_layer_words_tesseract_read_nothing_of_is_one_line():
    from PIL import Image

    from reflow.ingest.ocr_trust import grade_layer
    image = Image.new("RGB", (600, 200), "white")
    read = grade_words([_raw("body", 92, (10, 100, 60, 120), 4)], image)
    words = grade_layer([("AERO.", (10, 10, 90, 40)), ("ASTRO.", (100, 10, 190, 40)),
                         ("body", (10, 100, 60, 120))], read, image)
    assert words[0].line == words[1].line != words[2].line == 4


@pytest.mark.parametrize("kept, read, alike", [
    ("LEVY?", "LEVY2", True), ("body)}", "body)B", True), ('Expenses"', "Expenses\u2122", True),
    ("\ufb02ow", "flow", True),
    ("40", "4", False), ("UtaScore", "Secretafr", False)])
def test_a_tall_layer_word_survives_only_when_tesseracts_unsure_reading_is_alike(kept, read, alike):
    """Tesseract reads the word at 70, short of READ_CONF: readings alike but for one letter or
    their symbols are print set large and stay doubtful; any other reading of a tall word is
    writing, and unread."""
    from PIL import Image

    from reflow.ingest.ocr_trust import grade_layer
    image = Image.new("RGB", (600, 200), "white")
    body = [_raw(t, 92, (10 + 60 * i, 100, 60 + 60 * i, 120), 1)
            for i, t in enumerate(("the", "body", "text", "here"))]
    tall = (300, 70, 400, 130)
    reading = grade_words([*body, _raw(read, 70, tall, 1)], image)
    words = grade_layer([(w.text, w.box) for w in body] + [(kept, tall)], reading, image)
    assert words[-1].tall >= SCRIPT_HEIGHT
    assert words[-1].grade is (Grade.DOUBT if alike else Grade.UNREAD)


def test_a_kept_layer_word_a_confident_reading_contradicts_is_doubtful_in_its_own_right():
    # naca's layer splits its bold head "APPARATUS AND PROCEDURE" as "APPARATU A D
    # PROCEDUR E", which Tesseract reads right at 92 and 91. Two readings that differ, one of them
    # sure, make the word uncertain: marked and reviewed, never navigation. Where Tesseract is
    # unsure too, or read nothing, nothing contradicts the layer, and it stays unconfirmed.
    from PIL import Image

    from reflow.ingest.ocr_trust import grade_layer
    image = Image.new("RGB", (600, 100), "white")
    read = grade_words([_raw("APPARATUS", 92, (10, 10, 120, 30), 1), _raw("AND", 91, (130, 10, 170, 30), 1),
                        _raw("Unit", 16, (200, 10, 240, 30), 1)], image)
    layer = [("APPARATU", (10, 10, 112, 30)), ("A", (130, 10, 142, 30)), ("D", (156, 10, 170, 30)),
             ("Uuit", (200, 10, 240, 30)), ("6'17", (300, 10, 340, 30))]
    words = grade_layer(layer, read, image)
    assert [w.grade for w in words] == [Grade.DOUBT] * 5
    assert [w.unconfirmed for w in words] == [False, False, False, True, True]
    assert [w.contradicted for w in words] == [True, True, True, False, False]
