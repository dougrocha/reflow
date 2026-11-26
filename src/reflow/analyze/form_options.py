"""Option-label claims: the text of one run of checkbox markers, split across its same-row boxes (see
analyze.forms). The planner reads them first, so no single field takes an option's words."""

from __future__ import annotations

import re

from reflow.analyze.constants import OPTION_MARKER, RIGHT_GAP
from reflow.analyze.form_common import (
    MAX_LABEL_LINES,
    NUMERAL_ROW,
    ROW_TOLERANCE,
    STACK_GAP,
    Claim,
    _labels_first,
    _trim_markers,
    norm,
)
from reflow.analyze.form_labels import LabelSearch
from reflow.extract.forms import Widget
from reflow.extract.lines import Line
from reflow.geometry import centre

HINT = re.compile(r"\(.*\)")  # a hint printed under a caption: "(N.º de Medicare)"
CAPTION_REACH = 10.0  # the caption of a hint ends at most this far over it (pt)
LABEL_BEFORE_GAP = 12.0  # a label printed before its box ends at most this far left of it
OPTION_ROOM = 3.0  # an inline option label needs at least this much room (pt) beside its box


def _rows(widgets: list[Widget]) -> list[list[Widget]]:
    """The widgets as rows (tops within ROW_TOLERANCE), each left to right."""
    rows, pending = [], sorted(widgets, key=lambda w: (w.bbox[1], w.bbox[0], w.index))
    while pending:
        top = pending[0].bbox[1]
        row = [w for w in pending if w.bbox[1] <= top + ROW_TOLERANCE]
        rows.append(sorted(row, key=lambda w: (w.bbox[0], w.index)))
        pending = pending[len(row):]
    return rows


class OptionLabels(LabelSearch):
    """Claims the labels of the options in a run of checkbox markers."""

    def _option_labels(self) -> dict[int, Claim]:
        """Split one text run of checkbox markers and labels across its same-row controls."""
        labels: dict[int, Claim] = {}
        for line in self.lines:
            parts = OPTION_MARKER.split(line.text)
            if len(parts) <= 1:
                labels.update(self._numeral_labels(line))
                continue
            # "□ Yes □ No" prints each label after its marker; a scan's "Yes □ No □" before it.
            labels_first = _labels_first(parts)
            trailing = 0
            if labels_first:
                texts = parts[:-1]
            elif any(not part.strip() for part in _trim_markers(parts[1:])):
                continue
            else:
                texts = _trim_markers(parts[1:])
                trailing = len(parts) - 1 - len(texts)
            row = [w for w in self.widgets if w.kind in ("checkbox", "radio")
                   and self._row(w, line)
                   and line.bbox[0] <= centre(w.bbox)[0] <= line.bbox[2]]
            if (not texts or len(row) != len(texts) + trailing
                    or labels_first and not any(w.detected for w in row)):
                continue
            boxes = sorted(row, key=lambda w: w.bbox[0])
            # Each label is printed beside its box, on the side the run reads it: a box with no
            # room there ("Yes ■" at the end of the line, which the text layer read as "■ Yes")
            # is not the box of that text.
            ends = [line.x0] + [x for w in boxes for x in (w.bbox[0], w.bbox[2])] + [line.x1]
            sides = [ends[2 * k + 1] - ends[2 * k] if labels_first
                     else ends[2 * k + 3] - ends[2 * k + 2] for k in range(len(texts))]
            if min(sides) < OPTION_ROOM:
                continue
            first = 0 if labels_first else 1
            for k, (widget, label) in enumerate(zip(boxes, texts)):
                labels[widget.index] = Claim([line], "right", norm(label), first + k)
        return labels

    def _labels_before_boxes(self) -> dict[int, Claim]:
        """A row of check boxes that prints each label before its box ("M ■ F ■", "Spouse ■
        Child ■ Other ■"): the last box has no label on its right, and every box has words
        ending just left of it. Each box is named by the words left of it, with the line
        printed under them in the same column ("La misma" over "persona"). A row whose
        last box has a label on its right prints its labels after its boxes."""
        labels: dict[int, Claim] = {}
        for row in self._box_rows():
            if len(row) < 2:
                continue
            last = row[-1]
            if last.index in self.option_labels:
                continue  # its "■ No" starts inside the box, so the row prints labels after boxes
            if any(not l.rotated and self._row(last, l) and -1 <= l.x0 - last.bbox[2] <= RIGHT_GAP
                   and any(c.isalnum() for c in l.text) for l in self.lines):
                continue
            found: list[Claim] = []
            for k, w in enumerate(row):
                floor = row[k - 1].bbox[2] - 1 if k else float("-inf")
                cands = [l for l in self.lines if not l.rotated and self._row(w, l) and l.x0 >= floor
                         and l.x1 <= w.bbox[0] + 1 and w.bbox[0] - l.x1 <= LABEL_BEFORE_GAP
                         and any(c.isalnum() for c in l.text)]
                if not cands:
                    break
                line = max(cands, key=lambda l: l.x1)
                stack = [line]
                under = [l for l in self.lines if not l.rotated and l is not line
                         and -ROW_TOLERANCE <= l.top - line.bottom <= STACK_GAP and abs(l.x0 - line.x0) <= 3
                         and any(c.isalnum() for c in l.text) and floor <= l.x0 and l.x1 <= w.bbox[0] + 1]
                if under:
                    stack.append(min(under, key=lambda l: l.top))
                found.append(Claim(stack, "right"))
            if len(found) == len(row):
                for k, (w, claim) in enumerate(zip(row, found)):
                    labels[w.index] = claim
        return labels

    def _captioned_hints(self) -> dict[int, Claim]:
        """A row of check boxes whose labels are each a caption over a hint in parentheses, the
        hint on the box's own row ("MEDICARE" over "(N.º de Medicare)"): each box is named by the
        caption lines over its hint in its column and the hint. The text layer reads the hint
        alone as the line right of the box; the caption is the label's first words."""
        labels: dict[int, Claim] = {}
        for row in self._box_rows():
            for k, w in enumerate(row):
                right = row[k + 1].bbox[0] if k + 1 < len(row) else float("inf")
                hints = [l for l in self.lines if not l.rotated and self._row(w, l)
                         and -1 <= l.x0 - w.bbox[2] <= RIGHT_GAP and HINT.fullmatch(norm(l.text))]
                if not hints:
                    continue
                hint = min(hints, key=lambda l: l.x0)
                over = [l for l in self.lines if not l.rotated and l is not hint
                        and w.bbox[0] - 1 <= l.x0 < min(hint.x1, right) and l.x0 < right
                        and 0 <= hint.top - l.bottom <= CAPTION_REACH and any(c.isalpha() for c in l.text)]
                if not over:
                    continue
                stack = [max(over, key=lambda l: l.bottom)]
                while len(stack) < MAX_LABEL_LINES:
                    up = [l for l in self.lines if not l.rotated and l not in stack and l is not hint
                          and abs(l.x0 - stack[0].x0) <= 3 and -ROW_TOLERANCE <= stack[0].top - l.bottom <= STACK_GAP
                          and l.x0 < right and any(c.isalpha() for c in l.text)]
                    if not up:
                        break
                    stack.insert(0, max(up, key=lambda l: l.bottom))
                labels[w.index] = Claim([*stack, hint], "right")
        return labels

    def _box_rows(self) -> list[list[Widget]]:
        """The page's real check boxes and radios as rows, left to right; each box of a ruled
        form holds a row of its own."""
        boxes = [w for w in self.widgets if w.kind in ("checkbox", "radio") and w.detected is None]
        return [[w for w in whole if self.place[w.index] == place]
                for whole in _rows(boxes) for place in {self.place[w.index] for w in whole}]

    def _numeral_labels(self, line: Line) -> dict[int, Claim]:
        """A row of numbers with a check box over each ("1 2 3 ... 16", the missing-teeth chart):
        the k-th box from the left is named by the k-th number, when the row holds as many boxes
        as numbers. Each box has its own number, not the whole row."""
        text = norm(line.text)
        if line.rotated or not NUMERAL_ROW.fullmatch(text):
            return {}
        row = sorted((w for w in self.widgets if w.kind in ("checkbox", "radio") and self._row(w, line)
                      and line.x0 - ROW_TOLERANCE <= centre(w.bbox)[0] <= line.x1 + ROW_TOLERANCE),
                     key=lambda w: w.bbox[0])
        numbers = text.split()
        if len(row) != len(numbers):
            return {}
        return {w.index: Claim([line], "right", number, k) for k, (w, number) in enumerate(zip(row, numbers))}
