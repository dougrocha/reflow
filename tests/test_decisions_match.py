"""Matching a decision to the page again: fail-closed, never a guess."""

from __future__ import annotations

import pytest

from reflow.analyze import decisions as d, decisions_apply as da
from reflow.analyze.anchors import anchor_of, text_sha
from reflow.analyze.decisions_apply import Orphan, Reason, Satisfied, Target, Tier
from reflow.model import Document, Mark, Node, PageInfo, Role, Source

PDF = "a1" * 32
BOX = (72.0, 100.0, 300.0, 112.0)
TEXT = "Englewood, CO 8011O"


def doc_of(*nodes: Node, pages: int = 3) -> Document:
    return Document(root=Node(Role.DOC, children=list(nodes)),
                    pages=[PageInfo(number=n, width=612, height=792) for n in range(1, pages + 1)])


FLAGGED = "8011O"   # the word reflow flagged (a doubt mark), as ocr_words.flag leaves it


def ocr(text=TEXT, box=BOX, page=1, role=Role.P, source=Source.OCR, ident="",
        doubt=True) -> Node:
    node = Node(role, id=ident, page=page, bbox=box, text=text, source=source)
    at = text.find(FLAGGED) if doubt else -1
    while at >= 0:
        node.marks.append(Mark(at, at + len(FLAGGED), "doubt", FLAGGED, "80110"))
        at = text.find(FLAGGED, at + 1)
    return node


def shifted(box, dx=0.0, dy=0.0, dw=0.0, dh=0.0):
    return (box[0] + dx, box[1] + dy, box[2] + dx + dw, box[3] + dy + dh)


def rng(was="8011O", before="CO ", after="") -> d.Range:
    return d.Range(was=was, before=before, after=after, rule="zip-digit")


def correct(source: Node, *, now="80110", **kw) -> d.Correct:
    """A correction made against `source` as it was published."""
    return d.Correct(id="m1-00000001", at="t", pdf=PDF, anchor=anchor_of(source),
                     range=kw.pop("range", rng()), now=now)


def keep(source: Node, **kw) -> d.Keep:
    return d.Keep(id="m1-00000002", at="t", pdf=PDF, anchor=anchor_of(source),
                  range=kw.pop("range", rng()))


# -- tiers -------------------------------------------------------------------------------------


def test_an_unchanged_node_matches_exactly_and_the_range_is_located():
    node = ocr()
    decision = correct(node)
    got = da.match(doc_of(ocr()), decision)
    assert isinstance(got, Target) and got.tier == Tier.EXACT
    assert TEXT[got.start:got.end] == "8011O" and (got.start, got.end) == (14, 19)


def test_a_keep_matches_the_same_way():
    got = da.match(doc_of(ocr()), keep(ocr()))
    assert isinstance(got, Target) and got.tier == Tier.EXACT


def test_the_node_id_is_not_a_shortcut_and_the_hint_is_ignored():
    published = ocr(ident="old-id")
    rebuilt = ocr(ident="new-id")
    assert isinstance(da.match(doc_of(rebuilt), correct(published)), Target)


@pytest.mark.parametrize("dx", [0.0, 2.9, -3.0])
def test_every_edge_within_three_points_is_exact(dx):
    got = da.match(doc_of(ocr(box=shifted(BOX, dx=dx))), correct(ocr()))
    assert isinstance(got, Target) and got.tier == Tier.EXACT


def test_a_box_moved_past_three_points_with_the_same_text_is_moved():
    got = da.match(doc_of(ocr(box=shifted(BOX, dy=4.0))), correct(ocr()))
    assert isinstance(got, Target) and got.tier == Tier.MOVED


def test_a_box_whose_overlap_is_under_half_does_not_match_by_text():
    got = da.match(doc_of(ocr(box=shifted(BOX, dy=8.0))), correct(ocr()))   # IoU 4/20 = 0.2
    assert got == Orphan(Reason.NO_NODE)


def test_changed_text_in_nearly_the_same_box_is_rebased():
    rebuilt = ocr(text="Englewood, CO 8011O 80112")
    got = da.match(doc_of(rebuilt), correct(ocr()))
    assert isinstance(got, Target) and got.tier == Tier.REBASED
    assert rebuilt.text[got.start:got.end] == "8011O"


def test_changed_text_in_a_box_under_the_rebased_overlap_is_no_node():
    rebuilt = ocr(text="Englewood, CO 8011O 80112", box=shifted(BOX, dy=2.0))   # IoU 10/14 = 0.71
    assert da.match(doc_of(rebuilt), correct(ocr())) == Orphan(Reason.NO_NODE)


def test_half_of_a_split_node_is_no_node():
    half = ocr(text="Englewood,", box=(72.0, 100.0, 186.0, 112.0))
    assert da.match(doc_of(half), correct(ocr())) == Orphan(Reason.NO_NODE)


def test_a_merged_node_takes_the_decision_when_it_covers_the_old_box_and_holds_the_range_once():
    merged = ocr(text="Suite 4 " + TEXT, box=(72.0, 99.0, 300.0, 113.0))
    got = da.match(doc_of(merged), correct(ocr()))
    assert isinstance(got, Target) and got.tier == Tier.REBASED
    assert merged.text[got.start:got.end] == "8011O"


def test_the_role_is_not_compared():
    got = da.match(doc_of(ocr(role=Role.H2)), correct(ocr(role=Role.P)))
    assert isinstance(got, Target)


def test_the_page_is_never_relaxed():
    assert da.match(doc_of(ocr(page=3)), correct(ocr(page=1))) == Orphan(Reason.NO_NODE)


# -- ambiguity -----------------------------------------------------------------------------------


def test_two_equal_nodes_in_one_box_are_ambiguous_whatever_the_hint():
    a, b = ocr(ident="hinted"), ocr(ident="other")
    decision = correct(ocr(ident="hinted"))
    assert da.match(doc_of(a, b), decision) == Orphan(Reason.AMBIGUOUS)


def test_two_candidates_in_a_tier_stop_the_search_before_weaker_tiers():
    twin1 = ocr(box=shifted(BOX, dy=4.0))
    twin2 = ocr(box=shifted(BOX, dy=-4.0))   # two moved candidates
    rebased = ocr(text="Englewood, CO 8011O Suite", box=BOX)   # one rebased candidate
    assert da.match(doc_of(twin1, twin2, rebased), correct(ocr())) == Orphan(Reason.AMBIGUOUS)


def test_two_equal_address_blocks_on_a_page_each_match_their_own_box():
    low = shifted(BOX, dy=200.0)
    first, second = ocr(), ocr(box=low)
    doc = doc_of(first, second)
    for source, expected in ((ocr(), first), (ocr(box=low), second)):
        got = da.match(doc, correct(source))
        assert isinstance(got, Target) and got.node is expected


# -- the range -------------------------------------------------------------------------------------


def test_a_range_that_is_not_in_the_text_is_range_missing():
    rebuilt = ocr(text="Englewood, CO 8011X", box=BOX)
    got = da.match(doc_of(rebuilt), correct(ocr(), now="80119", range=rng(was="8011O")))
    assert got == Orphan(Reason.RANGE_MISSING)


def test_a_range_that_occurs_twice_is_range_missing():
    twice = "CO 8011O and CO 8011O"
    got = da.match(doc_of(ocr(text=twice)), correct(ocr(text=twice), range=rng(after="")))
    assert got == Orphan(Reason.RANGE_MISSING)


def test_context_makes_a_repeated_word_one_range():
    twice = "A 8011O and CO 8011O"
    node = ocr(text=twice)
    got = da.match(doc_of(ocr(text=twice)), correct(node, range=rng(before="CO ")))
    assert isinstance(got, Target) and got.start == 15 and twice[got.start:got.end] == "8011O"


def test_the_range_is_located_in_the_raw_text_not_the_normalized_one():
    spaced = "Englewood,  CO   8011O"      # runs of spaces: same text_sha as the single-space text
    assert text_sha(spaced) == text_sha("Englewood, CO 8011O")
    published = ocr(text=spaced)
    decision = correct(published, range=rng(before="CO   "))
    got = da.match(doc_of(ocr(text=spaced)), decision)
    assert isinstance(got, Target) and spaced[got.start:got.end] == "8011O"
    # a range copied with single spaces is not in the raw text: never mapped back
    single = correct(published, range=rng(before="CO "))
    assert da.match(doc_of(ocr(text=spaced)), single) == Orphan(Reason.RANGE_MISSING)


# -- satisfied -----------------------------------------------------------------------------------


def test_a_converter_that_now_reads_the_word_right_is_satisfied_and_changes_nothing():
    fixed = ocr(text="Englewood, CO 80110")
    before = fixed.text
    got = da.match(doc_of(fixed), correct(ocr()))
    assert got == Satisfied(fixed) and fixed.text == before


def test_both_readings_present_is_ambiguous():
    both = ocr(text="CO 8011O and CO 80110")
    got = da.match(doc_of(both), correct(ocr(text="CO 8011O and CO 80110")))
    assert got == Orphan(Reason.AMBIGUOUS)


def test_the_satisfied_node_must_pass_the_box_test():
    far = ocr(text="Englewood, CO 80110", box=shifted(BOX, dy=300.0))
    assert da.match(doc_of(far), correct(ocr())) == Orphan(Reason.NO_NODE)


def test_satisfied_needs_exactly_one_node_with_the_new_reading():
    a, b = ocr(text="Englewood, CO 80110"), ocr(text="Englewood, CO 80110")
    assert da.match(doc_of(a, b), correct(ocr())) == Orphan(Reason.AMBIGUOUS)


def test_a_keep_has_no_satisfied_reading():
    got = da.match(doc_of(ocr(text="Englewood, CO 80110")), keep(ocr()))
    assert got == Orphan(Reason.RANGE_MISSING)


# -- not OCR, invalid now ----------------------------------------------------------------------


def test_a_node_that_is_not_ocr_is_not_ocr():
    born = ocr(source=Source.HEURISTIC)
    assert da.match(doc_of(born), correct(ocr())) == Orphan(Reason.NOT_OCR)


def test_an_invalid_now_is_orphaned_before_any_search():
    assert da.match(doc_of(ocr()), correct(ocr(), now="a\nb")) == Orphan(Reason.INVALID_NOW)


def test_matching_changes_nothing():
    doc = doc_of(ocr(), ocr(text="Suite 2", box=shifted(BOX, dy=40.0)))
    before = doc.to_dict()
    da.match(doc, correct(ocr()))
    assert doc.to_dict() == before


# -- figures -------------------------------------------------------------------------------------


def figure(sha="fedcba9876543210", page=2, box=(10.0, 20.0, 110.0, 220.0)) -> Node:
    attrs = {"crop_sha": sha} if sha else {}
    return Node(Role.FIGURE, page=page, bbox=box, attrs=attrs)


def alt(source: Node) -> d.AltText:
    return d.AltText(id="m1-00000003", at="t", pdf=PDF, anchor=anchor_of(source),
                     text="A map", decorative=False)


def test_an_alt_matches_the_figure_with_its_picture():
    fig = figure()
    got = da.match(doc_of(figure(box=(0.0, 0.0, 50.0, 50.0), sha="1111111111111111"), fig),
                  alt(figure()))
    assert got == da.FigureTarget(fig)


def test_the_box_does_not_matter_for_a_figure():
    moved = figure(box=(300.0, 400.0, 400.0, 500.0))
    assert da.match(doc_of(moved), alt(figure())) == da.FigureTarget(moved)


def test_two_equal_crops_on_a_page_are_ambiguous():
    assert da.match(doc_of(figure(), figure(box=(0.0, 300.0, 10.0, 310.0))),
                   alt(figure())) == Orphan(Reason.AMBIGUOUS)


def test_a_changed_crop_is_figure_changed():
    assert da.match(doc_of(figure(sha="0000000000000000")), alt(figure())) == Orphan(
        Reason.FIGURE_CHANGED)


def test_a_figure_without_a_crop_never_matches():
    assert da.match(doc_of(figure(sha="")), alt(figure())) == Orphan(Reason.FIGURE_CHANGED)


def test_the_same_crop_on_another_page_does_not_match():
    assert da.match(doc_of(figure(page=3)), alt(figure(page=2))) == Orphan(Reason.FIGURE_CHANGED)


# -- only a span reflow flagged can be decided ----------------------------------------------------

def test_a_range_with_no_doubt_mark_is_not_flagged_and_never_applies():
    node = ocr(doubt=False)
    assert da.match(doc_of(node), correct(node)) == Orphan(Reason.NOT_FLAGGED)
    assert da.match(doc_of(node), keep(node)) == Orphan(Reason.NOT_FLAGGED)


def test_a_range_wider_than_the_flagged_span_is_not_flagged():
    node = ocr()
    wide = correct(node, range=rng(was="CO 8011O", before="Englewood, "), now="CO 80110")
    assert da.match(doc_of(node), wide) == Orphan(Reason.NOT_FLAGGED)
