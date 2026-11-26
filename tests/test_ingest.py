import pdfplumber
import pikepdf
import pytest

from conftest import make_pdf
from reflow.identity import file_sha256
from reflow.ingest.load import normalize
from reflow.ingest.pages import ingest
from reflow.ingest.load import open_pdf, restricted_action
from reflow.model import PageKind


def _owner_locked(tmp_path, name="locked.pdf"):
    """A PDF with an empty user password whose owner forbids extraction and modification."""
    src = make_pdf(tmp_path / "plain.pdf", ["text"], title="Locked")
    dst = tmp_path / name
    with pikepdf.open(src) as pdf:
        pdf.save(dst, encryption=pikepdf.Encryption(
            user="", owner="x",
            allow=pikepdf.Permissions(extract=False, modify_other=False)))
    return dst


def test_page_classification(mixed_pdf):
    doc = ingest(mixed_pdf)
    assert [p.kind for p in doc.pages] == [
        PageKind.BORN_DIGITAL, PageKind.SCANNED, PageKind.MIXED, PageKind.EMPTY,
    ]
    assert doc.pages[0].char_count == len("Helloaccessibleworld")
    assert doc.pages[1].image_coverage == 1.0
    assert (doc.pages[0].width, doc.pages[0].height) == (612, 792)


def test_metadata(mixed_pdf):
    doc = ingest(mixed_pdf)
    assert doc.meta["title"] == "Annual Report"
    assert doc.meta["lang"] == "en-US"
    assert doc.meta["page_count"] == 4
    assert doc.meta["tagged"] is False
    assert len(doc.meta["sha256"]) == 64
    assert doc.root.attrs == {"title": "Annual Report", "lang": "en-US"}


def test_missing_metadata(bare_pdf):
    meta = ingest(bare_pdf).meta
    assert meta["title"] is None
    assert meta["lang"] is None
    assert meta["encrypted"] is False


def test_tagged(tmp_path):
    meta = ingest(make_pdf(tmp_path / "t.pdf", ["text"], tagged=True)).meta
    assert meta["tagged"] is True
    assert meta["has_struct_tree"] is False  # marked but no tree: a common broken state


def test_password(tmp_path):
    path = make_pdf(tmp_path / "locked.pdf", ["text"], password="secret")
    with pytest.raises(pikepdf.PasswordError):
        ingest(path)
    doc = ingest(path, password="secret")
    assert doc.meta["encrypted"] is True
    assert doc.pages[0].kind == PageKind.BORN_DIGITAL


def test_progress_callback(mixed_pdf):
    calls = []
    ingest(mixed_pdf, on_page=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_normalize_linearizes(mixed_pdf, tmp_path):
    out = tmp_path / "web.pdf"
    normalize(mixed_pdf, out)
    with pikepdf.open(out) as pdf:
        assert pdf.is_linearized
        assert len(pdf.pages) == 4


def test_normalize_decrypts(tmp_path):
    out = tmp_path / "open.pdf"
    normalize(make_pdf(tmp_path / "locked.pdf", ["text"], password="pw"), out, "pw")
    with pikepdf.open(out) as pdf:
        assert not pdf.is_encrypted


def test_normalize_makes_junk_prefixed_pdf_readable_by_pdfplumber(mixed_pdf, tmp_path):
    # Same shape as samples/naca-ocr.pdf: a linearized file behind a 436-byte wrapper.
    # pdfminer trusts the linearized xref, whose offsets are now wrong, and finds no
    # pages; a non-linearized file with the same junk is recovered by its fallback scan.
    linearized = tmp_path / "lin.pdf"
    with pikepdf.open(mixed_pdf) as pdf:
        pdf.save(linearized, linearize=True)
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"\x00" * 436 + linearized.read_bytes())
    with pdfplumber.open(junk) as pdf:
        assert len(pdf.pages) == 0  # the bug this guards against

    out = tmp_path / "clean.pdf"
    normalize(junk, out)
    with pdfplumber.open(out) as pdf:
        assert len(pdf.pages) == 4
        assert "Hello accessible world" in pdf.pages[0].extract_text()


def test_normalize_working_copy_is_not_encrypted(tmp_path):
    locked = _owner_locked(tmp_path)
    work = tmp_path / "working.pdf"
    normalize(locked, work)
    with pikepdf.open(work) as copy:
        assert copy.is_encrypted is False


def test_restricted_action_refuses_by_default():
    assert restricted_action(allows_extract=True, allow_restricted=False) == "proceed"
    assert restricted_action(allows_extract=True, allow_restricted=True) == "proceed"
    assert restricted_action(allows_extract=False, allow_restricted=False) == "refuse"
    assert restricted_action(allows_extract=False, allow_restricted=True) == "flag"


def test_ingest_of_the_working_copy_has_the_pages_and_facts_of_the_original(mixed_pdf, tmp_path):
    # The damaged shape from the test above: qpdf repairs it, the working copy is what every
    # stage reads, and the facts about the file the user gave must still come from the original.
    linearized = tmp_path / "lin.pdf"
    with pikepdf.open(mixed_pdf) as pdf:
        pdf.save(linearized, linearize=True)
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"\x00" * 436 + linearized.read_bytes())
    working = tmp_path / "working.pdf"
    normalize(junk, working)

    doc = ingest(working, original=junk)
    assert [p.kind for p in doc.pages] == [p.kind for p in ingest(junk).pages]
    assert len(doc.pages) == 4
    assert doc.meta["sha256"] == file_sha256(junk)
    assert doc.meta["filename"] == "junk.pdf"


def test_ingest_of_the_decrypted_working_copy_keeps_the_security_facts(tmp_path):
    locked = make_pdf(tmp_path / "locked.pdf", ["text"], password="pw")
    working = tmp_path / "working.pdf"
    normalize(locked, working, "pw")

    doc = ingest(working, "pw", original=locked)
    assert doc.meta["encrypted"] is True
    assert doc.pages[0].kind == PageKind.BORN_DIGITAL
    assert doc.meta["sha256"] == file_sha256(locked)


def test_a_file_that_is_not_a_pdf_gets_a_plain_error(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    with pytest.raises(pikepdf.PdfError, match="not a PDF, or too damaged to repair"):
        open_pdf(junk)


def test_ingest_records_the_files_page_labels(tmp_path):
    from pikepdf import Array, Dictionary, Name
    path = make_pdf(tmp_path / "labelled.pdf", ["text", "text", "text"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.PageLabels = Dictionary(Nums=Array([
            0, Dictionary(S=Name.r), 2, Dictionary(S=Name.D, P="A-")]))
        pdf.save(path)
    assert [p.label for p in ingest(path).pages] == ["i", "ii", "A-1"]


def test_ingest_leaves_the_label_empty_when_the_file_declares_none(mixed_pdf):
    assert [p.label for p in ingest(mixed_pdf).pages] == ["", "", "", ""]


def test_a_text_free_page_with_a_logo_sized_image_is_scanned_so_its_words_get_read(tmp_path):
    # A back cover: a centred logo (about a quarter of the page) with its name drawn in the
    # picture and no text layer. It must go to OCR, not be classed as an empty page.
    pdf = pikepdf.new()
    image = pikepdf.Stream(pdf, bytes(100 * 100), Type=pikepdf.Name.XObject,
                           Subtype=pikepdf.Name.Image, Width=100, Height=100,
                           ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image))
    page.Contents = pdf.make_stream(b"q 359 0 0 372 126 210 cm /Im0 Do Q\n")
    path = tmp_path / "back-cover.pdf"
    pdf.save(path)
    assert ingest(path).pages[0].kind is PageKind.SCANNED
