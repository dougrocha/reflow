"""Page labels from the printed page numbers in the margins."""

from reflow.analyze.artifacts import Folio, printed_numbers
from reflow.analyze.page_numbers import infer_printed_labels
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent


def arabic(value):
    return [Folio(value, False)]


def roman(value):
    return [Folio(value, True)]


def printed(pages, offset):
    return {i: arabic(i + offset) for i in pages}


def test_a_consistent_offset_labels_every_page():
    result = infer_printed_labels(printed(range(1, 11), 2), 10)

    assert result == {i: str(i + 2) for i in range(1, 11)}


def test_a_negative_offset_labels_every_page():
    result = infer_printed_labels(printed(range(305, 316), -2), 315)

    assert result == {i: str(i - 2) for i in range(305, 316)}


def test_a_page_that_prints_another_number_besides_its_folio_is_still_labelled():
    candidates = printed(range(1, 16), 2)
    candidates[3] = arabic(123) + arabic(5)
    candidates[7] = arabic(123) + arabic(9)

    result = infer_printed_labels(candidates, 15)

    assert result[3] == "5" and result[7] == "9"


def test_too_few_pages_that_print_a_number_infer_nothing():
    assert infer_printed_labels(printed(range(1, 4), 2), 10) == {}


def test_no_labels_when_the_dominant_offset_is_under_sixty_percent():
    candidates = {**printed(range(1, 6), 2), **printed(range(6, 11), 100)}

    assert infer_printed_labels(candidates, 10) == {}


def test_a_gap_between_pages_that_agree_is_filled():
    candidates = {**printed(range(1, 6), 10), **printed(range(7, 11), 10)}

    assert infer_printed_labels(candidates, 10)[6] == "16"


def test_a_gap_between_pages_that_disagree_is_left():
    candidates = {**printed(range(1, 4), 10), **printed(range(5, 8), 20)}

    assert 4 not in infer_printed_labels(candidates, 7)


def test_an_empty_document_infers_nothing():
    assert infer_printed_labels({}, 10) == {}


def test_a_page_number_equal_to_the_page_gives_an_offset_of_zero():
    assert infer_printed_labels(printed(range(1, 11), 0), 10) == {i: str(i) for i in range(1, 11)}


def test_roman_front_matter_is_no_offset_but_still_a_page_that_prints_a_number():
    candidates = {1: roman(1), 2: roman(2), 3: roman(3), **printed(range(4, 11), 1)}

    result = infer_printed_labels(candidates, 10)

    assert result == {i: str(i + 1) for i in range(4, 11)}


def page(number, *texts, top=760.0):
    lines = [Line(page=number, bbox=(300.0, top + 12 * k, 330.0, top + 12 * k + 10), text=text,
                  size=10.0, bold=False, italic=False, font="Helvetica")
             for k, text in enumerate(texts)]
    return PageContent(number, 612.0, 792.0, lines, images=[])


def test_the_folios_printed_in_the_margin_are_read_in_any_of_their_forms():
    pages = [page(1, "Page 3 of 100"), page(2, "4"), page(3, "iv"), page(4, "Some words here")]

    assert printed_numbers(pages) == {1: [Folio(3, False)], 2: [Folio(4, False)],
                                      3: [Folio(4, True)]}


def test_a_number_in_the_body_is_not_a_folio():
    assert printed_numbers([page(1, "12", top=300.0)]) == {}
