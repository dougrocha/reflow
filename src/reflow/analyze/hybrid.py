"""Hybrid engine: the heuristic engine on every page, Docling (CUDA) only on hard pages.

The heuristic engine takes ~25 ms a page; Docling ~0.8 s a page on an RTX 3090. Most
pages of most documents are plain running text the heuristics handle well, so only the
pages where they're known to be weak go to Docling:

  * reading order needed a "soft" gutter (columns that tables or callouts span),
  * detected (ruled) tables: TableFormer recovers headers and spans,
  * vector figures (charts, diagrams): the heuristics split them and lose labels,
  * fragmented pages: many very short blocks (forms, tables without rules).

Ruling strokes alone aren't a signal: callout boxes and decorative rules are common on
ordinary text pages (samples/p17.pdf: median 16 per page). Raster images aren't either:
the heuristics place those fine (the provider directories have logos on 51 of 684 pages).

Scanned and empty pages are left alone (no OCR here yet).

On a hard page the heuristic nodes stay: their reading order, paragraphs and headings are
the text the page is read from. Docling contributes only its tables and figures, each one
adopted in place of the heuristic objects it covers and only when it validates (see
_adopt): a table must preserve the replaced values and each heuristic row's numbers,
meet the text-recall threshold, and be at least as large as the heuristic table; a figure
takes over the chart text it replaces as its labels. What doesn't validate is kept as the
heuristics had it. Every decision goes into Routing.decisions and onto the heuristic node
concerned (attrs["why"]). Docling's text is never used, so Docling can't join text across a
page break.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from reflow import geometry
from reflow.analyze.blocks import ends_hyphenated, join_lines
from reflow.analyze.constants import SPLIT_SOFT, TERMINAL
from reflow.analyze.headings import fix_heading_levels
from reflow.analyze.layout import analyze
from reflow.analyze.placement import caption_for
from reflow.analyze.table_evidence import compare, node_text
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData
from reflow.model import (FACT_SCAN_FIELDS, HEADING_BY_LEVEL, HEADING_ROLES, Node, PageInfo, PageKind,
                          Role, Source)

# Routing rules (see the module docstring).
MIN_VECTOR_FIGURES = 1
MIN_BLOCKS_FOR_FRAGMENTS = 12
MAX_SHORT_SHARE = 0.5   # share of blocks with <= SHORT_BLOCK_WORDS words

# Adoption rules (see _adopt). A heuristic object or text block is replaced when at least
# REPLACE_SHARE of its area (of the smaller of the two, for tables and figures) lies inside
# the Docling object; one that is only partly inside (PARTIAL_SHARE) blocks the adoption.
REPLACE_SHARE = 0.5
PARTIAL_SHARE = 0.1
# A replaced text block longer than this is body prose, which a figure must not absorb.
LABEL_MAX_WORDS = 15
# Rows and columns of a Docling table that replaces no heuristic table.
MIN_TABLE_SIDE = 2


@dataclass
class Routing:
    hard: dict[int, list[str]] = field(default_factory=dict)  # page → reasons
    # One entry per Docling table or figure on a hard page: adopted or not, and why.
    decisions: list[dict] = field(default_factory=list)
    # Docling's nodes on the hard pages, kept as computed (no extra pass) so the reading-order
    # check can compare against Docling's order (order_evidence.apply).
    docling: list[Node] = field(default_factory=list)

    @property
    def pages(self) -> list[int]:
        return sorted(self.hard)


def route(info: dict, page_infos: list[PageInfo]) -> Routing:
    kinds = {p.number: p.kind for p in page_infos}
    # A scan OCR recognized is re-read as MIXED, so its kind does not keep it from Docling; the
    # fields found on it (analyze.scan_fields) exist only in the heuristic layout.
    fielded = {p.number for p in page_infos if any(f.code == FACT_SCAN_FIELDS for f in p.facts)}
    routing = Routing()
    for n, s in info.get("pages", {}).items():
        if kinds.get(n) in (PageKind.SCANNED, PageKind.EMPTY) or n in fielded:
            continue
        reasons = []
        if s["split"] >= SPLIT_SOFT:
            reasons.append("soft-columns")
        if s["tables"]:
            reasons.append("tables")
        if s.get("vector_figures", 0) >= MIN_VECTOR_FIGURES:
            reasons.append("vector-figures")
        if s["blocks"] >= MIN_BLOCKS_FOR_FRAGMENTS and \
                s["short_blocks"] >= MAX_SHORT_SHARE * s["blocks"]:
            reasons.append("fragments")
        if reasons:
            routing.hard[n] = reasons
    return routing


def docling_nodes(pdf: Path, pages: list[int], converter) -> list[Node]:
    """Docling's nodes for the given pages, one single-page conversion per page.

    Docling merges text across page breaks within one document, neighbouring pages
    included (resnet.pdf pages 1 and 2 were joined that way), so the pages must never
    reach it side by side. Each page is therefore converted on its own (one shared
    converter, so the models load once), and its nodes keep the source page number."""
    import tempfile

    import pikepdf

    from reflow.analyze.docling_layout import to_nodes
    nodes: list[Node] = []
    with tempfile.TemporaryDirectory() as tmp, pikepdf.open(pdf) as src:
        for n in sorted(pages):
            subset = Path(tmp) / f"page-{n}.pdf"
            with pikepdf.new() as dst:
                dst.pages.append(src.pages[n - 1])
                dst.save(subset)
            page_nodes = to_nodes(converter.convert(subset).document)
            for node in page_nodes:
                for part in node.walk():
                    if part.page is not None:
                        part.page = n
            nodes += page_nodes
    return nodes


def _dims(table: Node) -> tuple[int, int]:
    """(rows, columns) of a TABLE node: columns are the widest row's cell spans."""
    rows = [tr for tr in table.children if tr.role == Role.TR]
    cols = max((sum(c.attrs.get("colspan", 1) for c in tr.children) for tr in rows), default=0)
    return len(rows), cols


def _decide(decisions: list[dict], page: int, kind: str, adopted: bool, reason: str,
            **extra) -> None:
    decisions.append({"page": page, "kind": kind, "adopted": adopted, "reason": reason,
                      **extra})


def _adopt(nodes: list[Node], d: Node, page: int, decisions: list[dict]) -> list[Node]:
    """`nodes` (a page's heuristic nodes, in order) with the Docling table or figure `d`
    adopted in place of what it replaces, or unchanged if it doesn't validate.

    Replaced: heuristic tables and figures sharing at least REPLACE_SHARE of the smaller of
    the two (which covers IoU >= 0.5), and text blocks at least REPLACE_SHARE inside it.
    Blocked: any other heuristic object or text block partly inside it (more than
    PARTIAL_SHARE), since its text would then be in the page twice. Lists and notes are
    never replaced. Captions keep their own place: the heuristic caption is reattached to
    the object (the one the replaced heuristic object had, else the nearest kind-matched
    one), and Docling's own captions are not used."""
    table = d.role == Role.TABLE
    kind = "table" if table else "figure"
    replaced: list[Node] = []
    blocking: list[Node] = []
    for n in nodes:
        if n.bbox is None or n.role in (Role.CAPTION, Role.ARTIFACT):
            continue
        if n.source == Source.DOCLING:
            share, replaceable = geometry.share(n.bbox, d.bbox), False  # already adopted: never twice
        elif n.role in (Role.TABLE, Role.FIGURE):
            share = max(geometry.share(n.bbox, d.bbox), geometry.share(d.bbox, n.bbox))
            replaceable = True
        else:
            share = geometry.share(n.bbox, d.bbox)
            replaceable = n.role not in (Role.LIST, Role.NOTE)
        if replaceable and share >= REPLACE_SHARE:
            replaced.append(n)
        elif share > PARTIAL_SHARE:
            blocking.append(n)

    def reject(reason: str, targets: list[Node], **extra) -> list[Node]:
        _decide(decisions, page, kind, False, reason, **extra)
        if targets:
            targets[0].attrs.setdefault("why", []).append(f"docling {kind} not adopted: {reason}")
        return nodes

    if blocking:
        return reject(f"overlaps a {blocking[0].role.value} block that is kept", blocking)

    replaced_ids = {id(n) for n in replaced}
    fits = [n for n in replaced if n.role in (Role.TABLE, Role.FIGURE)]
    if table:
        comparison = compare(replaced, d)
        recall = comparison.recall
        rows, cols = _dims(d)
        if reason := comparison.problem():
            return reject(reason, fits, recall=recall,
                          missing_numbers=list(comparison.missing_numbers),
                          split_rows=comparison.split_rows)
        tables = [n for n in fits if n.role == Role.TABLE]
        if tables:
            want_rows, want_cols = max(_dims(t) for t in tables)
            if rows < want_rows or cols < want_cols:
                return reject(f"{rows}x{cols} is smaller than the heuristic {want_rows}x"
                              f"{want_cols}", fits, recall=recall,
                              missing_numbers=list(comparison.missing_numbers),
                              split_rows=comparison.split_rows)
        elif rows < MIN_TABLE_SIDE or cols < MIN_TABLE_SIDE:
            return reject(f"{rows}x{cols} is too small for a table", fits, recall=recall,
                          missing_numbers=list(comparison.missing_numbers),
                          split_rows=comparison.split_rows)
    else:
        prose = [n for n in replaced if n.role not in (Role.TABLE, Role.FIGURE)
                 and len(" ".join(node_text(n)).split()) > LABEL_MAX_WORDS]
        if prose:
            words = len(" ".join(node_text(prose[0])).split())
            return reject(f"a replaced {prose[0].role.value} block has {words} words: body "
                          f"text, not chart labels", prose[:1])
    labels = [lab for n in replaced for lab in _label_nodes(n)] if not table else []

    # Captions: the replaced objects' own, else the nearest heuristic caption of this kind.
    caps = [c for n in replaced for c in n.children if c.role == Role.CAPTION]
    from_top = None
    if not caps:
        rest = [n for n in nodes if id(n) not in replaced_ids]
        from_top = caption_for(d.bbox, rest, table=table)
        if from_top is not None:
            caps = [from_top]

    if table:
        d.children = [tr for tr in d.children if tr.role == Role.TR]
        d.children[0:0] = caps
        rows, cols = _dims(d)
        d.attrs.update(method="docling", rows=rows, columns=cols)
        reason = (f"docling table {rows}x{cols}, text recall {recall:.2f}"
                  + (f", replaces {len(fits)} heuristic {'table' if tables else 'figure'}"
                     if fits else ""))
    else:
        d.children = [*caps, *labels]
        if any(label.source == Source.OCR for label in labels):   # as placement.attach_figures
            d.fact("ocr-in-picture", Source.OCR,
                   f"{len(labels)} lines read inside the picture are listed under it")
        reason = "docling figure" + (f", replaces {len(fits)} heuristic objects; "
                                     f"{len(labels)} labels kept" if fits else "")
    d.attrs["why"] = [reason]
    _decide(decisions, page, kind, True, reason, recall=recall if table else None,
            missing_numbers=list(comparison.missing_numbers) if table else [],
            split_rows=comparison.split_rows if table else 0,
            replaces=[n.role.value for n in replaced])

    drop = replaced_ids | ({id(from_top)} if from_top is not None else set())
    out: list[Node] = []
    placed = False
    for n in nodes:
        if id(n) in drop:
            if id(n) in replaced_ids and not placed:
                out.append(d)
                placed = True
            continue
        out.append(n)
    if not placed:
        at = next((i for i, n in enumerate(out) if n.bbox and n.bbox[1] >= d.bbox[1]
                   and min(n.bbox[2], d.bbox[2]) > max(n.bbox[0], d.bbox[0])), len(out))
        out.insert(at, d)
    return out


def _label_nodes(node: Node) -> list[Node]:
    """The text a replaced node gives an adopted figure, as figure labels: a figure's own
    labels as they are, else one label per text-bearing node (the text of node_text), each
    with its node's box."""
    if node.role == Role.CAPTION:
        return []
    if node.attrs.get("figure_label"):
        return [node]
    own = ([Node(Role.P, page=node.page, bbox=node.bbox, text=node.text,
                 attrs={"figure_label": True}, confidence=node.confidence, source=node.source)]
           if node.text and node.role != Role.TR else [])
    return own + [label for child in node.children for label in _label_nodes(child)]


def merge(heuristic: list[Node], docling: list[Node], hard: set[int],
          decisions: list[dict] | None = None) -> list[Node]:
    """Per page in page order: the heuristic nodes, except on hard pages, where Docling's
    tables and figures are adopted into them where they validate (see _adopt)."""
    if decisions is None:
        decisions = []
    by_page_h: dict[int | None, list[Node]] = {}
    for n in heuristic:
        by_page_h.setdefault(n.page, []).append(n)
    by_page_d: dict[int | None, list[Node]] = {}
    for n in docling:
        by_page_d.setdefault(n.page, []).append(n)
    out: list[Node] = []
    for page in sorted({*by_page_h, *by_page_d} - {None}):
        nodes = by_page_h.get(page, [])
        if page in hard:
            for d in by_page_d.get(page, []):
                if d.role in (Role.TABLE, Role.FIGURE) and d.bbox is not None:
                    nodes = _adopt(nodes, d, page, decisions)
        out += nodes
    return out


def _open_sentence(text: str) -> bool:
    """Whether a paragraph stops mid-sentence: a line-end hyphen, or no terminal punctuation
    (constants.TERMINAL) at its end. A modal verb with no full stop ("…can register to use")
    needs no test of its own: the join also requires the next paragraph to go on in lower
    case, and that already tells the sentence goes on."""
    t = text.rstrip()
    return ends_hyphenated(t) or not t.endswith(TERMINAL)


def _joined(a: Node, b: Node, pages: list[int]) -> Node:
    """`a` and `b` as one paragraph: a's id, page and bbox; the text joined by join_lines,
    which applies the hyphen rule (and join_wrapped for the spacing)."""
    why = [*a.attrs.get("why", []), "joined with the paragraph that continues on the next page"]
    node = Node(Role.P, id=a.id, page=a.page, bbox=a.bbox,
                attrs={**a.attrs, "pages": pages, "why": why}, confidence=a.confidence,
                source=a.source, facts=list(a.facts))
    node.join_inline([a, b])
    node.set_text(join_lines([a.text, b.text]))
    return node


def join_across_pages(nodes: list[Node], hard: set[int] | None = None) -> list[Node]:
    """`nodes` (the flow, in order) with each paragraph that goes on across a page break
    joined to its continuation: the last P of page n and the first P of page n+1, with
    nothing between them in the flow, when the first does not end a sentence and the second
    starts in lower case or the first ends with a hyphen. A chain of such pages joins into
    one node, whose pages are all listed in attrs["pages"].

    `hard` limits the joins to the pages Docling handled (routing.hard) and their neighbours:
    the heuristic engine itself never splits these paragraphs. None means no limit. Nodes
    with inline spans are left alone, since joining would move their offsets."""
    out: list[Node] = []
    for b in nodes:
        a = out[-1] if out else None
        if (a is not None and a.role == Role.P and b.role == Role.P and a.text and b.text
                and a.page is not None and b.page is not None and not a.spans and not b.spans):
            pages = a.attrs.get("pages", [a.page])
            if (b.page == pages[-1] + 1
                    and (hard is None or {pages[-1], b.page} & hard)
                    and _open_sentence(a.text)
                    and (ends_hyphenated(a.text) or b.text.lstrip()[:1].islower())):
                out[-1] = _joined(a, b, [*pages, b.page])
                continue
        out.append(b)
    return out


def analyze_hybrid(pdf: Path, pages: list[PageContent], tables: dict[int, list[TableData]],
                   page_infos: list[PageInfo], *, device: str = "auto",
                   converter=None) -> tuple[list[Node], Routing]:
    info: dict = {}
    heuristic = analyze(pages, tables, info)
    routing = route(info, page_infos)
    if not routing.hard:
        return heuristic, routing

    from reflow.analyze.docling_layout import make_converter, on_gpu
    with on_gpu(device):
        docling = docling_nodes(pdf, routing.pages, converter or make_converter(device=device))
    routing.docling = docling
    # Heading levels are the heuristic ranking's on every page: back on the same scale
    # (style rank) before normalizing once.
    for n in heuristic:
        if n.role in HEADING_ROLES and "style_level" in n.attrs:
            n.role = HEADING_BY_LEVEL[n.attrs["style_level"] - 1]
    nodes = merge(heuristic, docling, set(routing.hard), routing.decisions)
    nodes = join_across_pages(nodes, set(routing.hard))
    fix_heading_levels(nodes)
    return nodes, routing
