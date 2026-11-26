"""Label search: the printed text that names one control, above it, left of it, right of it or near
it (see analyze.forms). Claiming a line marks it taken, so no other control takes it again."""

from __future__ import annotations

import re

from reflow.analyze.constants import ABOVE_GAP, HANG, LEFT_GAP, OPTION_MARKER, RIGHT_GAP
from reflow.analyze.form_common import (
    MAX_LABEL_LINES,
    STACK_GAP,
    Claim,
    PlannerState,
    _cell_at,
    _tooltip,
    norm,
)
from reflow.extract.forms import DetectionSource, Widget
from reflow.extract.lines import Line
from reflow.extract.tables.cells import TableMethod
from reflow.geometry import centre
from reflow.model import BBox

DRAWN_LABEL_GAP = 4.0  # pt: text this near the right of a drawn box is its own label
LEAD_IN_GAP = 6.0  # pt: words left of a drawn box that end this near it run into its label
CHECK_LEFT_REACH = 160.0  # a scan-detected checkbox's label ends at most this far left of it (leader dots between)
MAX_LEFT_LABEL = 80  # a longer line to the left is a sentence about the row, not a label
BELOW_GAP = 14  # a caption printed under its field sits this close (pt)
BELOW_SPAN = 60.0  # a caption under a field starts within it and runs at most this far past it (one caption over a row of boxes)
BELOW_OVERLAP = 3  # the caption may start this far inside the field box (a rule drawn as the box edge)
ENTRY_GAP = 6.0  # a wrapped entry's lines are at most this far apart
MAX_ENTRY_LINES = 5
# A line number printed between an entry's text and its box: the whole line is a short number,
# optionally with a letter ("1a", "10", "8b") and the "$" that heads the box.
LINE_NUMBER = re.compile(r"\d{1,3}[a-z]?(?:\s*\$)?")
# A list marker that starts an entry ("a Qualified tips"): one lower-case letter, then a capital.
LETTER_MARKER = re.compile(r"[a-z]\s+[A-Z0-9$(\u201c\"]")
SENTENCE_END = (".", ":", "?", "!")
CONNECTORS = ("or", "and", "and/or")  # a word between two fields, not a caption


def _row_probe(box: Widget) -> Line:
    """A line standing where the widget is, to ask whether another widget's row holds it."""
    return Line(box.page, box.bbox, "", box.bbox[3] - box.bbox[1], False, False, "")


def _overlaps_x(a: BBox, b: BBox, slack: float = 0.0) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) - slack


def is_line_number(line: Line) -> bool:
    return bool(LINE_NUMBER.fullmatch(line.text.strip()))


def starts_lower(text: str) -> bool:
    """The text starts in lower case and not with a list marker ("an estimate ..." wraps the line
    above it; "a Qualified tips" starts an entry)."""
    text = text.strip()
    return text[:1].islower() and not LETTER_MARKER.match(text)


class LabelSearch(PlannerState):
    """Finds the printed text that names a control."""

    def _place(self, w: Widget) -> tuple[int, int, int] | None:
        return _cell_at(self.tables, centre(w.bbox))

    def _near_other_widget(self, w: Widget, line: Line) -> bool:
        """Another control sits between the line and the widget: the line is its label."""
        for v in self.widgets:
            if v is w:
                continue
            lo, hi = sorted((line.bottom, w.bbox[1]))
            if v.bbox[3] > lo and v.bbox[1] < hi and _overlaps_x(v.bbox, w.bbox) \
                    and v.bbox[1] >= line.top - 1:
                return True
        return False

    def _starts_below(self, w: Widget, line: Line) -> bool:
        """Another control starts under the line within a line height: the line likely labels
        that next row instead."""
        height = max(line.bottom - line.top, 1)
        return any(v is not w and v.bbox[1] >= line.top
                   and v.bbox[1] - line.bottom <= height and _overlaps_x(v.bbox, line.bbox)
                   and not self._caption_of_field_above(v, line)
                   for v in self.widgets)

    def _free(self, line: Line) -> bool:
        text = norm(line.text).lower()
        return (id(line) not in self.taken and not line.rotated
                and text not in ("", *CONNECTORS))

    def _above(self, w: Widget) -> Claim | None:
        x0, top, x1, _ = w.bbox
        best = None
        cell = self.place[w.index]
        for l in self.lines:
            if not self._free(l) or l.bottom > top + 3 or top - l.bottom > ABOVE_GAP:
                continue
            # In a table the label is in the field's own cell: the text above that is a header.
            if cell is not None and _cell_at(self.tables, centre(l.bbox)) != cell:
                continue
            if l.x0 < x0 - 6 or l.x0 >= x1 or l.x1 > x1 + 60:
                continue
            if self._near_other_widget(w, l) or self._caption_of_field_above(w, l):
                continue
            if best is None or l.bottom > best.bottom:
                best = l
        if best is None:
            return None
        stack = [best]
        while len(stack) < MAX_LABEL_LINES:
            up = [l for l in self.lines if self._free(l) and l not in stack
                  and 0 <= stack[0].top - l.bottom <= STACK_GAP
                  and abs(l.x0 - stack[0].x0) <= (HANG if self._in_form_grid(w) else 3)
                  and l.x1 <= x1 + 60
                  and l.bold == stack[0].bold and abs(l.size - stack[0].size) <= 0.5
                  and not self._near_other_widget(w, l)]
            if not up:
                break
            stack.insert(0, max(up, key=lambda l: l.bottom))
        return Claim(stack, "above")

    def _under(self, v: Widget, line: Line) -> bool:
        """The line is printed under the text box `v`, right under its rule, starting within it."""
        return (v.kind in ("text", "combo", "list") and -BELOW_OVERLAP <= line.top - v.bbox[3] <= BELOW_GAP
                and v.bbox[0] - 6 <= line.x0 < v.bbox[2])

    def _caption_of_field_above(self, w: Widget, line: Line) -> bool:
        """The line over `w` is the caption of the text box over the line, not of `w`: a form
        that prints every caption under its rule (box, caption, box, caption) sets the caption of
        one row right over the next row's box. The tooltips tell: the line reads as the tooltip
        of a box it is under, or `w`'s own tooltip reads as a line under `w`."""
        text = norm(line.text).lower()
        tip = norm(w.tooltip).lower()
        if tip and text != tip and any(
                norm(l.text).lower() == tip and self._under(w, l) for l in self.lines):
            return True
        return any(v is not w and norm(v.tooltip).lower() == text and self._under(v, line)
                   for v in self.widgets)

    def _row(self, w: Widget, line: Line) -> bool:
        cy = (line.top + line.bottom) / 2
        return w.bbox[1] - 3 <= cy <= w.bbox[3] + 3 and self._walled(w, line)

    def _in_form_grid(self, w: Widget) -> bool:
        cell = self.place.get(w.index)
        return cell is not None and self.tables[cell[0]].method == TableMethod.FORM_GRID

    def _in_boxes_apart(self, a: Widget, b: Widget) -> bool:
        """Whether two controls are in different boxes of a form grid: their choices are not one
        group, however close they stand."""
        ca, cb = self.place.get(a.index), self.place.get(b.index)
        return (ca != cb and ca is not None and cb is not None
                and self.tables[ca[0]].method == TableMethod.FORM_GRID and self.tables[cb[0]].method == TableMethod.FORM_GRID)

    def _walled(self, w: Widget, line: Line) -> bool:
        """Whether the line is in the box of the form grid that the control is in (a form grid
        is ruled into boxes, each holding a caption and its choices, so a label is never in the
        next box). Anywhere else a line is on the control's row wherever it is."""
        cell = self.place.get(w.index)
        if cell is None or self.tables[cell[0]].method != TableMethod.FORM_GRID:
            return True
        return _cell_at(self.tables, centre(line.bbox)) == cell

    def _left(self, w: Widget, longest: int = MAX_LEFT_LABEL) -> Claim | None:
        cands = [l for l in self.lines if self._free(l) and self._row(w, l)
                 and l.x1 <= w.bbox[0] + 1
                 and not any(v is not w and v.bbox[2] <= w.bbox[0] and v.bbox[2] > l.x1 and
                             v.bbox[1] < l.bottom and v.bbox[3] > l.top for v in self.widgets)]
        cands.sort(key=lambda l: -l.x1)
        numbers = 0
        while numbers < len(cands) and is_line_number(cands[numbers]):
            numbers += 1
        if numbers and numbers < len(cands):
            # Entry text, then its line number(s), then the box: the leader dots between them
            # may run any length, so the text need not be near the box.
            return Claim(self._entry(cands[numbers]), "left")
        cands = [l for l in cands if w.bbox[0] - l.x1 <= LEFT_GAP
                 and len(norm(l.text)) <= longest]
        if not cands:
            return None
        return Claim([cands[0]], "left")

    def _entry(self, last: Line) -> list[Line]:
        """The lines of the entry that ends with `last`, top first: `last` continues the line
        above it when it starts in lower case or that line does not end a sentence."""
        stack = [last]
        while len(stack) < MAX_ENTRY_LINES:
            top = stack[0]
            above = [l for l in self.lines if self._free(l) and l not in stack
                     and not is_line_number(l) and 0 <= top.top - l.bottom <= ENTRY_GAP
                     and l.x0 <= top.x0 + 3 and l.x1 > top.x0]
            if not above:
                break
            nearest = max(above, key=lambda l: l.bottom)
            if not (starts_lower(top.text) or not norm(nearest.text).endswith(SENTENCE_END)):
                break
            stack.insert(0, nearest)
        return stack

    def _right(self, w: Widget) -> Claim | None:
        if w.index in self.option_labels:
            return self.option_labels[w.index]
        cands = [l for l in self.lines if self._free(l) and self._row(w, l)
                 and -1 <= l.x0 - w.bbox[2] <= RIGHT_GAP]
        if cands:
            line = min(cands, key=lambda l: l.x0)
            if (w.detected is None or not (self._belongs_to_next(w, line) or self._takes_left(w))) \
                    and (claim := self._right_claim(w, line)):
                return self._lead_in(w, claim)
        return self._left_label(w) if w.detected is not None else None

    def _lead_in(self, w: Widget, claim: Claim) -> Claim:
        """A drawn box's label starts with the words left open just before the box ("(From [ ] LMP
        [ ] U/S)"): the label reads "(From LMP", so no stray "(From" is left in the flow."""
        if not w.drawn or claim.whole is not None:
            return claim
        cands = [line for line in self.lines if self._free(line) and self._row(w, line)
                 and 0 <= w.bbox[0] - line.x1 <= LEAD_IN_GAP
                 and line.text.count("(") > line.text.count(")")
                 and not any(v is not w and v.bbox[2] > line.x1 and v.bbox[0] < w.bbox[0]
                             and v.bbox[1] < line.bottom and v.bbox[3] > line.top
                             for v in self.widgets)]
        if not cands:
            return claim
        return Claim([max(cands, key=lambda line: line.x1), *claim.lines], "right")

    def _right_claim(self, w: Widget, line: Line) -> Claim | None:
        """The line right of a box. Taking it takes the whole line out of the flow, so the label
        is every word of the line that no other box takes as its option label (see
        _option_labels): the run from the first such word to the last. The markers around that
        run are dropped only where another box stands on the line, as its box or the box of an
        option printed apart ("M ■ F", "A ■"); with none they are printed text ("I agree • I
        understand ..."). A count of markers or boxes does not say whose words are whose, so a
        word no other box takes stays here ("Yes • see note ■ No" when the box on the ■ got no
        label). A line whose every word labels another box gives this one nothing."""
        parts = OPTION_MARKER.split(line.text)
        held = {c.part for c in self.option_labels.values() if c.lines[0] is line}
        followed = held or any(
            v is not w and v.kind in ("checkbox", "radio") and self._row(v, line)
            and v.bbox[0] >= w.bbox[2] and line.x0 <= centre(v.bbox)[0] <= line.x1
            for v in self.widgets)
        if len(parts) < 2 or not followed:
            return Claim([line], "right")
        free = [i for i, part in enumerate(parts)
                if i not in held and any(c.isalnum() for c in part)]
        if not free:
            return None if held else Claim([line], "right")
        first, last = free[0], free[-1]
        if any(i in held for i in range(first, last)):
            return Claim([line], "right")
        marks = list(OPTION_MARKER.finditer(line.text))
        start = marks[first - 1].end() if first else 0
        end = marks[last].start() if last < len(marks) else len(line.text)
        return Claim([line], "right", norm(line.text[start:end]))

    def _takes_left(self, w: Widget) -> bool:
        """The row prints each label before its box ("Dental? [ ] Medical? [ ] (note)"), and `w` is
        the last box, with a note after it: the words between the box before it and `w` are its
        label, not the note."""
        for p in self.widgets:
            if (p is w or p.detected is None or p.kind not in ("checkbox", "radio")
                    or p.bbox[2] > w.bbox[0] + 1 or not self._row(w, _row_probe(p))):
                continue
            if p.drawn and any(self._row(p, line) and -1 <= line.x0 - p.bbox[2] <= DRAWN_LABEL_GAP
                               for line in self.lines):
                continue  # `p` has its own label tight to its right: the row prints labels after its boxes
            between = [l for l in self.lines if self._free(l) and self._row(w, l)
                       and l.x0 >= p.bbox[2] - 1 and l.x1 <= w.bbox[0] + 1
                       and w.bbox[0] - l.x1 <= LEFT_GAP]
            if any(self._belongs_to_next(p, l, free=False) for l in between):
                return True
        return False

    def _belongs_to_next(self, w: Widget, line: Line, free: bool = True) -> bool:
        """The row prints its label first ("Finished this grade ... [ ] Did not finish ... [ ]"):
        `w` has words on its left, and the line on its right is followed by another checkbox."""
        left = self._left_candidates(w, free)
        if not left:
            return False
        if w.drawn \
                and line.x0 - w.bbox[2] <= DRAWN_LABEL_GAP \
                and w.bbox[0] - max(l.x1 for l in left) > line.x0 - w.bbox[2]:
            # A drawn box has its own label tight to its right, nearer than the words left of
            # it: "Type of Appeal [ ] Denied Claim [ ] Cost Share", not "Yes [ ] No [ ]". Text
            # farther off is a label printed before the next box: "Dental? [ ] Medical? [ ]".
            return False
        return any(
            v is not w and v.kind in ("checkbox", "radio") and self._row(v, line)
            and 0 <= v.bbox[0] - line.x1 <= LEFT_GAP for v in self.widgets)

    def _left_candidates(self, w: Widget, free: bool = True) -> list[Line]:
        return [l for l in self.lines if (self._free(l) or not free) and self._row(w, l)
                and l.x1 <= w.bbox[0] + 1 and w.bbox[0] - l.x1 <= CHECK_LEFT_REACH
                and sum(c.isalnum() for c in l.text) >= 2
                and not any(v is not w and v.bbox[2] <= w.bbox[0] and v.bbox[2] > l.x1
                            and v.bbox[1] < l.bottom and v.bbox[3] > l.top for v in self.widgets)]

    def _left_label(self, w: Widget) -> Claim | None:
        """A scan-detected checkbox with no label on its right takes the words on its row to its
        left, which may end far from it: the leader dots between were read off the page by the
        scan rules. A row of boxes (grades 1 to 8) names the later ones by the first one's
        label and their place in the row, as `_run_label` does for text boxes."""
        if cands := self._left_candidates(w):
            return Claim([max(cands, key=lambda l: l.x1)], "left")
        return self._run_label(w)

    def claim(self, w: Widget) -> Claim | None:
        if w.kind in ("checkbox", "radio"):
            found = self._right(w)
        elif (w.detected is not None and w.detected.source == DetectionSource.TEXT) or w.on_blank:
            # A blank printed in underscores follows its caption on the row ("Name: ______"):
            # the line above is the previous row's caption, not this one's.
            found = self._left(w) or self._above(w)
        else:
            found = self._above(w) or self._left(w)
        if found:
            self.taken.update(id(l) for l in found.lines)
        return found

    def nearby_label(self, w: Widget) -> Claim | None:
        """Name a field from shared printed text without consuming that text again."""
        if w.comb:
            # One heading labels all the adjacent segments of a number, not just the first.
            members = [w]
            for member in members:
                for other in self.widgets:
                    if (other not in members and other.comb
                            and abs(other.bbox[1] - w.bbox[1]) <= 3
                            and self.place[other.index] == self.place[w.index]
                            and max(other.bbox[0], member.bbox[0])
                            - min(other.bbox[2], member.bbox[2]) <= LEFT_GAP):
                        members.append(other)
            left = min(m.bbox[0] for m in members)
            right = max(m.bbox[2] for m in members)
            above = [line for line in self.lines if not line.rotated
                     and 0 <= w.bbox[1] - line.bottom <= ABOVE_GAP
                     and left - 6 <= line.x0 < right and line.x1 <= right + 60
                     and any(c.isalpha() for c in line.text)
                     and _cell_at(self.tables, centre(line.bbox)) == self.place[w.index]]
            if above:
                return Claim([max(above, key=lambda line: line.bottom)], "nearby")
        beside = [line for line in self.lines if not line.rotated and self._row(w, line)
                  and line.x1 <= w.bbox[0] + 1
                  and w.bbox[0] - line.x1 <= LEFT_GAP * (2 if id(line) in self.taken else 1)
                  and any(c.isalpha() for c in line.text)
                  and norm(line.text).lower() not in CONNECTORS
                  and _cell_at(self.tables, centre(line.bbox)) == self.place[w.index]
                  and not any(v is not w and v.bbox[2] <= w.bbox[0]
                              and v.bbox[2] > line.x1 and self._row(v, line)
                              for v in self.widgets)]
        if beside:
            return Claim([max(beside, key=lambda line: line.x1)], "nearby")
        if run := self._run_label(w):
            return run
        # Forms that print the caption under a rule ("Print first and last name"): the nearest
        # line just below, starting within the field's width.
        below = [line for line in self.lines if not line.rotated
                 and -BELOW_OVERLAP <= line.top - w.bbox[3] <= BELOW_GAP
                 and self._under(w, line) and line.x1 <= w.bbox[2] + BELOW_SPAN
                 and w.kind in ("text", "combo", "list")
                 and any(c.isalpha() for c in line.text)
                 and len(norm(line.text)) <= MAX_LEFT_LABEL
                 and _cell_at(self.tables, centre(line.bbox)) == self.place[w.index]
                 and not self._near_other_widget(w, line)
                 and (self._hugs(w, line) or not self._starts_below(w, line))]
        if below:
            return Claim([min(below, key=lambda line: line.top)], "nearby")
        return None

    def _hugs(self, w: Widget, line: Line) -> bool:
        """A caption printed right under the rule of a blank made from underscores, whatever
        else starts under it: each rule of a stack of blanks has its own caption."""
        return (w.detected is not None and w.detected.source == DetectionSource.TEXT
                and line.top - w.bbox[3] <= BELOW_OVERLAP)

    def _run_label(self, w: Widget) -> Claim | None:
        """The second and later boxes of one row of text boxes (month / day / year) are named
        by the row's caption and their place in it: "Fecha de nacimiento: 2/3". The caption is
        read where it is printed, left of the first box; it stays in the page as text. A row of
        checkboxes is named the same way, from the label left of its first box."""
        if w.kind not in ("text", "combo", "checkbox", "radio"):
            return None
        boxes = w.kind in ("checkbox", "radio")
        if boxes and w.detected is None:   # only for the boxes the scan rules drew
            return None
        kinds = ("checkbox", "radio") if boxes else ("text", "combo")
        cell = self.place[w.index]
        row = sorted((v for v in self.widgets if v.kind in kinds
                      and self.place[v.index] == cell
                      and abs(centre(v.bbox)[1] - centre(w.bbox)[1]) <= 3),
                     key=lambda v: v.bbox[0])
        at = row.index(w)
        first = at
        while first > 0 and row[first].bbox[0] - row[first - 1].bbox[2] <= LEFT_GAP:
            first -= 1
        if first == at:
            return None
        last = at
        while last + 1 < len(row) and row[last + 1].bbox[0] - row[last].bbox[2] <= LEFT_GAP:
            last += 1
        head = row[first]
        reach = CHECK_LEFT_REACH if boxes else LEFT_GAP * 2
        beside = [line for line in self.lines if not line.rotated and self._row(head, line)
                  and line.x1 <= head.bbox[0] + 1 and head.bbox[0] - line.x1 <= reach
                  and any(c.isalpha() for c in line.text)
                  and _cell_at(self.tables, centre(line.bbox)) == cell]
        if not beside and not boxes and not any(self._under(w, l) and any(c.isalpha() for c in l.text) for l in self.lines):
            # A caption printed under the run's first box that has one ("Date of Birth" under the
            # month of a month / day / year row) names the boxes after it.
            for k in range(at - 1, first - 1, -1):
                if _tooltip(row[k].tooltip) != _tooltip(w.tooltip) or not _tooltip(w.tooltip):
                    continue   # only a run whose boxes repeat one tooltip: the caption is the run's name
                under = [l for l in self.lines if not l.rotated and self._under(row[k], l)
                         and any(c.isalpha() for c in l.text) and len(norm(l.text)) <= MAX_LEFT_LABEL
                         and _cell_at(self.tables, centre(l.bbox)) == cell and not self._near_other_widget(row[k], l)]
                if under:
                    caption = norm(min(under, key=lambda l: l.top).text)
                    return Claim([], "nearby", f"{caption} {at - k + 1}/{last - k + 1}")
        if not beside:
            return None
        caption = norm(max(beside, key=lambda line: line.x1).text)
        return Claim([], "nearby", f"{caption} {at - first + 1}/{last - first + 1}")
