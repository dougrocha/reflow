"""OCR corruption stays readable without becoming heading or Contents navigation."""

import pytest

from reflow.analyze.blocks import Block
from reflow.analyze.headings import heading_decision
from reflow.analyze.layout import analyze
from reflow.extract.lines import Line
from reflow.extract.ocrwords import GradedWord
from reflow.extract.pages import PageContent
from reflow.ingest.ocr_trust import Grade
from reflow.model import Role, Source
from reflow.text_quality import fragment_reason, ocr_gate, ocr_text_reason


def line(top, text, *, size=10, ocr=True, unicode_errors=0):
    # An OCR box is as tall as its glyphs: a box of the body's height holds body text, whatever
    # size OCR gave it (see rhythm.snap_ocr_sizes).
    return Line(1, (72, top, 500, top + size), text, size, False, False, "Helvetica",
                ocr=ocr, unicode_errors=unicode_errors)


def classify(text, *, ocr=True, unicode_errors=0, images=()):
    lines = [line(100, text, size=16, ocr=ocr, unicode_errors=unicode_errors)]
    lines += [line(150 + i * 14, "The body text explains the requirements of this document.",
                   ocr=ocr) for i in range(8)]
    page = PageContent(1, 612, 792, lines, list(images))
    nodes = analyze([page])
    return next(n for parent in nodes for n in parent.walk()
                if n.bbox and n.bbox[1] == 100 and n.text)


@pytest.mark.parametrize("text", ["AKey A", "8USINESS NAfvtE", "8USINES3 NAfvtE",
                                 "S |20 /s", "teU stories", "6'17 #i 5"])
def test_ocr_noise_is_a_low_confidence_paragraph(text):
    node = classify(text)
    assert node.role == Role.P
    assert node.source == Source.OCR
    assert node.confidence < 0.4
    assert "OCR" in " ".join(node.attrs["why"])


@pytest.mark.parametrize("text", ["Annual Report", "BUSINESS NAME", "2025 Tax Changes",
                                 "Form W-9", "3D Printing", "École primaire", "日本語"])
def test_clean_ocr_can_still_be_a_heading(text):
    assert classify(text).role == Role.H2


def test_a_noisy_line_in_a_wrapped_ocr_heading_prevents_promotion():
    block = Block([line(100, "Annual Report", size=16),
                   line(112, "teU stories", size=16)])
    assert heading_decision(block, 10, [block])[0] is None


def test_unmapped_ocr_characters_prevent_heading_promotion():
    assert classify("Annual Report", unicode_errors=1).role == Role.P


def test_ocr_noise_is_not_promoted_to_a_contents_entry():
    node = classify("teU stories .......... 12")
    assert node.role == Role.P
    assert node.attrs.get("contents") is None
    assert node.confidence < 0.4


def test_clean_ocr_contents_entry_is_preserved():
    node = classify("Annual Report .......... 12")
    assert node.role == Role.LI
    assert node.text == "Annual Report"
    assert node.attrs["contents"] is True


def test_born_digital_mixed_case_headings_are_unchanged():
    assert classify("LaTeX and OpenType", ocr=False).role == Role.H2


def test_visible_ocr_on_a_page_sized_scan_is_checked():
    node = classify("Discover objects that teU stories", ocr=False,
                    images=[(0, 0, 612, 792)])
    assert node.role == Role.P
    assert node.source == Source.OCR
    assert node.confidence < 0.4


def test_a_small_image_does_not_make_born_digital_text_ocr():
    assert classify("LaTeX and OpenType", ocr=False,
                    images=[(0, 0, 100, 100)]).role == Role.H2


# Graded OCR: the words of each line carry Tesseract's grade.


def graded(text, *grades):
    """`text`'s words graded, in order (READ where `grades` runs out)."""
    words = text.split()
    marks = list(grades) + [Grade.READ] * (len(words) - len(grades))
    return tuple(GradedWord((72 + 40 * i, 100, 100 + 40 * i, 116), w, w, 90 if g is Grade.READ
                            else 60, g, None, 1.0) for i, (w, g) in enumerate(zip(words, marks)))


def classify_graded(text, *grades, size=16, ocr_lines=True):
    lines = [line(100, text, size=size)]
    lines[0].grades = graded(text, *grades)
    for i in range(8):
        body = line(150 + i * 14, "The body text explains the requirements of this document.")
        body.grades = graded(body.text)
        lines.append(body)
    nodes = analyze([PageContent(1, 612, 792, lines, [])])
    return next(n for parent in nodes for n in parent.walk()
                if n.bbox and n.bbox[1] == 100 and n.text)


@pytest.mark.parametrize("size", [16, 40])
def test_a_graded_line_with_a_doubtful_word_is_no_heading_whatever_its_size(size):
    node = classify_graded("Annual King Report", Grade.READ, Grade.DOUBT, size=size)
    assert node.role == Role.P
    assert "OCR is unsure of the word ‘King’" in " ".join(node.attrs["why"])
    assert node.confidence > 0.4      # a doubtful word is not noise: the block keeps its weight


def test_a_graded_line_without_doubt_may_be_a_heading():
    assert classify_graded("NATIONAL ADVISORY COMMITTEE").role == Role.H2
    # Its words were read confidently: the lexical gate is for ungraded text only.
    assert classify_graded("3Ww. Report").role == Role.H2


def test_a_doubtful_word_keeps_a_contents_entry_out():
    node = classify_graded("Annual Report .......... 12", Grade.DOUBT, size=10)
    assert node.attrs.get("contents") is None


def test_ocr_text_the_gate_refuses_is_no_contents_entry():
    # Every word READ, but a piece of a sentence: ocr_gate, not only the doubt grade, decides.
    node = classify_graded("and more .......... 12", size=10)
    assert node.attrs.get("contents") is None
    assert classify_graded("Annual Report .......... 12", size=10).attrs.get("contents") is True


@pytest.mark.parametrize("text", ["s S", "b LR SR S", "A el el e", "I P e e S"])
def test_ungraded_fragments_fail_the_lexical_gate(text):
    assert fragment_reason(text) == "fragmentary OCR text"
    assert ocr_text_reason(text) is not None
    assert classify(text).role == Role.P


@pytest.mark.parametrize("text", ["Part A", "IV. B", "U.S. EPA", "Annual Report"])
def test_short_printed_tokens_are_not_fragments(text):
    assert fragment_reason(text) is None


def test_the_gate_reads_each_line_by_its_grading():
    graded_line = line(100, "El B o", size=40)
    graded_line.grades = graded("El B o")
    assert ocr_gate([graded_line]) is None                 # READ words, whatever they spell
    ungraded = line(100, "El B o", size=40)
    assert ocr_gate([ungraded]) == "fragmentary OCR text"
    # "King" passes every lexical rule: only its grade can refuse it.
    assert ocr_gate([line(100, "El King", size=40)]) is None
    doubted = line(100, "El King", size=40)
    doubted.grades = graded("El King", Grade.READ, Grade.DOUBT)
    assert ocr_gate([doubted]) == "OCR is unsure of the word ‘King’"
    assert ocr_gate([line(100, "s S", ocr=False)]) is None  # born-digital


@pytest.mark.parametrize("text", ["and more...", "noted.", "ree D", "r--- I", "—el King"])
def test_ocr_text_that_starts_in_lowercase_is_never_navigation(text):
    graded_line = line(100, text, size=40)
    graded_line.grades = graded(text)
    assert ocr_gate([graded_line]) == "OCR text starts in lowercase: a piece of a sentence"
    assert ocr_gate([line(100, text, size=40)]) is not None
    assert ocr_gate([line(100, text, ocr=False)]) is None   # born-digital: not this gate's call


def test_a_second_line_may_go_on_in_lowercase():
    first, second = line(100, "EFFECT OF FORCE", size=16), line(118, "of a beam", size=16)
    first.grades, second.grades = graded("EFFECT OF FORCE"), graded("of a beam")
    assert ocr_gate([first, second]) is None


def test_a_word_left_alone_on_an_unreadable_line_is_never_navigation():
    from dataclasses import replace
    remnant = line(100, "—JohnSon", size=40)
    remnant.grades = tuple(replace(w, remnant=True) for w in graded("—JohnSon"))
    assert ocr_gate([remnant]) == "most of its OCR line could not be read"


def test_a_line_mostly_struck_through_is_no_heading_on_any_page():
    """Struck text is never navigation, whether or not its page passed the tracked-change gate
    (the gate only decides whether the words are marked as deleted)."""
    from dataclasses import replace
    text = "The division is abolished September"
    lines = [line(100, text, size=16)]
    lines[0].grades = tuple(replace(w, struck=i < 3) for i, w in enumerate(graded(text)))
    assert ocr_gate(lines) == "most of its OCR words are struck through"
    for i in range(8):
        body = line(150 + i * 14, "The body text explains the requirements of this document.")
        body.grades = graded(body.text)
        lines.append(body)
    nodes = analyze([PageContent(1, 612, 792, lines, [])])
    node = next(n for parent in nodes for n in parent.walk()
                if n.bbox and n.bbox[1] == 100 and n.text)
    assert node.role == Role.P


def test_a_kept_layers_unconfirmed_words_send_their_line_to_the_lexical_gate():
    from dataclasses import replace
    words = graded("BUILDING PERMIT APPLICATIONS", Grade.READ, Grade.READ, Grade.DOUBT)
    kept = line(100, "BUILDING PERMIT APPLICATIONS", size=40)
    kept.grades = words[:2] + (replace(words[2], unconfirmed=True),)
    assert ocr_gate([kept]) is None                       # it reads as words: navigation
    junk = line(100, "El B o", size=40)
    junk.grades = tuple(replace(w, grade=Grade.DOUBT, unconfirmed=True) for w in graded("El B o"))
    assert ocr_gate([junk]) == "fragmentary OCR text"
    unsure = line(100, "BUILDING PERMIT APPLICATIONS", size=40)
    unsure.grades = words                                 # Tesseract unsure of its own reading
    assert ocr_gate([unsure]) == "OCR is unsure of the word ‘APPLICATIONS’"
