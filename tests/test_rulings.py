"""extract.rulings: pdfium's paths tell which pages pdfplumber can find a ruled table on."""

import pikepdf
import pypdfium2 as pdfium
from conftest import make_text_pdf

from reflow.extract.parallel import extract_chunk
from reflow.extract.rulings import page_edges, ruled_pages, worth_parsing
from reflow.extract.tables.ruled import find_tables


def _with_ops(path, ops: list[str]):
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Contents = pdf.make_stream(
            pdf.pages[0].Contents.read_bytes() + b"\n" + "\n".join(ops).encode())
        pdf.save(path)
    return path


def _grid_ops(xs, ys, *, as_rects=False):
    if as_rects:
        return [f"{xs[i]} {ys[j]} {xs[i + 1] - xs[i]} {ys[j + 1] - ys[j]} re S"
                for i in range(len(xs) - 1) for j in range(len(ys) - 1)]
    return ([f"{xs[0]} {y} m {xs[-1]} {y} l S" for y in ys]
            + [f"{x} {ys[0]} m {x} {ys[-1]} l S" for x in xs])


TEXT = [[(60, 112, 10, "a"), (160, 112, 10, "b"), (60, 142, 10, "c"), (160, 142, 10, "d")]]


def _worth(path) -> bool:
    doc = pdfium.PdfDocument(path)
    try:
        page = doc[0]
        try:
            return worth_parsing(page)
        finally:
            page.close()
    finally:
        doc.close()


def test_a_page_with_no_paths_has_no_edges(tmp_path):
    path = make_text_pdf(tmp_path / "plain.pdf", TEXT)
    assert not _worth(path)


def test_a_grid_of_lines_is_worth_parsing(tmp_path):
    path = _with_ops(make_text_pdf(tmp_path / "grid.pdf", TEXT),
                     _grid_ops([50, 150, 250], [700, 670, 640]))
    assert _worth(path)
    assert find_tables(path, [1])


def test_a_grid_of_rectangles_is_worth_parsing(tmp_path):
    path = _with_ops(make_text_pdf(tmp_path / "rects.pdf", TEXT),
                     _grid_ops([50, 150, 250], [700, 670, 640], as_rects=True))
    edges = page_edges(pdfium.PdfDocument(path)[0])
    assert sum(e["orientation"] == "h" for e in edges) == 8
    assert _worth(path)


def test_a_single_column_of_rows_is_not_worth_parsing(tmp_path):
    # Two rules and no verticals: underlines, not a grid. And a one-column ruled list (two
    # verticals, many rows) is dropped by find_tables, so it needs no parse either.
    path = _with_ops(make_text_pdf(tmp_path / "list.pdf", TEXT),
                     _grid_ops([50, 250], [700, 670, 640, 610]))
    assert not _worth(path)
    assert not find_tables(path, [1])


def test_a_slanted_line_is_not_a_rule(tmp_path):
    path = _with_ops(make_text_pdf(tmp_path / "slant.pdf", TEXT),
                     ["50 700 m 250 650 l S", "50 650 m 250 600 l S"])
    assert not any(e["orientation"] == "h" for e in page_edges(pdfium.PdfDocument(path)[0]))
    assert not _worth(path)


def test_ruled_pages_picks_only_the_pages_that_can_hold_a_table(tmp_path):
    path = _with_ops(make_text_pdf(tmp_path / "two.pdf", TEXT * 2),
                     _grid_ops([50, 150, 250], [700, 670, 640]))
    assert ruled_pages(path, [1, 2]) == [1]
    assert ruled_pages(path, []) == []


def test_extract_chunk_finds_the_same_table_as_pdfplumber_alone(tmp_path):
    path = _with_ops(make_text_pdf(tmp_path / "grid.pdf", TEXT),
                     _grid_ops([50, 150, 250], [700, 670, 640]) * 2)
    pages, tables = extract_chunk(path, 1, 1)
    assert tables == find_tables(path, [1])
    assert [r for t in tables[1] for r in t.rows] == [["a", "b"], ["c", "d"]]
