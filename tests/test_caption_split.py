"""Captions that the text layer joined across neighbouring form cells are cut at the fields."""

from reflow.analyze.forms import apply_cuts, split_captions
from reflow.extract.forms import Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent


def line(text, x0, x1, top=194.7):
    return Line(page=1, bbox=(x0, top, x1, top + 8.2), text=text, size=6.0, bold=False, italic=False,
                font="ArialMT")


def field(index, x0, x1, comb=False):
    return Widget(page=1, index=index, kind="text", bbox=(x0, 200.9, x1, 212.3), flags=(1 << 24) if comb else 0)


def page(widgets):
    return PageContent(number=1, width=612, height=792, lines=[], images=[], widgets=widgets)


def test_two_captions_over_two_boxes_are_cut_in_two():
    joined = line("STATE ZIP CODE", 217.0, 271.2)
    cuts = split_captions(page([field(0, 216.4, 240.2), field(1, 242.4, 305.2)]), [joined])
    assert [p.text for p in apply_cuts([joined], cuts)] == ["STATE", "ZIP CODE"]


def test_one_caption_over_one_wide_box_is_kept_whole():
    caption = line("MAILING ADDRESS OF APPLICANT", 36.0, 160.0)
    assert split_captions(page([field(0, 35.4, 576.4)]), [caption]) == {}


def test_a_caption_not_starting_at_each_box_is_kept_whole():
    caption = line("SOCIAL SECURITY NUMBER", 100.0, 220.0)
    boxes = [field(0, 100.0, 130.0), field(1, 160.0, 190.0)]
    assert split_captions(page(boxes), [caption]) == {}


def test_boxes_of_one_comb_field_are_not_separate_captions():
    joined = line("STATE ZIP CODE", 217.0, 271.2)
    comb = [field(0, 216.4, 240.2, comb=True), field(1, 242.4, 305.2, comb=True)]
    assert split_captions(page(comb), [joined]) == {}
