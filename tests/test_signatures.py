"""signature_label: a pen region is a signature by the short line or ruling it sits on."""
from reflow.analyze.figures import SignatureMatch, signature_label
from reflow.extract.lines import Line
from reflow.extract.ocrwords import GradedWord
from reflow.ingest.ocr_trust import Grade

REGION = (300.0, 200.0, 420.0, 240.0)   # 120 x 40 pt
WIDTH = 612.0


def _line(bbox, text, conf=90):
    grade = Grade.READ if conf >= 80 else Grade.DOUBT
    words = tuple(GradedWord(bbox, w, w, conf, grade, None, 1.0) for w in text.split())
    return Line(1, bbox, text, 10.0, False, False, "GlyphLessFont", ocr=True, grades=words)


def test_a_signature_over_a_read_role_names_it():
    president = _line((320, 252, 380, 264), "President")
    assert signature_label(REGION, [president], [], WIDTH) == SignatureMatch(0, "President")


def test_a_doubtful_role_anchors_the_signature_but_is_not_its_role():
    president = _line((320, 252, 380, 264), "President", conf=78)
    assert signature_label(REGION, [president], [], WIDTH) == SignatureMatch(0, None)


def test_an_ungraded_role_is_not_quoted():
    president = Line(1, (320, 252, 380, 264), "President", 10.0, False, False, "OCR", ocr=True)
    assert signature_label(REGION, [president], [], WIDTH) == SignatureMatch(0, None)


def test_a_large_region_is_no_signature():
    assert signature_label((100.0, 100.0, 500.0, 400.0), [_line((120, 410, 200, 422), "Name")],
                           [], WIDTH) is None


def test_a_region_on_a_ruling_with_no_line_signs_the_ruling():
    ruling = (280.0, 238.0, 460.0, 239.0)
    assert signature_label(REGION, [], [ruling], WIDTH) == SignatureMatch(None, None)


def test_a_signature_hanging_below_its_line_still_signs_it():
    # Budget p4's Secretary: a 56 pt tall region whose line crosses it at 0.64, 19 pt above its foot.
    ruling = (160.0, 317.0, 200.0, 318.0)
    assert signature_label((96.0, 280.0, 224.0, 336.0), [], [ruling], WIDTH) == SignatureMatch(None, None)


def test_a_line_beyond_the_gap_is_no_role():
    assert signature_label(REGION, [_line((320, 290, 380, 302), "President")], [], WIDTH) is None


def test_two_signatures_in_columns_each_sign_their_own_line():
    applicant = _line((80, 252, 160, 264), "Applicant")
    witness = _line((360, 252, 430, 264), "Witness")
    lines = [applicant, witness]
    assert signature_label((60.0, 200.0, 260.0, 240.0), lines, [], WIDTH) == SignatureMatch(0, "Applicant")
    assert signature_label((340.0, 200.0, 540.0, 240.0), lines, [], WIDTH) == SignatureMatch(1, "Witness")


def test_a_line_in_another_column_is_no_role():
    assert signature_label(REGION, [_line((460, 252, 540, 264), "Witness")], [], WIDTH) is None


def test_two_lines_at_nearly_the_same_gap_are_ambiguous():
    lines = [_line((310, 250, 350, 260), "Approved"), _line((360, 252, 410, 262), "Rejected")]
    assert signature_label(REGION, lines, [], WIDTH) is None


def test_a_signature_line_decides_between_two_lines_at_the_same_gap():
    # legislation p1: "On the part of the House" under Meyer's line, OCR read in two pieces.
    lines = [_line((300, 244, 336, 256), "On the"), _line((366, 244, 420, 256), "of the House")]
    ruling = (300.0, 240.0, 500.0, 241.0)
    assert signature_label(REGION, lines, [ruling], WIDTH) == SignatureMatch(None, None)


def _remnant(bbox, text):
    line = _line(bbox, text)
    return Line(1, bbox, text, 10.0, False, False, "GlyphLessFont", ocr=True,
                grades=tuple(GradedWord(w.bbox, w.text, w.text, 90, Grade.READ, None, 1.0,
                                        remnant=True) for w in line.grades))


def test_an_ocr_remnant_of_writing_is_neither_print_on_the_row_nor_a_role():
    # legislation p1: the "h" OCR kept of a handwritten "Leach" lies on Meyer's row.
    ruling = (300.0, 240.0, 500.0, 241.0)
    beside = _remnant((330, 204, 337, 219), "h")
    under = _remnant((310, 246, 380, 260), "—JohnSon")
    assert signature_label(REGION, [beside], [ruling], WIDTH) == SignatureMatch(None, None)
    assert signature_label(REGION, [under], [ruling], WIDTH) == SignatureMatch(None, None)


def test_a_remnant_beside_a_struck_passage_is_print_on_the_row():
    # legislation p37: OCR left out the struck words of "county"'s line, so "county" is a
    # remnant, but of a struck passage, not of writing: print beside the region.
    county = _remnant((432, 204, 474, 218), "county")
    struck = GradedWord((300, 204, 420, 218), "extent—practicable", "", 30, Grade.UNREAD, None,
                        1.0, struck=True)
    ruling = (300.0, 240.0, 500.0, 241.0)
    assert signature_label(REGION, [county], [ruling], WIDTH, [struck, *county.grades]) is None


def test_writing_that_stops_above_an_empty_signature_line_is_no_signature():
    # legislation p1: "Dyson", written under its own line, ends 28 pt above Johnson's.
    ruling = (300.0, 268.0, 500.0, 269.0)
    johnson = _line((310, 270, 380, 282), "Johnson")
    assert signature_label(REGION, [johnson], [ruling], WIDTH) is None
    assert signature_label(REGION, [], [ruling], WIDTH) is None


def test_initials_beside_a_paragraph_are_no_signature():
    paragraph = _line((60, 205, 290, 217), "the tenant agrees to the terms set out above")
    assert signature_label((292.0, 200.0, 322.0, 220.0), [paragraph], [], WIDTH) is None


def test_a_notary_stamp_is_too_tall_for_a_signature():
    notary = _line((120, 310, 220, 322), "Notary Public")
    assert signature_label((100.0, 100.0, 300.0, 300.0), [notary], [], WIDTH) is None


def test_a_long_line_is_no_role():
    sentence = _line((300, 250, 420, 262), "signed in the presence of the clerk")
    assert signature_label(REGION, [sentence], [], WIDTH) is None


def test_writing_on_a_row_of_print_is_no_signature():
    # A struck or unreadable word at the start of a printed line, over a short line ending a
    # paragraph ("or controversy."): writing within print.
    rest = _line((424, 204, 545, 216), "that the claim forms part of")
    tail = _line((300, 252, 400, 264), "or controversy.")
    assert signature_label(REGION, [rest, tail], [], WIDTH) is None
    assert signature_label(REGION, [tail], [], WIDTH) == SignatureMatch(0, "or controversy.")


def test_a_filled_in_date_beside_print_is_no_signature():
    place = _line((400, 196, 470, 208), "Austin, Texas")   # beside the region's top, overlapping it
    date = _line((340, 250, 370, 261), "Date")
    assert signature_label(REGION, [place, date], [], WIDTH) is None


def test_print_above_the_region_does_not_count_as_its_row():
    district = _line((250, 180, 510, 194), "DEER CREEK VILLAS METROPOLITAN DISTRICT")
    president = _line((307, 234, 350, 249), "President")   # its top crosses the lower third
    assert signature_label(REGION, [district, president], [], WIDTH) == SignatureMatch(1, "President")


def test_a_table_rule_is_no_signature_line():
    across = (24.0, 238.0, 590.0, 240.0)       # a rule across the table of a ledger
    cluster = (280.0, 238.0, 460.0, 300.0)     # a cluster of a grid's rules
    assert signature_label(REGION, [], [across], WIDTH) is None
    assert signature_label(REGION, [], [cluster], WIDTH) is None
