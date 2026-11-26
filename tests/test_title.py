"""The title is chosen from evidence: the naca case, agreement, generic metadata, Docling."""

import pytest

import checkout
from reflow.analyze.title import Candidate, candidates, choose_title, score
from reflow.model import Node, Role, Source
from testkit import heading


def test_naca_garbage_heading_loses_to_the_filename_and_is_suspect():
    choice = choose_title([heading("6'17 #i 5")], None, None, "naca-ocr")
    assert choice.text == "naca-ocr"
    assert choice.origin == "filename"
    assert [f.code for f in choice.facts] == ["title-suspect"]
    assert choice.facts[0].source == Source.HEURISTIC
    (loser, reason), = choice.rejected
    assert loser.origin == "heading" and "letter share" in reason


def test_meta_title_agreeing_with_the_top_heading_wins_without_a_fact():
    nodes = [heading("Annual Report", 18.0), heading("Introduction", 12.0)]
    choice = choose_title(nodes, "Annual Report", None, "report-2024")
    assert choice.text == "Annual Report"
    assert choice.origin == "meta"
    assert choice.score == pytest.approx(0.9)
    assert choice.facts == []


@pytest.mark.parametrize("generic", ["Untitled", "  untitled document ", "Document1",
                                     "document", "PDF", "pdf",
                                     "Microsoft Word - memo.docx"])
def test_generic_meta_title_loses_to_the_heading(generic):
    nodes = [heading("Quarterly Results", 18.0)]
    choice = choose_title(nodes, generic, None, "report-2024")
    assert choice.text == "Quarterly Results"
    assert choice.origin == "heading"
    assert choice.facts == []
    assert any(c.origin == "meta" and "generic" in r for c, r in choice.rejected)


@pytest.mark.parametrize("real", ["Document Management Guide", "PDF Accessibility Primer",
                                  "Documents of the Senate", "Untitled Works by Mark Twain"])
def test_real_titles_starting_with_a_generic_word_are_kept(real):
    assert score(Candidate(real, "meta"), []) == (0.6, None)
    choice = choose_title([heading("Something Else", 18.0)], real, None, "report-2024")
    assert choice.text == real
    assert choice.origin == "meta"


def test_docling_h2_wins_over_a_smaller_heading():
    docling = Node(Role.H2, page=1, text="Real Title", attrs={"font_size": 1000.0},
                   source=Source.DOCLING)
    nodes = [docling, heading("Smaller Heading", 12.0)]
    choice = choose_title(nodes, None, None, "file")
    assert choice.text == "Real Title"
    assert choice.origin == "docling"
    assert choice.facts == []
    assert any(c.text == "Smaller Heading" and "lost" in r for c, r in choice.rejected)


def test_unicode_errors_reject_a_heading():
    nodes = [heading("Gl�ssary of terms", 18.0, unicode_errors=2)]
    choice = choose_title(nodes, None, None, "glossary")
    assert choice.origin == "filename"
    assert any(c.origin == "heading" and "unicode" in r for c, r in choice.rejected)


def test_word_count_outside_the_range_halves_the_score():
    long_title = " ".join(["word"] * 31)
    assert score(Candidate(long_title, "heading"), []) == (0.25, None)


def test_candidates_come_in_preference_order():
    nodes = [heading("Heading Text", 14.0)]
    origins = [c.origin for c in candidates(nodes, "Meta Title", "Bookmark", "stem")]
    assert origins == ["meta", "bookmark", "heading", "filename"]


def test_no_usable_candidate_falls_back_to_the_filename_with_a_fact():
    choice = choose_title([], None, None, "6'17 #i 5")
    assert choice.text == "6'17 #i 5"
    assert choice.origin == "filename"
    assert [f.code for f in choice.facts] == ["title-suspect"]


def test_choose_title_does_not_touch_the_nodes():
    """The chooser is pure: promoting the winner to H1 is evidence.witness' job."""
    nodes = [heading("Quarterly Results", 18.0), heading("Scope", 12.0, role=Role.H3)]
    choice = choose_title(nodes, None, None, "report-2024")
    assert choice.origin == "heading"
    assert [n.role for n in nodes] == [Role.H2, Role.H3]


def test_bookmark_on_page_one_beats_a_smaller_heading():
    choice = choose_title([heading("Section 1", 12.0)], None, "Field Guide", "guide")
    assert (choice.text, choice.origin) == ("Field Guide", "bookmark")
    assert choice.facts == []


def test_title_punctuation_does_not_count_against_the_letter_share():
    form = Candidate(text="Form W-9 (Rev. March 2024)", origin="meta")
    assert score(form, [])[1] is None
    garbage = Candidate(text="6'17 #i 5", origin="heading")
    assert score(garbage, [])[0] == 0.0


@pytest.mark.parametrize("fragment", ["el", "I", "a."])
def test_short_ocr_title_falls_back_to_the_next_good_heading(fragment):
    noise = heading(fragment, 30)
    noise.source = Source.OCR
    choice = choose_title([noise, heading("Legislative amendments", 14, page=3)],
                          None, None, "legislation")
    assert (choice.text, choice.origin) == ("Legislative amendments", "heading")
    assert any(c.text == fragment and ("fewer than 3 letters" in reason
                                       or "starts in lowercase" in reason)
               for c, reason in choice.rejected)


def test_short_ocr_title_without_a_good_heading_falls_back_to_the_filename():
    noise = heading("el", 30)
    noise.source = Source.OCR
    choice = choose_title([noise], None, None, "legislation")
    assert (choice.text, choice.origin) == ("legislation", "filename")
    assert [f.code for f in choice.facts] == ["title-suspect"]


@pytest.mark.parametrize("text", ["AKey A", "8USINESS NAfvtE", "teU stories"])
def test_ocr_word_shape_noise_cannot_be_the_title(text):
    noise = heading(text, 30)
    noise.source = Source.OCR
    choice = choose_title([noise, heading("Annual Report", 14)], None, None, "file")
    assert (choice.text, choice.origin) == ("Annual Report", "heading")


def test_low_confidence_heading_cannot_be_the_title():
    noise = heading("Recognized words", 30)
    noise.source = Source.OCR
    noise.confidence = 0.35
    choice = choose_title([noise, heading("Annual Report", 14)], None, None, "file")
    assert choice.text == "Annual Report"
    assert any(c.text == noise.text and reason == "low-confidence heading"
               for c, reason in choice.rejected)


def test_bad_top_heading_does_not_hide_a_good_heading_on_the_same_page():
    choice = choose_title([heading("6'17 #i 5", 30), heading("Annual Report", 14)],
                          None, None, "file")
    assert choice.text == "Annual Report"


def test_a_short_born_digital_title_is_preserved():
    assert choose_title([heading("AI")], None, None, "file").text == "AI"
    assert choose_title([], "AI", None, "file").text == "AI"


def test_corrupt_docling_title_falls_back_to_a_good_heading():
    docling = heading("el", 1000)
    docling.source = Source.OCR
    choice = choose_title([docling, heading("Annual Report", 14)], None, None, "file")
    assert (choice.text, choice.origin) == ("Annual Report", "heading")


def test_repeated_secondary_headings_are_not_independent_title_agreement():
    nodes = [heading("Annual Report", 20), heading("Report summary", 14, page=2),
             heading("Report summary", 14, page=3)]
    choice = choose_title(nodes, None, None, "file")
    assert (choice.text, choice.origin) == ("Annual Report", "heading")


@pytest.mark.parametrize("stamp", ["Final Employee Booklet 04102026",
                                   "Exhibit 131 Medical Remittance Advice_92723"])
def test_file_name_meta_title_loses_to_the_heading(stamp):
    choice = choose_title([heading("Open Enrollment 2026", 28.0)], stamp, None, "booklet")
    assert choice.text == "Open Enrollment 2026"
    assert choice.origin == "heading"
    assert any(c.origin == "meta" and "file name" in r for c, r in choice.rejected)


@pytest.mark.parametrize("real", ["Form 1040 Instructions", "CMS-10780 Notice and Consent",
                                  "Form W-9 (Rev. March 2024)"])
def test_meta_titles_with_short_form_numbers_are_kept(real):
    assert score(Candidate(real, "meta"), []) == (0.6, None)


def paragraph(text, page=1):
    return Node(Role.P, page=page, text=text)


def test_title_set_over_two_sizes_is_joined_when_the_first_line_runs_on():
    nodes = [Node(Role.ARTIFACT, page=1, text="/"),
             heading("Prior Authorization Request for", 21.0),
             heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1),
             heading("Optional Information", 13.0)]
    choice = choose_title(nodes, None, None, "prior-auth")
    assert choice.text == "Prior Authorization Request for Medications, DME, and Medical Devices"
    assert choice.lead == "Prior Authorization Request for"


def test_title_continuing_in_lowercase_is_joined_to_the_paragraph_above():
    nodes = [Node(Role.FIGURE, page=1), paragraph("Protect your quality of life by"),
             heading("protecting your eyes.", 30.0, role=Role.H1)]
    choice = choose_title(nodes, None, None, "vision")
    assert choice.text == "Protect your quality of life by protecting your eyes."


@pytest.mark.parametrize("above", ["Plan administered by Blue Cross.",   # a closed sentence
                                   "Welcome to the plan year",           # no run-on word
                                   "Something on page two"])
def test_line_above_is_not_joined_without_a_run_on(above):
    nodes = [paragraph(above, page=2 if "two" in above else 1),
             heading("Medications and Devices", 27.0, role=Role.H1)]
    assert choose_title(nodes, None, None, "file").text == "Medications and Devices"


def test_title_apply_merges_the_joined_line_into_the_heading():
    from reflow.analyze.evidence import title_apply
    lead = heading("Prior Authorization Request for", 21.0)
    top = heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1)
    nodes = [lead, top, heading("Optional Information", 13.0)]
    choice = choose_title(nodes, None, None, "prior-auth")
    assert title_apply(nodes, choice) is top
    assert top.role == Role.H1 and top.text == choice.text
    assert lead not in nodes


def test_joined_title_keeps_the_marks_spans_facts_and_box_of_both_lines():
    from reflow.analyze.evidence import title_apply
    from reflow.model import Mark, Span
    lead = heading("Prior Authorization Request for", 21.0)
    lead.bbox = (72.0, 40.0, 300.0, 60.0)
    lead.marks = [Mark(0, 19, "strong", "Prior Authorization")]
    lead.spans = [Span(20, 27, "link", "https://example.org/request")]
    lead.fact("link", Source.TAGS, "from the PDF")
    top = heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1)
    top.bbox = (72.0, 62.0, 500.0, 96.0)
    top.marks = [Mark(13, 16, "ins", "DME")]
    top.spans = [Span(0, 11, "link", "https://example.org/meds")]
    nodes = [lead, top, heading("Optional Information", 13.0)]
    choice = choose_title(nodes, None, None, "prior-auth")

    assert title_apply(nodes, choice) is top

    shift = len("Prior Authorization Request for ")
    assert top.text == "Prior Authorization Request for Medications, DME, and Medical Devices"
    # The lead's emphasis goes with its paragraph role: a heading carries no strong or em.
    assert top.marks == [Mark(shift + 13, shift + 16, "ins", "DME")]
    assert [top.text[s.start:s.end] for s in top.spans] == ["Request", "Medications"]
    assert top.spans[1].start == shift
    assert [f.code for f in top.facts] == ["link"]
    assert "ocr-mark-lost" not in [f.code for f in top.facts]
    assert top.bbox == (72.0, 40.0, 500.0, 96.0)
    assert lead not in nodes

# --- The page's own title ----------------------------------------------------------------

from reflow.analyze.title import clean_meta, cover_candidates  # noqa: E402
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent


def line(text, top, size, *, page=1, x0=100.0, x1=500.0, ocr=False, errors=0):
    return Line(page, (x0, top, x1, top + size), text, size, False, False, "F",
                unicode_errors=errors, ocr=ocr)


def body(page=1, n=12):
    return [line("Body text of the page runs on in the ordinary size.", 300 + 14 * i, 8, page=page)
            for i in range(n)]


def page(number, lines, width=612.0, height=792.0):
    return PageContent(number, width, height, lines, [])


@pytest.mark.parametrize("raw, cleaned", [
    ("Microsoft Word - Kitsap 911 - Computer Aided Dispatch - Request for Proposal.docx",
     "Kitsap 911 - Computer Aided Dispatch - Request for Proposal"),
    ("Form W-9: Request for Taxpayer Identification Number",
     "Form W-9: Request for Taxpayer Identification Number"),
    ("Microsoft PowerPoint - Quarterly Review.pptx", "Quarterly Review"),
    ("Microsoft Word - Document1", "Document1"),
    ("Microsoft Word - memo.docx", ""),
    ("- ACFR Cover FY 22-23] (SMCMVCD 2023 Audit  [6/30/2023] (In Process))", None),
    ("Budget (draft", None),
])
def test_clean_meta(raw, cleaned):
    assert clean_meta(raw) == cleaned


def test_kitsap_metadata_title_is_published_over_the_section_heading():
    nodes = [heading("Section 1 – Project Description", 16.0)]
    meta = "Microsoft Word - Kitsap 911 - Computer Aided Dispatch - Request for Proposal.docx"
    choice = choose_title(nodes, meta, None, "rfp")
    assert (choice.text, choice.origin) == (
        "Kitsap 911 - Computer Aided Dispatch - Request for Proposal", "meta")


def test_tool_prefixed_working_file_name_is_still_rejected_after_cleaning():
    meta = "Microsoft Word - Exhibit 131 Medical Remittance Advice_92723.docx"
    choice = choose_title([heading("Remittance Advice", 20.0)], meta, None, "exhibit")
    assert choice.text == "Remittance Advice"
    assert any(c.origin == "meta" and "file name" in r for c, r in choice.rejected)


def test_metadata_fragment_loses_to_the_heading():
    meta = "- ACFR Cover FY 22-23] (SMCMVCD 2023 Audit  [6/30/2023] (In Process))"
    choice = choose_title([heading("Annual Report", 20.0)], meta, None, "audit")
    assert choice.text == "Annual Report"
    assert any(c.origin == "meta" and "fragment" in r for c, r in choice.rejected)


def test_cover_joins_the_kicker_above_a_display_title():
    p = page(1, [line("KILLEEN INDEPENDENT SCHOOL DISTRICT", 60, 37.4),
                 line("2024-2025 DISTRICT CALENDAR", 104, 50.7), *body()])
    cover, = cover_candidates([p])
    assert cover.text == "KILLEEN INDEPENDENT SCHOOL DISTRICT 2024-2025 DISTRICT CALENDAR"
    assert cover.origin == "cover" and cover.page == 1 and not cover.ocr
    assert [l.text for l in cover.lines] == ["KILLEEN INDEPENDENT SCHOOL DISTRICT",
                                             "2024-2025 DISTRICT CALENDAR"]
    assert cover.lines[1].bbox == (100.0, 104, 500.0, 104 + 50.7)


def test_cover_joining_ends_when_a_small_kicker_sits_over_a_much_larger_title():
    p = page(1, [line("A SMALL KICKER", 40, 14), line("THE LARGE TITLE", 54, 60), *body()])
    cover, = cover_candidates([p])
    assert cover.text == "A SMALL KICKER THE LARGE TITLE"


def test_cover_leaves_a_label_in_the_corner_out_of_the_title():
    # az-accident-insurance-flyer: "EMPLOYER" is set at the right edge, over the end of the
    # headline's first line, and shares no edge or centre with it.
    p = page(1, [line("EMPLOYER", 97, 17, x0=503, x1=582),
                 line("How accident", 104, 47, x0=35, x1=289),
                 line("insurance can help enhance", 145, 47, x0=35, x1=548), *body()])
    cover, = cover_candidates([p])
    assert cover.text == "How accident insurance can help enhance"


@pytest.mark.parametrize("label", ["REMEMBER", "NOTE", "TIP:", "REMINDER"])
def test_a_callout_label_is_never_the_title(label):
    # az-claim-form-instruction-booklet: "REMEMBER" over a tinted box on page 2.
    choice = choose_title([heading(label, 16.4, page=2, role=Role.H1)], None, None, "booklet")
    assert (choice.text, choice.origin) == ("booklet", "filename")
    (loser, reason), = [r for r in choice.rejected if r[0].origin == "heading"]
    assert "callout label" in reason


def test_a_heading_far_past_the_first_is_not_a_title_candidate():
    nodes = [heading("LINE 1a", 12, page=3), heading("QUESTIONS?", 30, page=8)]
    choice = choose_title(nodes, None, None, "booklet")
    assert all(c.text != "QUESTIONS?" for c, _ in choice.rejected)
    assert choice.text != "QUESTIONS?"


def test_cover_keeps_the_subtitle_out_of_the_title():
    p = page(1, [line("Annual Comprehensive Financial Report", 100, 30),
                 line("For the Fiscal Year Ended June 30, 2023", 138, 24), *body()])
    cover, = cover_candidates([p])
    assert cover.text == "Annual Comprehensive Financial Report"
    assert cover.subtitle is not None and cover.subtitle.text.startswith("For the Fiscal Year")


def test_cover_joins_a_wrapped_title_line_of_the_same_size():
    p = page(1, [line("Annual Comprehensive", 100, 30), line("Financial Report", 132, 30), *body()])
    cover, = cover_candidates([p])
    assert cover.text == "Annual Comprehensive Financial Report"


def test_cover_takes_the_larger_title_of_page_two_over_ocr_junk_on_page_one():
    p1 = page(1, [line("m0uining, and m0re...", 80, 30, ocr=True), *body()])
    p2 = page(2, [line("RATTLESNAKE", 500, 11.3, page=2),
                  line("OLD AUBURN CEMETERY TOUR", 60, 21.6, page=2), *body(2)])
    cover, = cover_candidates([p1, p2])
    assert (cover.text, cover.page) == ("OLD AUBURN CEMETERY TOUR", 2)


def test_cover_reads_centred_capitals_when_nothing_is_display_size():
    p = page(1, [line("BUILDING PERMIT APPLICATIONS", 40, 12, x0=300, x1=712, ocr=True),
                 *[line("Fill in every field of the form.", 100 + 14 * i, 12) for i in range(10)]],
             width=1008.0)
    cover, = cover_candidates([p])
    assert cover.text == "BUILDING PERMIT APPLICATIONS" and cover.ocr


def test_cover_needs_a_centred_all_capitals_head():
    left = page(1, [line("BUILDING PERMIT APPLICATIONS", 40, 12, x0=20, x1=300),
                    *[line("Fill in every field.", 100 + 14 * i, 12) for i in range(10)]])
    mixed = page(1, [line("Building Permit Applications", 40, 12, x0=100, x1=512),
                     *[line("Fill in every field.", 100 + 14 * i, 12) for i in range(10)]])
    assert cover_candidates([left]) == [] and cover_candidates([mixed]) == []


def test_cover_finds_nothing_when_the_big_lines_are_handwriting_noise():
    p = page(1, [line("6'17 #i 5", 60, 30, ocr=True), line("Colligative Properties", 200, 8),
                 *body()])
    assert cover_candidates([p]) == []


def test_an_ocr_kicker_that_fails_the_gate_rejects_the_whole_joined_title():
    p = page(1, [line("KILLEEN INDEPENDENT SCH00L DISTRICT", 60, 37.4, ocr=True),
                 line("2024-2025 DISTRICT CALENDAR", 104, 50.7, ocr=True), *body()])
    cover, = cover_candidates([p])
    assert cover.doubtful == "digits inside OCR words"
    choice = choose_title([], None, None, "calendar", [p])
    assert choice.origin == "filename"
    assert any(c.origin == "cover" and r == "digits inside OCR words" for c, r in choice.rejected)


def test_cover_title_wins_on_a_document_with_no_headings():
    p = page(1, [line("Annual Comprehensive Financial Report", 100, 30), *body()])
    choice = choose_title([], None, None, "audit", [p])
    assert (choice.text, choice.origin) == ("Annual Comprehensive Financial Report", "cover")
    assert choice.facts == []
    assert [l.text for l in choice.lines] == ["Annual Comprehensive Financial Report"]
    assert choice.nodes == ()   # nothing to promote


def test_the_page_spelling_replaces_a_metadata_typo():
    p = page(1, [line("CICP Provider Directory", 100, 20), *body()])
    choice = choose_title([], "CICP Provider Director January 2024", None, "dir", [p])
    assert (choice.text, choice.origin) == ("CICP Provider Directory", "cover")
    assert any(c.origin == "meta" and r == "the page prints it as 'CICP Provider Directory'"
               for c, r in choice.rejected)


def boxed(text, top, bottom, size, *, x0=36.0, x1=400.0, page=1):
    """A line whose box is not its size tall: a display face with a tall box, or a tight leading."""
    return Line(page, (x0, top, x1, bottom), text, size, False, False, "F")


@pytest.mark.parametrize("name", ["Cover", "Table of Contents", "CONTENTS", "Front Cover"])
def test_a_bookmark_that_names_a_part_of_the_document_is_not_the_title(name):
    p = page(1, [line("The BlueCard Program Provider Guide", 100, 40), *body()])
    choice = choose_title([heading(name, 22.0, page=2)], None, name, "guide", [p])
    assert (choice.text, choice.origin) == ("The BlueCard Program Provider Guide", "cover")
    assert all(r.startswith("the name of a part") for c, r in choice.rejected if c.text == name)


def test_a_web_address_is_not_the_title():
    nodes = [heading("azblue.com/bluedentalprovider", 20.0), heading("Provider Manual", 18.0)]
    assert choose_title(nodes, None, None, "manual").text == "Provider Manual"


def test_cover_joins_the_lines_of_a_title_set_with_a_tight_leading():
    texts = ["Protect your", "quality of life by", "protecting", "your eyes."]
    lines = [boxed(t, 97.2 + 30 * i, 137.0 + 30 * i, 30) for i, t in enumerate(texts)]
    cover, = cover_candidates([page(1, [*lines, *body()])])
    assert cover.text == "Protect your quality of life by protecting your eyes."


def test_cover_joins_a_year_above_and_the_wrapped_line_below():
    p = page(1, [boxed("2026", 195.8, 276.6, 59.4), boxed("Member", 258.4, 322.1, 48),
                 boxed("Guide", 307.4, 371.1, 48), *body()])
    cover, = cover_candidates([p])
    assert cover.text == "2026 Member Guide" and cover.suspect is None


@pytest.mark.parametrize("lines, title", [
    # The line above runs on into the smaller line under it.
    ([boxed("INTRODUCING A NEW OPTION:", 119.4, 160.0, 34),
      boxed("Blue High Performance Network (BlueHPN)", 158.7, 195.1, 30)],
     "INTRODUCING A NEW OPTION: Blue High Performance Network (BlueHPN)"),
    # The smaller line runs on itself, under a kicker set larger.
    ([boxed("MEDICARE ADVANTAGE", 27.0, 64.1, 28), boxed("Appointment of", 57.2, 89.0, 24),
      boxed("Representative Instructions", 83.2, 115.0, 24)],
     "MEDICARE ADVANTAGE Appointment of Representative Instructions"),
    # The line above is only a section label, right-aligned with its name.
    ([boxed("Section 19", 109.3, 160.9, 36, x0=364.1, x1=544.1),
      boxed("Claim Submission", 158.6, 183.2, 18, x0=401.0, x1=545.0)],
     "Section 19 Claim Submission"),
])
def test_cover_joins_a_smaller_line_when_the_text_shows_the_title_goes_on(lines, title):
    cover, = cover_candidates([page(1, [*lines, *body()])])
    assert cover.text == title


def test_cover_keeps_a_touching_subtitle_out_of_the_title():
    p = page(1, [boxed("Annual Comprehensive Financial Report", 201.1, 237.7, 30, x0=19.7,
                       x1=592.3),
                 boxed("For the Fiscal Year Ended June 30th, 2023", 235.8, 265.1, 24, x0=69.4,
                       x1=542.6), *body()])
    cover, = cover_candidates([p])
    assert cover.text == "Annual Comprehensive Financial Report"


def test_a_title_joined_over_very_different_sizes_goes_to_review():
    p = page(1, [boxed("HELLO", 64.4, 210.2, 111.6, x0=0, x1=340),
                 boxed("TALK TO A NURSE ANYTIME, AND", 207.1, 234.5, 21, x0=36, x1=309),
                 boxed("DAY OR NIGHT", 225.6, 264.8, 30, x0=36, x1=239), *body()])
    choice = choose_title([], None, None, "flyer", [p])
    assert choice.text == "HELLO TALK TO A NURSE ANYTIME, AND DAY OR NIGHT"
    assert [f.code for f in choice.facts] == ["title-suspect"]
    assert "sizes from 21 to 111.6" in choice.facts[0].detail


def test_a_one_word_title_goes_to_review():
    p = page(1, [boxed("HELLO", 64.4, 210.2, 111.6, x0=0, x1=340), *body()])
    choice = choose_title([], None, None, "flyer", [p])
    assert choice.text == "HELLO" and [f.code for f in choice.facts] == ["title-suspect"]


def test_the_cover_replaces_a_bookmark_it_prints_in_full():
    p = page(1, [boxed("Section 19", 109.3, 160.9, 36, x0=364.1, x1=544.1),
                 boxed("Claim Submission", 158.6, 183.2, 18, x0=401.0, x1=545.0), *body()])
    choice = choose_title([], None, "19 Claim Submission", "guide", [p])
    assert (choice.text, choice.origin) == ("Section 19 Claim Submission", "cover")
    assert choice.facts == []


def test_a_stale_metadata_title_gives_way_to_the_line_it_names():
    p = page(1, [line("BLUE CROSS AND BLUE SHIELD OF ARIZONA, INC.", 35, 11, x0=168, x1=444),
                 line("AUDIT, COMPLIANCE, AND RISK COMMITTEE CHARTER", 47, 11, x0=156, x1=456),
                 line("MARCH 2026", 60, 11, x0=272, x1=340), *body()])
    choice = choose_title([], "Audit Committee Charter 2015", None, "charter", [p])
    assert choice.origin == "cover"
    assert choice.text == "AUDIT, COMPLIANCE, AND RISK COMMITTEE CHARTER"
    assert any(c.origin == "meta" and "names 2015" in r for c, r in choice.rejected)


def test_a_metadata_title_whose_year_the_cover_prints_stays():
    p = page(1, [line("AUDIT COMMITTEE CHARTER 2015", 35, 11, x0=156, x1=456), *body()])
    choice = choose_title([], "Audit Committee Charter 2015", None, "charter", [p])
    assert choice.origin == "meta"


def test_display_lines_set_out_of_reading_order_go_to_review_not_into_the_title():
    tangled = boxed("2026 COVERAGE", 436.2, 567.6, 35, x0=48.1, x1=487.3)
    inner = boxed("OUTLINE OF", 461.8, 508.3, 35, x0=300.4, x1=504.7)
    p = page(1, [tangled, inner, *body()])
    nodes = [heading("2026 COVERAGE OUTLINE OF", 35.0), heading("Medicare Supplement", 22.0)]
    nodes[0].bbox = (48.1, 436.2, 504.7, 567.6)
    choice = choose_title(nodes, None, None, "outline", [p])
    assert choice.text == "Medicare Supplement"
    assert [f.code for f in choice.facts] == ["title-suspect"]
    assert "out of reading order" in choice.facts[0].detail
    assert any(c.text == "2026 COVERAGE OUTLINE OF" and "out of reading order" in r
               for c, r in choice.rejected)


def test_a_shorter_printed_title_does_not_override_a_longer_metadata_title():
    p = page(1, [line("Form W-9", 100, 20), *body()])
    choice = choose_title([], "Form W-9 Request for Taxpayer Identification Number", None,
                          "fw9", [p])
    assert choice.origin == "meta"


def test_section_headings_lose_to_cleaner_candidates():
    nodes = [heading("Section 1 – Project Description", 16.0)]
    assert score(Candidate(nodes[0].text, "heading"), []) == (0.25, None)
    assert score(Candidate("5 Administrative Requirements", "heading"), []) == (0.25, None)
    assert score(Candidate("2025 Publication 17", "heading"), []) == (0.5, None)


def test_a_title_read_by_ocr_is_flagged():
    noise_free = heading("Annual Report", 20.0)
    noise_free.source = Source.OCR
    choice = choose_title([noise_free], None, None, "scan")
    assert choice.text == "Annual Report"
    (fact,) = choice.facts
    assert fact.code == "title-suspect" and "OCR" in fact.detail


def test_a_joined_title_whose_only_ocr_part_is_the_lead_is_an_ocr_title():
    lead = heading("Prior Authorization Request for", 21.0)
    lead.source = Source.OCR
    top = heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1)
    choice = choose_title([lead, top], None, None, "prior-auth")
    assert choice.text == "Prior Authorization Request for Medications, DME, and Medical Devices"
    assert [f.code for f in choice.facts] == ["title-suspect"]


def test_a_gated_lead_is_not_joined_and_stays_a_paragraph():
    from reflow.analyze.evidence import title_apply
    lead = paragraph("Prior Authorizat1on Request for")
    lead.source = Source.OCR
    top = heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1)
    nodes = [lead, top, heading("Optional Information", 13.0)]
    choice = choose_title(nodes, None, None, "prior-auth")
    assert choice.text == "Medications, DME, and Medical Devices"
    assert choice.lead is None
    assert any(c.lead and r == "digits inside OCR words" for c, r in choice.rejected)
    assert title_apply(nodes, choice) is top
    assert lead in nodes and top.text == "Medications, DME, and Medical Devices"


def test_a_joined_title_lists_both_contributors_boxes_lead_first():
    lead = heading("Prior Authorization Request for", 21.0)
    lead.bbox = (50.0, 40.0, 400.0, 60.0)
    top = heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1)
    top.bbox = (50.0, 62.0, 500.0, 90.0)
    choice = choose_title([lead, top], None, None, "prior-auth")
    assert [(l.text, l.bbox) for l in choice.lines] == [
        ("Prior Authorization Request for", lead.bbox),
        ("Medications, DME, and Medical Devices", top.bbox)]


def test_cover_and_a_joined_heading_with_the_same_words_promote_one_h1():
    from reflow.analyze.evidence import title_apply
    lead = heading("Prior Authorization Request for", 21.0)
    lead.bbox = (50.0, 40.0, 400.0, 60.0)
    top = heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1)
    top.bbox = (50.0, 62.0, 500.0, 90.0)
    sub = heading("Optional Information", 13.0)
    p = page(1, [line("Prior Authorization Request for", 40, 20, x0=50, x1=400),
                 line("Medications, DME, and Medical Devices", 62, 28, x0=50, x1=500), *body()])
    nodes = [lead, top, sub]
    choice = choose_title(nodes, None, None, "prior-auth", [p])
    assert choice.origin == "cover"
    assert choice.nodes == (lead, top) and choice.lead == "Prior Authorization Request for"
    assert title_apply(nodes, choice) is top
    assert top.role == Role.H1 and top.text == choice.text and lead not in nodes


def test_one_line_one_verdict_for_a_gated_cover_kicker_and_the_same_lead_paragraph():
    kicker = "KILLEEN INDEPENDENT SCH00L DISTRICT for"
    p = page(1, [line(kicker, 60, 37.4, ocr=True, x0=50, x1=560),
                 line("2024-2025 DISTRICT CALENDAR", 104, 50.7, x0=50, x1=560), *body()])
    lead = paragraph(kicker)
    lead.bbox = (50.0, 60.0, 560.0, 97.4)   # born-digital node: its own gate passes
    top = heading("2024-2025 DISTRICT CALENDAR", 50.7, role=Role.H1)
    top.bbox = (50.0, 104.0, 560.0, 154.7)
    choice = choose_title([lead, top], None, None, "calendar", [p])
    assert "SCH00L" not in choice.text and "KILLEEN" not in choice.text
    assert choice.text == "2024-2025 DISTRICT CALENDAR"
    joined = [r for c, r in choice.rejected if c.text.startswith("KILLEEN")]
    assert joined and set(joined) == {"digits inside OCR words"}


def test_title_evidence_round_trips_through_the_document():
    from reflow.model import Document
    p = page(1, [line("Annual Comprehensive Financial Report", 100, 30),
                 line("For the Fiscal Year", 138, 24), *body()])
    choice = choose_title([heading("Introduction", 12.0)], "Audit [draft", None, "audit", [p])
    evidence = choice.evidence()
    assert evidence["origin"] == "cover" and evidence["subtitle"]["text"] == "For the Fiscal Year"
    assert evidence["lines"][0]["bbox"] == [100, 100, 500.0, 130]
    assert {r["origin"] for r in evidence["rejected"]} >= {"meta", "heading", "filename"}
    doc = Document(root=Node(Role.DOC))
    doc.root.attrs["title_evidence"] = evidence
    again = Document.from_dict(doc.to_dict())
    assert again.root.attrs["title_evidence"] == evidence
    assert choice.pages == [1]


def test_a_meta_winner_promotes_no_heading():
    from reflow.analyze.evidence import title_apply
    nodes = [heading("Annual Report", 18.0)]
    choice = choose_title(nodes, "Annual Report 2024", None, "file")
    assert choice.origin == "meta" and choice.nodes == ()
    assert title_apply(nodes, choice) is None and nodes[0].role == Role.H2


@pytest.mark.pages
def test_controls_keep_their_titles():
    """The assessment appeal prints its title as a heading and has no metadata title: the chooser
    must keep it (the sample is gitignored, so this is a page-check test). The scanned controls,
    legislation and Carmel, need OCR; their `doc_title` specs cover them."""
    from pathlib import Path

    from reflow.analyze.layout import analyze
    from reflow.extract.pages import extract_pages

    pdf = checkout.sample(Path(__file__).resolve().parents[1], "samples/docaccess/03-assessment-appeal-application.pdf")
    pages = list(extract_pages(pdf, 1, 2))
    choice = choose_title(analyze(pages), None, None, pdf.stem, pages)
    assert choice.text == "ASSESSMENT APPEAL APPLICATION"


def test_a_title_page_behind_the_cover_that_repeats_the_title_has_no_headings():
    # da-financial-audit-comprehensive p2: the title, the district and the year in plain capitals.
    from reflow.analyze.title import title_page_headings
    nodes = [heading("Annual Comprehensive Financial Report", 30, page=1),
             heading("SAN MATEO COUNTY MOSQUITO", 14, page=2, role=Role.H3),
             heading("ANNUAL COMPREHENSIVE FINANCIAL REPORT", 14, page=2, role=Role.H3),
             paragraph("BRIAN WEBER", page=2),
             heading("Introduction", 14, page=3)]
    from reflow.analyze.title import TitleChoice, TitleLine
    choice = TitleChoice("Annual Comprehensive Financial Report", "cover", 0.55,
                         lines=(TitleLine(1, None, "Annual Comprehensive Financial Report", False),))
    assert title_page_headings(nodes, choice) == nodes[1:3]
    assert [n.role for n in nodes] == [Role.H2, Role.H3, Role.H3, Role.P, Role.H2]   # decided, not made


def test_a_second_page_with_prose_keeps_its_headings():
    from reflow.analyze.title import TitleChoice, TitleLine, title_page_headings
    prose = paragraph(" ".join(["word"] * 40), page=2)
    nodes = [heading("Annual Report", 30, page=1),
             heading("ANNUAL REPORT", 14, page=2, role=Role.H3), prose]
    choice = TitleChoice("Annual Report", "cover", 0.55,
                         lines=(TitleLine(1, None, "Annual Report", False),))
    assert title_page_headings(nodes, choice) == []
    assert nodes[1].role == Role.H3


def test_the_heading_on_the_covers_page_is_the_h1_when_the_title_is_printed_again_inside():
    # da-financial-audit-comprehensive: the cover's title is also a running head on page 3.
    nodes = [heading("Annual Comprehensive Financial Report", 30, page=1),
             heading("Annual Comprehensive Financial Report", 12, page=3)]
    p = page(1, [line("Annual Comprehensive Financial Report", 100, 30), *body()])
    choice = choose_title(nodes, None, None, "report", [p])
    assert choice.origin == "cover"
    assert choice.nodes == (nodes[0],)
