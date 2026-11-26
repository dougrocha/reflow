"""OCR suggestions: a likely slip, and the reading the document's own evidence suggests."""

import re
from types import SimpleNamespace

from reflow.analyze import structure
from reflow.analyze.engine import Backbone
from reflow.analyze.ocr_words import document_lexicon, flag, suggestions
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import Document, Mark, Node, PageInfo, Role, Source
from reflow.render import html as render


def p(page, text, **attrs):
    return Node(Role.P, page=page, bbox=(72.0, 100.0, 300.0, 112.0), text=text, attrs=attrs,
                source=Source.OCR)


def suggested(target, nodes):
    return [(s.was, s.now, s.rule) for s in suggestions(target, document_lexicon(nodes))]


def carmel(*others):
    slip = p(4, "Message from the Ge neral Manager")
    return slip, [p(2, "Barbara Buikema, General Manager"), slip, *others]


def test_a_split_word_the_document_prints_twice_is_suggested_joined():
    slip, nodes = carmel(p(12, "General Manager Principal Engineer"))
    assert suggested(slip, nodes) == [("Ge neral", "General", "split-word")]


def test_one_other_occurrence_of_the_join_is_not_enough():
    slip, nodes = carmel()
    assert suggested(slip, nodes) == []


def test_a_half_printed_elsewhere_is_a_word_not_a_slip():
    slip, nodes = carmel(p(12, "General Manager Principal Engineer"), p(9, "neral"))
    assert suggested(slip, nodes) == []


def test_a_short_word_printed_elsewhere_is_not_joined():
    target = p(3, "a new plan")
    nodes = [target, p(4, "anew"), p(5, "anew again"), p(6, "a plan")]
    assert suggested(target, nodes) == []


def test_contents_entries_and_context_crumbs_are_not_evidence():
    entry = Node(Role.LI, page=1, text="General Manager 4")
    contents = Node(Role.LIST, page=1, attrs={"contents": True}, children=[entry])
    crumb = p(5, "General Manager (continued)", context=True)
    slip, nodes = carmel(contents, crumb)
    assert suggested(slip, nodes) == []


def test_a_figure_and_an_ai_draft_are_not_evidence():
    figure = Node(Role.FIGURE, page=3, text="General view of the plant")
    draft = Node(Role.P, page=6, text="General notes", source=Source.LLM,
                 attrs={"provenance": "ai-drafted"})
    slip, nodes = carmel(figure, draft)
    assert suggested(slip, nodes) == []


def test_a_text_repeated_on_three_pages_counts_once():
    slip = p(4, "Message from the Ge neral Manager")
    heads = [p(n, "General Manager's Report") for n in (5, 6, 7)]
    assert suggested(slip, [slip, *heads]) == []
    assert suggested(slip, [slip, *heads, p(12, "General Manager Principal Engineer")]) == [
        ("Ge neral", "General", "split-word")]


def test_the_suggestion_keeps_the_page_casing():
    slip = p(4, "MESSAGE FROM THE GE NERAL MANAGER")
    nodes = [slip, p(2, "GENERAL MANAGER"), p(12, "THE GENERAL FUND")]
    assert suggested(slip, nodes) == [("GE NERAL", "GENERAL", "split-word")]


def test_a_letter_in_a_printed_zip_code_is_suggested_as_a_digit():
    node = p(5, "Colorado Community Media 750 W. Hampden Ave. Littleton, CO 8011O")
    assert suggested(node, [node]) == [("8011O", "80110", "zip-digit")]
    node = p(5, "Boise, ID 8370O-1234")
    assert suggested(node, [node]) == [("8370O-1234", "83700-1234", "zip-digit")]
    node = p(5, "Springfield, IL 6270l")
    assert suggested(node, [node]) == [("6270l", "62701", "zip-digit")]


def test_a_zip_slot_needs_a_city_a_comma_a_state_and_five_characters():
    for text in ("ID 1234O", "Code 8011O", "PO 8011O", "Room CO 8011O", "Littleton CO 8011O",
                 "Littleton, XX 8011O", "Littleton, CO 8011OA", "Littleton, CO 8O11O",
                 "Littleton, CO 80110", "Littleton, CO 8011O Suite 4"):
        node = p(5, text)
        assert suggested(node, [node]) == [], text


def test_a_zip_at_the_end_of_an_address_line_is_a_candidate():
    lines = ["Englewood, CO 8011O", "Phone 303-555-0100"]
    node = p(5, " ".join(lines), lines=lines)
    assert suggested(node, [node]) == [("8011O", "80110", "zip-digit")]


# The pass: a doubt mark with the suggestion as its alt, a fact, and the text unchanged.

def address(lines, page=5):
    return Node(Role.P, page=page, bbox=(72.0, 100.0, 300.0, 136.0), text=" ".join(lines),
                attrs={"lines": list(lines)}, source=Source.OCR)


def doc_of(*nodes):
    return Document(Node(Role.DOC, children=list(nodes)), [PageInfo(5, 612.0, 792.0)])


def at(text, word, kind, alt=""):
    start = text.index(word)
    return Mark(start, start + len(word), kind, word, alt)


def test_the_pass_marks_the_slip_and_keeps_the_text():
    lines = ["Colorado Community Media", "Englewood, CO 8011O"]
    node = address(lines)
    assert flag(doc_of(node), None) == 1
    assert node.text == " ".join(lines) and node.attrs["lines"] == lines
    assert node.marks == [at(node.text, "8011O", "doubt", alt="80110")]
    [fact] = node.facts
    assert fact.code == "ocr-suggestion" and fact.source == Source.OCR
    assert fact.detail == "OCR read ‘8011O’; the document suggests ‘80110’ (ZIP code digit)"
    assert fact.data == {"was": "8011O", "now": "80110", "rule": "zip-digit",
                         "start": 39, "end": 44, "line_box": [72.0, 100.0, 300.0, 136.0]}


def test_the_fact_points_at_the_ocr_line_that_holds_the_slip():
    node = address(["Colorado Community Media", "Englewood, CO 8011O"])
    lines = [Line(5, (72.0, 100.0, 250.0, 112.0), "Colorado Community Media", 10.0, False, False,
                  "F", ocr=True),
             Line(5, (72.0, 124.0, 210.0, 136.0), "Englewood, CO 8011O", 10.0, False, False, "F",
                  ocr=True)]
    flag(doc_of(node), [PageContent(5, 612.0, 792.0, lines, [])])
    assert node.facts[0].data["line_box"] == [72.0, 124.0, 210.0, 136.0]


def test_an_existing_doubt_on_the_token_is_widened_not_doubled():
    node = address(["Englewood, CO 8011O"])
    node.marks = [Mark(18, 19, "doubt", "O")]
    flag(doc_of(node), None)
    assert node.marks == [Mark(14, 19, "doubt", "8011O", "80110")]
    wide = address(["Englewood, CO 8011O"])
    wide.marks = [at(wide.text, "CO 8011O", "doubt")]
    flag(doc_of(wide), None)
    assert wide.marks == [Mark(11, 14, "doubt", "CO "), Mark(14, 19, "doubt", "8011O", "80110")]


def test_a_suggestion_inside_an_insertion_or_a_deletion_nests_in_it():
    for kind in ("del", "ins"):
        node = address(["Englewood, CO 8011O"])
        node.marks = [at(node.text, "CO 8011O", kind)]
        flag(doc_of(node), None)
        assert str(render.inline(node)) == (
            f'Englewood, <{kind}>CO <span class="ocr-doubt" data-alt="80110" title="OCR is unsure '
            f'of this word. The document suggests: 80110">8011O</span></{kind}>')


def test_a_struck_unread_word_inside_the_token_is_never_a_candidate():
    node = address(["Englewood, CO 8011O"])
    node.marks = [Mark(16, 16, "del-unread", "")]
    assert flag(doc_of(node), None) == 0 and not node.facts


def test_the_pass_is_idempotent():
    node = address(["Englewood, CO 8011O"])
    doc = doc_of(node)
    flag(doc, None)
    marks, facts = list(node.marks), list(node.facts)
    flag(doc, None)
    assert node.marks == marks and node.facts == facts and len(facts) == 1


def test_an_address_set_line_by_line_marks_the_zip_and_keeps_its_breaks():
    node = address(["750 W. Hampden Ave.", "Englewood, CO 8011O"])
    flag(doc_of(node), None)
    html = re.sub(r' data-(?:node|source)="[^"]*"', "", str(render.NodeRenderer({}, {}).render(node)))
    assert html.startswith(
        '<p>750 W. Hampden Ave.<br>Englewood, CO <span class="ocr-doubt" data-alt="80110" '
        'title="OCR is unsure of this word. The document suggests: 80110">8011O</span>')


def test_only_ocr_text_is_flagged():
    node = address(["Englewood, CO 8011O"])
    node.source = Source.HEURISTIC
    assert flag(doc_of(node), None) == 0 and not node.marks


def test_the_structure_pass_runs_it():
    node = address(["Englewood, CO 8011O"])
    doc = doc_of(node)
    structure.finish(SimpleNamespace(doc=doc), Backbone(pages=None, tables={}, trace={},
                                                        routing=None))
    assert [f.code for f in node.facts] == ["ocr-suggestion"]
    assert node.marks == [Mark(14, 19, "doubt", "8011O", "80110")]
