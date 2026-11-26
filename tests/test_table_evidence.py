from collections import Counter

import pytest

from reflow.analyze.table_evidence import (
    compare,
    flag_structure,
    heading_warnings,
    numbers,
    table_warning,
)
from reflow.analyze.review import queue
from reflow.analyze.blocks import Break
from reflow.model import Document, Node, PageInfo, Role, Why


def value_table(value):
    return Node(Role.TABLE, children=[
        Node(Role.TR, children=[Node(Role.TD, text="value"), Node(Role.TD, text=value)])
    ])


@pytest.mark.parametrize(("replaced", "candidate"), [
    ("–12", "-12"),
    ("1,024", "1024"),
    ("12¹", "12"),
    ("ILSVRC’14", "ILSVRC'14"),
    ("#layers", "# layers"),
])
def test_comparison_accepts_equivalent_numeric_formatting(replaced, candidate):
    result = compare([value_table(replaced)], value_table(candidate))

    assert result.problem() is None
    assert result.missing_numbers == ()
    assert result.split_rows == 0


def test_plus_minus_is_a_distinct_value():
    result = compare([value_table("±12")], value_table("12"))

    assert result.problem() is not None
    assert result.missing_numbers == ("±12",)


def test_whitespace_does_not_join_adjacent_numeric_values():
    result = compare([value_table("56 110")], value_table("56110"))

    assert numbers("56 110") == ["56", "110"]
    assert result.missing_numbers == ("56", "110")
    assert result.problem() is not None


def test_multiplication_sign_separates_numeric_values():
    assert numbers("112×112") == ["112", "112"]
    assert compare([value_table("112×112")], value_table("112 x 112")).problem() is None


def test_whitespace_join_is_not_assumed_to_be_a_thousands_separator():
    result = compare([value_table("1 024")], value_table("1024"))

    assert result.missing_numbers == ("1", "024")
    assert result.problem() is not None


def test_numeric_values_cannot_move_between_labeled_rows():
    replaced = Node(Role.TABLE, children=[
        Node(Role.TR, children=[Node(Role.TD, text="alpha"), Node(Role.TD, text="12")]),
        Node(Role.TR, children=[Node(Role.TD, text="beta"), Node(Role.TD, text="34")]),
    ])
    candidate = Node(Role.TABLE, children=[
        Node(Role.TR, children=[Node(Role.TD, text="alpha"), Node(Role.TD, text="34")]),
        Node(Role.TR, children=[Node(Role.TD, text="beta"), Node(Role.TD, text="12")]),
    ])

    result = compare([replaced], candidate)

    assert result.missing_numbers == ()
    assert result.split_rows == 2
    assert result.problem() is not None


def cell(text: str, role: Role = Role.TD, **attrs) -> Node:
    return Node(role, text=text, attrs=attrs)


def row(*values: str) -> Node:
    return Node(Role.TR, children=[cell(value) for value in values])


def table(*rows: Node) -> Node:
    return Node(Role.TABLE, children=list(rows))


def test_repeated_cells_with_several_numbers_flag_collapsed_columns():
    broken = table(row("Name", "Values"), row("A", "1 2 3"), row("B", "4 5 6"))
    near_miss = table(row("Name", "Amount"), row("A", "$1,200"), row("B", "$3,400"))
    prose = table(row("Notice", "Details"),
                  row("Rule", "File Form 8832 or Form 2553 for this entity"),
                  row("Note", "See the instructions for Forms 1040, 1099, and 2550."))
    formula = table(row("Output", "Architecture"),
                    row("112 112", "(cid:20) 3 3,64 × 2"),
                    row("56 56", "(cid:20) 3 3,128 × 4"))

    assert table_warning(broken) == "table-collapsed"
    assert table_warning(near_miss) is None
    assert table_warning(prose) is None
    assert table_warning(formula) is None


def heading_warning(heading, siblings):
    return heading_warnings(siblings).get(id(heading))


def test_heading_fragments_flag_bare_numbers_and_adjacent_label_parts():
    number = Node(Role.H4, text="501")
    sign = Node(Role.H3, text="Sign", page=1, bbox=(36, 576, 58, 589))
    here = Node(Role.H3, text="Here", page=1, bbox=(36, 587, 59, 599))
    siblings = [sign, here]

    assert heading_warning(number, [number]) == "heading-fragment"
    assert heading_warning(sign, siblings) == "heading-fragment"
    assert heading_warning(here, siblings) == "heading-fragment"
    assert heading_warning(Node(Role.H3, text="Introduction", page=1), siblings) is None
    tip = Node(Role.H4, text="TIP", page=1, bbox=(46, 337, 63, 346))
    tip_text = Node(Role.P, text="See the next section.", page=1, bbox=(72, 343, 168, 355))
    misplaced_tip = Node(Role.H4, text="TIP", page=1, bbox=(46, 420, 63, 429))
    body = Node(Role.P, text="Ordinary body text.", page=1, bbox=(41, 426, 210, 466))
    assert heading_warning(tip, [tip, tip_text]) == "heading-fragment"
    assert heading_warning(misplaced_tip, [misplaced_tip, body]) is None
    wrapped_tip = Node(Role.H4, text="TIP", page=1, bbox=(46, 420, 63, 429),
                       attrs={"why": [Why(f"new block: {Break.HYPHEN_COLUMN}", Break.HYPHEN_COLUMN)]})
    adjacent_text = Node(Role.P, text="Text after the column break.", page=1,
                         bbox=(71, 407, 210, 428))
    assert heading_warning(wrapped_tip, [adjacent_text, wrapped_tip]) is None
    # the code is what says it, not the words
    wrapped_tip.attrs["why"] = [Why("reworded", Break.HYPHEN_COLUMN)]
    assert heading_warning(wrapped_tip, [adjacent_text, wrapped_tip]) is None
    wrapped_tip.attrs["why"] = [f"new block: {Break.HYPHEN_COLUMN}"]
    assert heading_warning(wrapped_tip, [adjacent_text, wrapped_tip]) == "heading-fragment"
    preceding_text = Node(Role.P, text="A callout begins.", page=1, bbox=(252, 351, 390, 372))
    inline_tip = Node(Role.H4, text="TIP", page=1, bbox=(226, 364, 243, 373))
    following_text = Node(Role.P, text="The callout continues.", page=1, bbox=(222, 371, 390, 391))
    assert heading_warning(inline_tip, [preceding_text, inline_tip, following_text]) == "heading-fragment"
    far = Node(Role.H3, text="Address", page=1, bbox=(36, 620, 70, 632))
    after = Node(Role.H3, text="Phone", page=1, bbox=(300, 620, 330, 632))
    assert heading_warning(far, [far, after]) is None


def test_flag_structure_counts_only_new_flags_and_preserves_existing_warning():
    broken = table(row("Name", "Values"), row("A", "1 2 3"), row("B", "4 5 6"))
    prior = Node(Role.TABLE, attrs={"warn": "existing"}, children=[
        row("Name", "Values"), row("A", "1 2 3"), row("B", "4 5 6")
    ])
    root = Node(Role.DOC, children=[broken, prior])

    assert flag_structure(root) == Counter({"table-collapsed": 1})
    assert broken.attrs["warn"] == "table-collapsed"
    assert prior.attrs["warn"] == "existing"


@pytest.mark.parametrize("length, flagged", [(199, False), (200, True), (3500, True)])
def test_giant_single_cell_table_is_flagged_but_small_note_is_not(length, flagged):
    cell = Node(Role.TD, page=1, text="x" * length)
    table = Node(Role.TABLE, page=1, children=[Node(Role.TR, page=1, children=[cell])])
    doc = Document(Node(Role.DOC, children=[table]), [PageInfo(1, 612, 792)], {"lang": "en"})
    flag_structure(doc.root)
    items = queue(doc)
    assert [i["label"] for i in items] == (["Table that is one giant cell"] if flagged else [])


def test_calendar_months_in_one_cell_are_flagged_even_with_other_notes():
    calendar = "J U L Y " + " ".join(str(day) for day in range(1, 32))
    cells = [Node(Role.TH, page=1, text=calendar * 12),
             Node(Role.TD, page=1, text="Dates to remember"),
             Node(Role.TD, page=1, text="School holidays and contact information.")]
    table = Node(Role.TABLE, page=1, children=[Node(Role.TR, page=1, children=cells)])
    doc = Document(Node(Role.DOC, children=[table]), [PageInfo(1, 612, 792)], {"lang": "en"})
    flag_structure(doc.root)
    assert [i["label"] for i in queue(doc) if i["role"] == "table"] == [
        "Table that is one giant cell"]


def test_long_prose_cells_are_not_flattened_calendars():
    cells = [Node(Role.TD, page=1, text="An ordinary explanatory note. " * 50),
             Node(Role.TD, page=1, text="Another note.")]
    table = Node(Role.TABLE, page=1, children=[Node(Role.TR, page=1, children=cells)])
    root = Node(Role.DOC, children=[table])
    flag_structure(root)
    assert "warn" not in table.attrs
