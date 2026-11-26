"""marks_for: bold and italic words of a block, as marks on the block's text."""

from reflow.analyze.blocks import Block, join_lines
from reflow.analyze.marks import drop_emphasis, marks_for
from reflow.extract.chars import accent_of
from reflow.extract.lines import SOFT_HYPHEN, Line, StyleRun
from reflow.model import Mark, Node, Role

REGULAR, BOLD, ITALIC, BOTH = (False, False), (True, False), (False, True), (True, True)


def line(top, *parts, symbol=()):
    """A line from (text, (bold, italic)) parts, joined by single spaces. A space takes the
    style before it, and the booleans follow the extractor: bold at 90% of the characters,
    italic at 60%. A line of one style has no runs."""
    text, runs = "", []
    for k, (piece, (bold, italic)) in enumerate(parts):
        if k:
            text += " "
            runs[-1] = runs[-1]._replace(end=len(text))
        runs.append(StyleRun(len(text), len(text) + len(piece), bold, italic, piece in symbol))
        text += piece
    n = len(text)
    n_bold = sum(r.end - r.start for r in runs if r.bold)
    n_italic = sum(r.end - r.start for r in runs if r.italic)
    one_style = len({r[2:] for r in runs}) == 1 and not runs[0].symbol
    return Line(1, (72.0, top, 72.0 + 5 * n, top + 10), text, 10.0, n_bold / n >= 0.9,
                n_italic / n >= 0.6, "F", runs=() if one_style else tuple(runs))


def words(text, marks):
    return [(m.kind, text[m.start:m.end]) for m in marks]


def test_a_bold_tail_across_a_wrap_is_one_strong():
    first = line(100, ("Please read the notice.", REGULAR), ("Do not", BOLD))
    second = line(112, ("attach hearing evidence.", BOLD))
    text = join_lines([first.text, second.text])

    marks, whole = marks_for(text, [first, second])

    assert words(text, marks) == [("strong", "Do not attach hearing evidence.")]
    assert not whole


def test_a_soft_hyphen_removed_between_lines_does_not_shift_the_mark():
    first = line(100, ("Read the", REGULAR), (f"hear{SOFT_HYPHEN}", BOLD))
    second = line(112, ("ing notice today", BOLD))
    text = join_lines([first.text, second.text])
    assert text == "Read the hearing notice today"

    marks, _ = marks_for(text, [first, second])

    assert words(text, marks) == [("strong", "hearing notice today")]


def test_a_list_label_cut_from_the_front_keeps_the_marks_in_place():
    lines = [line(100, ("a.", REGULAR), ("Pay the", REGULAR), ("FEE:", BOLD))]
    text = "Pay the FEE:"

    marks, _ = marks_for(text, lines)

    assert words(text, marks) == [("strong", "FEE:")]


def test_text_not_found_gives_no_marks_and_the_old_italic_rule():
    lines = [line(100, ("Totally different words", BOLD))]
    assert marks_for("Something else entirely", lines) == ([], False)
    italic = [line(100, ("Totally different words", ITALIC))]
    assert marks_for("Something else entirely", italic) == ([], True)


def test_a_mostly_bold_paragraph_marks_the_bold_part_only():
    text = "Do not attach the evidence to this form. Ask a clerk."
    lines = [line(100, ("Do not attach the evidence to this form.", BOLD), ("Ask a clerk.", REGULAR))]

    marks, whole = marks_for(text, lines)

    assert words(text, marks) == [("strong", "Do not attach the evidence to this form.")]
    assert not whole


def test_an_italic_majority_block_with_a_regular_tail_gets_em_and_no_class():
    text = "The Furnished System shall be kept clean by the Contractor"
    lines = [line(100, ("The Furnished System shall be kept clean by", ITALIC),
                  ("the Contractor", REGULAR))]

    marks, whole = marks_for(text, lines)

    assert words(text, marks) == [("em", "The Furnished System shall be kept clean by")]
    assert whole is False


def test_a_wholly_bold_block_is_one_strong_over_all_of_it():
    lines = [line(100, ("WARNING: do not send cash.", BOLD)),
             line(112, ("Use a money order.", BOLD))]
    text = join_lines([l.text for l in lines])

    marks, whole = marks_for(text, lines)

    assert marks == [Mark(0, len(text), "strong", text)]
    assert not whole


def test_a_wholly_italic_block_has_no_marks_and_keeps_its_class():
    lines = [line(100, ("Figure 1. The model, in outline.", ITALIC)),
             line(112, ("Courtesy of the author.", ITALIC))]
    text = join_lines([l.text for l in lines])

    assert marks_for(text, lines) == ([], True)


def test_bold_italic_words_get_both_marks_strong_outside_em():
    text = "We call this the Furnished System here"
    lines = [line(100, ("We call this the", REGULAR), ("Furnished", BOTH), ("System", BOLD),
                  ("here", REGULAR))]

    marks, whole = marks_for(text, lines)

    assert words(text, marks) == [("strong", "Furnished System"), ("em", "Furnished")]
    assert not whole


def test_a_symbol_font_run_inside_a_text_line_gets_no_mark():
    text = "x Yes I agree"          # the "x" is a check box drawn from a symbol font
    boxed = [line(100, ("x", BOLD), ("Yes I agree", REGULAR), symbol=("x",))]
    plain = [line(100, ("x", BOLD), ("Yes I agree", REGULAR))]

    assert marks_for(text, boxed)[0] == []
    assert words(text, marks_for(text, plain)[0]) == [("strong", "x")]


def test_the_block_text_of_a_real_block_aligns():
    first = line(100, ("Fee:", BOLD), ("pay", REGULAR))
    second = line(112, ("by April.", REGULAR))
    block = Block([first, second])

    marks, _ = marks_for(block.text, block.lines)

    assert words(block.text, marks) == [("strong", "Fee:")]


def test_punctuation_takes_the_style_it_was_drawn_in():
    text = "referred to as the “Furnished System”."
    # the opening quote is regular, the italic run starts at F and runs to the full stop
    first = Line(1, (72.0, 100.0, 300.0, 110.0), text, 10.0, False, False, "F",
                 runs=(StyleRun(0, 20, False, False), StyleRun(20, len(text), False, True)))

    marks, whole = marks_for(text, [first])

    assert words(text, marks) == [("em", "Furnished System”.")] and not whole


def test_a_text_whose_punctuation_was_edited_still_aligns_by_letters():
    source = line(100, ("Fee due", REGULAR), ("now,", BOLD), ("pay", REGULAR))
    text = "Fee due now; pay"           # the comma became a semicolon after the lines were read

    marks, _ = marks_for(text, [source])

    assert words(text, marks) == [("strong", "now;")]


def _page(*lines):
    from reflow.extract.pages import PageContent
    return PageContent(1, 612, 792, list(lines), [])


def test_a_paragraph_node_carries_the_marks_of_its_lines():
    from reflow.analyze.layout import analyze
    rows = [line(100, ("Please read the notice.", REGULAR), ("Do not attach evidence", BOLD)),
            line(112, ("to this application.", REGULAR))]
    [para] = [n for n in analyze([_page(*rows)]) if n.role == Role.P]
    assert words(para.text, para.marks) == [("strong", "Do not attach evidence")]
    assert "italic" not in para.attrs


def test_an_italic_majority_paragraph_gets_em_and_no_italic_class():
    from reflow.analyze.layout import analyze
    rows = [line(100, ("The Furnished System stays clean", ITALIC), ("at all times.", REGULAR)),
            line(112, ("The contractor shall comply.", REGULAR))]
    [para] = [n for n in analyze([_page(*rows)]) if n.role == Role.P]
    assert words(para.text, para.marks) == [("em", "The Furnished System stays clean")]
    assert "italic" not in para.attrs


def test_a_wholly_italic_paragraph_keeps_its_class_and_has_no_em():
    from reflow.analyze.layout import analyze
    rows = [line(100, ("A quotation set in italics that", ITALIC)),
            line(112, ("runs over two lines.", ITALIC))]
    [para] = [n for n in analyze([_page(*rows)]) if n.role == Role.P]
    assert para.marks == [] and para.attrs["italic"] is True


def test_stale_runs_are_not_trusted():
    stale = line(100, ("Fee: pay", BOLD), ("now", REGULAR))
    edited = Line(**{**stale.__dict__, "text": "Fee: pay later"})   # runs end at 12, text is 14

    marks, _ = marks_for("Fee: pay later", [edited])

    assert marks == []                     # the line's own booleans stand: not bold, not italic



def raised(text, *runs):
    """A regular 10 pt line of `text` whose (start, end) `runs` the PDF set small and raised."""
    return Line(1, (72.0, 100, 72.0 + 5 * len(text), 110), text, 10.0, False, False, "F",
                sup=tuple(runs))


def test_a_raised_marker_is_a_sup_mark_without_the_period_before_it():
    # az-accident-insurance-flyer p1: "each year.¹", the 1 raised and no note found for it.
    text = "Americans visit the emergency room 42.2 million times each year.1"
    lines = [raised(text, (len(text) - 1, len(text)))]

    marks, _ = marks_for(text, lines)

    assert words(text, marks) == [("sup", "1")]


def test_a_raised_marker_keeps_its_place_when_the_lines_are_joined():
    first = raised("Two-thirds of Americans would struggle to")
    second = raised("pay $1,000 in an emergency.2", (27, 28))
    text = join_lines([first.text, second.text])

    marks, _ = marks_for(text, [first, second])

    assert words(text, marks) == [("sup", "2")]
    assert text.endswith("emergency.2")


def test_a_line_with_no_raised_runs_has_no_sup():
    text = "Pure H2O and 10 units"
    marks, _ = marks_for(text, [raised(text)])
    assert marks == []



def coloured(text, *runs):
    """A line of `text` with an accent over each (start, end, accent) run, as the extractor
    records one: every stretch between them is the text colour."""
    at, parts = 0, []
    for start, end, accent in runs:
        if at < start:
            parts.append(StyleRun(at, start, False, False))
        parts.append(StyleRun(start, end, False, False, False, accent))
        at = end
    if at < len(text):
        parts.append(StyleRun(at, len(text), False, False))
    return Line(1, (72.0, 100, 72.0 + 5 * len(text), 110), text, 10.0, False, False, "F",
                runs=tuple(parts))


def test_a_short_coloured_run_is_an_accent_mark():
    text = "FEE: $65.75 PER APPLICATION/PARCEL"
    marks, _ = marks_for(text, [coloured(text, (5, 11, "red"))])
    assert words(text, marks) == [("red", "$65.75")]


def test_each_accent_gets_its_own_mark_and_a_mark_keeps_its_bold():
    text = "Call now or write to us"
    bold_red = Line(1, (72.0, 100, 200, 110), text, 10.0, False, False, "F",
                     runs=(StyleRun(0, 4, True, False, False, "red"), StyleRun(4, 12, False, False),
                           StyleRun(12, 23, False, False, False, "green")))
    marks, _ = marks_for(text, [bold_red])
    assert sorted(words(text, marks)) == [("green", "write to us"), ("red", "Call"),
                                          ("strong", "Call")]


def test_a_block_wholly_or_mostly_in_a_colour_has_no_accent_mark():
    whole = "Every word of this paragraph is set in red."
    assert marks_for(whole, [coloured(whole, (0, len(whole), "blue"))])[0] == []
    mostly = "Read the red part of this line carefully now"
    assert marks_for(mostly, [coloured(mostly, (9, 45, "blue"))])[0] == []


def test_a_long_coloured_run_is_no_accent_mark():
    text = "x " * 3 + "a long coloured sentence that runs past the limit of forty characters " + "tail " * 30
    start = 6
    end = start + 70
    assert marks_for(text, [coloured(text, (start, end, "red"))])[0] == []


def test_a_symbol_run_is_never_coloured_text():
    text = "o Yes please, tick one"
    marks, _ = marks_for(text, [Line(1, (72.0, 100, 200, 110), text, 10.0, False, False, "F", runs=(
        StyleRun(0, 1, False, False, True, "red"), StyleRun(1, len(text), False, False)))])
    assert marks == []


def test_accents_are_saturated_reds_and_greens_only():
    assert accent_of(255, 0, 0) == "red"
    assert accent_of(0, 128, 0) == "green"
    for plain in [(0, 0, 0), (27, 27, 27), (128, 128, 128), (0, 45, 98),      # text, grey, navy
                  (255, 200, 200), (255, 128, 0), (255, 255, 0), (160, 32, 240),
                  (0, 154, 218), (11, 87, 208), (0, 0, 255)]:  # pastel, orange, yellow, violet, blues
        assert accent_of(*plain) == "", plain


def test_a_block_that_became_a_heading_loses_its_accent_marks():
    node = Node(Role.H2, text="Fee $65.75", marks=[Mark(4, 10, "red", "$65.75"),
                                                     Mark(0, 3, "strong", "Fee"),
                                                     Mark(4, 10, "doubt", "$65.75")])
    drop_emphasis(node)
    assert [m.kind for m in node.marks] == ["doubt"]
