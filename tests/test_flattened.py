"""A form flattened into its pages (reflow.ingest.flattened): the page gets `form-flattened`
over the boxes its fields were in, read from the traces flattening leaves in the file. Ruled
boxes, a table and drawn check boxes are not traces, and a document that still has its fields
never gets the fact."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pikepdf
import pytest
from checkout import sample
from pikepdf import Array, Dictionary, Name

from reflow.analyze.confidence import CODES
from reflow.convert import convert
from reflow.extract.forms import read_widgets
from reflow.ingest import flattened
from reflow.names import DOCUMENT_JSON

REPO = Path(__file__).resolve().parents[1]
needs_qpdf = pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf is not installed")

# Forms that draw their boxes and have no AcroForm, and a directory whose last page holds one
# flattened field (a date stamp): none of them is a flattened form.
NOT_FLATTENED = [
    "samples/azblue/site/pediatric-nicu-case-management-referral-form.pdf",
    "samples/azblue/site/standardappealpacket.pdf",
    "samples/azblue/site/cobra-application.pdf",
    "samples/azblue/site/medsup-application.pdf",
    "samples/docaccess/02-building-permit-applications.pdf",
    "samples/docaccess/03-assessment-appeal-application.pdf",
    "samples/azblue/ma-provider-directory-bluejourney-sp.pdf",
]


def _flatten(src: Path, dst: Path) -> Path:
    """The robustness kind forms-flattened's transform."""
    done = subprocess.run(["qpdf", "--generate-appearances", "--flatten-annotations=all",
                           str(src), str(dst)], capture_output=True)
    assert done.returncode in (0, 3), done.stderr   # 3: done, with warnings
    return dst


def _near(a, b, slack: float = 1.0) -> bool:
    return all(abs(x - y) <= slack for x, y in zip(a, b, strict=True))


def _form_page(path: Path, xobjects: list[tuple[bytes, str]], *, fields: bool = False,
               drawing: bytes = b"") -> Path:
    """A letter page that draws `drawing` and then each (appearance, cm) form XObject; with
    `fields`, an AcroForm with one text field on the page."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    names, body = {}, [drawing]
    for i, (stream, cm) in enumerate(xobjects):
        names[f"/Fx{i}"] = pdf.make_stream(stream, Type=Name.XObject, Subtype=Name.Form,
                                           BBox=Array([0, 0, 100, 14]))
        body.append(f"q {cm} cm /Fx{i} Do Q\n".encode())
    page.obj.Resources = Dictionary(XObject=Dictionary(names))
    page.obj.Contents = pdf.make_stream(b"".join(body))
    if fields:
        widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx,
                                              T=pikepdf.String("name"), Rect=Array([72, 600, 300, 620]),
                                              P=page.obj))
        page.obj.Annots = Array([widget])
        pdf.Root.AcroForm = Dictionary(Fields=Array([widget]))
    pdf.save(path)
    return path


TEXT_FIELD = b"/Tx BMC q BT /Helv 9 Tf 2 4 Td (Ann) Tj ET Q EMC"


def test_the_code_is_a_page_warning_at_weight_zero():
    spec = CODES[flattened.FACT]
    assert (spec.weight, spec.severity, spec.scope) == (0.0, "warning", "page")


def test_the_text_fields_a_page_draws_are_traces_where_they_are_drawn(tmp_path):
    pdf = _form_page(tmp_path / "a.pdf", [(TEXT_FIELD, "1 0 0 1 72 692"),
                                          (TEXT_FIELD, "1 0 0 1 300 600")])
    assert flattened.find(pdf) == {1: [(72.0, 86.0, 172.0, 100.0), (300.0, 178.0, 400.0, 192.0)]}


def test_one_text_field_alone_is_a_stamp_not_a_form(tmp_path):
    assert flattened.find(_form_page(tmp_path / "a.pdf", [(TEXT_FIELD, "1 0 0 1 72 692")])) == {}


def test_ruled_boxes_a_table_and_drawn_squares_are_not_traces(tmp_path):
    grid = b"".join(f"{72 + 60 * c} {600 - 20 * r} 60 20 re S\n".encode()
                    for r in range(5) for c in range(4))
    squares = b"".join(f"{72 + 30 * i} 300 8 8 re S\n".encode() for i in range(6))
    plain = b"q 0 0 1 rg 0 0 100 14 re f Q"   # a form XObject that marks no /Tx
    pdf = _form_page(tmp_path / "a.pdf", [(plain, "1 0 0 1 72 692"), (plain, "1 0 0 1 72 500")],
                     drawing=grid + squares)
    assert flattened.find(pdf) == {}


def test_a_document_that_still_has_its_fields_is_skipped(tmp_path):
    pdf = _form_page(tmp_path / "a.pdf", [(TEXT_FIELD, "1 0 0 1 72 692"),
                                          (TEXT_FIELD, "1 0 0 1 300 600")], fields=True)
    assert flattened.find(pdf) == {}


@pytest.mark.parametrize("relative", NOT_FLATTENED)
def test_the_samples_that_draw_boxes_without_a_form_get_no_fact(relative):
    path = sample(REPO, relative)
    if not path.exists():
        pytest.skip(f"{relative} is not in this checkout or the main one")
    assert flattened.find(path) == {}


@needs_qpdf
@pytest.mark.parametrize("relative", ["samples/fw9.pdf", "samples/corpus/uscis-i9.pdf"])
def test_a_flattened_form_has_traces_on_its_form_pages_where_its_fields_were(tmp_path, relative):
    clean = REPO / relative
    assert flattened.find(clean) == {}
    found = flattened.find(_flatten(clean, tmp_path / "flat.pdf"))
    widgets = read_widgets(clean)
    assert found and set(found) <= {n for n, ws in widgets.items() if ws}
    for number, boxes in found.items():
        assert len(boxes) >= flattened.MIN_TRACES
        # Every trace is one of the page's own fields, at its place.
        assert all(any(_near(box, w.bbox) for w in widgets[number]) for box in boxes), number


@needs_qpdf
def test_a_converted_flattened_form_has_the_fact_and_a_review_item_over_its_fields(tmp_path):
    out = tmp_path / "out"
    convert(_flatten(REPO / "samples/fw9.pdf", tmp_path / "flat.pdf"), out, page_images=False)
    doc = json.loads((out / DOCUMENT_JSON).read_text(encoding="utf-8"))
    pages = {p["number"]: [f for f in p.get("facts", []) if f["code"] == flattened.FACT]
             for p in doc["pages"]}
    assert [n for n, facts in pages.items() if facts] == [1]
    review = json.loads((out / "review.json").read_text(encoding="utf-8"))
    items = review if isinstance(review, list) else review["items"]
    [item] = [i for i in items if i["id"] == "page:1"]
    assert item["warn"] == flattened.FACT and len(item["targets"]) == len(
        pages[1][0]["data"]["region_boxes"]) // 4
