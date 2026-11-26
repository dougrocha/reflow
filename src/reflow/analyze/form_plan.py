"""The planner: builds each field's and group's node, takes the claimed text out of the flow or its
table cell, and places the nodes (see analyze.forms)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from reflow.analyze.constants import LEFT_GAP
from reflow.analyze.form_common import (
    ROW_TOLERANCE,
    Claim,
    FormPlan,
    _cell_at,
    _is_section_title,
    _tooltip,
    norm,
)
from reflow.analyze.form_groups import Grouping, _Group
from reflow.analyze.form_options import OptionLabels
from reflow.extract.forms import DetectionSource, Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData, TableMethod
from reflow.geometry import centre
from reflow.model import LabelSource, Node, Role, Source

MAX_CELL_LABEL = 100  # a cell's text is a label up to this long; more is a paragraph
MAX_ARIA = 160  # a tooltip longer than this is a paragraph, not a name
# The printed boxes of a form: a symbol font draws each one as a character, under the widget.
BOX_GLYPHS = re.compile(r"[■□▪▫☐☑☒]")


def _cut(tables: list[TableData], cell: tuple[int, int, int], text: str) -> bool:
    """Remove `text` (once, whole) from a table cell; False when the cell doesn't hold it."""
    ti, i, j = cell
    held = tables[ti].rows[i][j]
    if held is None:
        return False
    flat = norm(held)
    if not text:
        return False
    start = r"(?<!\w)" if text[0].isalnum() else ""
    end = r"(?!\w)" if text[-1].isalnum() else ""
    match = re.search(start + re.escape(text) + end, flat)
    if match is None:
        return False
    at = match.start()
    tables[ti].rows[i][j] = norm(flat[:at] + " " + flat[at + len(text):])
    return True


class _Planner(OptionLabels, Grouping):

    def __init__(self, page: PageContent, lines: Sequence[Line], flow: Sequence[Line],
                 tables: Sequence[TableData]):
        self.page = page
        self.lines = sorted(lines, key=lambda l: (l.top, l.x0))
        self.flow_ids = {id(l) for l in flow}
        # Copies, so cutting a cell never changes the caller's tables.
        self.tables = [replace(t, rows=[list(r) for r in t.rows]) if t.grid else t
                       for t in tables]
        self.label_rows = [[list(row) for row in t.rows] for t in tables]
        self.widgets = sorted(page.widgets, key=lambda w: (w.bbox[1], w.bbox[0], w.index))
        self.place = {w.index: self._place(w) for w in self.widgets}
        self.option_labels = self._option_labels()
        for found in (self._labels_before_boxes(), self._captioned_hints()):
            for index, claim in found.items():
                self.option_labels.setdefault(index, claim)
        self.taken: set[int] = set()  # id() of lines already claimed

    def cell_header(self, w: Widget) -> str:
        """Read the original header or row label even after printed labels were consumed."""
        cell = self.place[w.index]
        if cell is None:
            return ""
        ti, i, j = cell
        rows = self.label_rows[ti]
        for row in reversed(rows[:min(i, self.tables[ti].header_rows)]):
            col = j
            while col > 0 and row[col] is None:
                col -= 1
            text = norm(row[col] or "")
            if text:
                return text
        return next((norm(text) for text in rows[i] if text and norm(text)), "")

    def cell_label(self, w: Widget, per_cell: dict[tuple[int, int, int], int]) -> Claim | None:
        """The whole text of a table cell that holds this one field and nothing else: ruled
        cells are the form's own boxes, so their text is the label (and it spares the
        guesswork when pdfium merged two neighbouring labels into one line)."""
        cell = self.place[w.index]
        if cell is None or w.kind not in ("text", "combo", "list") or per_cell[cell] != 1:
            return None
        ti, i, j = cell
        held = norm(self.tables[ti].rows[i][j] or "")
        if not held or len(held) > MAX_CELL_LABEL:
            return None
        self.taken.update(id(l) for l in self.lines
                          if _cell_at(self.tables, centre(l.bbox)) == cell)
        return Claim([], "cell", held)

    def caption_above(self, w: Widget) -> str:
        """The text of the ruled cell right over a text box's own cell, when the box is the only
        text box there and its cell prints nothing: the form sets each caption in a shaded cell
        of its own above the box ("Name and title of the legally valid representative ..."). The
        cell above must be as wide as the box's and hold no text box of its own. A row that
        prints a label of its own ("Start Time" beside a row of boxes) is a grid whose boxes are
        named by both their row and their column, so no caption above names a box of it."""
        cell = self.place[w.index]
        if cell is None or w.kind not in ("text", "combo", "list"):
            return ""
        ti, i, j = cell
        grid, rows = self.tables[ti].grid, self.label_rows[ti]
        texts = ("text", "combo", "list")
        if any(norm(t or "") for t in rows[i]) or sum(1 for v in self.widgets if v.kind in texts
                                         and self.place[v.index] == cell) != 1:
            return ""
        box = grid[i][j]
        for k in range(i - 1, -1, -1):
            for l, b in enumerate(grid[k]):
                if b is None or abs(b[3] - box[1]) > 2 or abs(b[0] - box[0]) > 2 \
                        or abs(b[2] - box[2]) > 2:
                    continue
                if any(v.kind in texts and self.place[v.index] == (ti, k, l) for v in self.widgets):
                    return ""
                text = norm(rows[k][l] or "")
                return text if len(text) <= MAX_ARIA else ""
        return ""

    def consume(self, claim: Claim, widget_cell: tuple[int, int, int] | None) -> bool:
        """Take the claimed text out of where it is printed. Flow lines leave the flow; a
        table cell is cut by the exact text. False leaves everything as it was."""
        if claim.how == "cell":
            ti, i, j = widget_cell
            self.tables[ti].rows[i][j] = ""
            if self.tables[ti].method == TableMethod.FORM_GRID:   # the box's lines are in the flow: they are the label
                self.consumed_now.update(id(l) for l in self.lines if id(l) in self.flow_ids
                                         and _cell_at(self.tables, centre(l.bbox)) == widget_cell)
            return True
        flow = [id(l) in self.flow_ids for l in claim.lines]
        if all(flow):
            self.consumed_now.update(id(l) for l in claim.lines)
            return True
        if any(flow):
            return False
        cells = {_cell_at(self.tables, centre(l.bbox)) for l in claim.lines}
        if len(cells) != 1 or None in cells:
            return False
        cell = cells.pop()
        if claim.how in ("above",) and cell != widget_cell:
            return False
        return _cut(self.tables, cell, claim.text)

    def node(self, w: Widget, label: Claim | None, bound: bool) -> Node:
        attrs: dict = {"kind": w.kind, "fid": f"f{w.page}-{w.index}", "name": w.name,
                       "width": round(w.bbox[2] - w.bbox[0], 1),
                       "height": round(w.bbox[3] - w.bbox[1], 1)}
        tooltip = _tooltip(w.tooltip)
        if tooltip:
            attrs["tooltip"] = tooltip
        # A password's value is never copied out: the output would publish it.
        for key, value in (("value", "" if w.password else w.value), ("export", w.export),
                           ("maxlen", w.maxlen)):
            if value not in ("", None):
                attrs[key] = value
        if w.options:
            attrs["options"] = list(w.options)
            if w.selected:
                attrs["selected"] = list(w.selected)
        for key, on in (("multiline", w.multiline), ("required", w.required),
                        ("readonly", w.readonly), ("comb", w.comb), ("checked", w.checked),
                        ("password", w.password)):
            if on:
                attrs[key] = True
        text = label.text if label is not None and bound else None
        # A tooltip that joins the author's field-name parts with "_" ("..._Row", "Phone_Best way
        # to contact you:") is not a name to read out: the caption printed over the box is. The
        # tooltip stays as the control's title.
        caption = self.caption_above(w) if "_" in tooltip and not text else ""
        if text and w.kind in ("radio", "checkbox") and bound:
            # A group's members share one tooltip (the question); each is named by its option.
            attrs["label"] = LabelSource.PRINTED.value
        elif caption:
            attrs["aria"], attrs["label"] = caption, LabelSource.PRINTED_NEARBY.value
        elif tooltip and self._generic_tooltip(w, tooltip, label):
            attrs["aria"], attrs["label"] = label.text[:MAX_ARIA], LabelSource.PRINTED_NEARBY.value
        elif tooltip:
            attrs["aria"], attrs["label"] = tooltip[:MAX_ARIA], LabelSource.TOOLTIP.value
        elif text:
            attrs["label"] = LabelSource.PRINTED.value
        elif label is not None:
            attrs["aria"], attrs["label"] = label.text[:MAX_ARIA], LabelSource.PRINTED_NEARBY.value
        elif header := self.cell_header(w):
            attrs["aria"], attrs["label"] = header[:MAX_ARIA], LabelSource.CELL_HEADER.value
        else:
            attrs["label"] = LabelSource.NONE.value
        if w.detected is not None:
            attrs["detected"] = w.detected.source.value
        box = w.bbox
        if label is not None and bound and label.how == "left" and (w.on_blank or (
                w.detected is not None and w.detected.source == DetectionSource.TEXT)):
            # A printed fill-in line ("Reason for C/Sect: ____") is its caption and its blank:
            # place_flow then finds the text printed above the caption as above the field.
            box = (min(box[0], *(l.x0 for l in label.lines)), box[1], box[2], box[3])
        node = Node(Role.FIELD, page=w.page, bbox=box, text=text, attrs=attrs)
        if w.detected is not None and w.detected.source == DetectionSource.ASSIST:
            d = w.detected
            extra = {} if d.confidence is None else {"confidence": round(d.confidence, 3)}
            node.fact("field-assisted", Source.ASSIST,
                      f"AI judged this {d.shape} of a scanned page to be a form field",
                      model=d.model, shape=d.shape, doubts=list(d.doubts), **extra)
        if attrs["label"] == LabelSource.NONE:
            node.fact("label-missing", Source.HEURISTIC, "no tooltip, printed label or cell header")
        return node

    def _generic_tooltip(self, w: Widget, tooltip: str, label: Claim | None) -> bool:
        """A text box whose tooltip another, differently named text box repeats ("Member ID" on
        the month, day and year of a date of birth) and that a printed caption names otherwise:
        the author's tooltip is not this box's name, the caption is. The repeat must be the box
        the tooltip names: the caption printed under it reads the same."""
        if label is None or w.kind not in ("text", "combo") or label.how != "nearby":
            return False
        text = norm(re.sub(r"\s\d+/\d+$", "", label.text)).lower()
        if not text or text == tooltip.lower() or text in tooltip.lower():
            return False
        # ...and the tooltip is the printed caption of that other box, which it was written for.
        return any(v is not w and v.kind in ("text", "combo") and v.name != w.name
                   and _tooltip(v.tooltip).lower() == tooltip.lower()
                   and any(norm(l.text).lower() == tooltip.lower() and self._under(v, l) for l in self.lines)
                   for v in self.widgets)

    @staticmethod
    def _covers(w: Widget, line: Line, box: Widget) -> bool:
        """The text box `w` is drawn over the label `line` of the check box `box` beside it."""
        return (w.bbox[0] - 1 <= line.x0 < line.x1 <= w.bbox[2] + 1
                and line.top < w.bbox[3] and line.bottom > w.bbox[1] - ROW_TOLERANCE
                and abs(centre(line.bbox)[1] - centre(box.bbox)[1]) <= max(box.bbox[3] - box.bbox[1], 10))

    def _inline_blanks(self, singles: list[tuple[Node, Widget]],
                       pending: list[tuple[list[Widget], list[Node]]],
                       option_claims: dict[int, Claim]) -> list[tuple[Node, Widget]]:
        """A text box printed at the end of an option's label ("OTHER: ____", "NO. OF UNITS
        ____") belongs to that option: it goes into the group right after the option's box and
        is named by the option's text. Returns the singles that stay."""
        kid_of = {k.attrs.get("fid"): kids for _, kids in pending for k in kids}
        boxes = {w.index: w for w in self.widgets if w.index in option_claims}
        stay: list[tuple[Node, Widget]] = []
        for node, w in singles:
            owner = next((i for i, c in option_claims.items()
                          if w.kind == "text" and not w.comb
                          and len(c.lines) == 1
                          and c.how == "right" and c.text.strip()
                          and w.bbox[0] >= boxes[i].bbox[2]
                          and ((w.on_blank or w.detected) and self._row(w, c.lines[0])
                               and 0 <= w.bbox[0] - c.lines[0].x1 <= LEFT_GAP
                               # a box drawn over its option's own label ("Other:"), the text's
                               # line box a point or two above the box's top
                               or self._covers(w, c.lines[0], boxes[i]))
                          and f"f{self.page.number}-{i}" in kid_of), None)
            if owner is None:
                stay.append((node, w))
                continue
            kids = kid_of[f"f{self.page.number}-{owner}"]
            at = next(n for n, k in enumerate(kids) if k.attrs.get("fid") == f"f{self.page.number}-{owner}")
            if node.text is None:
                node.attrs["aria"], node.attrs["label"] = option_claims[owner].text[:MAX_ARIA], LabelSource.PRINTED_NEARBY.value
            kids.insert(at + 1, node)
        return stay

    def plan(self) -> FormPlan:
        self.consumed_now: set[int] = set()
        result = FormPlan(self.tables)
        member_ids: set[int] = set()
        pending: list[tuple[list[Widget], list[Node]]] = []
        # Groups' option labels first (the text right of each box), so no single field takes
        # them; their legends are claimed after the single fields have taken their labels.
        option_claims: dict[int, Claim] = {}
        for members in self.groups():
            kids = []
            for m in members:
                claim = self._right(m)
                if claim:
                    self.taken.update(id(l) for l in claim.lines)
                    option_claims[m.index] = claim
                bound = bool(claim) and self.consume(claim, self.place[m.index])
                kids.append(self.node(m, claim, bound))
            pending.extend(self._carve_yes_no(members, kids))
            member_ids.update(m.index for m in members)
        singles = []
        per_cell: dict[tuple[int, int, int], int] = {}
        for w in self.widgets:
            if self.place[w.index] is not None:
                per_cell[self.place[w.index]] = per_cell.get(self.place[w.index], 0) + 1
        for w in self.widgets:
            if w.index in member_ids:
                continue
            claim = self.cell_label(w, per_cell) or self.claim(w) or self.nearby_label(w)
            bound = False
            if claim is not None and claim.how in ("above", "cell"):
                bound = self.consume(claim, self.place[w.index])
            elif claim is not None and claim.how in ("left", "right") \
                    and self.place[w.index] is None:
                bound = self.consume(claim, None)
            elif claim is not None and self._in_form_grid(w):
                bound = self.consume(claim, self.place[w.index])   # its lines are in the flow
            singles.append((self.node(w, claim, bound), w))
        singles = self._inline_blanks(singles, pending, option_claims)
        entries: list[_Group] = []
        for members, kids in pending:
            legend = self.legend(members)
            is_radio = members[0].kind == "radio"
            # A section title above the box names the group for assistive technology, but stays
            # in the page as the heading it is.
            titled = legend is not None and (legend.shared or all(_is_section_title(l) for l in legend.lines))
            if legend is not None and not titled:
                self.taken.update(id(l) for l in legend.lines)
            text = None
            if legend is not None and not titled and self.consume(legend, self.place[members[0].index]):
                text = legend.text
            attrs = {"kind": "group", "radio": is_radio}
            if legend is None:
                attrs["label"] = LabelSource.NONE.value
            elif text is None:
                attrs["aria"] = legend.text.rstrip(" \u2610\u2611\u2612")[:MAX_ARIA]
            box = (min(m.bbox[0] for m in members), min(m.bbox[1] for m in members),
                   max(m.bbox[2] for m in members), max(m.bbox[3] for m in members))
            if is_radio:
                self._as_radio_group(attrs, kids, f"g{self.page.number}-{members[0].index}")
            node = Node(Role.GROUP, page=self.page.number, bbox=box, text=text,
                        attrs=attrs, children=kids)
            if legend is None:
                node.fact("label-missing", Source.HEURISTIC,
                          "no tooltip, printed label or cell header")
            title = {id(l) for l in legend.lines} if titled else set()
            entries.append(_Group(node, list(members), title,
                                  self._title_above(box[1]) if legend is None else None))
        entries = self._merge_unnamed(entries)
        for g in entries:
            self._exclusive_choice(g)
        group_nodes = [(g.node, g.members) for g in entries]
        for node, w in singles + group_nodes:
            members = w if isinstance(w, list) else [w]
            cell = self.place[members[0].index]
            if cell is None:
                result.flow.append(node)
            else:
                result.cells.setdefault(cell, []).append(node)
        # The printed box of every checkbox in a cell is its widget: no glyph stays in the text.
        for ti, i, j in {cell for cell, nodes in result.cells.items()
                         if any(n.attrs.get("kind") in ("checkbox", "radio") or n.role == Role.GROUP
                                for n in nodes)}:
            held = self.tables[ti].rows[i][j]
            if held and BOX_GLYPHS.search(held):
                self.tables[ti].rows[i][j] = norm(BOX_GLYPHS.sub(" ", held))
        result.consumed = self.consumed_now
        return result
