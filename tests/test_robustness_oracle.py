"""The robustness harness's own logic (tests/robustness), without converting anything: the
registry, the order-aware oracle, the flag rule, and the checks that make a mutation invalid."""

from __future__ import annotations

import random
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from robustness import harness
from robustness.harness import Run
from robustness.kinds import (
    ALL,
    KINDS,
    Damage,
    DamageKind,
    Pixels,
    Unsupported,
    flagged,
    repaired,
    same,
    save,
)
from robustness.oracle import Fact, Outcome, PageView, Projection, differences, flag_found, judge

KIND = {k.id: k for k in KINDS}


def _text(path: Path, page: int = 0) -> str:
    doc = pdfium.PdfDocument(str(path))
    try:
        return doc[page].get_textpage().get_text_range()
    finally:
        doc.close()


def test_every_kind_of_the_issue_is_registered():
    assert set(KIND) == {
        "tounicode-missing", "tounicode-shifted", "tounicode-pua", "dropped-spaces",
        "broken-ligature", "outlines", "rasterized", "one-glyph-per-tj", "shuffled-objects",
        "rotated", "offset-cropbox", "white-text", "tiny-text", "hidden-text", "doubled-text",
        "tags-deleted", "tags-wrong", "all-artifact", "wrong-lang", "forms-flattened",
        "text-in-annotation"}   # #310: the partial loss words-missing catches
    assert len(KINDS) == len(KIND)


def test_shifted_tounicode_is_flagged_by_the_font_repair_on_every_base():
    kind = KIND["tounicode-shifted"]
    assert kind.enforced == frozenset(harness.BASE_BY_NAME)
    assert kind.expect == flagged("font-map") and kind.target == repaired("font-map")
    # the witness's own fact still may not be raised by a clean control
    from robustness.kinds import WATCHED_CODES
    assert "text-disputed" in WATCHED_CODES and "text-disputed" in harness._watched()


def test_a_flattened_form_is_flagged_on_the_bases_that_have_a_form():
    kind = KIND["forms-flattened"]
    assert kind.expect == flagged("form-flattened") and kind.target is None
    assert kind.enforced == frozenset({"fw9", "i9"})


def test_every_kind_is_enforced_where_it_passes():
    # #311: every cell that passed two full runs is enforced; #342 enforced the last three.
    assert {k.id for k in KINDS if not k.enforced} == set()
    assert {k.id for k in KINDS if k.enforced == ALL} >= {"outlines", "rasterized", "all-artifact",
                                                         "tounicode-pua"}
    assert KIND["tounicode-pua"].expect == flagged("unicode-errors", "ocr-page")
    assert str(KIND["tounicode-pua"].expect) == "flagged(unicode-errors or ocr-page)"
    assert KIND["broken-ligature"].enforced == frozenset({"prose", "scan"})
    assert "fw9" in KIND["text-in-annotation"].enforced
    assert not any(k.unenforced for k in KINDS)
    assert KIND["tounicode-missing"].enforced == frozenset({"arxiv-p1-2", "i9"})
    assert KIND["shuffled-objects"].enforced == frozenset({"prose", "tagged", "scan"})
    for kind in ("tags-deleted", "tags-wrong"):
        assert KIND[kind].enforced == frozenset({"tagged", "fw9", "i9"})
    bases = set(harness.BASE_BY_NAME)
    assert all(k.enforced - ALL <= bases for k in KINDS)   # ALL: every base


def test_a_reason_names_a_base_the_kind_does_not_enforce():
    bases = set(harness.BASE_BY_NAME)
    for k in KINDS:
        assert set(k.unenforced) <= bases | {"*"}, k.id
        assert not any(k.enforced_on(b) for b in k.unenforced if b != "*"), k.id
        assert all(why and not why.endswith(".") for why in k.unenforced.values()), k.id
    assert KIND["broken-ligature"].why_unenforced("prose") == ""   # enforced: no reason
    assert KIND["broken-ligature"].why_unenforced("tagged") == ""  # no reason written for it


def _record(cells: list[dict]) -> dict:
    """A run.json with only what the scorecard reads."""
    tools = dict.fromkeys(("reflow", "pikepdf", "qpdf", "pypdfium2", "pdfplumber", "qpdf-cli",
                           "gs", "ocr"), "x")
    return {"started": "s", "ended": "e", "seed": 1, "tools": tools,
            "bases": {"prose": {}, "scan": {}},
            "kinds": {k.id: {"expect": str(k.expect), "target": None} for k in KINDS},
            "cells": [{"met": c["outcome"] in ("preserved", "caught"), "detail": "",
                       "why_unenforced": "", **c} for c in cells]}


def test_the_scorecard_says_why_each_open_cell_is_not_enforced():
    record = _record([
        {"base": "prose", "kind": "clean", "outcome": "preserved", "enforced": False},
        {"base": "prose", "kind": "tounicode-pua", "outcome": "silent loss", "enforced": False,
         "why_unenforced": "no letters to vote on"},
        {"base": "scan", "kind": "tounicode-pua", "outcome": "silent loss", "enforced": False,
         "why_unenforced": "no letters to vote on"},
        {"base": "scan", "kind": "tags-wrong", "outcome": "unsupported", "enforced": False},
        {"base": "prose", "kind": "rotated", "outcome": "preserved", "enforced": True},
        {"base": "scan", "kind": "rotated", "outcome": "preserved", "enforced": False},
    ])
    assert harness.unexplained(record) == ["rotated on scan"]
    text = harness.scorecard(record)
    assert ("- `tounicode-pua` on prose (LOSS), scan (LOSS): no letters to vote on." in text)
    assert "- `rotated` on scan (ok): no reason written" in text
    assert "clean on" not in text and "`tags-wrong` on scan (n/a)" not in text
    record["cells"][-1]["why_unenforced"] = "flaky under load"
    assert harness.unexplained(record) == []


def test_every_expected_code_is_reflows_or_planned():
    from robustness.kinds import PLANNED_CODES

    from reflow.analyze.confidence import CODES

    for kind in KINDS:
        for expect in (kind.expect, kind.target):
            for code in (expect.codes if expect is not None else ()):
                assert code in CODES or code in PLANNED_CODES, kind.id
    assert not set(PLANNED_CODES) & set(CODES), "a planned code has landed: drop it from PLANNED_CODES"


def _projection(*pages: tuple[str, ...]) -> Projection:
    return Projection("en", {i: PageView(tokens) for i, tokens in enumerate(pages, 1)})


def test_the_same_words_in_another_order_are_not_the_same():
    clean = _projection(("first", "second", "third", "fourth"))
    shuffled = _projection(("third", "fourth", "first", "second"))
    diffs = differences(clean, shuffled)
    assert diffs and "another order" in diffs[0]
    verdict = judge(same(), diffs, flagged=False)
    assert (verdict.outcome, verdict.met) == (Outcome.SILENT_LOSS, False)


def test_an_equal_projection_is_preserved():
    clean = _projection(("a", "b"), ("c",))
    assert differences(clean, _projection(("a", "b"), ("c",))) == []
    assert judge(same(), [], flagged=False) == harness.Verdict(Outcome.PRESERVED, True, "", ())


def test_tables_links_fields_and_language_are_part_of_the_projection():
    a = Projection("en", {1: PageView(("w",), tables=((("x", "y"),),), links=("home",),
                                      fields=(("text", "Name", ""),))})
    for change in (dict(tables=((("x", "z"),),)), dict(links=()), dict(fields=())):
        b = Projection("en", {1: PageView(**{**a.pages[1].__dict__, **change})})
        assert differences(a, b), change
    assert differences(a, Projection("ja", a.pages)) == ["language 'en' became 'ja'"]


def test_a_select_projects_its_selected_option():
    from pages.checks import parse_html
    from robustness.oracle import view_of

    def state(chosen: str) -> PageView:
        options = "".join(f'<option value="{v}"{" selected" if v == chosen else ""}>{v}</option>'
                          for v in ("AZ", "CA"))
        html = f'<div class="pg" data-page="1"><select aria-label="State">{options}</select></div>'
        return view_of(next(n for n in parse_html(html).walk() if n.tag == "div"))

    assert state("AZ").fields == (("select", "State", "AZ"),)
    assert state("AZ") != state("CA")


def test_expectations():
    lost = ["page 1 text: ..."]
    assert judge(flagged("x"), lost, True).outcome is Outcome.CAUGHT
    assert judge(flagged("x"), lost, True).met
    assert not judge(flagged("x"), lost, False).met
    unflagged = judge(flagged("x"), [], False)            # preserved, but nothing raised the fact
    assert (unflagged.outcome, unflagged.met) == (Outcome.PRESERVED, False)
    assert not judge(repaired("x"), lost, True).met
    assert not judge(repaired("x"), [], False).met
    assert judge(repaired("x"), [], True).met


def test_only_the_named_fact_over_the_damage_and_new_to_the_page_counts():
    damage = Damage(frozenset({1}), {1: (100, 100, 200, 120)})
    over = Fact("dropped-ligature", 1, (90, 95, 150, 110))
    assert flag_found("dropped-ligature", [], [over], damage)
    assert not flag_found("dropped-ligature", [], [Fact("unicode-errors", 1, over.box)], damage)
    assert not flag_found("dropped-ligature", [], [Fact("dropped-ligature", 1, (10, 10, 50, 50))],
                          damage)
    assert not flag_found("dropped-ligature", [], [Fact("dropped-ligature", 2, over.box)], damage)
    elsewhere = Fact("dropped-ligature", 1, (0, 0, 9, 9))   # the clean page has one, not over the damage
    assert flag_found("dropped-ligature", [elsewhere], [elsewhere, over], damage)
    assert not flag_found("dropped-ligature", [over], [over], damage)   # the clean file had it there
    assert flag_found("ocr-doc", [], [Fact("ocr-doc", None)], damage)   # a document-wide fact


@pytest.fixture
def prose(tmp_path):
    return harness.BASE_BY_NAME["prose"].make(tmp_path / "base.pdf")


def test_shifted_tounicode_reads_the_next_letter_and_renders_the_same(prose, tmp_path):
    clean, dst = tmp_path / "clean.pdf", tmp_path / "shifted.pdf"
    with pikepdf.open(prose) as pdf:
        save(pdf, clean)
    damage = KIND["tounicode-shifted"].transform(clean, dst, random.Random(1))
    assert damage.pages == {1} and damage.boxes[1]
    text = _text(dst)
    assert "Uif tubgg gjmfe gjwf pggjdjbm gjmft" in text   # the body font: "The staff filed ..."
    assert "Annual office report" in text                  # the heading's bold font is clean
    assert harness.render_difference(clean, dst) is None


def _mutate(tmp_path: Path, monkeypatch, kind: DamageKind, base: str = "prose") -> harness.Cell:
    monkeypatch.setitem(harness.KIND_BY_ID, kind.id, kind)
    run = Run(tmp_path, [(base, kind.id)])
    cell = run.cells[(base, kind.id)]
    run._mutate(cell)
    return cell


def test_a_pixel_same_kind_whose_render_changes_is_invalid(tmp_path, monkeypatch):
    visible = KIND["tiny-text"].transform
    lie = DamageKind("lie", "a visible line, declared pixel-same",
                     lambda s, d, r: visible(s, d, r) and _bigger(d), Pixels.SAME, same())
    cell = _mutate(tmp_path, monkeypatch, lie)
    assert cell.verdict.outcome is Outcome.INVALID
    assert "renders differently" in cell.verdict.detail


def _bigger(path: Path) -> Damage:
    """Draw the tiny line at 10 pt, so it is plainly visible."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        streams = page.obj.Contents
        last = streams[len(streams) - 1]
        last.write(last.read_bytes().replace(b" 0.5 Tf", b" 10 Tf"))
        pdf.save(path)
    return Damage(frozenset({1}))


def test_a_transform_that_changes_nothing_is_invalid(tmp_path, monkeypatch):
    def copy(src: Path, dst: Path, rng) -> Damage:
        with pikepdf.open(src) as pdf:
            save(pdf, dst)
        return Damage(frozenset({1}))

    cell = _mutate(tmp_path, monkeypatch, DamageKind("noop", "nothing", copy, Pixels.SAME, same()))
    assert (cell.verdict.outcome, cell.verdict.detail) == (Outcome.INVALID,
                                                           "the transform changed nothing")


def test_an_unmet_precondition_is_unsupported_and_a_broken_transform_invalid(tmp_path, monkeypatch):
    def missing(src, dst, rng):
        raise Unsupported("no tag tree")

    def broken(src, dst, rng):
        raise KeyError("/Font")

    cell = _mutate(tmp_path / "a", monkeypatch, DamageKind("pre", "", missing, Pixels.SAME, same()))
    assert (cell.verdict.outcome, cell.verdict.detail) == (Outcome.UNSUPPORTED, "no tag tree")
    cell = _mutate(tmp_path / "b", monkeypatch, DamageKind("bug", "", broken, Pixels.SAME, same()))
    assert cell.verdict.outcome is Outcome.INVALID and "KeyError" in cell.verdict.detail


@pytest.mark.parametrize("kind", [k for k in KINDS if k.tool == "pikepdf"], ids=lambda k: k.id)
def test_each_pikepdf_kind_makes_a_valid_mutant_of_the_prose_base(kind, tmp_path, monkeypatch):
    cell = _mutate(tmp_path, monkeypatch, kind)
    assert cell.verdict is None or cell.verdict.outcome is Outcome.UNSUPPORTED, cell.verdict
    if cell.verdict is None:
        assert cell.pdf.exists() and cell.damage.pages
