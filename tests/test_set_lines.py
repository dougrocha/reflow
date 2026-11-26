"""set_lines: a block whose author broke every line keeps its lines (rendered with <br>)."""

from reflow.analyze.blocks import Block, set_lines
from reflow.extract.lines import Line


def _block(texts, x1s, x0=54.0, top=107.0, pitch=13.7, size=12.0):
    return Block([Line(1, (x0, top + pitch * i, x1, top + pitch * i + 17), t, size, True, False,
                       "Arial-BoldMT") for i, (t, x1) in enumerate(zip(texts, x1s))])


def test_a_list_of_drug_names_keeps_one_name_per_line():
    # The pharmacy guideline, p1: a heading line (the widest) over one drug per line.
    texts = ["BIOLOGIC AND IMMUNOLOGICAL AGENTS – INJECTION:", "ACTEMRA® (tocilizumab) IV & SQ",
             "AMJEVITA™ (adalimumab-atto) SQ", "CIMZIA® (certolizumab pegol) SQ",
             "ENBREL® (etanercept) SQ"]
    assert set_lines(_block(texts, [386, 248, 255, 246, 205])) == texts


def test_wrapped_prose_keeps_its_lines_joined():
    texts = ["If you will be retiring between July 1, 2026 and",
             "May 31, 2027, now is your time to make sure that you",
             "have the coverage you want."]
    assert set_lines(_block(texts, [330, 340, 220])) is None


def test_a_line_that_runs_on_or_a_lowercase_next_line_is_a_wrap():
    # Short lines, but the phrase runs on: "Call us for" / "Your benefits".
    assert set_lines(_block(["Call us for", "Your benefits", "Your claims"],
                            [120, 140, 130])) is None
    assert set_lines(_block(["Call Member Services", "about your benefits", "Your claims"],
                            [200, 190, 130])) is None


def test_table_fragments_are_not_a_list():
    # A label, its dot leader and its figures read as one block: cells, not lines of a list.
    assert set_lines(_block(["Collected ..........", "15,381", "16,026"], [300, 120, 120])) is None
    # Cells side by side: the lines do not share a left edge.
    side = Block([Line(1, (54, 100, 150, 112), "fosinopril sodium", 9, False, False, "Arial"),
                  Line(1, (200, 106, 240, 118), "fentanyl", 9, False, False, "Arial"),
                  Line(1, (54, 113, 120, 125), "fluocinonide", 9, False, False, "Arial")])
    assert set_lines(side) is None
