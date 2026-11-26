"""Field names in non-English documents, and text boxes printed in one row."""

from reflow.model import Node, Role
from reflow.render import html as render


def _tooltip_field(text):
    return Node(Role.FIELD, page=1, text=text, bbox=(0, 0, 100, 12), attrs={
        "kind": "text", "fid": "f1-0", "label": "tooltip", "aria": "Patient Name",
        "tooltip": "Patient Name"})


def test_spanish_caption_beats_english_tooltip_in_a_spanish_document():
    field = _tooltip_field("NOMBRE DEL PACIENTE (apellido, primer nombre)")
    html = render.NodeRenderer({}, {}, doc_lang="es").render(field)
    assert "aria-label" not in html
    assert '<label for="f1-0"' in html
    assert 'title="Patient Name"' in html


def test_a_tooltip_does_not_override_the_printed_label_in_an_english_document():
    # WCAG 2.5.3: the accessible name holds the visible label; the tooltip is the description.
    html = render.NodeRenderer({}, {}, doc_lang="en").render(_tooltip_field("Name of the patient"))
    assert "aria-label" not in html
    assert '<label for="f1-0"' in html and "Name of the patient" in html
    assert 'title="Patient Name"' in html


def test_a_tooltip_does_not_override_an_english_caption_in_a_spanish_document():
    field = _tooltip_field("Name of the patient, as it is on the card")
    html = render.NodeRenderer({}, {}, doc_lang="es").render(field)
    assert "aria-label" not in html
    assert 'title="Patient Name"' in html


def test_a_tooltip_that_holds_the_printed_label_still_names_the_field():
    # "Patient Name" holds the label's words, so it is the more exact name and meets 2.5.3.
    for caption in ("PATIENT NAME:", "*Patient name", "Patient Name"):
        html = render.NodeRenderer({}, {}, doc_lang="en").render(_tooltip_field(caption))
        assert 'aria-label="Patient Name"' in html


def test_a_tooltip_still_names_a_field_with_no_printed_label():
    field = _tooltip_field(None)
    html = render.NodeRenderer({}, {}, doc_lang="en").render(field)
    assert 'aria-label="Patient Name"' in html and "<label" not in html


def _box(i, x):
    return Node(Role.FIELD, page=1, bbox=(x, 10, x + 40, 28), attrs={
        "kind": "text", "fid": f"f1-{i}", "aria": f"Date {i}", "label": "printed-nearby"})


def test_text_boxes_side_by_side_share_a_row():
    row = render.NodeRenderer({}, {}).cell_fields([_box(0, 0), _box(1, 44), _box(2, 90)])
    assert row.count('class="field-row"') == 1 and row.count("<input") == 3


def test_distant_text_boxes_stay_stacked():
    assert "field-row" not in render.NodeRenderer({}, {}).cell_fields([_box(0, 0), _box(1, 300)])
