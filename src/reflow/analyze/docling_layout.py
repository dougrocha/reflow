"""Alternative layout engine: Docling's ML models (layout, TableFormer, reading order).

Optional: needs `uv sync --extra docling`. Runs on CUDA when available. Produces the
same node tree as reflow.analyze.layout.analyze, so the rest of the pipeline (figures,
page images, HTML, review) is shared and the two engines can be compared directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from reflow.analyze.constants import CONF_FIGURE_NO_ALT, DOCLING_TITLE_SIZE
from reflow.analyze.local_model import free_cuda
from reflow import budget
from reflow.model import BBox, HEADING_BY_LEVEL, Node, Role, Source

# Docling doesn't expose per-item confidence in its document; use one value per engine.
CONF_DOCLING = 0.85


def make_converter(device: str = "auto", ocr: bool = False, threads: int = 8):
    """A Docling converter for PDFs. `device` is "auto", "cuda", "mps" or "cpu"."""
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = ocr
    options.do_table_structure = True
    options.accelerator_options = AcceleratorOptions(
        device=AcceleratorDevice(device), num_threads=threads)
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def analyze_docling(pdf: Path, *, device: str = "auto", ocr: bool = False,
                    converter=None) -> list[Node]:
    with on_gpu(device):
        result = (converter or make_converter(device=device, ocr=ocr)).convert(pdf)
    return to_nodes(result.document)


@contextmanager
def on_gpu(device: str) -> Iterator[None]:
    """The GPU lease (reflow.budget.gpu) for a Docling stage on cuda, and the models' memory
    handed back to the card when it ends, so the next holder finds it free."""
    from reflow.device import resolve_device
    device = resolve_device(device)
    with budget.gpu(device):
        try:
            yield
        finally:
            if device == "cuda":
                free_cuda()


def to_nodes(doc: Any) -> list[Node]:
    from docling_core.types.doc import DocItemLabel as L

    nodes: list[Node] = []
    skip: set[str] = set()  # captions already attached to a figure or table
    for item, page, bbox, text in _placed_items(doc):
        ref = item.self_ref
        if ref in skip:
            continue
        label = item.label

        if label in (L.PAGE_HEADER, L.PAGE_FOOTER):
            nodes.append(_node(Role.ARTIFACT, page, bbox, text))
        elif label == L.TITLE:
            # Ranked above every section heading; choose_title promotes it to h1.
            nodes.append(_node(Role.H2, page, bbox, text, font_size=DOCLING_TITLE_SIZE))
        elif label == L.SECTION_HEADER:
            level = min(int(getattr(item, "level", 1) or 1) + 1, 6)
            nodes.append(_node(HEADING_BY_LEVEL[level - 1], page, bbox, text))
        elif label == L.LIST_ITEM:
            _add_list_item(nodes, item, page, bbox, text)
        elif label in (L.PICTURE, L.CHART):
            node = Node(Role.FIGURE, page=page, bbox=bbox, confidence=CONF_FIGURE_NO_ALT,
                        source=Source.DOCLING, attrs={"needs": "alt"})
            node.children += _captions(doc, item, skip)
            nodes.append(node)
        elif label == L.TABLE:
            nodes.append(_table(doc, item, page, bbox, skip))
        elif label == L.FOOTNOTE and text:
            nodes.append(_node(Role.NOTE, page, bbox, text))
        elif label == L.CAPTION and text:
            nodes.append(_node(Role.CAPTION, page, bbox, text))
        elif text:
            nodes.append(_node(Role.P, page, bbox, text))

    from reflow.analyze.headings import fix_heading_levels
    fix_heading_levels(nodes)
    return nodes


def _where(doc: Any, item: Any) -> tuple[int | None, BBox | None]:
    if not getattr(item, "prov", None):
        return None, None
    prov = item.prov[0]
    return prov.page_no, _bbox(doc, prov)


def _bbox(doc: Any, prov: Any) -> BBox:
    height = doc.pages[prov.page_no].size.height
    l, t, r, b = prov.bbox.to_top_left_origin(page_height=height).as_tuple()
    return (round(l, 2), round(t, 2), round(r, 2), round(b, 2))


# Docling's charspans can be a few characters off where it merged and dehyphenated
# elements across a page break, so a run boundary snaps to a space this close to it.
SNAP_CHARS = 8


def _placed_items(doc: Any):
    """(item, page, bbox, text) for each piece of each item, in reading order."""
    for item, _level in doc.iterate_items():
        text = (getattr(item, "text", "") or "").strip()
        for page, bbox, piece in _placed(doc, item, text):
            yield item, page, bbox, piece


def _placed(doc: Any, item: Any, text: str) -> list[tuple[int | None, BBox | None, str]]:
    """An item's text as (page, bbox, text) pieces, one per run of consecutive provenance
    on a page. Docling can merge a paragraph across a page break into one item; its text
    is then split at the charspans, so each part keeps its own page. Items without text,
    and items whose spans can't be matched to the text, stay whole on their first page."""
    provs = list(getattr(item, "prov", None) or [])
    runs: list[list[Any]] = []
    for prov in provs:
        if runs and runs[-1][-1].page_no == prov.page_no:
            runs[-1].append(prov)
        else:
            runs.append([prov])
    whole = [(*_where(doc, item), text)]
    if len(runs) <= 1 or not hasattr(item, "text"):
        return whole
    starts = [run[0].charspan[0] for run in runs]
    points = _split_points(text, starts)
    if points is None:
        return whole
    edges = [0, *points, len(text)]
    out = []
    for run, lo, hi in zip(runs, edges, edges[1:]):
        piece = text[lo:hi].strip()
        if not piece:
            continue
        boxes = [_bbox(doc, prov) for prov in run]
        bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
        out.append((run[0].page_no, bbox, piece))
    kept = "".join("".join(piece.split()) for _, _, piece in out)
    if kept != "".join(text.split()):
        return whole  # defensive: never lose or invent characters
    return out


def _split_points(text: str, starts: list[int]) -> list[int] | None:
    """Where each run after the first begins in `text`: the space nearest its declared
    start (within SNAP_CHARS). None if a boundary can't be found in order."""
    points: list[int] = []
    prev = 0
    for declared in starts[1:]:
        candidates = [p for p in range(max(declared - SNAP_CHARS, prev + 1),
                                       min(declared + SNAP_CHARS, len(text)))
                      if text[p].isspace()]
        if not candidates:
            return None
        point = min(candidates, key=lambda p: (abs(p - declared), p))
        points.append(point)
        prev = point
    return points


def _node(role: Role, page: int | None, bbox: BBox | None, text: str, **attrs) -> Node:
    return Node(role, page=page, bbox=bbox, text=text, attrs=attrs,
                confidence=CONF_DOCLING, source=Source.DOCLING)


def _add_list_item(nodes: list[Node], item: Any, page, bbox, text: str) -> None:
    ordered = bool(getattr(item, "enumerated", False))
    marker = (getattr(item, "marker", "") or "").strip()
    parent = getattr(getattr(item, "parent", None), "cref", None)
    li = _node(Role.LI, page, bbox, text, marker=marker)
    last = nodes[-1] if nodes else None
    # A list is split at a page break: its container page decides where its items render, so
    # a continuation starts a new list on its own page. Its printed markers keep the numbering.
    if not (last and last.role == Role.LIST and last.page == page
            and last.attrs.get("parent") == parent and last.attrs.get("ordered") == ordered):
        last = Node(Role.LIST, page=page, bbox=bbox, confidence=CONF_DOCLING,
                    source=Source.DOCLING, attrs={"ordered": ordered, "parent": parent})
        nodes.append(last)
    last.children.append(li)


def _captions(doc: Any, item: Any, skip: set[str]) -> list[Node]:
    out = []
    for ref in getattr(item, "captions", []) or []:
        cap = ref.resolve(doc)
        skip.add(cap.self_ref)
        page, bbox = _where(doc, cap)
        if cap.text:
            out.append(_node(Role.CAPTION, page, bbox, cap.text.strip()))
    return out


def _table(doc: Any, item: Any, page, bbox, skip: set[str]) -> Node:
    """TableFormer's grid → TABLE/TR/TH/TD. Spanned cells repeat in the grid; emit each
    cell once, at its top-left position, with colspan/rowspan."""
    table = Node(Role.TABLE, page=page, bbox=bbox, confidence=CONF_DOCLING,
                 source=Source.DOCLING)
    table.children += _captions(doc, item, skip)
    for r, row in enumerate(item.data.grid):
        tr = Node(Role.TR, page=page, text=f"row {r}", source=Source.DOCLING)
        for c, cell in enumerate(row):
            if cell.start_row_offset_idx != r or cell.start_col_offset_idx != c:
                continue  # covered by a spanning cell
            attrs: dict = {}
            if cell.col_span > 1:
                attrs["colspan"] = cell.col_span
            if cell.row_span > 1:
                attrs["rowspan"] = cell.row_span
            if cell.column_header:
                role = Role.TH
            elif cell.row_header:
                role, attrs["scope"] = Role.TH, "row"
            else:
                role = Role.TD
            tr.children.append(Node(role, page=page, text=(cell.text or "").strip(),
                                    attrs=attrs, confidence=CONF_DOCLING,
                                    source=Source.DOCLING))
        table.children.append(tr)
    return table
