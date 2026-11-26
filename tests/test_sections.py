"""Sections in the tree."""

from __future__ import annotations

import json
import re

from reflow.analyze import sections
from reflow.analyze.sections import heading_of, nest_sections, section_region
from reflow.model import Document, Node, PageInfo, Role
from reflow.render import chunks as render_chunks
from reflow.render.links import anchor
from reflow.render.sidecars import page_sidecar
from reflow.render.site import render_html


def n(role, page=1, text="", bbox=None):
    return Node(role, page=page, text=text or role.value, bbox=bbox)


def shape(nodes):
    return [(x.role.value, shape(x.children) if x.role == Role.SECTION else None) for x in nodes]


def test_headings_nest_by_level_and_h1_is_not_a_section():
    title, intro = n(Role.H1, text="Title"), n(Role.P, text="intro")
    a, a1, a2, b = (n(Role.H2, text="A"), n(Role.H3, text="A1"), n(Role.H3, text="A2"),
                    n(Role.H2, text="B"))
    pa, p1, pb = n(Role.P, text="pa"), n(Role.P, text="p1"), n(Role.P, text="pb")
    out = nest_sections([title, intro, a, pa, a1, p1, a2, b, pb])
    assert out[:2] == [title, intro]
    sec_a, sec_b = out[2], out[3]
    assert [c.role for c in sec_a.children] == [Role.H2, Role.P, Role.SECTION, Role.SECTION]
    assert heading_of(sec_a) is a and sec_a.attrs["level"] == 2
    assert [heading_of(c) for c in sec_a.children[2:]] == [a1, a2]
    assert sec_a.children[2].children == [a1, p1]
    assert sec_b.children == [b, pb]
    assert sec_a.page is None and sec_a.bbox is None and sec_a.text is None


def test_a_deeper_first_heading_and_a_skipped_level_still_nest():
    h3, h5, h4 = n(Role.H3), n(Role.H5), n(Role.H4)
    out = nest_sections([h3, h5, h4])
    assert shape(out) == [("section", [("h3", None), ("section", [("h5", None)]),
                                       ("section", [("h4", None)])])]


def test_h1_closes_open_sections():
    out = nest_sections([n(Role.H2), n(Role.P), n(Role.H1, text="Part"), n(Role.P, text="after")])
    assert [x.role for x in out] == [Role.SECTION, Role.H1, Role.P]


def test_nothing_without_headings_is_wrapped():
    flat = [n(Role.P), n(Role.FIGURE)]
    assert nest_sections(flat) == flat


def test_flow_round_trips_the_reading_order():
    flat = [n(Role.P), n(Role.H2), n(Role.P), n(Role.H3), n(Role.P), n(Role.H2), n(Role.P)]
    doc = Document(root=Node(Role.DOC, children=nest_sections(flat)))
    assert list(doc.flow()) == flat


def test_section_region():
    h = n(Role.H2, bbox=(10, 10, 100, 30))
    p = n(Role.P, bbox=(10, 40, 200, 80))
    q = n(Role.P, page=2, bbox=(5, 5, 50, 50))
    art = n(Role.ARTIFACT, bbox=(0, 0, 999, 999))
    root = Node(Role.DOC, children=nest_sections([h, p, art, q]))
    sec = root.children[0]
    assert section_region(sec, 1) == (10, 10, 200, 80)
    assert section_region(sec, 2) == (5, 5, 50, 50)
    assert section_region(sec, 3) is None


def test_insert_by_page_goes_inside_the_section_that_spans_the_page():
    h, p1, p3 = n(Role.H2), n(Role.P, page=1), n(Role.P, page=3)
    top = nest_sections([h, p1, p3])
    new = n(Role.P, page=2, text="draft")
    sections.insert_by_page(top, new, 2)
    assert top[0].children == [h, p1, new, p3]
    late = n(Role.P, page=9)
    sections.insert_by_page(top, late, 9)
    assert top[-1] is late


def doc_of(nodes, pages):
    doc = Document(root=Node(Role.DOC, children=nest_sections(nodes)),
                   pages=[PageInfo(i, 612, 792) for i in range(1, pages + 1)])
    doc.ensure_unique_ids()
    return doc


def convert(tmp_path, doc):
    render_html(doc, "T", tmp_path, page_widths=())
    return (tmp_path / "index.html").read_text(), json.loads((tmp_path / "toc.json").read_text())


def test_rendering_wraps_each_section_and_splits_it_per_page(tmp_path):
    h2, h3 = n(Role.H2, text="Alpha"), n(Role.H3, text="Beta")
    nodes = [h2, n(Role.P, text="a1"), h3, n(Role.P, page=1, text="b1"),
             n(Role.P, page=2, text="b2"), n(Role.H2, page=2, text="Gamma")]
    index, toc = convert(tmp_path, doc_of(nodes, 2))
    assert f'<section aria-labelledby="{anchor(h2)}">' in index
    assert f'<section aria-labelledby="{anchor(h3)}">' in index
    page2 = index[index.index('id="p2"'):]
    # Alpha and Beta continue onto page 2 as unnamed groupings; Gamma opens a named section.
    assert page2.count("<section>") == 2
    assert page2.count("<section aria-labelledby=") == 1
    assert len(re.findall(r"<section", index)) == len(re.findall(r"</section>", index))
    assert [e["text"] for e in toc] == ["Alpha", "Beta", "Gamma"]


def test_the_first_chunk_barely_grows_with_sections(tmp_path):
    nodes = [n(Role.H2, text="One")] + [n(Role.P, text="word " * 40) for _ in range(5)]
    flat = Document(root=Node(Role.DOC, children=list(nodes)), pages=[PageInfo(1, 612, 792)])
    nested = doc_of(nodes, 1)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    render_html(flat, "T", a, page_widths=())
    render_html(nested, "T", b, page_widths=())
    grow = len((b / "chunks/0001.html").read_text()) - len((a / "chunks/0001.html").read_text())
    assert 0 < grow < 100  # one <section aria-labelledby="..."> wrapper per section


def test_chunks_split_where_a_top_level_section_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(render_chunks, "MIN_BYTES", 10)
    monkeypatch.setattr(render_chunks, "FIRST_TARGET_BYTES", 10)
    nodes = [n(Role.H2, text="One"), n(Role.P, text="a"), n(Role.H3, page=2, text="Sub"),
             n(Role.H2, page=3, text="Two"), n(Role.P, page=3, text="b")]
    render_html(doc_of(nodes, 3), "T", tmp_path, page_widths=())
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [(c["first"], c["last"]) for c in manifest["chunks"]] == [(1, 1), (2, 2), (3, 3)]
    assert "Continue reading: pages 3 (Two)" in (tmp_path / "index.html").read_text()


def test_the_sidecar_lists_the_sections_whose_heading_is_on_the_page():
    h = n(Role.H2, bbox=(10, 10, 100, 30))
    sub = n(Role.H3, page=2, bbox=(10, 10, 100, 30))
    doc = doc_of([h, n(Role.P, bbox=(10, 40, 200, 80)), n(Role.P, page=2, bbox=(10, 40, 300, 90)),
                  sub], 2)
    page1, page2 = page_sidecar(doc, 1), page_sidecar(doc, 2)
    assert page1["sections"] == [{"id": h.id, "level": 2, "bbox": [10, 10, 200, 80]}]
    assert page2["sections"] == [{"id": sub.id, "level": 3, "bbox": [10, 10, 100, 30]}]
    assert all(node["role"] != "section" for node in page1["nodes"])
