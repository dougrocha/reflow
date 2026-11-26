"""The witness pass on a hand-built Run: the title from evidence and its fact."""

from pathlib import Path

import pytest
from conftest import make_pdf, make_tagged_pdf

from reflow.analyze import evidence
from reflow.analyze.engine import Backbone
from reflow.convert import Summary
from reflow.ingest.ocr import OcrResult
from reflow.model import Document, Node, Role
from reflow.output import SiteStats
from reflow.pipeline import Model, ModelFacts
from reflow.run import Options, Run, Workers
from testkit import heading

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
BACKBONE = Backbone(pages=None, tables={}, trace={}, routing=None)


def make_run(pdf: Path, nodes: list[Node], meta: dict | None = None) -> Run:
    doc = Document(root=Node(Role.DOC, children=nodes), meta=dict(meta or {}))
    return Run(pdf=pdf, stage=pdf.parent, options=Options(), workers=Workers.serial(),
               progress=lambda *_: None, doc=doc, working=pdf, ocr=OcrResult())


@pytest.fixture
def plain(tmp_path):
    return make_pdf(tmp_path / "report-2024.pdf", ["text"])


def title_facts(run: Run) -> list:
    return [f for f in run.doc.root.facts if f.code == "title"]


def test_meta_title_wins_and_leaves_the_headings_alone(plain):
    nodes = [heading("Annual Report", 18.0), heading("Introduction", 12.0)]
    run = make_run(plain, nodes, {"title": "Annual Report"})
    assert evidence.witness(run, BACKBONE).facts == {}      # nothing tagged to fuse
    assert run.doc.root.attrs["title"] == "Annual Report"
    (fact,) = run.doc.root.facts
    assert fact.code == "title" and fact.data["origin"] == "meta"
    assert fact.data["score"] == pytest.approx(0.9)
    assert [n.role for n in nodes] == [Role.H2, Role.H2]   # no H1 promotion for a meta title


def test_winning_heading_becomes_the_h1_and_the_rest_move_up(plain):
    nodes = [heading("Quarterly Results", 18.0), heading("Scope", 12.0, role=Role.H3)]
    run = make_run(plain, nodes)
    evidence.witness(run, BACKBONE)
    assert run.doc.root.attrs["title"] == "Quarterly Results"
    assert [f.code for f in run.doc.root.facts] == ["title"]
    assert title_facts(run)[0].data["origin"] == "heading"
    assert [n.role for n in nodes] == [Role.H1, Role.H2]


def test_generic_meta_title_loses_to_the_heading(plain):
    nodes = [heading("Quarterly Results", 18.0)]
    run = make_run(plain, nodes, {"title": "Untitled"})
    evidence.witness(run, BACKBONE)
    assert run.doc.root.attrs["title"] == "Quarterly Results"
    assert title_facts(run)[0].data["origin"] == "heading"
    assert nodes[0].role is Role.H1


def test_first_bookmark_on_page_one_is_a_candidate(tmp_path):
    tagged = make_tagged_pdf(tmp_path / "tagged.pdf")   # outline: "Chapter One" → page 1
    run = make_run(tagged, [Node(Role.P, page=1, text="body")])
    evidence.witness(run, BACKBONE)
    assert run.doc.root.attrs["title"] == "Chapter One"
    assert title_facts(run)[0].data["origin"] == "bookmark"
    assert [f.code for f in run.doc.root.facts] == ["title"]


def test_garbage_heading_falls_back_to_the_filename_and_is_suspect(tmp_path):
    pdf = make_pdf(tmp_path / "naca-ocr.pdf", ["text"])
    nodes = [heading("6'17 #i 5", 18.0)]
    run = make_run(pdf, nodes)
    evidence.witness(run, BACKBONE)
    assert run.doc.root.attrs["title"] == "naca-ocr"
    assert [f.code for f in run.doc.root.facts] == ["title-suspect", "title"]
    assert title_facts(run)[0].data["origin"] == "filename"
    assert nodes[0].role is Role.H2   # nothing promoted


def test_title_apply_without_a_matching_heading_changes_nothing():
    nodes = [heading("Other", 18.0), Node(Role.P, page=1, text="Title Text")]
    choice = evidence.title.TitleChoice(text="Title Text", origin="heading", score=0.5)
    assert evidence.title_apply(nodes, choice) is None
    assert [n.role for n in nodes] == [Role.H2, Role.P]


def summary_of(run: Run) -> Summary:
    model = Model(run.doc, run.doc.to_json(indent=None), [], {},
                  ModelFacts(ocr=run.ocr, alt_text={"provider": "none"}))
    return Summary.of(run, model, SiteStats(1, 0, 0, 0))


def test_summary_title_origin_comes_from_the_title_fact(plain):
    # The meta title and the heading agree, so the fallback inference ("meta" when the title
    # equals doc.meta["title"]) would say meta; the fact says what actually won.
    nodes = [heading("Annual Report", 18.0)]
    run = make_run(plain, nodes, {"title": "Untitled"})
    evidence.witness(run, BACKBONE)
    run.doc.meta["title"] = "Annual Report"
    s = summary_of(run)
    assert (s.title, s.title_origin, s.title_suspect) == ("Annual Report", "heading", False)


def test_summary_reports_bookmark_and_filename_origins(tmp_path):
    tagged = make_tagged_pdf(tmp_path / "tagged.pdf")
    run = make_run(tagged, [Node(Role.P, page=1, text="body")])
    evidence.witness(run, BACKBONE)
    assert summary_of(run).title_origin == "bookmark"

    pdf = make_pdf(tmp_path / "naca-ocr.pdf", ["text"])
    run = make_run(pdf, [heading("6'17 #i 5")])
    evidence.witness(run, BACKBONE)
    s = summary_of(run)
    assert (s.title_origin, s.title_suspect, s.facts["title"]) == ("filename", True, 1)


@pytest.mark.skipif(not (SAMPLES / "scan.pdf").exists(), reason="samples/scan.pdf missing")
def test_scan_sample_has_no_headings_so_the_metadata_decides():
    run = make_run(SAMPLES / "scan.pdf", [], {"title": "scan"})
    evidence.witness(run, BACKBONE)
    assert run.doc.root.attrs["title"] == "scan"
    assert title_facts(run)[0].data["origin"] == "meta"
