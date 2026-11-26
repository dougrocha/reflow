"""The review queue read from the ledger."""

from __future__ import annotations

import pytest

from reflow.analyze import review
from reflow.model import Document, Fact, Node, PageInfo, Role, Source


def build(*nodes: Node, pages: int = 2, lang: str | None = "en-US", **page_facts) -> Document:
    infos = [PageInfo(number=n, width=612, height=792, facts=page_facts.get(f"p{n}", []))
             for n in range(1, pages + 1)]
    return Document(root=Node(Role.DOC, children=list(nodes)), pages=infos,
                    meta={"lang": lang} if lang else {})


queue = review.queue


def test_figure_without_alt_is_an_error_with_the_old_needs_name():
    fig = Node(Role.FIGURE, page=1, bbox=(10.0, 20.0, 110.0, 220.0), confidence=0.6,
               attrs={"needs": "alt", "alt": None})
    [item] = queue(build(fig))
    assert item["severity"] == "error" and item["needs"] == "alt" and item["warn"] is None
    assert item["label"] == "Figure missing alt text"
    assert item["id"] == fig.id and item["role"] == "figure" and item["page"] == 1
    assert item["bbox"] == [10.0, 20.0, 110.0, 220.0] and item["pageSize"] == [612, 792]
    assert item["alt"] is None and item["description"] is None and item["reasons"] == []


def test_collapsed_table_is_a_warning():
    table = Node(Role.TABLE, page=1, confidence=0.9, attrs={"warn": "table-collapsed"})
    [item] = queue(build(table))
    assert (item["severity"], item["needs"], item["warn"]) == ("warning", None, "table-collapsed")
    assert item["label"] == "Table columns probably collapsed"
    assert item["confidence"] == pytest.approx(0.6)
    assert item["reasons"] == ["Table columns probably collapsed"]


def test_a_shifted_month_grid_is_one_warning_on_its_table():
    from reflow.analyze.layout import analyze
    from calendarpage import month_lines
    from reflow.extract.pages import PageContent
    nodes = analyze([PageContent(1, 612, 792, images=[],
                                 lines=month_lines(2024, 7, 40, 100, shift=1))])
    items = [i for i in queue(build(*nodes, pages=1)) if i["warn"] == "calendar-grid-mismatch"]
    [item] = items
    assert item["role"] == "table" and item["severity"] == "warning"
    assert item["label"] == "Printed month whose dates are not the month's"


def test_a_low_confidence_node_with_no_severity_fact_is_derived():
    para = Node(Role.P, page=1, text="x" * 300, confidence=0.2)
    [item] = queue(build(para))
    assert (item["severity"], item["warn"], item["needs"]) == ("warning", "low-confidence", None)
    assert item["label"] == "Low-confidence structure" and len(item["text"]) == 200
    assert not any(f.code == "low-confidence" for f in para.facts)  # derived, never appended


def test_page_fact_makes_a_page_item_and_a_node_item_only_below_threshold():
    sturdy = Node(Role.P, page=1, text="stays", confidence=0.9)        # 0.9 - 0.3 = 0.6 >= 0.4
    shaky = Node(Role.P, page=1, text="drops", confidence=0.6)         # 0.6 - 0.3 = 0.3 < 0.4
    doc = build(sturdy, shaky,
                p1=[Fact("order-disagreement", Source.DOCLING, "blocks 3-7 first")])
    items = queue(doc)
    assert [i["id"] for i in items] == ["page:1", shaky.id]
    page_item, node_item = items
    assert (page_item["role"], page_item["page"], page_item["bbox"]) == ("page", 1, None)
    assert page_item["pageSize"] == [612, 792] and page_item["severity"] == "warning"
    assert page_item["warn"] == "order-disagreement" and page_item["text"] == "blocks 3-7 first"
    assert node_item["warn"] == "low-confidence"
    assert node_item["reasons"] == ["Reading order disputed by another engine"]


def test_page_error_uses_the_ocr_short_name():
    doc = build(Node(Role.P, page=1, text="ok"), p2=[Fact("ocr-missing", Source.OCR)])
    [item] = queue(doc)
    assert (item["id"], item["severity"], item["needs"]) == ("page:2", "error", "ocr")


def test_document_facts_become_doc_items():
    doc = build(Node(Role.P, page=1, text="ok"))
    doc.root.fact("title-suspect", Source.HEURISTIC, "title is a file name")
    [item] = queue(doc)
    assert (item["role"], item["page"], item["id"]) == ("doc", None, doc.root.id)
    assert (item["severity"], item["needs"], item["bbox"]) == ("error", "title", None)
    assert item["text"] == "title is a file name"


ENGLISH = ("the and of to is are with for that this from which have has were was be it a in "
           "on as by an at or not but all can their there would about more other into than "
           ) * 6


def test_language_item_keeps_its_shape():
    doc = build(Node(Role.P, page=1, text=ENGLISH), lang="es")
    [item] = queue(doc)
    assert (item["role"], item["page"], item["severity"]) == ("doc", None, "warning")
    assert item["warn"] == "language-mismatch" and item["needs"] is None
    assert item["label"] == "Document language contradicts the text"
    assert item["id"] == doc.root.id and item["source"] == "heuristic"
    assert item["confidence"] == 1.0 and item["pageSize"] is None


def test_artifacts_are_skipped_and_a_clean_document_has_no_items():
    doc = build(Node(Role.ARTIFACT, page=1, text="header", confidence=0.1),
                Node(Role.P, page=1, text="fine", confidence=0.9))
    assert queue(doc) == []


def test_order_is_document_items_then_each_page_with_its_nodes_worst_page_first():
    p1 = Node(Role.FIGURE, page=1, attrs={"needs": "alt"})
    p2a = Node(Role.TABLE, page=2, attrs={"warn": "table-collapsed"})
    p2b = Node(Role.P, page=2, text="weak", confidence=0.1)
    doc = build(Node(Role.P, page=1, text=ENGLISH), p1, p2a, p2b, lang="es",
                p2=[Fact("order-disagreement", Source.DOCLING)])
    doc.root.fact("title-suspect", Source.HEURISTIC)
    ids = [i["id"] for i in queue(doc)]
    assert ids == [doc.root.id, doc.root.id, "page:2", p2a.id, p2b.id, p1.id]


def pages_of(doc: Document) -> list[int]:
    """The pages of the queue's paged items, in walk order, each once."""
    return list(dict.fromkeys(i["page"] for i in queue(doc) if i["page"] is not None))


def test_a_low_confidence_page_comes_before_a_clean_warning():
    """A warning that does not lower its node's confidence, and no page fact, on page 1; a
    low-confidence node on page 2. The reviewer opens page 2 first."""
    clean = Node(Role.P, page=1, text="link", confidence=0.9,
                 facts=[Fact("link-unresolved", Source.HEURISTIC)])
    weak = Node(Role.P, page=2, text="weak", confidence=0.2)
    assert [i["id"] for i in queue(build(clean, weak))] == [weak.id, clean.id]


def test_a_page_error_comes_first_then_a_page_warning():
    weakest = Node(Role.P, page=1, text="weakest", confidence=0.05)
    doc = build(weakest, pages=3, p2=[Fact("scan-noisy", Source.OCR)],
                p3=[Fact("scan-noisy", Source.OCR), Fact("ocr-missing", Source.OCR)])
    assert [i["id"] for i in queue(doc)] == ["page:3", "page:2", weakest.id]


def test_among_pages_with_a_warning_the_lowest_node_score_comes_first():
    """A page with no nodes counts as fully trusted."""
    weak = Node(Role.P, page=2, text="weak", confidence=0.6)
    doc = build(weak, p1=[Fact("scan-noisy", Source.OCR)], p2=[Fact("scan-noisy", Source.OCR)])
    assert [i["id"] for i in queue(doc)] == ["page:2", "page:1"]


def test_the_lowest_node_score_counts_nodes_that_make_no_item():
    shaky = Node(Role.TABLE, page=1, text="below 0.55", confidence=0.5)
    lower = Node(Role.P, page=2, text="above 0.4, no item", confidence=0.45)
    fig = Node(Role.FIGURE, page=2, attrs={"needs": "alt"}, confidence=0.9)
    assert pages_of(build(shaky, lower, fig)) == [2, 1]


def test_at_the_same_lowest_score_the_larger_share_of_doubtful_nodes_comes_first():
    one_of_three = [Node(Role.P, page=1, text="weak", confidence=0.2),
                    Node(Role.TABLE, page=1, text="fine"), Node(Role.P, page=1, text="fine")]
    two_of_three = [Node(Role.P, page=2, text="weak", confidence=0.2),
                    Node(Role.H2, page=2, text="weak", confidence=0.3),
                    Node(Role.P, page=2, text="fine")]
    assert pages_of(build(*one_of_three, *two_of_three)) == [2, 1]


def test_equally_trusted_pages_keep_page_order():
    nodes = [Node(Role.P, page=n, text="weak", confidence=0.2) for n in (2, 1, 3)]
    assert pages_of(build(*nodes, pages=3)) == [1, 2, 3]


def test_a_dismissed_page_fact_does_not_rank_its_page():
    doc = build(Node(Role.P, page=1, text="shaky", confidence=0.3),
                Node(Role.P, page=2, text="weak", confidence=0.1),
                p1=[Fact("scan-noisy", Source.OCR)])
    page_item = queue(doc)[0]
    assert page_item["id"] == "page:1"
    doc.meta["decisions"] = {"ledger": {"dismissed": [{"finding": f["id"]} for f in page_item["findings"]]}}
    assert pages_of(doc) == [2, 1]


def test_the_walk_is_read_from_the_model_and_stores_nothing():
    """A stored trust fact would be scored again, so a read leaves the model as it was, and the
    reloaded document (document.json) gives the same walk."""
    doc = build(Node(Role.P, page=1, text="link", confidence=0.9,
                     facts=[Fact("link-unresolved", Source.HEURISTIC)]),
                Node(Role.P, page=2, text="weak", confidence=0.2),
                p1=[Fact("scan-noisy", Source.OCR)])
    first = queue(doc)
    model = doc.to_dict()
    assert review.items(doc) == first and doc.to_dict() == model
    assert review.items(Document.from_dict(model)) == first


def test_items_demands_apply_first():
    with pytest.raises(RuntimeError, match="confidence.apply"):
        review.items(build(Node(Role.P, page=1, text="x")))


def test_summary():
    items = queue(build(
        Node(Role.FIGURE, page=1, attrs={"needs": "alt"}),
        Node(Role.TABLE, page=1, attrs={"warn": "table-collapsed"}),
        Node(Role.P, page=2, text="weak", confidence=0.1)))
    assert review.summary(items) == {"errors": 1, "warnings": 2, "pages": 2}
    assert review.summary([]) == {"errors": 0, "warnings": 0, "pages": 0}


def test_queue_scores_then_reads_the_same_items():
    doc = build(Node(Role.FIGURE, page=1, attrs={"needs": "alt"}))
    items = review.queue(doc)
    assert all(any(f.code == "prior" for f in n.facts) for n in doc.root.children)
    assert items == review.items(doc)


def test_repeated_low_confidence_items_collapse_per_page_and_reason():
    first = Node(Role.P, page=1, text="first", confidence=0.3)
    second = Node(Role.H2, page=1, text="second", confidence=0.2)
    other_page = Node(Role.P, page=2, text="third", confidence=0.2)
    other_reason = Node(Role.P, page=1, text="different", confidence=0.2,
                        facts=[Fact("untagged", Source.TAGS)])
    fig = Node(Role.FIGURE, page=1, attrs={"needs": "alt"})
    doc = build(first, second, other_page, other_reason, fig)
    items = queue(doc)
    assert [i["id"] for i in items] == [first.id, other_reason.id, fig.id, other_page.id]
    grouped = items[0]
    assert grouped["group"] == {"nodeIds": [first.id, second.id]}
    assert grouped["text"] == "2 blocks on this page. first"
    assert grouped["confidence"] == 0.2
    assert grouped["warn"] == "low-confidence"
    assert "group" not in items[-1]
    assert review.items(doc) == items == queue(doc)


def test_recovered_image_text_collapses_without_hiding_independent_errors():
    nodes = [Node(Role.P, page=1, text=f"line {n}", confidence=0.3,
                  facts=[Fact("ocr-image-text", Source.OCR)]) for n in range(4)]
    fig = Node(Role.FIGURE, page=1, attrs={"needs": "alt"})
    items = queue(build(*nodes, fig))
    assert len(items) == 2
    assert items[0]["group"] == {"nodeIds": [n.id for n in nodes]}
    assert items[0]["text"].startswith("4 blocks on this page.")
    assert items[1]["needs"] == "alt"


def test_lost_characters_collapse_to_one_item_per_page():
    nodes = [Node(Role.P, page=1, text=f"line {n}", attrs={"unicode_errors": 2})
             for n in range(3)]
    elsewhere = Node(Role.P, page=2, text="other page", attrs={"unicode_errors": 1})
    items = queue(build(*nodes, elsewhere))
    assert [i["warn"] for i in items] == ["unicode-errors", "unicode-errors"]
    assert items[0]["group"] == {"nodeIds": [n.id for n in nodes]}
    assert items[0]["text"].startswith("3 blocks on this page.")
    assert "group" not in items[1]


def test_repeated_errors_remain_individually_reviewable():
    nodes = [Node(Role.FIELD, page=1, text=f"field {n}",
                  facts=[Fact("ocr-image-text", Source.OCR),
                         Fact("label-missing", Source.HEURISTIC)]) for n in range(2)]
    items = queue(build(*nodes))
    assert [i["id"] for i in items] == [node.id for node in nodes]
    assert all(i["severity"] == "error" and "group" not in i for i in items)


def test_docling_order_disagreement_alone_makes_no_item_but_corroborates():
    """A resnet-p1-style Docling misordering of a two-column page: no page item, and a node
    is queued only when other doubt already has it near its threshold."""
    left = Node(Role.P, page=1, text="left column", bbox=(50.0, 250.0, 287.0, 420.0),
                confidence=0.9)
    right = Node(Role.P, page=1, text="right column", bbox=(309.0, 360.0, 545.0, 490.0),
                 confidence=0.9)
    shaky = Node(Role.P, page=1, text="doubtful", bbox=(50.0, 430.0, 287.0, 500.0),
                 confidence=0.45)
    fact = Fact("order-disagreement-docling", Source.DOCLING, "tau 0.38", {"tau": 0.38})
    items = queue(build(left, right, shaky, p1=[fact]))
    assert [i["id"] for i in items] == [shaky.id]
    assert items[0]["warn"] == "low-confidence"
    assert not any(i["role"] == "page" for i in items)
    assert left.confidence == pytest.approx(0.8) and right.confidence == pytest.approx(0.8)


def test_an_ocr_suggestion_item_shows_the_reading_the_suggestion_the_rule_and_the_box():
    from reflow.analyze.ocr_words import flag
    text = "Englewood, CO 8011O"
    node = Node(Role.P, page=1, bbox=(72.0, 100.0, 300.0, 112.0), text=text, source=Source.OCR)
    doc = build(node)
    flag(doc, None)
    [item] = queue(doc)
    assert item["severity"] == "warning" and item["label"] == "OCR reading may be wrong"
    assert item["text"] == text
    [suggestion] = item["suggestions"]
    assert {k: suggestion[k] for k in ("was", "now", "rule", "bbox")} == {
        "was": "8011O", "now": "80110", "rule": "zip-digit", "bbox": [72.0, 100.0, 300.0, 112.0]}
    assert set(suggestion) == {"was", "now", "rule", "bbox", "anchor", "before", "after", "id"}


def test_an_ocr_suggestion_on_image_text_is_its_own_item_with_its_own_label():
    from reflow.analyze.ocr_words import flag
    nodes = [Node(Role.P, page=1, bbox=(72.0, top, 300.0, top + 12), text=text, source=Source.OCR)
             for top, text in ((100.0, "Englewood, CO 8011O"), (130.0, "Suite 225"),
                               (160.0, "Colorado Community Media"))]
    for node in nodes:
        node.fact("ocr-image-text", Source.OCR, "Text recovered from a text-heavy image")
    doc = build(*nodes)
    flag(doc, None)
    items = queue(doc)
    [item] = [i for i in items if i.get("suggestions")]
    assert item["id"] == nodes[0].id and item["warn"] == "ocr-suggestion"
    assert item["label"] == "OCR reading may be wrong" and "group" not in item
    assert [i["group"]["nodeIds"] for i in items if "group" in i] == [[n.id for n in nodes[1:]]]


# Review targets: every fact's boxes, tagged with its code.

A, B, C = [10.0, 20.0, 110.0, 40.0], [50.0, 300.0, 250.0, 330.0], [400.0, 600.0, 500.0, 620.0]


def published(doc: Document) -> list[dict]:
    """The queue as review.json publishes it."""
    import json
    return json.loads(json.dumps(queue(doc)))


def test_a_page_fact_s_boxes_are_the_item_s_targets_and_their_union_its_bbox():
    doc = build(p1=[Fact("ocr-band-missing", Source.HEURISTIC, data={"region_boxes": A + B})])
    [item] = published(doc)
    assert item["targets"] == [{"code": "ocr-band-missing", "box": A},
                               {"code": "ocr-band-missing", "box": B}]
    assert item["boxes"] == [A, B] and item["bbox"] == [10.0, 20.0, 250.0, 330.0]


def test_a_page_error_and_a_page_warning_keep_both_geometries():
    doc = build(p1=[Fact("scan-noisy", Source.HEURISTIC, data={"region_boxes": A}),
                    Fact("empty-transcript", Source.HEURISTIC, data={"region_boxes": B + C})])
    [item] = published(doc)
    assert (item["severity"], item["label"]) == ("error", review.CODES["empty-transcript"].label)
    assert item["targets"] == [{"code": "scan-noisy", "box": A},
                               {"code": "empty-transcript", "box": B},
                               {"code": "empty-transcript", "box": C}]
    assert item["bbox"] == [10.0, 20.0, 500.0, 620.0]


def test_two_warning_facts_on_one_page_keep_both_targets():
    doc = build(p1=[Fact("ocr-band-missing", Source.HEURISTIC, data={"region_boxes": A}),
                    Fact("scan-noisy", Source.HEURISTIC, data={"region_boxes": B})])
    [item] = published(doc)
    assert [(t["code"], t["box"]) for t in item["targets"]] == [
        ("ocr-band-missing", A), ("scan-noisy", B)]


def test_a_node_item_takes_targets_from_all_its_facts_and_keeps_its_box():
    node = Node(Role.P, page=1, text="x", bbox=(5.0, 5.0, 600.0, 700.0), confidence=0.9,
                facts=[Fact("ocr-image-text", Source.OCR, data={"box": A}),
                       Fact("unicode-errors", Source.HEURISTIC, data={"region_boxes": B + C})])
    [item] = published(build(node))
    assert item["bbox"] == [5.0, 5.0, 600.0, 700.0]
    assert item["targets"] == [{"code": "ocr-image-text", "box": A},
                               {"code": "unicode-errors", "box": B},
                               {"code": "unicode-errors", "box": C}]
    assert review.item_for(node, [612, 792], node.facts[0], [])["targets"] == item["targets"]


def test_a_collapsed_group_holds_every_member_s_targets():
    nodes = [Node(Role.P, page=1, text=f"line {n}", confidence=0.3,
                  facts=[Fact("ocr-image-text", Source.OCR, data={"box": box})])
             for n, box in enumerate((A, B))]
    [item] = published(build(*nodes))
    assert item["group"] == {"nodeIds": [n.id for n in nodes]}
    assert item["targets"] == [{"code": "ocr-image-text", "box": A},
                               {"code": "ocr-image-text", "box": B}]


def test_a_fact_without_boxes_has_no_targets_and_the_old_bbox():
    fig = Node(Role.FIGURE, page=1, bbox=(10.0, 20.0, 110.0, 220.0), attrs={"needs": "alt"})
    doc = build(fig, p2=[Fact("ocr-missing", Source.OCR)])
    page_item, node_item = published(doc)   # page 2's error page item, then page 1
    assert (node_item["targets"], node_item["bbox"]) == ([], [10.0, 20.0, 110.0, 220.0])
    assert (page_item["targets"], page_item["boxes"], page_item["bbox"]) == ([], [], None)


def test_a_box_list_that_is_not_four_numbers_each_is_a_corrupt_ledger():
    with pytest.raises(ValueError):
        review.targets_of([Fact("ocr-band-missing", Source.HEURISTIC,
                                data={"region_boxes": [1.0, 2.0, 3.0]})])
    with pytest.raises(ValueError):
        review.targets_of([Fact("ocr-image-text", Source.OCR, data={"box": [1.0, 2.0]})])


def _unread(page, box, code):
    fig = Node(Role.FIGURE, page=page, bbox=box, attrs={"unread": True, "alt": "a note"})
    fig.fact(code, Source.HEURISTIC, "", box=list(box))
    return fig


def test_unread_figures_give_one_error_per_page_and_code_with_every_crop():
    a = _unread(1, (10.0, 10.0, 100.0, 50.0), "unread-text")
    b = _unread(1, (10.0, 300.0, 200.0, 350.0), "unread-text")
    c = _unread(1, (300.0, 300.0, 400.0, 350.0), "unread-handwriting")
    other = _unread(2, (10.0, 10.0, 100.0, 50.0), "unread-text")
    items = queue(build(a, b, c, other))
    assert [(i["page"], i["label"], i["severity"]) for i in items] == [
        (1, "Writing OCR could not read", "error"), (1, "Handwriting not transcribed", "error"),
        (2, "Writing OCR could not read", "error")]
    grouped = items[0]
    assert grouped["id"] == a.id and grouped["bbox"] == list(a.bbox)
    assert grouped["group"] == {"nodeIds": [a.id, b.id], "boxes": [list(a.bbox), list(b.bbox)]}
    assert grouped["targets"] == [{"code": "unread-text", "box": list(a.bbox)},
                                  {"code": "unread-text", "box": list(b.bbox)}]
    assert "group" not in items[1] and "group" not in items[2]


def test_each_signature_is_a_warning_of_its_own():
    a = _unread(1, (10.0, 10.0, 100.0, 50.0), "unread-signature")
    b = _unread(1, (300.0, 10.0, 400.0, 50.0), "unread-signature")
    items = queue(build(a, b))
    assert [(i["id"], i["label"], i["severity"]) for i in items] == [
        (a.id, "Signature not transcribed", "warning"), (b.id, "Signature not transcribed", "warning")]
    assert not any("group" in i for i in items)



# Anchors, finding ids and suggestion ids: what a reviewer's decision copies.


def test_a_node_item_lists_one_finding_per_severe_fact_with_a_stable_id():
    from reflow.analyze.anchors import finding_id, node_scope
    node = Node(Role.P, page=1, bbox=(10.0, 20.0, 110.0, 40.0), text="Some text",
                facts=[Fact("ocr-image-text", Source.OCR, "from an image", {"box": A}),
                       Fact("unicode-errors", Source.OCR, "lost", {"box": B})])
    [item] = published(build(node))
    scope = node_scope(node)
    assert item["findings"] == [
        {"code": "ocr-image-text", "severity": "warning",
         "id": finding_id(scope, "ocr-image-text", "from an image", {"box": A})},
        {"code": "unicode-errors", "severity": "warning",
         "id": finding_id(scope, "unicode-errors", "lost", {"box": B})}]
    [again] = published(build(Node(Role.P, page=1, bbox=(10.0, 20.0, 110.0, 40.0),
                                   text="Some text", facts=node.facts)))
    assert again["findings"] == item["findings"]   # the id follows the evidence, not the node id


def test_a_finding_id_changes_when_the_text_or_the_evidence_does():
    def ids(text, detail):
        node = Node(Role.P, page=1, bbox=(10.0, 20.0, 110.0, 40.0), text=text,
                    facts=[Fact("unicode-errors", Source.OCR, detail, {"box": A})])
        return [f["id"] for f in published(build(node))[0]["findings"]]
    assert ids("one", "x") != ids("two", "x") and ids("one", "x") != ids("one", "y")


def test_a_low_confidence_item_has_a_low_confidence_finding():
    para = Node(Role.P, page=1, bbox=(10.0, 20.0, 110.0, 40.0), text="weak", confidence=0.2)
    [item] = queue(build(para))
    [finding] = item["findings"]
    assert finding["code"] == "low-confidence" and finding["severity"] == "warning"
    assert len(finding["id"]) == 16


def test_page_and_document_items_and_the_language_item_have_findings():
    doc = build(Node(Role.P, page=1, text=ENGLISH), lang="es", p2=[Fact("ocr-missing", Source.OCR)])
    doc.root.fact("title-suspect", Source.HEURISTIC)
    by_role = {}
    for item in queue(doc):
        by_role.setdefault(item["role"], []).append(item)
    assert [f["code"] for i in by_role["doc"] for f in i["findings"]] == [
        "title-suspect", "language-mismatch"]
    assert [f["code"] for f in by_role["page"][0]["findings"]] == ["ocr-missing"]
    assert all(len(f["id"]) == 16 for i in queue(doc) for f in i["findings"])


def test_a_group_item_lists_every_members_findings():
    nodes = [Node(Role.P, page=1, bbox=(10.0, 20.0 + 30 * n, 110.0, 40.0 + 30 * n),
                  text=f"line {n}", confidence=0.3) for n in range(3)]
    [item] = queue(build(*nodes))
    assert len(item["group"]["nodeIds"]) == 3
    assert [f["code"] for f in item["findings"]] == ["low-confidence"] * 3
    assert len({f["id"] for f in item["findings"]}) == 3


def test_node_and_figure_items_carry_an_anchor_and_page_items_do_not():
    from reflow.analyze.anchors import anchor_of
    para = Node(Role.P, page=1, bbox=(10.04, 20.0, 110.0, 40.0), text="weak", confidence=0.2)
    fig = Node(Role.FIGURE, page=1, bbox=(10.0, 60.0, 110.0, 160.0),
               attrs={"needs": "alt", "crop_sha": "fedcba9876543210"})
    items = queue(build(para, fig, p2=[Fact("ocr-missing", Source.OCR)]))
    by_id = {i["id"]: i for i in items}
    assert by_id[para.id]["anchor"] == anchor_of(para).to_dict()
    assert by_id[para.id]["anchor"]["bbox"] == [10.0, 20.0, 110.0, 40.0]
    assert by_id[fig.id]["anchor"]["crop_sha"] == "fedcba9876543210"
    assert by_id[fig.id]["anchor"]["text_sha"] == "" and by_id[fig.id]["anchor"]["hint"] == fig.id
    assert "anchor" not in by_id["page:2"]


def test_a_node_without_a_box_has_no_anchor():
    [item] = queue(build(Node(Role.P, page=1, text="weak", confidence=0.2)))
    assert "anchor" not in item


def test_a_suggestion_carries_its_anchor_its_context_and_an_id():
    import hashlib
    from reflow.analyze.anchors import anchor_of
    from reflow.analyze.ocr_words import flag
    text = "Our main office is at 100 Main Street, Englewood, CO 8011O"
    node = Node(Role.P, page=1, bbox=(72.0, 100.0, 300.0, 112.0), text=text, source=Source.OCR)
    doc = build(node)
    flag(doc, None)
    [item] = queue(doc)
    [s] = item["suggestions"]
    start = text.index("8011O")
    assert s["before"] == text[start - 20:start] and s["after"] == ""
    assert len(s["before"]) == 20
    assert s["anchor"] == anchor_of(node).to_dict() == item["anchor"]
    key = "|".join([s["was"], s["now"], s["rule"], s["before"], s["after"]])
    assert s["id"] == hashlib.sha256(key.encode()).hexdigest()[:16]
    assert s["was"] == "8011O" and s["now"] == "80110"


def test_suggestion_context_stops_at_the_ends_of_the_text():
    from reflow.analyze.ocr_words import flag
    node = Node(Role.P, page=1, bbox=(72.0, 100.0, 300.0, 112.0), text="Englewood, CO 8011O",
                source=Source.OCR)
    doc = build(node)
    flag(doc, None)
    [s] = queue(doc)[0]["suggestions"]
    assert (s["before"], s["after"]) == ("Englewood, CO ", "")


def test_the_findings_of_a_suggestion_item_include_its_suggestion_fact():
    from reflow.analyze.ocr_words import flag
    node = Node(Role.P, page=1, bbox=(72.0, 100.0, 300.0, 112.0), text="Englewood, CO 8011O",
                source=Source.OCR)
    doc = build(node)
    flag(doc, None)
    [item] = queue(doc)
    assert "ocr-suggestion" in [f["code"] for f in item["findings"]]
