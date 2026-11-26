"""Heuristics + AI polish (analyze.polish): the page the model reads, each edit and its
checks, the AI-drafted label, and a whole conversion with a fake model."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from reflow.analyze import polish
from reflow.analyze.direct import PageInput, Reading
from reflow.model import Node, Role
from reflow.run import Options

REPO = Path(__file__).resolve().parents[1]


def p(text: str, role: Role = Role.P, y: float = 0.0) -> Node:
    return Node(role, page=1, bbox=(10.0, y, 200.0, y + 10), text=text)


def field(name: str, y: float) -> Node:
    return Node(Role.FIELD, page=1, bbox=(10.0, y, 200.0, y + 10), text=name,
                attrs={"kind": "text", "label": name})


def table() -> Node:
    def cell(t: str, y: float) -> Node:
        return Node(Role.TD, page=1, bbox=(10.0, y, 50.0, y + 5), text=t)
    rows = [Node(Role.TR, page=1, children=[cell("Name", 0), cell("Fee", 0)]),
            Node(Role.TR, page=1, children=[cell("Visit", 6), cell("$5", 6)])]
    return Node(Role.TABLE, page=1, bbox=(10.0, 0.0, 50.0, 11.0), children=rows)


def edits(*items: dict) -> str:
    full = [{"op": "", "block": "", "blocks": [], "level": 0, "after": "", **e} for e in items]
    return json.dumps({"edits": full})


def test_the_model_reads_each_block_with_an_id_role_and_text():
    blocks = [p("Title", Role.H2), p("Body text"), table(), field("Name", 50)]
    read = json.loads(polish.page_blocks(blocks))
    assert [b["id"] for b in read] == ["b1", "b2", "b3", "b4"]
    assert read[0] == {"id": "b1", "role": "h2", "text": "Title"}
    assert read[2]["first_row"] == ["Name", "Fee"] and read[2]["first_row_header"] is False
    assert read[3]["kind"] == "text" and read[3]["label"] == "Name"


def test_a_heading_edit_is_applied_and_labelled_ai_drafted():
    blocks = [p("Part I", y=0), p("Some text", y=20)]
    new, counts = polish.polish_page(blocks, edits({"op": "heading", "block": "b1", "level": 2}), "m")
    assert counts == {"heading": 1, "applied": 1}
    assert new[0].role == Role.H2 and blocks[0].role == Role.P   # the edit worked on a copy
    assert new[0].attrs["provenance"] == "ai-drafted" and new[0].attrs["reviewed"] is False
    assert {f.code for f in new[0].facts} >= {"ai-drafted", "ai-polished"}


def test_consecutive_paragraphs_become_one_list_with_markers_off_the_text():
    blocks = [p("Intro"), p("• first", y=10), p("• second", y=20)]
    new, counts = polish.polish_page(blocks, edits({"op": "list", "blocks": ["b2", "b3"]}), "m")
    assert counts["applied"] == 1 and [n.role for n in new] == [Role.P, Role.LIST]
    assert [li.text for li in new[1].children] == ["first", "second"]
    assert polish.words(new) == polish.words(blocks)


def test_a_table_header_a_move_and_a_form_group():
    blocks = [table(), p("Late line", y=30), p("Sex", y=40), field("Male", 50), field("Female", 60)]
    new, counts = polish.polish_page(blocks, edits(
        {"op": "table_header", "block": "b1"},
        {"op": "move", "block": "b2", "after": ""},
        {"op": "group", "blocks": ["b4", "b5"], "after": "b3"}), "m")
    assert counts == {"table_header": 1, "move": 1, "group": 1, "applied": 3}
    assert [n.role for n in new] == [Role.P, Role.TABLE, Role.GROUP]
    assert [c.role for c in new[1].children[0].children] == [Role.TH, Role.TH]
    assert new[2].text == "Sex" and [c.text for c in new[2].children] == ["Male", "Female"]


@pytest.mark.parametrize("edit", [
    {"op": "heading", "block": "b9", "level": 2},          # no such block
    {"op": "heading", "block": "b3", "level": 2},          # a field is not text
    {"op": "heading", "block": "b1", "level": 9},          # no such level
    {"op": "paragraph", "block": "b1"},                    # not a heading
    {"op": "list", "blocks": ["b1", "b3"]},                # not in a row, and a field
    {"op": "group", "blocks": ["b3"], "after": "b2"},      # one field is no group
    {"op": "move", "block": "b2", "after": "b1"},          # already there
    {"op": "rewrite", "block": "b1"},                      # no such op
])
def test_an_edit_that_fails_its_checks_is_rejected(edit):
    blocks = [p("One"), p("Two", y=10), field("Name", 20)]
    new, counts = polish.polish_page(blocks, edits(edit), "m")
    assert counts == {"rejected": 1} and new is blocks


def test_an_edit_that_would_change_the_text_is_rejected(monkeypatch):
    from reflow.analyze import lists

    def drop_a_word(nodes):
        nodes[0].set_text("changed")
        return [Node(Role.LIST, page=1, children=nodes, attrs={"ordered": False})]
    monkeypatch.setattr(lists, "group_lists", drop_a_word)
    blocks = [p("first"), p("second", y=10)]
    _, counts = polish.polish_page(blocks, edits({"op": "list", "blocks": ["b1", "b2"]}), "m")
    assert counts == {"rejected": 1}


def test_a_later_edit_names_the_blocks_of_the_first_and_an_answer_that_is_not_json_is_ignored():
    blocks = [p("A"), p("B", y=10), p("C", y=20)]
    new, counts = polish.polish_page(blocks, edits(
        {"op": "move", "block": "b3", "after": ""},
        {"op": "heading", "block": "b3", "level": 2}), "m")
    assert counts["applied"] == 2 and [(n.text, n.role) for n in new] == [
        ("C", Role.H2), ("A", Role.P), ("B", Role.P)]
    assert polish.polish_page(blocks, "not json", "m") == (blocks, {"rejected": 1})


def test_polish_options_are_checked():
    assert Options(polish=True).polish_model == "claude-haiku-5-5"
    for bad in ({"polish_model": "glm-ocr"}, {"polish_max_dollars": -1},
                {"polish": True, "engine": "direct"}):
        with pytest.raises(ValueError):
            Options(**bad)


# ---------------------------------------------------------------------------- a whole conversion

class FakePolish:
    model, provider, prompt, reads_text = "claude-haiku-5-5", "anthropic", "polish-test", True

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.lock = threading.Lock()

    def read(self, page: PageInput) -> Reading:
        with self.lock:
            self.calls.append(page.number)
        first = json.loads(page.text)[0]["id"]
        return Reading(edits({"op": "heading", "block": first, "level": 3}, {"op": "move", "block": "b99"}),
                       False, 1500, 100, 0.0002, 0.1)


def test_a_conversion_polishes_each_page_and_records_what_it_cost(tmp_path):
    from reflow.convert import convert
    reader = FakePolish()
    out = tmp_path / "out"
    summary = convert(REPO / "samples" / "fw9.pdf", out, polish=True, polish_reader=reader,
                      direct_cache=tmp_path / "cache", page_images=False)
    meta = summary.polish
    assert meta["model"] == "claude-haiku-5-5" and meta["read"] == len(reader.calls) > 0
    assert meta["rejected"] >= meta["read"] and meta["applied"] >= 1   # b99 never exists
    assert meta["usd"] == pytest.approx(0.0002 * meta["read"])
    assert next(iter(meta["pages"].values()))["edits"]["rejected"] == 1
    html = "".join(f.read_text() for f in out.glob("*.html"))
    assert 'data-provenance="ai-drafted"' in html

    again = convert(REPO / "samples" / "fw9.pdf", tmp_path / "again", polish=True,
                    polish_reader=FakePolish(), direct_cache=tmp_path / "cache", page_images=False)
    assert again.polish["cached"] == meta["read"] and again.polish["spent"] == 0


def test_a_header_row_drops_the_row_scope_the_rules_gave_its_first_cell():
    t = table()
    first = t.children[0].children[0]
    first.role, first.attrs["scope"] = Role.TH, "row"
    new, counts = polish.polish_page([t], edits({"op": "table_header", "block": "b1"}), "m")
    assert counts["applied"] == 1
    assert all(c.role == Role.TH and "scope" not in c.attrs for c in new[0].children[0].children)
