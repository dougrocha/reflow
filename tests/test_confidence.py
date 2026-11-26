"""The confidence scorer, the facts it adds to a document and the bridge from legacy node attributes."""

from __future__ import annotations

import pytest

from reflow.analyze.confidence import CODES, apply, legacy_facts, prior_fact, score
from reflow.analyze.constants import SPLIT_COLUMNS, SPLIT_SOFT, UNHELD_WHY, reading_order_why
from reflow.model import Document, Fact, Node, PageInfo, Role, Source, Why


def doc_of(*nodes: Node, pages: list[PageInfo] | None = None) -> Document:
    return Document(root=Node(Role.DOC, children=list(nodes)),
                    pages=pages or [PageInfo(number=1, width=612, height=792)])


def codes(facts: list[Fact]) -> list[str]:
    return [f.code for f in facts]


def test_prior_is_the_engines_confidence_and_is_recorded_once():
    node = Node(Role.TABLE, page=1, confidence=0.8, source=Source.DOCLING)
    fact = prior_fact(node)
    assert (fact.code, fact.source, fact.data) == ("prior", Source.DOCLING, {"value": 0.8})
    doc = doc_of(node)
    apply(doc)
    apply(doc)
    assert codes(node.facts) == ["prior"]
    assert node.confidence == 0.8


def test_prior_comes_before_the_bridged_facts():
    node = Node(Role.FIGURE, page=1, confidence=0.6, attrs={"needs": "alt"})
    apply(doc_of(node))
    assert codes(node.facts) == ["prior", "alt-missing"]


def test_score_adds_weights_and_clamps():
    prior = Fact("prior", Source.HEURISTIC, data={"value": 0.7})
    s = score([prior, Fact("table-collapsed", Source.HEURISTIC)])
    assert s.value == pytest.approx(0.4)
    assert s.reasons == (CODES["table-collapsed"].label,)
    low = score([Fact("prior", Source.HEURISTIC, data={"value": 0.2}),
                 Fact("table-collapsed", Source.HEURISTIC),
                 Fact("unicode-errors", Source.HEURISTIC)])
    assert low.value == 0.0
    high = score([Fact("prior", Source.HEURISTIC, data={"value": 0.95}),
                  Fact("tag-alt", Source.TAGS), Fact("tag-scope", Source.TAGS)])
    assert high.value == 1.0


def test_score_without_a_prior_starts_at_one():
    assert score([]).value == 1.0
    assert score([Fact("role-disagreement", Source.TAGS)]).value == pytest.approx(0.9)


def test_reasons_are_ordered_by_weight_then_code_and_skip_zero_weights():
    facts = [Fact("prior", Source.HEURISTIC, data={"value": 0.9}),
             Fact("header-guess", Source.HEURISTIC),       # -0.05
             Fact("alt-missing", Source.HEURISTIC),        # 0.0: no reason
             Fact("unicode-errors", Source.HEURISTIC),     # -0.2
             Fact("table-collapsed", Source.HEURISTIC),    # -0.3
             Fact("role-agreement", Source.TAGS)]          # +0.1
    assert score(facts).reasons == tuple(CODES[c].label for c in (
        "table-collapsed", "unicode-errors", "role-agreement", "header-guess"))


def test_page_facts_lower_every_node_on_the_page():
    on_page = Node(Role.P, page=1, confidence=0.9)
    elsewhere = Node(Role.P, page=2, confidence=0.9)
    pages = [PageInfo(number=1, width=612, height=792,
                      facts=[Fact("order-disagreement", Source.DOCLING)]),
             PageInfo(number=2, width=612, height=792)]
    apply(doc_of(on_page, elsewhere, pages=pages))
    assert on_page.confidence == pytest.approx(0.6)
    assert elsewhere.confidence == pytest.approx(0.9)
    assert CODES["order-disagreement"].label in score(on_page.facts, pages[0].facts).reasons


def test_a_page_fact_with_regions_lowers_only_the_nodes_in_them():
    # Redo OCR read only the page's textless bands: its own visible title is not OCR text.
    band = Fact("ocr-page", Source.OCR, data={"reason": "textless-band", "mode": "redo",
                                              "region_boxes": [0, 160, 612, 1008]})
    title = Node(Role.H1, page=1, bbox=(170, 70, 440, 130), confidence=0.6)
    body = Node(Role.P, page=1, bbox=(80, 200, 540, 260), confidence=0.6)
    unplaced = Node(Role.P, page=1, confidence=0.6)
    apply(doc_of(title, body, unplaced, pages=[PageInfo(1, 612, 1008, facts=[band])]))
    assert title.confidence == pytest.approx(0.6)
    assert body.confidence == pytest.approx(0.35) and unplaced.confidence == pytest.approx(0.35)


def test_apply_counts_nodes_below_their_threshold_by_role():
    weak = Node(Role.TABLE, page=1, confidence=0.5)
    fine = Node(Role.TABLE, page=1, confidence=0.9)
    artifact = Node(Role.ARTIFACT, page=1, confidence=0.1)
    assert apply(doc_of(weak, fine, artifact)) == {"table": 1}


def test_unknown_code_raises():
    with pytest.raises(KeyError):
        score([Fact("no-such-code", Source.HEURISTIC)])
    node = Node(Role.P, page=1)
    node.facts.append(Fact("no-such-code", Source.HEURISTIC))
    with pytest.raises(KeyError):
        apply(doc_of(node))


def bridged(role: Role = Role.P, source: Source = Source.HEURISTIC, **attrs) -> list[Fact]:
    return legacy_facts(Node(role, page=1, attrs=attrs, source=source))


def test_needs_alt_and_label():
    assert codes(bridged(Role.FIGURE, needs="alt")) == ["alt-missing"]
    assert codes(bridged(Role.FIELD, needs="label")) == ["label-missing"]
    assert bridged(Role.FIGURE) == []


def test_warn_codes_and_unknown_warn_names_the_value():
    for warn in ("table-collapsed", "heading-fragment", "demoted-heading", "suggested-alt",
                 "suggested-description", "suggested-alt-uncertain"):
        assert codes(bridged(warn=warn)) == [warn]
    with pytest.raises(KeyError, match="mystery"):
        bridged(warn="mystery")


def test_source_of_the_bridged_fact_is_the_nodes():
    assert bridged(Role.FIGURE, Source.DOCLING, needs="alt")[0].source == Source.DOCLING


def test_aligned_table_carries_the_first_why():
    facts = bridged(Role.TABLE, method="aligned", why=["aligned table, 3 x 4", "more"])
    assert [(f.code, f.detail) for f in facts] == [("aligned-table", "aligned table, 3 x 4")]
    assert bridged(Role.P, method="aligned", why=["x"]) == []
    assert bridged(Role.TABLE, method="ruled", why=["ruled table"]) == []


def test_header_guess_only_on_heuristic_tables_with_header_rows():
    assert codes(bridged(Role.TABLE, header_rows=1)) == ["header-guess"]
    assert bridged(Role.TABLE, header_rows=0) == []
    assert bridged(Role.TABLE, Source.DOCLING, header_rows=1) == []


def test_unheld_lines_marker():
    assert codes(bridged(Role.TABLE, why=["ruled table", UNHELD_WHY])) == ["unheld-lines"]


def test_soft_columns_marker_lands_on_the_node():
    assert codes(bridged(why=[reading_order_why(SPLIT_SOFT)])) == ["soft-columns"]
    assert bridged(why=[reading_order_why(SPLIT_COLUMNS)]) == []


def test_a_why_becomes_a_fact_by_its_code_not_by_its_words():
    reworded = [Why("a table left this line out", "unheld-lines"),
                Why("two columns, not sure", "soft-columns")]
    assert codes(bridged(Role.TABLE, why=reworded)) == ["unheld-lines", "soft-columns"]
    assert bridged(why=[str(UNHELD_WHY), str(reading_order_why(SPLIT_SOFT))]) == []


def test_unicode_errors():
    assert codes(bridged(unicode_errors=3)) == ["unicode-errors"]
    assert bridged(unicode_errors=0) == []


def test_bridge_does_not_repeat_a_code_the_ledger_has():
    node = Node(Role.FIGURE, page=1, attrs={"needs": "alt"})
    node.fact("alt-missing", Source.HEURISTIC, "already known")
    assert legacy_facts(node) == []
