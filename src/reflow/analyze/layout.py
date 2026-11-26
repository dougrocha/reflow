"""Heuristic layout analysis for born-digital pages: lines to content nodes.

Stages: artifacts (running headers/footers, page numbers) -> columns and reading
order -> blocks -> headings / list items / paragraphs -> figures. Every node gets
a confidence so later stages (and people) know what to double-check.

This module runs the stages in order (analyze) and builds the nodes; each stage
lives in its own module.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Executor

from reflow.patterns import CAPTION_BLOCK
from reflow.extract.drawings import MIN_RULED_RULES
from reflow import geometry
from reflow.analyze import pageplan
from reflow.analyze.artifacts import NO_TEXT_WHY, artifact_reasons
from reflow.analyze.blocks import Block, Break, address_lines, set_lines, wrap_offsets
from reflow.analyze.boxes import group_boxes
from reflow.analyze.contents import fold_contents
from reflow.analyze.breaks import attach_continued, link_pages, make_blocks, merge_context, split_banners
from reflow.analyze.columns import empty_gaps, min_gutter, xy_cut
from reflow.analyze.constants import (
    CALLOUT_LABEL,
    CONF_PARAGRAPH,
    CONTINUED,
    HEADING_SCALE,
    SHORT_BLOCK_WORDS,
    PUA_BULLETS,
    SPLIT_NAMES,
    UNHELD_WHY,
    reading_order_why,
)
from reflow.analyze.context import PageContext, PlanState
from reflow.analyze.figures import FigureCandidate, covered, find_figures, find_legend, mark_icons
from reflow.analyze import form_grid
from reflow.analyze.forms import place_flow, plan_page_forms
from reflow.analyze.headings import (
    body_size,
    demoted_by_twin,
    fix_heading_levels,
    heading_decision,
    heading_levels,
    from_ink,
    is_index_letter,
    is_index_lines,
    is_label_caps,
    is_section_heading,
    join_wrapped_headings,
    page_body,
    ranked_style,
    refused_ink,
    set_apart,
    style_level,
)
from reflow.analyze.lists import (
    continue_roman_items,
    group_lists,
    merge_list_markers,
    ocr_bullets,
    promote_legal_labels,
)
from reflow.analyze.marks import marks_for
from reflow.analyze.placement import attach_figures, attach_forms, attach_tables
from reflow.analyze.rotated import place_rotated, rotated_nodes
from reflow.analyze.table_nodes import mark_section_bars
from reflow.text_quality import ocr_gate, ungraded_reason
from reflow.analyze.outline import relevel_numbered
from reflow.analyze.rhythm import Rhythm, snap_ocr_sizes
from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.extract.pages import PageContent
from reflow.extract.scan import visible_ocr
from reflow.analyze.tables.aligned import aligned_tables
from reflow.extract.tables.cells import TableData, TableMethod
from reflow.analyze.tables.records import split_records
from reflow.analyze.tables.repair import (
    merge_tables,
    recase_cells,
    repair_ruled,
    unheld_lines,
    unscramble_cells,
)
from reflow.analyze.tables.vetoes import chart_grid, page_frame
from reflow.analyze.calendar import annotate, find_calendars
from reflow.leaders import parse_contents_entry
from reflow.model import (
    HEADING_BY_LEVEL,
    BBox,
    Fact,
    Mark,
    Node,
    Role,
    Source,
    Why,
    code_of,
)




# Confidence values for heuristic decisions.
CONF_SIZE_HEADING = 0.8
CONF_BOLD_HEADING = 0.6
CONF_INK_HEADING = 0.5  # a heading seen only on a scan's page image (headings.ink_cue)
CONF_COLUMNS = 0.7  # reading order on multi-column pages


def _in_table(line: Line, table: TableData) -> bool:
    """Whether a line is one of the table's cells. A table's box can hold other text (a prose
    line beside a schedule), which stays text; a table with no cell boxes uses its box."""
    if not table.cells:  # a ruled table's header line can overhang its top rule
        return geometry.inside(geometry.core(line.bbox), table.bbox, slack=2)
    # A line split into cells (a row code and its label, a row of figures) overlaps them: most of
    # its box is in cells.
    area = geometry.area(line.bbox)
    return sum(geometry.overlap(line.bbox, cell) for cell in table.cells) >= 0.5 * area




def analyze(pages: list[PageContent],
            tables: dict[int, list[TableData]] | None = None,
            info: dict | None = None, trace: dict | None = None,
            executor: Executor | None = None, jobs: int = 1,
            on_planning: Callable[[], None] | None = None) -> list[Node]:
    """Turn extracted pages into content nodes in reading order (artifacts included).
    `tables` (from reflow.extract.tables.ruled) replaces the text inside each table's box.
    If `info` is given, it's filled with the body size, the heading-style → level map,
    and per-page signals (reading-order split, figures, tables, blocks, short blocks)
    for callers that route hard pages elsewhere (reflow.analyze.hybrid). If `trace` is
    given, it's filled with what each step saw: every line of each page, why it was dropped
    if it was, the reading-order split and its gutters, and the figure boxes.
    With an `executor` (a process pool of `jobs` workers) the per-page planning runs there; the
    nodes are the same as without one (reflow.analyze.pageplan); `on_planning()` is called once
    the pages are on the pool (never, when they are planned here)."""
    tables = tables or {}
    pooled = executor if executor is not None and info is None and len(pages) >= pageplan.MIN_PAGES else None
    state = _prepare(pages, pooled, jobs)
    # First pass: a context per page, so heading styles can be ranked across the document.
    if pooled is not None:
        contexts = pageplan.plan_pages(plan_page, pages, tables, state, trace, pooled, jobs, on_planning)
    else:
        contexts = [plan_page(page, state, tables.get(page.number, []), info, trace)
                    for page in pages]
    link_pages(contexts)
    continue_roman_items(contexts)
    promote_legal_labels(contexts)
    levels = heading_levels(state.styles)
    if info is not None:
        info["body"], info["levels"] = state.body, levels
    nodes: list[Node] = []
    for ctx in contexts:
        nodes += _page_nodes(ctx, state, levels)
    relevel_numbered(nodes)
    fix_heading_levels(nodes)
    return nodes


def _prepare(pages: list[PageContent], executor: Executor | None = None,
             jobs: int = 1) -> PlanState:
    """Clean the text layer and find what is shared across pages: the body size, the running
    artifacts, the icon legend, and the document's hanging indents."""
    # Map PUA bullets to the bullets they show before anything reads the text. Other PUA
    # characters have no mapping, so they stay as they are.
    for page in pages:
        scanned = visible_ocr(page.images, page.lines, page.width, page.height)
        for line in page.lines:
            line.text = line.text.translate(PUA_BULLETS)
            line.ocr = line.ocr or scanned
        page.lines[:] = ocr_bullets(page.lines)
        snap_ocr_sizes(page.lines)  # before body_size, so it and the heading styles see one size
    body = body_size(pages)
    reasons = artifact_reasons(pages, body)
    state = PlanState(body, reasons, set(reasons), find_legend(pages))
    if executor is not None:
        state.offsets += pageplan.offsets_of(pages, reasons, body, executor, jobs)
        return state
    for page in pages:
        lines = [l for l in page.lines if id(l) not in state.artifacts and not l.rotated]
        state.offsets += wrap_offsets(xy_cut(merge_list_markers(lines), body * 1.2)[0])
    return state


def plan_page(page: PageContent, state: PlanState, page_tables: list[TableData],
               info: dict | None, trace: dict | None) -> PageContext:
    """Find a page's figures and tables, take their text out of the flow, and cut the rest into
    blocks in reading order."""
    if state.legend:
        page_marks, legend_lines = mark_icons(page, state.legend)
        state.marks |= page_marks
        # Each marked line now carries the meaning; the legend itself is redundant.
        state.artifacts |= legend_lines
        for lid in legend_lines:
            state.reasons.setdefault(lid, "icon legend, its meaning is kept on the line it marks")
    # A chart's gridlines are not a table (see chart_grid). The veto runs here, before the
    # figures are weighed against the tables, so the chart can stand as a figure; the
    # lines it drops stay in the text (or become the figure's labels).
    ruled: list[TableData] = []
    grids: list[BBox] = []  # the vetoed chart gridlines' boxes
    chart_grids: list[dict] = []
    for t in page_tables:
        why = chart_grid(t, page.drawings)
        if why is None:
            frame = page_frame(t, page.width, page.height, page.lines)
            if frame is None:
                ruled += split_records(t) if all(w.drawn for w in page.widgets) else [t]
            else:  # the page's border: its text returns to the flow, and it is no chart
                chart_grids.append({"bbox": list(t.bbox), "why": frame})
        else:
            grids.append(t.bbox)
            chart_grids.append({"bbox": list(t.bbox), "why": why})
    # A printed month is a table of its own, found from where its lines lie. Its lines have one
    # owner: the flow, the figures and the aligned tables never see them.
    upright = [l for l in page.lines if id(l) not in state.artifacts and not l.rotated]
    calendars = find_calendars(upright)
    held = frozenset().union(*(g.lines for g in calendars))
    cal_tables = [g.to_table(page.number) for g in calendars]
    # What the colour key and the dated list say about each day (see calendar.annotate).
    annotate(calendars, cal_tables, upright, page.fills)
    lines = [l for l in page.lines if id(l) not in state.artifacts and not l.rotated
             and id(l) not in held
             and not any(geometry.inside(l.bbox, t.bbox, slack=2) for t in ruled)]
    figures, turned = find_figures(page, lines, state.body, state.artifacts, ruled, grids,
                                   [t.bbox for t in cal_tables])
    # A grid that is a form layout is read as a form: its repeated band stays a table, and its
    # lines leave the table finders (see analyze.form_grid).
    ruled, regions = form_grid.split(ruled, [l for l in upright if id(l) not in held])
    region_lines = [l for l in upright if id(l) not in held and form_grid.in_any_region(l, regions)]
    page_tables, kept = _find_tables(page, state, ruled, figures, held | {id(l) for l in region_lines})
    page_tables += cal_tables
    # The text-loss guard: a line inside a table's box that no cell holds stays in the
    # text flow, so it can't vanish from the page.
    unheld = {id(l): t for t in page_tables for l in unheld_lines(t, kept)}
    lines = [l for l in kept if id(l) in unheld
             or not any(_in_table(l, t) for t in page_tables)]
    figures = [f for f in figures if f.labels == () or not covered(f.bbox, page_tables)]
    # Form fields: each is bound to its printed label, placed in its table cell or in the
    # flow, and the labels they take leave the text (see analyze.forms).
    forms = plan_page_forms(page, regions, region_lines, kept, lines, page_tables)
    kept, lines, page_tables, plan, grids = forms.kept, forms.lines, forms.tables, forms.plan, forms.grids
    mark_section_bars(page_tables, plan.cells if plan else {},
                      [l for l in kept if not plan or id(l) not in plan.consumed])
    content = [l for l in lines if not _leaves_flow(l, page, figures)]
    ruled_boxes = [d.bbox for d in page.drawings if d.rules >= MIN_RULED_RULES]
    ordered, columns = xy_cut(merge_list_markers(content), state.body * 1.2, ruled_boxes)
    blocks = attach_continued(make_blocks(ordered, state.offsets, is_index_lines(ordered),
                                          Rhythm.of(ordered)),
                              [l for l in page.lines if id(l) in state.artifacts])
    blocks = split_banners(blocks, ruled_boxes, ordered)
    # Ruled boxes that are not tables: the form areas that labels may sit in (see heading_decision).
    form = [d.bbox for d in page.drawings if d.rules >= MIN_RULED_RULES
            and not any(geometry.overlap(d.bbox, t.bbox) > 0 for t in page_tables)
            and not any(geometry.overlap(d.bbox, g.bbox) > 0 for g in grids)]
    body = page_body(blocks, state.body)
    blocks = join_wrapped_headings(blocks, body, form)
    for b in blocks:  # a notice in the bottom margin is not the heading of what follows on the next page
        b.foot = len(b.lines) == 1 and b.bbox[3] > FOOT_MARGIN * page.height
    held = [l for l in kept if id(l) in unheld]
    ctx = PageContext(page, blocks, figures, page_tables, columns, form,
                      plan, {id(l) for l in held}, turned, held, grids)
    _record(ctx, content, state, chart_grids, [g.to_trace() for g in calendars], unheld, info, trace)
    # Only a heading its type makes ranks its style (headings.ranked_style), whatever its OCR text.
    for b in blocks:
        if style := ranked_style(b, body, blocks, form, [w.bbox for w in page.widgets]):
            state.styles[style] += 1
    return ctx


def _find_tables(page: PageContent, state: PlanState, ruled: list[TableData],
                 figures: list[FigureCandidate],
                 held: frozenset[int] = frozenset()) -> tuple[list[TableData], list[Line]]:
    """The page's tables (ruled ones completed by aligned ones) and the lines still in play."""
    # Tables with no full grid: found from the text alignment outside figures. Lines inside
    # a ruled table stay in: a ruled table that is only partly ruled (its other columns
    # unruled) is completed by the aligned table that covers it (see merge_tables).
    # Dashes and other lines with no letters or digits are artifacts, but a table's
    # "–" placeholder is cell text: they stay visible to the alignment step.
    glyphs = [l for l in page.lines if id(l) in state.artifacts
              and state.reasons.get(id(l)) == NO_TEXT_WHY]
    text = [l for l in page.lines if id(l) not in state.artifacts and not l.rotated
            and id(l) not in held] + glyphs
    text = [l for l in text if not any(
        geometry.inside(l.bbox, f.bbox, slack=6) and f.labels != () for f in figures)]
    rules = [d.bbox for d in page.drawings if d.rules]
    page_tables = merge_tables(ruled, aligned_tables(page.number, text, page.width, rules), text)
    kept = [l for l in page.lines if id(l) not in state.artifacts and not l.rotated
            and id(l) not in held]
    # A ruled table whose cells lost lines gets them back where it can (see repair_ruled), and
    # its cell words take the case the text layer gives them (see recase_cells).
    page_tables = [unscramble_cells(recase_cells(repair_ruled(t, kept, glyphs), kept), kept)
                   if t.method == TableMethod.RULED else t for t in page_tables]
    return page_tables, kept


def _record(ctx: PageContext, content: list[Line], state: PlanState, chart_grids: list[dict],
            calendars: list[dict], unheld: dict[int, TableData], info: dict | None, trace: dict | None) -> None:
    """Fill the caller's `info` and `trace` with what this page's steps saw."""
    page, blocks = ctx.page, ctx.blocks
    if info is not None:
        info.setdefault("pages", {})[page.number] = {
            "split": ctx.columns, "figures": len(ctx.figures), "tables": len(ctx.tables),
            "vector_figures": sum(1 for f in ctx.figures if f.source in ("drawing", "chart")),
            "rules": sum(d.rules for d in page.drawings), "blocks": len(blocks),
            "short_blocks": sum(1 for b in blocks if len(b.text.split()) <= SHORT_BLOCK_WORDS),
        }
    if trace is not None:
        trace.setdefault("pages", {})[page.number] = {
            "lines": [{"bbox": list(l.bbox), "text": l.text, "size": l.size,
                       "artifact": state.reasons.get(id(l)),
                       **({"kept_in_text": UNHELD_WHY} if id(l) in unheld else {})}
                      for l in page.lines],
            "split": SPLIT_NAMES[ctx.columns],
            "chart_grids": chart_grids,
            "calendars": calendars,
            "gutters": [list(g) for g in empty_gaps([(l.x0, l.x1) for l in content],
                                               min_gutter(content))]
            if content else [],
        }


def _page_nodes(ctx: PageContext, state: PlanState, levels: dict) -> list[Node]:
    """Build a page's nodes: artifacts, then blocks with the tables, figures, form fields and
    rotated text placed among them."""
    page, blocks = ctx.page, ctx.blocks
    nodes = [Node(Role.ARTIFACT, page=page.number, bbox=line.bbox,
                  text=line.text.replace(SOFT_HYPHEN, ""),
                  attrs={"why": [f"dropped: {state.reasons.get(id(line), 'running text')}"]})
             for line in page.lines if id(line) in state.artifacts]
    body = page_body(blocks, state.body)
    fields = [w.bbox for w in page.widgets]
    page_nodes = [_block_node(b, blocks, page.number, body, levels, ctx.columns, ctx.form, fields)
                  for b in blocks]
    for b, node in zip(blocks, page_nodes):
        if any(id(l) in ctx.guarded for l in b.lines):
            node.attrs.setdefault("why", []).append(UNHELD_WHY)
        if errors := sum(l.unicode_errors for l in b.lines):
            node.attrs["unicode_errors"] = errors  # confidence.apply turns it into a review fact
    for node in demoted_by_twin(blocks, page_nodes, body, ctx.form, fields):
        node.attrs["warn"] = "demoted-heading"
    if state.marks:
        _apply_marks(blocks, page_nodes, state.marks)
    plan = ctx.plan
    # Beside an invisible OCR layer, a block of visible text was not read from the image (see
    # _prepare): it is the file's own text.
    drawn = ({id(n) for b, node in zip(blocks, page_nodes) if not any(l.ocr for l in b.lines)
              for n in node.walk()} if any(line.ocr for line in page.lines) else set())
    owners = {id(line): node for b, node in zip(blocks, page_nodes) for line in b.lines}
    page_nodes = attach_tables(page_nodes, ctx.tables, plan.cells if plan else None)
    page_nodes = attach_forms(page_nodes, ctx.forms)
    for node in (n for top in page_nodes for n in top.walk()):
        if (node.text or node.role == Role.TABLE) and node.bbox and id(node) not in drawn and any(
                geometry.inside(node.bbox, region, slack=6) for region in page.scan.text_images):
            node.source = Source.OCR
            node.confidence = min(node.confidence, 0.35)
            node.fact("ocr-image-text", Source.OCR, "Text recovered from a text-heavy image")
    page_nodes, anchored = attach_figures(page_nodes, ctx.figures, page, owners)
    if plan and plan.flow:
        page_nodes = place_flow(page_nodes, plan.flow, anchored)
    held = state.artifacts | ctx.label_turned
    page_nodes = place_rotated(page_nodes, rotated_nodes(page, held, analyze))
    page_nodes = fold_contents(page_nodes)
    return nodes + group_boxes(merge_context(group_lists(page_nodes)), page, state.artifacts)


FOOT_MARGIN = 0.94  # of the page height: text below this line sits in the bottom margin


def _block_node(block: Block, blocks: list[Block], page: int, body: float, levels: dict,
                columns: int, form: Sequence[BBox] = (), fields: Sequence[BBox] = ()) -> Node:
    text = block.text
    style, heading_why = heading_decision(block, body, blocks, form, fields)
    why: list[str] = []
    facts: list[Fact] = []
    if block.why:
        why.append(Why(f"new block: {block.why}",
                       block.why if isinstance(block.why, Break) else None))
    if columns:
        why.append(reading_order_why(columns))
    source = Source.OCR if any(line.ocr for line in block.lines) else Source.HEURISTIC
    # OCR text the lexical gate refuses is a low-confidence paragraph; a word graded doubtful
    # keeps the block's role, but text ocr_gate refuses is no heading (text_quality.ocr_gate) and no
    # contents entry.
    if reason := ungraded_reason(block.lines):
        return Node(Role.P, page=page, bbox=block.bbox, text=text,
                    attrs={"why": why + [f"paragraph: {reason}"]},
                    confidence=0.35, source=source)
    if (len(block.lines) == 1 and ocr_gate(block.lines) is None
            and (entry := parse_contents_entry(text))):
        title, page_ref = entry
        return Node(Role.LI, page=page, bbox=block.bbox, text=title,
                    attrs={"contents": True, "page_ref": page_ref, "size": block.size,
                           "x": block.first.x0,
                           "why": why + ["contents entry: title, dot leader and page number"]},
                    confidence=CONF_PARAGRAPH, source=source)
    if style and CONTINUED.search(text):
        # Emit continued headings as context paragraphs, not as artifacts
        return Node(Role.P, page=page, bbox=block.bbox, text=text,
                    attrs={"context": True, "why": why + ["'(continued)' heading kept as a paragraph"]},
                    confidence=CONF_PARAGRAPH, source=source)
    if style:
        role = HEADING_BY_LEVEL[style_level(levels, style) - 1]
        text = text.rstrip(" \u2610\u2611\u2612") or text  # a drawn check box beside a title is no part of it
        if role == Role.H1 and is_section_heading(text):
            role = Role.H2
        conf = CONF_SIZE_HEADING if style[0] >= body * HEADING_SCALE else CONF_BOLD_HEADING
        why.append(f"heading: {heading_why}")
        if (cue := from_ink(block, body, heading_why)) is not None:
            facts.append(Fact("heading-from-ink", Source.OCR, cue.why,
                              {"stroke": None if cue.stroke is None else round(cue.stroke, 2),
                               "underlined": cue.underlined}))
            conf = min(conf, CONF_INK_HEADING)
    elif block.item:
        role, conf = Role.LI, CONF_PARAGRAPH
        why.append("index entry: one item, its sub-entries nest under it" if block.entry
                   else "worksheet line item: its number is the list's" if block.label is not None
                   else f"list item: {Break.LIST_MARKER}")
    elif CAPTION_BLOCK.match(text):
        role, conf = Role.CAPTION, CONF_PARAGRAPH
        why.append("caption: starts with Figure, Table or Chart and a number")
    elif CALLOUT_LABEL.match(text.strip()):
        role, conf = Role.NOTE, CONF_PARAGRAPH
        why.append("callout: a label such as TIP or CAUTION opens a note")
    else:
        role, conf = Role.P, CONF_PARAGRAPH
        why.append(Why(f"paragraph: not a heading ({heading_why})", code_of(heading_why)))
        if (cue := refused_ink(block, body)) is not None:   # a heading by its ink, not its text
            facts.append(Fact("heading-from-ink", Source.OCR,
                              f"{cue.why}, but its OCR text may not be a heading: a paragraph",
                              {"stroke": None if cue.stroke is None else round(cue.stroke, 2),
                               "underlined": cue.underlined}))
    if columns:
        conf = min(conf, CONF_COLUMNS)
    attrs: dict = {"italic": True} if all(l.italic for l in block.lines) else {}
    if role == Role.P or style:
        # the type's own witness to a heading, which the page-vision checks read (vision_gates)
        if (apart := set_apart(block, body)) is not None:
            attrs["set_apart"] = apart
    if style:
        attrs["font_size"] = style[0]
        attrs["style_level"] = style_level(levels, style)  # before fix_heading_levels normalizes it
        if block.bold and is_label_caps(text):
            attrs["label_caps"] = True  # may rank under a numbered form section (see fix_heading_levels)
        if is_index_letter(block, blocks):
            attrs["index_letter"] = True  # its level comes from the "Index" heading (see fix_heading_levels)
    if role == Role.LI:
        # The nesting of items reads each item's first line: its indent and size (see _nest_items)
        attrs["size"] = block.size
        attrs["x"] = block.first.x0
        if block.label is not None:
            # The number's right edge: worksheet numbers are right-aligned, so the items nest alike
            if block.label.isdigit():
                attrs["x"] = block.first.x1
            attrs["marker"] = block.label
        if block.entry:
            attrs["index"] = True  # an index entry: its text carries no list marker
    if role == Role.P and (lines := address_lines(block) or set_lines(block)):
        attrs["lines"] = lines
        text = " ".join(lines)
    # Emphasis inside the text is marks (strong, em); a block set wholly in italics keeps the
    # class, and marks_for decides both from the same style runs. A heading carries neither: its
    # style is its level.
    marks: list[Mark] = []
    if role in (Role.P, Role.LI, Role.NOTE, Role.CAPTION):
        marks, whole_italic = marks_for(text, block.lines)
        if whole_italic:
            attrs["italic"] = True
        else:
            attrs.pop("italic", None)
    attrs["why"] = why
    return Node(role, page=page, bbox=block.bbox, text=text, attrs=attrs, confidence=conf,
                source=source, marks=marks, facts=facts)


def _apply_marks(blocks: list[Block], nodes: list[Node], marks: dict[int, str]) -> None:
    """Give each block's node the meaning of a legend icon beside one of its lines
    (attrs["status"], e.g. "Accepting New Patients")."""
    for block, node in zip(blocks, nodes):
        status = next((marks[id(l)] for l in block.lines if id(l) in marks), None)
        if status is not None:
            node.attrs["status"] = status




def _leaves_flow(line: Line, page: PageContent, figures: Sequence[FigureCandidate]) -> bool:
    """Whether a line's text moves into a figure: exactly when it is one of a figure's labels,
    which the figure keeps (see figure_boxes and placement.attach_figures). A line inside a chart that
    is not a label stays in the flow, and so do the words laid over a photo (a section title)."""
    return any(line is page.lines[i] for f in figures for i in f.labels or ())


