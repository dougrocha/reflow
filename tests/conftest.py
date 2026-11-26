"""Synthetic PDF fixtures built with pikepdf, so tests don't depend on binary files."""

from pathlib import Path

import os
import sys
from contextlib import ExitStack

import pikepdf
import pytest
from pikepdf import Dictionary, Name

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))   # checkout.py, benchlib.py

LETTER = (612, 792)


def make_pdf(path: Path, pages: list[str], *, title=None, lang=None, tagged=False,
             password=None, owner_only=False) -> Path:
    """Each entry in `pages` is "text", "scan", "scan+text" or "empty". `owner_only` writes a
    PDF that opens without a password but whose owner forbids text extraction."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    image = pikepdf.Stream(pdf, bytes(100 * 100), Type=Name.XObject, Subtype=Name.Image,
                           Width=100, Height=100, ColorSpace=Name.DeviceGray,
                           BitsPerComponent=8)
    for kind in pages:
        page = pdf.add_blank_page(page_size=LETTER)
        page.Resources = Dictionary(Font=Dictionary(F1=font),
                                    XObject=Dictionary(Im0=image))
        ops = b""
        if kind.startswith("scan"):
            ops += b"q 612 0 0 792 0 0 cm /Im0 Do Q\n"
        if kind in ("text", "scan+text"):
            ops += b"BT /F1 12 Tf 72 720 Td (Hello accessible world) Tj ET\n"
        page.Contents = pdf.make_stream(ops)

    if title:
        pdf.docinfo["/Title"] = title
    if lang:
        pdf.Root.Lang = lang
    if tagged:
        pdf.Root.MarkInfo = Dictionary(Marked=True)
    if owner_only:
        encryption = pikepdf.Encryption(user="", owner="owner",
                                        allow=pikepdf.Permissions(extract=False))
    else:
        encryption = pikepdf.Encryption(user=password, owner="owner") if password else False
    pdf.save(path, encryption=encryption)
    return path


FONTS = {"R": "Helvetica", "B": "Helvetica-Bold", "I": "Helvetica-Oblique"}


def make_text_pdf(path: Path, pages: list[list[tuple]], size=LETTER) -> Path:
    """Each page is a list of (x, top, font_size, text) or (x, top, font_size, text, style),
    with style "R", "B" or "I". `top` is measured from the top of the page, like the model.
    """
    pdf = pikepdf.new()
    fonts = Dictionary({
        f"/F{k}": pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                               BaseFont=Name("/" + base),
                                               Encoding=Name.WinAnsiEncoding))
        for k, base in FONTS.items()
    })
    for items in pages:
        page = pdf.add_blank_page(page_size=size)
        page.Resources = Dictionary(Font=fonts)
        ops = []
        for x, top, pt, text, *style in items:
            baseline = size[1] - top - pt  # place the glyph box roughly at `top`
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /F{style[0] if style else 'R'} {pt} Tf {x} {baseline} Td "
                       f"({escaped}) Tj ET")
        page.Contents = pdf.make_stream("\n".join(ops).encode("cp1252"))
    pdf.save(path)
    return path


def make_kerned_table_pdf(path: Path) -> Path:
    """A ruled 2 x 2 table of 8 pt Helvetica whose word spaces are TJ kerning, as pdfTeX writes
    them, and whose digits are kerned tight: "two words", "2553 electing", "1706" and "W-9"."""
    cells = [["[(two)-278(words)]", "[(2553)-278(electing)]"],
             ["[(1)-20(7)-20(0)-20(6)]", "[(W)80(-9)]"]]
    ops = []
    for r, row in enumerate(cells):
        for c, shown in enumerate(row):
            ops.append(f"BT /F1 8 Tf {80 + 180 * c} {792 - 115 - 20 * r} Td {shown} TJ ET")
    for y in (100, 120, 140):
        ops.append(f"72 {792 - y} m 432 {792 - y} l S")
    for x in (72, 252, 432):
        ops.append(f"{x} {792 - 100} m {x} {792 - 140} l S")
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=LETTER)
    page.Resources = Dictionary(Font=Dictionary(F1=pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
        Encoding=Name.WinAnsiEncoding))))
    page.Contents = pdf.make_stream("\n".join(ops).encode())
    pdf.save(path)
    return path


def make_scan_pdf(path: Path, pages: list[list[tuple]], visible: bool = False) -> Path:
    """Scanned Letter pages as OCRmyPDF leaves them: a page image at 1 px/pt holding each line,
    and the same words over it as invisible text (drawn, with `visible`, as some OCR tools
    leave it). Each page is a list of (top, text) or (top, text, heavy); a heavy line is drawn
    with a 1 px stroke around its glyphs, as bold type prints."""
    import zlib

    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=12)
    ascent = font.getmetrics()[0]
    pdf = pikepdf.new()
    helvetica = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                             BaseFont=Name.Helvetica))
    for items in pages:
        image = Image.new("L", (612, 792), 255)
        draw = ImageDraw.Draw(image)
        ops = "q 612 0 0 792 0 0 cm /Im0 Do Q\n"
        for top, text, *heavy in items:
            draw.text((72, top), text, fill=0, font=font, stroke_width=1 if heavy and heavy[0] else 0,
                      stroke_fill=0)
            ops += f"BT {0 if visible else 3} Tr /F1 12 Tf 72 {792 - top - ascent} Td ({text}) Tj ET\n"
        stream = pikepdf.Stream(pdf, zlib.compress(image.tobytes()), Type=Name.XObject,
                                Subtype=Name.Image, Width=612, Height=792,
                                ColorSpace=Name.DeviceGray, BitsPerComponent=8,
                                Filter=Name.FlateDecode)
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = Dictionary(Font=Dictionary(F1=helvetica), XObject=Dictionary(Im0=stream))
        page.Contents = pdf.make_stream(ops.encode())
    pdf.save(path)
    return path


@pytest.fixture
def mixed_pdf(tmp_path):
    return make_pdf(tmp_path / "mixed.pdf", ["text", "scan", "scan+text", "empty"],
                    title="Annual Report", lang="en-US")


@pytest.fixture
def bare_pdf(tmp_path):
    return make_pdf(tmp_path / "bare.pdf", ["text"])


def make_tagged_pdf(path: Path) -> Path:
    """Two Letter pages in Courier (every glyph is 0.6 em wide) with a structure tree.

    Page 1: /H1 "Chapter One" (24 pt at x=72, top=72), /P, a /Figure with /Alt over an image
    (72, 242)-(172, 292), and a /P that continues on page 2. Page 2: the rest of that /P, a
    custom /Heading2 that /RoleMap sends to /H2, and a /Table row of a /TH (scope Column) and
    a /TD. The outline has "Chapter One" (page 1, /XYZ, top 72), its child "Details" (page 2,
    /FitH, through the /Names /Dests name tree) and "Table" (page 2, through the old
    /Root /Dests dictionary)."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Courier))
    image = pikepdf.Stream(pdf, bytes(100 * 50), Type=Name.XObject, Subtype=Name.Image,
                           Width=100, Height=50, ColorSpace=Name.DeviceGray,
                           BitsPerComponent=8)
    pages = [pdf.add_blank_page(page_size=LETTER) for _ in range(2)]

    def text(tag, mcid, size, x, top, string):
        return (f"/{tag} << /MCID {mcid} >> BDC BT /F1 {size} Tf {x} {792 - top - size} Td "
                f"({string}) Tj ET EMC\n")

    streams = [
        text("H1", 0, 24, 72, 72, "Chapter One") + text("P", 1, 12, 72, 120, "First paragraph.")
        + "/Figure << /MCID 2 >> BDC q 100 0 0 50 72 500 cm /Im0 Do Q EMC\n"
        + text("P", 3, 12, 72, 400, "A paragraph that"),
        text("P", 0, 12, 72, 72, "continues here.") + text("Heading2", 1, 18, 72, 120, "Details")
        + text("TH", 2, 12, 72, 200, "Name") + text("TD", 3, 12, 200, 200, "Value"),
    ]
    for page, ops in zip(pages, streams):
        page.Resources = Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im0=image))
        page.Contents = pdf.make_stream(ops.encode("latin-1"))

    def elem(tag, kids, parent, pg=None, **extra):
        d = Dictionary(Type=Name.StructElem, S=Name("/" + tag), P=parent, K=kids, **extra)
        if pg is not None:
            d.Pg = pg
        return pdf.make_indirect(d)

    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,
                                        RoleMap=Dictionary(Heading2=Name.H2)))
    doc = elem("Document", [], root)
    p0, p1 = pages[0].obj, pages[1].obj
    mcr = Dictionary(Type=Name.MCR, Pg=p1, MCID=0)
    table = elem("Table", [], doc, p1)
    row = elem("TR", [], table, p1)
    row.K = [elem("TH", [2], row, p1, A=Dictionary(O=Name.Table, Scope=Name.Column)),
             elem("TD", [3], row, p1)]
    table.K = [row]
    doc.K = [elem("H1", [0], doc, p0), elem("P", [1], doc, p0),
             elem("Figure", [2], doc, p0, Alt=pikepdf.String("A grey box")),
             elem("P", [3, mcr], doc, p0), elem("Heading2", [1], doc, p1), table]
    root.K = doc
    pdf.Root.StructTreeRoot = root
    pdf.Root.MarkInfo = Dictionary(Marked=True)

    pdf.Root.Names = Dictionary(Dests=pdf.make_indirect(Dictionary(Names=[
        pikepdf.String("details"), Dictionary(D=[p1, Name.FitH, 700])])))
    pdf.Root.Dests = Dictionary(tbl=[p1, Name.FitH, 600])
    outline = pdf.make_indirect(Dictionary(Type=Name.Outlines))
    chapter, details, table_mark = (pdf.make_indirect(Dictionary(Title=pikepdf.String(t),
                                                                 Parent=outline))
                                    for t in ("Chapter One", "Details", "Table"))
    chapter.Dest = [p0, Name.XYZ, 0, 720, 0]
    details.Parent = chapter
    details.A = Dictionary(S=Name.GoTo, D=pikepdf.String("details"))
    table_mark.Dest = Name.tbl
    chapter.First = chapter.Last = details
    chapter.Next = table_mark
    table_mark.Prev = chapter
    outline.First, outline.Last = chapter, table_mark
    pdf.Root.Outlines = outline
    pdf.save(path)
    return path


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--quick", action="store_true",
                     help="page checks: convert only the tested pages of long documents (with "
                          "neighbours), not whole documents. For iterating; run the full suite "
                          "before merging.")
    parser.addoption("--robustness", action="store_true",
                     help="collect the robustness suite (tests/robustness): damage good PDFs on "
                          "purpose and score what reflow publishes. Run it with -m pages.")
    parser.addoption("--robustness-out", default=None,
                     help="robustness: the folder for scorecard.md and run.json (default: the "
                          "session's temporary folder)")
    parser.addoption("--robustness-doc", action="store_true",
                     help="robustness: also write the scorecard into docs/robustness-scorecard.md")


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """The robustness suite is collected only with --robustness, so neither the unit suite nor
    `-m pages` sees it."""
    if collection_path.name == "robustness" and collection_path.parent == Path(__file__).parent:
        return None if config.getoption("--robustness") else True
    return None


FAST_SUITE_WORKERS = pytest.StashKey[int]()


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """The whole fast suite (no paths, no -m, no -n) runs on pytest-xdist workers, as many as
    the machine's worker budget (reflow.budget) gives this run now, held until the session
    ends. A narrower run, the page checks, or a budget share under 2 stays in one process."""
    if (not config.pluginmanager.hasplugin("xdist") or config.option.numprocesses is not None
            or config.args_source == pytest.Config.ArgsSource.ARGS
            or config.option.markexpr != "not pages" or config.option.collectonly
            or config.option.usepdb or os.environ.get("PYTEST_XDIST_WORKER")):
        return
    from reflow import budget
    held = ExitStack()
    config.add_cleanup(held.close)
    # pytest-xdist starts its workers up front and keeps them for the session, so they cannot be
    # metered task by task as a pool of reflow's is (`Lease.meter`): they hold their slots until
    # the session ends. With fewer than 2 free, the suite runs serially and holds nothing.
    lease = held.enter_context(budget.lease())
    workers = lease.take(budget.size() or os.cpu_count() or 1)
    if workers < 2:
        lease.give(workers)
        workers = 1
    config.stash[FAST_SUITE_WORKERS] = workers
    if workers > 1:   # set what xdist's own pytest_cmdline_main would for -n <workers>
        config.option.numprocesses, config.option.dist = workers, "load"
        config.option.tx = ["popen"] * workers


def pytest_sessionstart(session: pytest.Session) -> None:
    """Says how many workers the budget gave the fast suite, even under -q: a run that got
    none is serial, and takes about a minute and a half."""
    workers = session.config.stash.get(FAST_SUITE_WORKERS, None)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if workers is not None and reporter is not None:
        reporter.write_line(f"fast suite: {workers} xdist workers from the worker budget"
                            if workers > 1 else
                            "fast suite: serial, the worker budget has no 2 slots free")


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--quick"):
        os.environ["REFLOW_PAGE_CHECKS_QUICK"] = "1"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A page-check session ends by evicting from the shared caches by size and age
    (tests/pages/corpus.py `prune`), and says what it removed. A session that only collects
    never prunes: the fast suite collects the page checks in nested sessions. Neither does a
    session of the robustness suite alone, which reads the cache and must leave it untouched."""
    corpus = sys.modules.get("corpus")
    if corpus is None or session.config.option.collectonly \
            or not any(item.get_closest_marker("pages") and "robustness" not in item.path.parts
                       for item in session.items):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    for report in corpus.prune():
        if reporter is not None:
            reporter.write_line(f"page-check cache: {report.summary()}")


@pytest.fixture(autouse=True)
def strict_html_attributes(monkeypatch):
    monkeypatch.setenv("REFLOW_STRICT_HTML_ATTRIBUTES", "1")


@pytest.fixture(autouse=True)
def private_gpu_lease(request, monkeypatch):
    """Unit tests talk to fake model servers on localhost, so they never wait for the machine's
    GPU lease (reflow.budget.gpu) behind another run's real model. Page checks run real models
    and take the real lease."""
    if request.node.get_closest_marker("pages") is None:
        monkeypatch.setenv("REFLOW_GPU_HOLDERS", "0")


def extract_cached(root: Path, pdf: Path, working: Path):
    """(pages and tables, hit): the extraction of `working` through the stage cache, as a
    conversion asks for it. The first call for a working copy is a miss that saves it."""
    from functools import partial

    from reflow import stagecache
    from reflow.extract.parallel import extract_document
    return stagecache.cached_extract(root, pdf, working, partial(extract_document, working, 1))
