"""Grouping and legends: which checkboxes and radios form a group, the question that names it, and
whether the group is exclusive (see analyze.forms)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from reflow.analyze.constants import RIGHT_GAP
from reflow.analyze.form_common import (
    MAX_LABEL_LINES,
    NUMERAL_ROW,
    ROW_TOLERANCE,
    STACK_GAP,
    Claim,
    _is_section_title,
    norm,
)
from reflow.analyze.form_labels import LabelSearch
from reflow.extract.forms import DetectionSource, Widget
from reflow.extract.lines import Line
from reflow.geometry import centre
from reflow.model import LabelSource, Node

CHART_CAPTION_REACH = 30.0  # the caption of a chart of numbered boxes ends at most this far over its first row
GROUP_STEP = 14.0  # stacked checkboxes: the next one starts at most this far below
LEGEND_GAP = 18.0
LEGEND_OVERLAP = 2.0  # a box this little inside the legend line's height (a trailing box glyph, a field above) is not beside it
DRAWN_ROW_BLANK = 48.0  # pt: drawn boxes with more blank than this between them are not one row
TITLE_REACH = 24.0  # pt: a heading this near a column of boxes is over it
COLUMN_MIN = 3  # drawn boxes stacked in a column that make it a list of options, not a row's boxes
GROUP_ROW_GAP = 24.0  # × box width: same-row checkboxes group when no further apart than this
MAX_LEFT_QUESTION = 240  # but a question (or a "label:") on the group's own row may run this long
QUESTION_GAP = 6.0  # a group's question printed after its options starts at most this far past the last label (pt)
# A printed instruction that the choices are exclusive ("Check only one."), and its reach above the group.
CHOOSE_ONE = re.compile(r"\b(?:check|select|mark|tick|choose)\s+(?:only\s+)?(?:one|1)\b(?!\s+(?:or|and|\+)\b)", re.I)
CHOOSE_ONE_REACH = 36.0
YES_NO = (("yes", "no"), ("y", "n"))


def _row_order(widgets: list[Widget]) -> list[Widget]:
    """Read each row left to right despite small differences in control height."""
    pending = sorted(widgets, key=lambda w: (w.bbox[1], w.bbox[0], w.index))
    ordered = []
    while pending:
        top = pending[0].bbox[1]
        row = [w for w in pending if w.bbox[1] <= top + ROW_TOLERANCE]
        ordered.extend(sorted(row, key=lambda w: (w.bbox[0], w.index)))
        pending = pending[len(row):]
    return ordered


def _stem(name: str) -> str:
    return re.sub(r"\[\d+\]$", "", name)


def _words(text: str | None) -> list[str]:
    return re.sub(r"[^\w\s]", " ", (text or "").lower()).split()


def _answers(*kids: Node) -> tuple[str, ...]:
    """The words of the options' printed labels, one string each ("yes", "no")."""
    return tuple(" ".join(_words(k.text)) for k in kids)


def _opposite(a: Node, b: Node) -> bool:
    """One label is the other with a "not" put in ("Are requested", "Are not requested")."""
    wa, wb = _words(a.text), _words(b.text)
    if "not" in wb:
        wa, wb = wb, wa
    return "not" in wa and "not" not in wb and [w for w in wa if w != "not"] == wb and len(wb) >= 2


def _plain(g: _Group) -> bool:
    """A group of checkboxes (not a PDF's own radio group, nor one found on a scan). The text
    box of an option's blank ("OTHER: ____", see _inline_blanks) is part of its option."""
    return not g.node.attrs.get("radio") and all(
        k.attrs.get("kind") == "text"
        or k.attrs.get("kind") == "checkbox" and k.attrs.get("detected") in (None, DetectionSource.DRAWN)
        for k in g.node.children)


@dataclass
class _Group:
    """A group of controls on its way to a Node: what names it, for _merge_unnamed."""
    node: Node
    members: list[Widget]
    title: set[int]  # id() of the section title lines that name it (its legend), if they do
    under: Line | None  # the section title above a group with no legend of its own


class Grouping(LabelSearch):
    """Groups the page's checkboxes and radios, and finds each group's legend."""

    def groups(self) -> list[list[Widget]]:
        """Radios sharing a name; checkboxes sharing a name stem, row or close column."""
        out: list[list[Widget]] = []
        used: set[int] = set()
        by_name: dict[tuple[str, str], list[Widget]] = {}
        for w in self.widgets:
            if w.name and w.kind in ("radio", "checkbox"):
                name = w.name if w.kind == "radio" else _stem(w.name)
                by_name.setdefault((w.kind, name), []).append(w)
        for members in by_name.values():
            if len(members) > 1:
                out.append(members)
                used.update(m.index for m in members)
        # A PDF's radios with no name are not grouped, but the ovals drawn on a page are, as boxes are.
        boxes = [w for w in self.widgets if w.index not in used
                 and (w.kind == "checkbox" or w.kind == "radio" and w.drawn)]
        while boxes:
            members = [boxes.pop(0)]
            for member in members:
                for w in list(boxes):
                    if w.kind != member.kind:
                        continue
                    left, right = sorted((w, member), key=lambda v: v.bbox[0])
                    same_row = (abs(centre(w.bbox)[1] - centre(member.bbox)[1]) <= 3
                                and self.place[w.index] == self.place[member.index]
                                and right.bbox[0] - left.bbox[2]
                                <= GROUP_ROW_GAP * max(left.bbox[2] - left.bbox[0], 1)
                                and not self._columns_apart(left, right))
                    upper, lower = sorted((w, member), key=lambda v: v.bbox[1])
                    stacked = (abs(w.bbox[0] - member.bbox[0]) <= 3
                               and -1 <= lower.bbox[1] - upper.bbox[3] <= GROUP_STEP
                               and not self._in_boxes_apart(w, member)
                               and not self._title_between(upper, lower))
                    if same_row or stacked:
                        members.append(w)
                        boxes.remove(w)
            if len(members) > 1:
                out.append(sorted(members, key=lambda w: (w.bbox[1], w.bbox[0], w.index)))
        return [_row_order(members) for members in out]

    def _title_between(self, upper: Widget, lower: Widget) -> bool:
        """A line of words in the gap between two stacked drawn boxes: a heading for the list
        below ("DENTAL COVERAGE" over its options), so the lists are two groups."""
        return upper.drawn and lower.drawn and any(
            not l.rotated and upper.bbox[3] - 1 <= l.top and l.bottom <= lower.bbox[1] + 1
            and l.x0 < lower.bbox[2] + TITLE_REACH and l.x1 > lower.bbox[0] - TITLE_REACH
            and any(c.isalpha() for c in l.text) for l in self.lines)

    def _columns_apart(self, left: Widget, right: Widget) -> bool:
        """Two drawn boxes on a row are not one row of choices when each heads a column of them
        (an option list in one column, another beside it), or when the blank between the words
        after the left box and the right box is wider than a row leaves (DRAWN_ROW_BLANK)."""
        if not (left.drawn and right.drawn):
            return False
        between = [l.x1 for l in self.lines if self._row(left, l) and l.x0 >= left.bbox[2] - 1
                   and l.x1 <= right.bbox[0] + 1]
        return (right.bbox[0] - max(between, default=left.bbox[2]) > DRAWN_ROW_BLANK
                or self._in_column(left) and self._in_column(right))

    def _in_column(self, w: Widget) -> bool:
        """Whether `w` is one of at least COLUMN_MIN drawn boxes of its kind stacked in one
        column, each starting at most GROUP_STEP below the one above."""
        column = sorted((v for v in self.widgets if v.kind == w.kind and v.drawn
                         and abs(v.bbox[0] - w.bbox[0]) <= 3), key=lambda v: v.bbox[1])
        run: list[Widget] = []
        for v in column:
            if run and not -1 <= v.bbox[1] - run[-1].bbox[3] <= GROUP_STEP:
                if w in run:
                    break
                run = []
            run.append(v)
        return w in run and len(run) >= COLUMN_MIN

    def _chart_legend(self, members: list[Widget]) -> Claim | None:
        """The caption of a chart of numbered boxes ("33. Missing Teeth Information ..."): the
        nearest line of words over the group, whichever box of a ruled form it is printed in.
        Each row of the chart names itself by the caption, but only the first takes it out of the
        text (the second one's legend is `shared`)."""
        if not all((c := self.option_labels.get(m.index)) is not None and NUMERAL_ROW.fullmatch(norm(c.lines[0].text))
                   for m in members):
            return None
        top, left = min(m.bbox[1] for m in members), min(m.bbox[0] for m in members)
        cands = [l for l in self.lines if not l.rotated and l.bottom <= top + 2 and top - l.bottom <= CHART_CAPTION_REACH
                 and l.x0 <= left + 24 and l.x1 > left and not NUMERAL_ROW.fullmatch(norm(l.text))
                 and sum(c.isalpha() for c in l.text) >= 3]
        if not cands:
            return None
        line = max(cands, key=lambda l: l.bottom)
        return Claim([line], "legend", shared=True)

    def legend(self, members: list[Widget]) -> Claim | None:
        if chart := self._chart_legend(members):
            return chart
        top = min(m.bbox[1] for m in members)
        left = min(m.bbox[0] for m in members)
        # Columns of one group start at different heights: the question is over the first
        # column, whichever column rises higher.
        column = min(m.bbox[1] for m in members if m.bbox[0] <= left + 24)
        if column > top + ROW_TOLERANCE and (claim := self._legend_over(members, column, left, same_row=False)):
            return claim
        return self._legend_over(members, top, left) or self._left(
            min((w for w in members if w.bbox[1] <= top + ROW_TOLERANCE), key=lambda w: w.bbox[0]))

    def _legend_over(self, members: list[Widget], top: float, left: float, same_row: bool = True) -> Claim | None:
        # The leftmost box of the top row: boxes a point or two apart in height are one row.
        first = min((w for w in members if w.bbox[1] <= top + ROW_TOLERANCE), key=lambda w: w.bbox[0])
        # A same-row question beats prose above; a sentence fragment to the left does not.
        if same_row:
            if (claim := self._left(first, MAX_LEFT_QUESTION)) and claim.text.rstrip().endswith(("?", ":")):
                return claim
            if claim := self._question_after(members, top):
                return claim
        cands = [l for l in self.lines if self._free(l) and l.bottom <= top + 2
                 and top - l.bottom <= LEGEND_GAP and l.x0 <= left + 24 and l.x1 > left
                 and self._walled(first, l)
                 and not any(v.bbox[1] < l.bottom - LEGEND_OVERLAP and v.bbox[3] > l.top + LEGEND_OVERLAP
                             and v.bbox[0] < l.x1 and v.bbox[2] > l.x0 for v in self.widgets)]
        if cands:
            stack = [max(cands, key=lambda l: l.bottom)]
            while len(stack) < MAX_LABEL_LINES:
                above = [l for l in cands if l not in stack
                         and -1 <= stack[0].top - l.bottom <= STACK_GAP
                         and abs(l.x0 - stack[0].x0) <= 24]
                if not above:
                    break
                stack.insert(0, max(above, key=lambda l: l.bottom))
            return Claim(stack, "legend")
        return None

    def _question_after(self, members: list[Widget], top: float) -> Claim | None:
        """The question printed after the last label of the group's top row ("□ Yes □ No Is this
        ...?"), where split_option_questions cut it off the label of the last box."""
        row = [w for w in members if w.bbox[1] <= top + ROW_TOLERANCE]
        last = max(row, key=lambda w: w.bbox[2])
        for label in self.lines:
            if id(label) not in self.taken or not self._row(last, label) \
                    or not -1 <= label.x0 - last.bbox[2] <= RIGHT_GAP:
                continue
            for l in self.lines:
                if (self._free(l) and self._row(last, l) and 0 <= l.x0 - label.x1 <= QUESTION_GAP
                        and norm(l.text).endswith("?")):
                    return Claim([l], "legend")
        return None

    def _title_above(self, top: float) -> Line | None:
        titles = [l for l in self.lines if not l.rotated and l.bottom <= top + 2 and _is_section_title(l)]
        return max(titles, key=lambda l: l.bottom, default=None)

    @staticmethod
    def _as_radio_group(attrs: dict, kids: list[Node], name: str) -> None:
        attrs["radio"], attrs["name"] = True, name
        for kid in kids:
            kid.attrs["group"] = name

    def _carve_yes_no(self, members: list[Widget],
                      kids: list[Node]) -> list[tuple[list[Widget], list[Node]]]:
        """A Y / N (Yes / No) pair printed on a row with other options ("Pathways  Health Choice
        Y  N") is a group of its own: the question to the pair's left names it. The rest stay a
        group when two or more remain."""
        if len(members) > 2 and all(m.kind == "checkbox" and m.detected is None for m in members):
            for i in range(len(members) - 1):
                a, b = members[i], members[i + 1]
                if (_answers(kids[i], kids[i + 1]) in YES_NO
                        and abs(a.bbox[1] - b.bbox[1]) <= ROW_TOLERANCE and a.bbox[0] < b.bbox[0]
                        and len(members) - 2 >= 2):
                    return [(members[:i] + members[i + 2:], kids[:i] + kids[i + 2:]),
                            (members[i:i + 2], kids[i:i + 2])]
        return [(members, kids)]

    def _merge_unnamed(self, entries: list[_Group]) -> list[_Group]:
        """A checkbox group nothing names, under a section title that already names another
        group ("PROPERTY TYPE": its boxes print in three columns), is one more part of that group:
        two fieldsets with one legend would read as two questions."""
        for g in list(entries):
            if g.under is None or g.node.attrs.get("label") != LabelSource.NONE or not _plain(g):
                continue
            host = next((h for h in entries if h is not g and id(g.under) in h.title and _plain(h)), None)
            if host is None:
                continue
            # Side by side (columns of one list): left first; otherwise the upper one.
            side = abs(host.node.bbox[1] - g.node.bbox[1]) <= 2 * ROW_TOLERANCE
            first, second = sorted((host, g), key=lambda e: e.node.bbox[0] if side else e.node.bbox[1])
            host.node.children = first.node.children + second.node.children
            host.members = first.members + second.members
            # The top stays the host's: the named part sits under its title, and the group is
            # placed in the flow by its top (place_flow), so a box a point higher in the next
            # column must not lift it above that title.
            host.node.bbox = (min(host.node.bbox[0], g.node.bbox[0]), host.node.bbox[1],
                              max(host.node.bbox[2], g.node.bbox[2]), max(host.node.bbox[3], g.node.bbox[3]))
            entries.remove(g)
        return entries

    def _exclusive_choice(self, g: _Group) -> None:
        """Turn a group's checkboxes into radios where the form says only one can be chosen:
        a Yes / No pair, or a printed "Check only one" in its legend or just above it. Only boxes
        that are separate PDF fields qualify (each radio then fills its own field) and none may
        be ticked already beyond one (a radio group holds a single choice)."""
        kids = g.node.children
        names = [k.attrs.get("name") for k in kids]
        # Each radio fills its own PDF field, so the boxes need distinct names; a form with no
        # fields of its own has none, and its group is the only thing that binds the radios.
        drawn = all(k.attrs.get("detected") == DetectionSource.DRAWN for k in kids)
        if (not _plain(g) or any(k.attrs.get("kind") == "text" for k in kids)
                or len(kids) < 2 or not drawn and (not all(names) or len(set(names)) != len(names))
                or sum(1 for k in kids if k.attrs.get("checked")) > 1):
            return
        answers = _answers(*kids) if len(kids) == 2 else None
        legend = g.node.text or g.node.attrs.get("aria") or ""
        if not (answers in YES_NO or (len(kids) == 2 and _opposite(*kids))
                or CHOOSE_ONE.search(legend) or self._choose_one_above(g)):
            return
        for kid in kids:
            kid.attrs["kind"] = "radio"
        self._as_radio_group(g.node.attrs, kids, f"g{self.page.number}-{g.members[0].index}")

    def _choose_one_above(self, g: _Group) -> bool:
        """A "Check only one" printed just above the group (or beside the title over it) that no
        other control sits under."""
        left, top, right, _ = g.node.bbox
        mine = {m.index for m in g.members}
        for l in self.lines:
            if (l.rotated or not CHOOSE_ONE.search(l.text) or l.bottom > top + 2
                    or top - l.bottom > CHOOSE_ONE_REACH or l.x1 < left or l.x0 > right):
                continue
            if not any(w.index not in mine and w.bbox[1] >= l.bottom - 2 and w.bbox[3] <= top + 2
                       and w.bbox[0] < right and w.bbox[2] > left for w in self.widgets):
                return True
        return False
