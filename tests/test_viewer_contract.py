"""The contract between what Python publishes and what the web viewer reads.

schema/manifest.schema.json describes manifest.json; the viewer's types are generated from it
(web/src/reflow/manifest.gen.ts). This file renders a small document and checks that its
manifest validates against the schema, and that schema/viewer-contract.json holds what Python
publishes for it now: the manifest, the page each entry should expand to, and the anchor of each
element that has one. schema/viewer-contract/ holds the published pages of the same render
(index.html and chunks/), so the viewer's tests read real markup, not markup written by hand to
look like it. web/src/reflow/contract.test.ts reads them through the viewer's own code
(normalizeManifest, transcriptAnchor, loadChunkPages, ...) and must reach the same pages and
anchors. schema/viewer-contract/hostile/ is the exception: a chunk page written by hand, with
what a hostile PDF's page could hold and the markup the viewer must keep (web/src/reflow/
sanitize.test.ts). Nothing here renders or checks it.

After a change to what the renderer writes: REFLOW_WRITE_CONTRACT=1 and run this file, then
`cd web && bun test src/reflow`.
"""
import json
import os
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from reflow.analyze.sections import nest_sections
from reflow.model import Document, Fact, Node, PageInfo, Role, Source, Span
from reflow.render import chunks as render_chunks
from reflow.render.manifest import decode_size, encode_size, page_text_sizes
from reflow.render.site import render_html

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "manifest.schema.json"
CONTRACT = ROOT / "schema" / "viewer-contract.json"
PUBLISHED = ROOT / "schema" / "viewer-contract"
PAGES = ("index.html", "chunks/0001.html", "chunks/0002.html", "chunks/0003.html")


ANCHORED = {"h1", "h2", "h3", "h4", "h5", "h6", "table", "figure"}


class Anchors(HTMLParser):
    """[data-node, id] of every heading, table and figure that carries both: the elements whose
    id is the node's anchor (render/html.py, anchor). A form control's id is its field's."""

    def __init__(self):
        super().__init__()
        self.pairs: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ANCHORED and a.get("id") and a.get("data-node"):
            self.pairs.append([a["data-node"], a["id"]])


def publish(out: Path, monkeypatch) -> dict:
    """Render a four-page document that uses every part of the manifest and of the pages' markup
    that the viewer reads: a page of another size, printed labels with an offset and one that
    breaks it, several chunks, a title, a form field, a note with its reference and backlink, a
    contents entry that links to a page, a figure a model described, a page read by
    OCR, page images at two widths and a version digest. Returns the contract."""
    monkeypatch.setattr(render_chunks, "MIN_BYTES", 10)
    monkeypatch.setattr(render_chunks, "FIRST_TARGET_BYTES", 10)
    fact = "The report covers one year.1"
    note = Node(Role.NOTE, page=1, text="1 Rounded to the nearest tenth.", bbox=(72, 700, 400, 712),
                attrs={"marker": "1", "refs": 1})
    figure = Node(Role.FIGURE, page=2, bbox=(72, 300, 272, 400),
                  attrs={"alt": "Bar chart of sales", "description": "Two blue bars rise.",
                         "provenance": "ai-drafted"},
                  children=[Node(Role.CAPTION, page=2, text="Fig. 1. Sales", bbox=(72, 402, 272, 414)),
                            Node(Role.P, page=2, text="Q1 revenue", bbox=(80, 310, 140, 322),
                                 attrs={"figure_label": True})])
    nodes = [
        Node(Role.H1, page=1, text="Contract sample", bbox=(72, 40, 400, 64)),
        Node(Role.H2, page=1, text="Overview", bbox=(72, 72, 300, 90)),
        Node(Role.P, page=1, text=fact, bbox=(72, 100, 500, 130),
             spans=[Span(len(fact) - 1, len(fact), "noteref", note.id)]),
        Node(Role.FIELD, page=1, text="Name", attrs={"kind": "text", "fid": "f1-0", "name": "Name"}),
        note,
        Node(Role.H2, page=2, text="Results", bbox=(72, 72, 300, 90)),
        Node(Role.H3, page=2, text="By region", bbox=(72, 100, 300, 118)),
        Node(Role.LIST, page=2, bbox=(72, 130, 400, 150), children=[
            Node(Role.LI, page=2, text="Appendix", bbox=(72, 130, 400, 150),
                 attrs={"contents": True, "page_ref": "3"})]),
        figure,
        Node(Role.P, page=3, text="A wide table page.", bbox=(72, 72, 700, 100)),
        Node(Role.H2, page=4, text="Appendix", bbox=(72, 72, 300, 90)),
        Node(Role.P, page=4, text="Notes.", bbox=(72, 100, 300, 118), source=Source.OCR),
    ]
    sizes = {1: (612, 792), 2: (612, 792), 3: (792, 612), 4: (612, 792)}
    ocr = Fact("ocr-page", Source.OCR, "read by OCR", {})
    doc = Document(root=Node(Role.DOC, children=nest_sections(nodes)),
                   pages=[PageInfo(n, *sizes[n], facts=[ocr] if n == 4 else []) for n in sizes],
                   meta={"lang": "en-US"})
    labels = {1: "i", 2: "1", 3: "2", 4: "3"}
    render_html(doc, "Contract sample", out, labels=labels, version="0123456789ab",
                page_widths=(800, 1600), figures={figure.id: ("p0002-1.webp", 200, 100)})
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    chunk_of = {n: c["href"] for c in manifest["chunks"] for n in range(c["first"], c["last"] + 1)}
    text = page_text_sizes(doc)
    pages = [{
        "number": p.number, "label": labels[p.number], "width": p.width, "height": p.height,
        "image": f"pages/{p.number:04d}.webp", "chunk": chunk_of[p.number],
        "text": dict(zip(("chars", "blocks", "lines"),
                         (decode_size(encode_size(c)) for c in text[p.number]))),
    } for p in doc.pages]

    anchors = Anchors()
    for chunk in manifest["chunks"]:
        anchors.feed((out / chunk["href"]).read_text(encoding="utf-8"))
    return {"manifest": manifest, "pages": pages, "anchors": anchors.pairs}


@pytest.fixture
def contract(tmp_path, monkeypatch):
    return publish(tmp_path, monkeypatch)


@pytest.fixture
def published(tmp_path, contract):
    """The pages of the render the contract is made from, by name."""
    return {name: (tmp_path / name).read_text(encoding="utf-8") for name in PAGES}


def test_the_published_manifest_validates_against_the_schema(contract):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
              for e in Draft202012Validator(schema).iter_errors(contract["manifest"])]
    assert errors == []


def test_the_sample_uses_what_the_viewer_reads(contract):
    m = contract["manifest"]
    assert len(m["chunks"]) > 1 and m["pages"] and m["fillable"] is True
    assert m["labelOffset"] == -1 and m["imageWidths"] == [800, 1600]
    assert contract["anchors"] and all(":" in node for node, _ in contract["anchors"])


def test_the_contract_file_is_what_python_publishes(contract):
    text = json.dumps(contract, ensure_ascii=False, indent=1) + "\n"
    if os.environ.get("REFLOW_WRITE_CONTRACT"):
        CONTRACT.write_text(text, encoding="utf-8")
    assert CONTRACT.read_text(encoding="utf-8") == text, (
        f"{CONTRACT.relative_to(ROOT)} is stale: run REFLOW_WRITE_CONTRACT=1 pytest "
        "tests/test_viewer_contract.py, update the schema if the manifest changed, then "
        "cd web && bun run schema:gen && bun test src/reflow")


def test_the_published_pages_are_what_python_publishes(published):
    if os.environ.get("REFLOW_WRITE_CONTRACT"):
        for name, html in published.items():
            (PUBLISHED / name).parent.mkdir(parents=True, exist_ok=True)
            (PUBLISHED / name).write_text(html, encoding="utf-8")
    for name, html in published.items():
        assert (PUBLISHED / name).read_text(encoding="utf-8") == html, (
            f"schema/viewer-contract/{name} is stale: run REFLOW_WRITE_CONTRACT=1 pytest "
            "tests/test_viewer_contract.py, then cd web && bun test src/reflow")


def test_the_sample_pages_carry_the_markup_the_viewer_reads(published):
    shell, first, second, last = (published[name] for name in PAGES)
    assert '<p class="title"' in first and 'data-source="ocr"' in last
    assert 'class="ocr-note"' in last and "data-generated" in last
    assert 'class="ai-draft"' in second and 'role="doc-footnote"' in first
    assert 'role="doc-noteref"' in first and 'role="doc-backlink"' in first
    assert 'data-page="4"' in last
    assert 'class="chunk-content"' in first and 'data-loaded="1"' in shell
