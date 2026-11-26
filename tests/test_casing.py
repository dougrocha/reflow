from reflow.render.casing import LABEL_MAX_WORDS, RUN_MAX_WORDS, Casing
from reflow.model import Node, Role
from reflow.render.html import NodeRenderer

BODY = ["Send the assessment to the County of San Luis Obispo before the hearing.",
        "Applicants must attach a copy of the notice."]


def shown(text: str, words: int = LABEL_MAX_WORDS) -> str | None:
    return Casing(BODY).display(text, words)


def test_words_take_the_case_the_document_prints_them_in():
    assert shown("MAIL TO: ASSESSMENT BOARD") == "Mail to: Assessment board"
    assert shown("COUNTY OF SAN LUIS OBISPO") == "County of San Luis Obispo"
    assert shown("1. APPLICANT INFORMATION - PLEASE PRINT") == "1. Applicant information - please print"
    assert shown("APPLICANT'S NOTICE") == "Applicant's notice"


def test_acronyms_and_words_with_digits_keep_their_capitals():
    assert shown("FORM W-4 AND EIN") == "Form W-4 and EIN"
    assert shown("USCIS FORM I-9") == "USCIS form I-9"
    assert shown("MAIL TO CA OFFICE") == "Mail to CA office"


def test_an_unknown_long_word_leaves_the_run_as_printed():
    assert shown("SUBMIT TO MONTEREY") is None


def test_text_not_wholly_in_capitals_or_too_long_is_left_alone():
    assert shown("Mail to: ASSESSMENT") is None
    assert shown("PLEASE PRINT THE NAME OF THE APPLICANT AND THE DATE OF THE NOTICE",
                 RUN_MAX_WORDS) is None
    assert shown("W-4") is None   # nothing to change


def test_the_markup_keeps_the_printed_words_and_carries_the_display_form():
    def paragraph(text):
        return NodeRenderer({}, {}, casing=Casing(BODY)).render(Node(Role.P, text=text, page=1))

    assert ('<span class="sc" data-sc="Mail to"><span>MAIL TO</span></span>'
            in paragraph("MAIL TO"))
    assert "<span>W-4</span>" not in paragraph("W-4") and ">W-4</p>" in paragraph("W-4")
