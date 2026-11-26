"""Reading-order agreement with the tag tree."""

from reflow.analyze import order_evidence as oe
from reflow.model import Document, Node, PageInfo, Role, Source
from reflow.run import RunState


def block(i, text=None):
    top = 10.0 + 20 * i
    return oe.Block((10.0, top, 100.0, top + 10), tuple(oe.words(text or f"word{i} alpha{i} beta{i}")))


def test_words_normalizes():
    assert oe.words("FIne, Ünï-code") == ["fine", "ünï", "code"]


def test_same_order_agrees():
    blocks = [block(i) for i in range(8)]
    verdict = oe.compare_page(1, Source.TAGS, blocks, blocks)
    assert verdict and verdict.tau == 1.0 and not verdict.disagrees and verdict.matched == 8


def test_swapped_regions_disagree():
    blocks = [block(i) for i in range(8)]
    witness = blocks[4:] + blocks[:4]
    verdict = oe.compare_page(1, Source.TAGS, blocks, witness)
    assert verdict and verdict.disagrees and verdict.tau < 0.5


def test_reversed_is_minus_one():
    blocks = [block(i) for i in range(8)]
    assert oe.compare_page(1, Source.TAGS, blocks, blocks[::-1]).tau == -1.0


def test_too_few_matches_has_no_verdict():
    blocks = [block(i) for i in range(oe.MIN_MATCHED - 1)]
    assert oe.compare_page(1, Source.TAGS, blocks, blocks) is None


def test_order_inside_one_witness_block_follows_its_text():
    # one tagged paragraph holds six boxes; the text order inside it is the witness order
    texts = [f"option{i} label{i} extra{i}" for i in range(6)]
    big = oe.Block((0.0, 0.0, 100.0, 70.0), tuple(w for t in texts for w in oe.words(t)))
    boxes = [oe.Block((0.0, 10.0 * i, 100.0, 10.0 * i + 9), tuple(oe.words(t)))
             for i, t in enumerate(texts)]
    assert oe.compare_page(1, Source.TAGS, boxes, [big]).tau == 1.0
    assert oe.compare_page(1, Source.TAGS, boxes[::-1], [big]).tau == -1.0


def test_all_tied_is_none():
    assert oe.kendall_tau_b([(0, None)] * 4) is None


def test_node_blocks_open_wrappers_and_skip_artifacts():
    def p(i):
        return Node(Role.P, text=f"para{i} one two", page=1, bbox=(10.0, 20.0 * i, 90.0, 20.0 * i + 10))
    lst = Node(Role.LIST, children=[Node(Role.LI, text="item one two", page=1,
                                         bbox=(10.0, 100.0, 90.0, 110.0))])
    art = Node(Role.ARTIFACT, text="running head", page=1, bbox=(0.0, 0.0, 50.0, 8.0))
    got = oe.node_blocks([p(1), art, lst, p(2)])
    assert [b.words[0] for b in got[1]] == ["para1", "item", "para2"]


def test_flag_adds_one_fact_per_page_and_source():
    doc = Document(root=Node(Role.DOC), pages=[PageInfo(number=1, width=100, height=100)])
    bad = oe.OrderAgreement(1, Source.TAGS, 0.2, 9, 10)
    good = oe.OrderAgreement(1, Source.DOCLING, 1.0, 9, 10)
    assert oe.flag(doc, [bad, good]) == [bad]
    assert oe.flag(doc, [bad]) == []
    facts = doc.pages[0].facts
    assert [(f.code, f.source) for f in facts] == [("order-disagreement", Source.TAGS)]
    assert facts[0].data["tau"] == 0.2


def run_with(tmp_path, monkeypatch, exc=None):
    from types import SimpleNamespace

    import reflow.extract.tags as tags

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(tags, "read_tags", boom)
    doc = Document(root=Node(Role.DOC), pages=[PageInfo(number=1, width=100, height=100)])
    workers = SimpleNamespace(jobs=1, pool=None)
    return SimpleNamespace(doc=doc, working=tmp_path / "w.pdf", workers=workers, state=RunState(),
                           options=SimpleNamespace(stage_cache=None))


def test_failing_tag_read_does_not_break_apply(tmp_path, monkeypatch, caplog):
    run = run_with(tmp_path, monkeypatch, RuntimeError("worker pool died"))
    assert oe.apply(run) == []
    fact = run.doc.root.facts[0]
    assert fact.code == "order-unchecked" and "worker pool died" in fact.detail
    assert "tag tree unreadable" in caplog.text


def wide(text, top=0.0, bottom=1000.0):
    return oe.Block((0.0, top, 100.0, bottom), tuple(oe.words(text)))


def test_broad_tag_block_over_many_blocks():
    # one /P over everything: with text offsets the real order is compared; without any, no verdict
    blocks = [block(i) for i in range(12)]
    broad = oe.Block((0.0, 0.0, 100.0, 300.0), tuple(w for b in blocks for w in b.words))
    shuffled = blocks[6:] + blocks[:6]
    for order in (blocks, shuffled):
        got = oe.compare_page(1, Source.TAGS, order, [broad])
        assert got is not None and got.matched == 12
    assert oe.compare_page(1, Source.TAGS, shuffled, [broad]).disagrees
    same = [oe.Block(b.bbox, ("unrelated", "text", str(i))) for i, b in enumerate(blocks)]
    assert oe.compare_page(1, Source.TAGS, same, [broad]) is None


def test_repeated_text_in_one_witness_block_collapses():
    texts = ["same words here"] * 8
    boxes = [oe.Block((0.0, 10.0 * i, 100.0, 10.0 * i + 9), tuple(oe.words(t)))
             for i, t in enumerate(texts)]
    big = oe.Block((0.0, 0.0, 100.0, 80.0), tuple(oe.words(" ".join(texts))))
    assert oe.compare_page(1, Source.TAGS, boxes, [big]) is None


def test_duplicate_geometry_counts_once():
    dup = [block(i) for i in range(3) for _ in range(4)]
    witness = [block(i) for i in range(3)]
    assert oe.compare_page(1, Source.TAGS, dup, witness) is None


def test_many_blocks_one_distinct_anchor_cannot_agree_falsely():
    blocks = [block(i) for i in range(10)]
    broad = oe.Block((0.0, 0.0, 100.0, 300.0), ("unrelated",))
    assert oe.compare_page(1, Source.TAGS, blocks, [broad]) is None


def test_reordered_page_with_distinct_blocks_is_flagged_even_with_extra_blocks():
    blocks = [block(i) for i in range(10)]
    witness = blocks[5:] + blocks[:5]
    verdict = oe.compare_page(1, Source.TAGS, blocks, witness)
    assert verdict and verdict.disagrees and verdict.matched == 10


# -- notes tagged out of the flow --------------------------------------------------------


def _el(tag, i, children=(), text=None, box=None):
    from reflow.extract.tags import TagElement
    top = 10.0 + 20 * i
    return TagElement(tag, 1, box or (10.0, top, 100.0, top + 10),
                      text or f"word{i} alpha{i} beta{i}", 1, children=list(children))


def _page(elements):
    from reflow.extract.tags import TagPage
    return TagPage(1, elements, 1.0)


def test_note_tagged_after_the_flow_is_an_aside():
    # six paragraphs in one /Sect; the note printed between #2 and #3 is tagged last, in a
    # /Sect of its own: the tag tree's position for it is a choice, not evidence
    body = [_el("/P", i) for i in (0, 1, 3, 4, 5, 6)]
    note = _el("/P", 2)
    main = _el("/Sect", 0, body, box=(10.0, 10.0, 100.0, 140.0))
    doc = _el("/Document", 0, [main, _el("/Sect", 2, [note])], box=(10.0, 10.0, 100.0, 140.0))
    tags = oe.tag_blocks(_page([doc]))
    assert [b.aside for b in tags] == [False] * 6 + [True]

    heuristic = [block(i) for i in range(7)]   # printed order: the note sits at #2
    verdict = oe.compare_page(1, Source.TAGS, heuristic, tags)
    assert verdict and verdict.tau == 1.0 and not verdict.disagrees
    # without the aside rule the same page is flagged
    plain = [oe.Block(b.bbox, b.words) for b in tags]
    assert oe.compare_page(1, Source.TAGS, heuristic, plain).disagrees


def test_second_section_outside_the_first_is_not_an_aside():
    first = _el("/Sect", 0, [_el("/P", i) for i in range(3)], box=(10.0, 10.0, 100.0, 70.0))
    second = _el("/Sect", 5, [_el("/P", 5)])
    doc = _el("/Document", 0, [first, second], box=(10.0, 10.0, 100.0, 120.0))
    assert [b.aside for b in oe.tag_blocks(_page([doc]))] == [False] * 4


def test_displaced_block_inside_the_flow_still_disagrees():
    # one block read out of place inside a single /Sect is a real disagreement
    order = [0, 1, 4, 2, 3, 5, 6, 7]
    main = _el("/Sect", 0, [_el("/P", i) for i in order], box=(10.0, 10.0, 100.0, 180.0))
    tags = oe.tag_blocks(_page([main]))
    assert not any(b.aside for b in tags)
    verdict = oe.compare_page(1, Source.TAGS, [block(i) for i in range(8)], tags)
    assert verdict and verdict.disagrees


def _docling_run(n_pages=1, tags=None):
    from types import SimpleNamespace
    doc = Document(root=Node(Role.DOC), pages=[PageInfo(number=i + 1, width=100, height=100)
                                               for i in range(n_pages)])
    paras = [Node(Role.P, text=f"word{i} alpha{i} beta{i}", page=1,
                  bbox=(10.0, 20.0 * i, 90.0, 20.0 * i + 10)) for i in range(8)]
    doc.root.children = paras
    run = SimpleNamespace(doc=doc, state=RunState(tags=tags or {}))
    return run, paras


def test_apply_compares_docling_when_untagged():
    run, paras = _docling_run()
    got = oe.apply(run, docling=paras[4:] + paras[:4])
    assert [v.source for v in got] == [Source.DOCLING]
    assert [f.code for f in run.doc.pages[0].facts] == ["order-disagreement-docling"]
    run2, paras2 = _docling_run()
    assert oe.apply(run2, docling=paras2)[0].tau == 1.0
    assert run2.doc.pages[0].facts == []


def test_apply_tags_win_over_docling(monkeypatch):
    run, paras = _docling_run(tags={1: object()})
    monkeypatch.setattr(oe, "compare_tags", lambda doc, tags: [])
    called = []
    monkeypatch.setattr(oe, "compare_docling", lambda *a: called.append(1) or [])
    oe.apply(run, docling=paras)
    assert not called


def test_apply_docling_failure_and_page_cap(monkeypatch):
    run, paras = _docling_run()

    def boom(*a):
        raise RuntimeError("bad node")

    monkeypatch.setattr(oe, "compare_docling", boom)
    assert oe.apply(run, docling=paras) == []
    monkeypatch.undo()
    big, paras = _docling_run(n_pages=301)
    assert oe.apply(big, docling=paras[4:] + paras[:4]) == []
    assert big.doc.pages[0].facts == []
