"""The reasons and trace that the heuristic layout records (attrs.why, trace/NNNN.json)."""

from reflow.analyze.artifacts import artifact_reasons
from reflow.analyze.blocks import Block, Break
from reflow.analyze.breaks import break_reason
from reflow.analyze.constants import SPLIT_COLUMNS, SPLIT_NAMES
from reflow.analyze.headings import heading_decision
from reflow.analyze.layout import analyze
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.leaders import normalize_document
from reflow.model import Document, Node, PageInfo, Role, code_of


def line(top, text="Some words here", *, x0=72.0, size=10.0, bold=False, italic=False,
         width=200.0, page=1, height=10.0):
    return Line(page=page, bbox=(x0, top, x0 + width, top + height), text=text, size=size,
                bold=bold, italic=italic, font="Helvetica")


# --- break_reason: why a line starts a new block -------------------------------------------

def test_break_reason_is_none_for_a_continuing_line():
    prev = line(100)
    assert break_reason(prev, line(112)) is None


def test_break_reason_names_each_break():
    prev = line(100)
    resized = break_reason(prev, line(112, size=12))
    assert "font size" in resized and "12" in resized
    assert break_reason(prev, line(112, bold=True)) == Break.BOLD_CHANGES
    assert "gap" in break_reason(prev, line(120))
    assert "column" in break_reason(prev, line(50))
    assert break_reason(prev, line(112, x0=90)) == Break.FIRST_LINE_INDENT
    assert break_reason(prev, line(112, text="• first item")) == Break.LIST_MARKER


def test_a_bold_word_split_by_its_hyphen_does_not_end_the_paragraph_on_the_weight_change():
    # "de-" ends a bold line and "duction." carries the word on in the body weight: one word,
    # one paragraph. A weight change after a line that does not end in a hyphen, or before a
    # capitalised line, still starts a new block.
    bold_split = line(100, "Standard deduc-", bold=True)
    assert break_reason(bold_split, line(112, "tion amounts for 2025")) is None
    assert break_reason(line(100, "Standard deduction", bold=True),
                        line(112, "amounts for 2025")) == Break.BOLD_CHANGES
    assert break_reason(bold_split, line(112, "Tion amounts for 2025")) == Break.BOLD_CHANGES


def test_a_block_that_starts_after_a_hyphenated_column_end_carries_the_break_code():
    prev = line(100, "to afford-", width=100)
    beside = line(100, "rural families", x0=330, width=100)
    assert break_reason(prev, beside) == Break.HYPHEN_COLUMN
    page = PageContent(1, 612, 792, [prev, beside], [])
    found = analyze([page])
    # the engine normalizes the text of the tree before any later pass reads a why
    normalize_document(Document(Node(Role.DOC, children=found), [PageInfo(1, 612, 792)]))
    nodes = [n for n in found if n.role.value != "artifact" and n.text.startswith("rural")]
    [why] = [w for n in nodes for w in n.attrs["why"] if w.startswith("new block")]
    assert code_of(why) == Break.HYPHEN_COLUMN and why == f"new block: {Break.HYPHEN_COLUMN}"


# --- heading_decision: the style and the reason ---------------------------------------------

def test_heading_decision_gives_the_style_and_the_reason():
    heading = Block([line(100, "Overview", size=14, bold=True)])
    paragraph = Block([line(200, "Plain sentence of body text.")])
    short_bold = Block([line(300, "Bold label", bold=True)])
    blocks = [heading, paragraph, short_bold]

    style, why = heading_decision(heading, 10.0, blocks)
    assert style == (14.0, True) and "1.40×" in why

    style, why = heading_decision(paragraph, 10.0, blocks)
    assert style is None and "body text" in why

    style, why = heading_decision(short_bold, 10.0, blocks)
    assert style == (10.0, True) and "bold" in why


# --- artifact_reasons: each running header, page number and rule with its reason -----------

def test_artifact_reasons_name_each_artifact():
    pages = []
    running, numbers, rule, body_lines = [], [], [], []
    for n in range(1, 5):
        header = line(20, "Annual Report", page=n)
        number = line(760, str(n), page=n)
        text = line(300, f"Body text on page {n}", page=n)
        pages.append(PageContent(number=n, width=612, height=792,
                                 lines=[header, number, text], images=[]))
        running.append(header)
        numbers.append(number)
        body_lines.append(text)
    dash = line(400, "____", page=1)
    pages[0].lines.append(dash)
    rule.append(dash)

    reasons = artifact_reasons(pages, 10.0)
    assert {id(l) for l in running} <= set(reasons)
    assert "running header" in reasons[id(running[0])] and "4 pages" in reasons[id(running[0])]
    assert "page number" in reasons[id(numbers[0])]
    assert "rule" in reasons[id(rule[0])]
    assert all(id(l) not in reasons for l in body_lines)


# --- analyze(trace=...) -------------------------------------------------------------------

def test_analyze_trace_records_lines_split_and_gutters():
    left = [line(top, f"Left column line {top}", x0=72.0) for top in (100, 115, 130)]
    right = [line(top, f"Right column line {top}", x0=330.0) for top in (100, 115, 130)]
    page = PageContent(number=1, width=612, height=792, lines=left + right, images=[])
    trace: dict = {}

    nodes = analyze([page], trace=trace)

    recorded = trace["pages"][1]
    assert [entry["bbox"] for entry in recorded["lines"]] == [list(l.bbox) for l in left + right]
    assert all(entry["artifact"] is None for entry in recorded["lines"])
    assert recorded["split"] == SPLIT_NAMES[SPLIT_COLUMNS]
    assert recorded["gutters"] == [[272.0, 330.0]]
    assert any("reading order: columns (clean gutters)" in n.attrs.get("why", []) for n in nodes)


def test_a_callout_label_between_a_hyphenated_line_and_its_word_does_not_split_the_paragraph():
    # p17 p94: "...your standard de-" / CAUTION / "duction may be limited."
    from reflow.analyze.breaks import make_blocks
    lines = [line(100, "If you can be claimed as a dependent, your standard de-", size=8.0),
             line(115, "CAUTION", size=4.4, width=24.0),
             line(112, "duction may be limited. See Standard Deduction.", size=8.0)]
    blocks = make_blocks(lines)
    assert [b.text for b in blocks] == [
        "If you can be claimed as a dependent, your standard deduction may be limited. "
        "See Standard Deduction.", "CAUTION"]
