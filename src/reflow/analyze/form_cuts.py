"""Line cutting for forms: a printed line that the text layer ran across several fields, options or
a blank is cut where the controls stand, before the planner binds labels (see analyze.forms)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from reflow.analyze.constants import ABOVE_GAP, OPTION_MARKER, RIGHT_GAP
from reflow.analyze.form_common import _labels_first, _tooltip, _trim_markers, norm
from reflow.extract.forms import Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.geometry import centre

BLANK_TAIL = re.compile(r"\s*_{3,}\s*$")  # the blank line a field is printed over ("DATE: ______")
MIN_QUESTION_WORDS = 3
LEADER_TAIL = re.compile(r"(?:\s*\.){3,}\s*$|(?:\s+\.){2,}\s*$")  # dots that lead the eye from a question to its boxes
CAPTION_SLACK = 6.0  # points: a caption word starts this near its own field's left edge


def split_captions(page: PageContent, lines: Sequence[Line]) -> dict[int, list[Line]]:
    """Captions that the text layer joined across neighbouring cells, cut at the fields under
    them: "STATE ZIP CODE" over a narrow STATE box and a ZIP CODE box is two captions. A line is
    cut only when two or more fields sit under it, side by side, and the start of one of its
    words lies at each field's left edge (word positions are estimated from the character
    count). Returns {id(line): parts}."""
    fields = [w for w in page.widgets if w.kind in ("text", "combo", "list") and not w.comb]
    cuts: dict[int, list[Line]] = {}
    for line in lines:
        words = list(re.finditer(r"\S+", line.text))
        if line.rotated or len(words) < 2 or line.x1 <= line.x0:
            continue
        under = sorted((w for w in fields if -(line.bottom - line.top) <= w.bbox[1] - line.bottom <= ABOVE_GAP
                        and line.x0 - CAPTION_SLACK <= w.bbox[0] < line.x1), key=lambda w: w.bbox[0])
        if len(under) < 2 or any(b.bbox[0] - a.bbox[2] > 3 * line.size or b.bbox[0] < a.bbox[0] + 1
                                 for a, b in zip(under, under[1:])):
            continue
        whole = norm(line.text).lower()
        if any(whole in norm(_tooltip(w.tooltip)).lower() for w in under):
            continue  # a field named by the whole caption ("Daytime Telephone" area code and number)
        per_char = (line.x1 - line.x0) / max(len(line.text), 1)
        starts = [0]
        for w in under[1:]:
            near = [(abs(line.x0 + m.start() * per_char - w.bbox[0]), m.start()) for m in words[1:]
                    if m.start() > starts[-1]]
            best = min(near, default=None)
            if best is None or best[0] > CAPTION_SLACK:
                starts = []
                break
            starts.append(best[1])
        if not starts:
            continue
        ends = starts[1:] + [len(line.text)]
        parts = [replace(line.cut(a, b, strip=True),
                         bbox=(line.x0 + a * per_char, line.top, line.x0 + b * per_char, line.bottom))
                 for a, b in zip(starts, ends)]
        # The author's own names for the fields must agree with the cut ("STATE", "ZIP CODE").
        if all(not (name := norm(_tooltip(w.tooltip)).lower())
               or norm(part.text).lower() in name or name in norm(part.text).lower()
               for part, w in zip(parts, under)):
            cuts[id(line)] = parts
    return cuts


def apply_cuts(lines: Sequence[Line], cuts: dict[int, list[Line]]) -> list[Line]:
    return [part for l in lines for part in cuts.get(id(l), [l])]


def _label_apart(box: Widget, run: Line, lines: Sequence[Line]) -> bool:
    """Whether `box`, the last box of `run`, has a label of its own printed just right of it
    ("■ Yes ■", then "No"): words on its row, starting within RIGHT_GAP, that are not a run of
    labels printed before boxes of their own ("No ■")."""
    return any(l is not run and not l.rotated and -1 <= l.x0 - box.bbox[2] <= RIGHT_GAP
               and box.bbox[1] - 3 <= (l.top + l.bottom) / 2 <= box.bbox[3] + 3
               and any(c.isalnum() for c in l.text) and not _labels_first(OPTION_MARKER.split(l.text))
               for l in lines)


def split_option_rows(page: PageContent, lines: Sequence[Line]) -> dict[int, list[Line]]:
    """A question and its boxes printed as one run ("1. Have you been ...? . . . . ■ Yes ■ No"):
    cut at the first box into the question, which stays in the text, and the options, which
    label the boxes (see _option_labels). A line is cut only when the boxes under its markers
    are all on its row. A run that ends at a box is cut only when its head reads as a question
    (ends in "?" or ":") and that box's label is printed apart, just right of it: otherwise
    the run prints each label before its box ("Yes ■ No ■", "Smoker? Yes ■ No ■"), and a cut
    would give each box the next one's label. A scan's labels-first run is never cut.
    Returns {id(line): [question, options]}."""
    boxes = [w for w in page.widgets if w.kind in ("checkbox", "radio")]
    cuts: dict[int, list[Line]] = {}
    for line in lines:
        parts = OPTION_MARKER.split(line.text)
        question = LEADER_TAIL.sub("", parts[0]).strip()
        texts = _trim_markers(parts[1:])
        if (line.rotated or len(parts) < 2 or sum(c.isalnum() for c in question) < 2
                or any(not part.strip() for part in texts)):
            continue
        row = [w for w in boxes if line.top - 3 <= (w.bbox[1] + w.bbox[3]) / 2 <= line.bottom + 3
               and line.x0 <= centre(w.bbox)[0] <= line.x1]
        if len(row) != len(parts) - 1 or _labels_first(parts) and (
                any(w.detected for w in row) or not question.endswith(("?", ":"))
                or not _label_apart(max(row, key=lambda w: w.bbox[2]), line, lines)):
            continue
        edge = min(w.bbox[0] for w in row) - 2
        if not line.x0 < edge < line.x1:
            continue
        at = line.text.find(question)
        head = line.cut(at, at + len(question)) if at >= 0 else line.retext(question)
        cuts[id(line)] = [replace(head, bbox=(line.x0, line.top, edge, line.bottom)),
                          replace(line.cut(len(parts[0]), len(line.text), strip=True),
                                  bbox=(edge, line.top, line.x1, line.bottom))]
    return cuts


def split_blank_lines(page: PageContent, lines: Sequence[Line]) -> dict[int, list[Line]]:
    """A caption printed with the blank line that its text box sits on ("*DATE OF NOTICE: ______"):
    cut into the caption, which labels the box, and the underscores, which the box replaces and
    which are left out. A line is cut only when it ends in a run of underscores, has words before
    it, and exactly one text box on its row lies over that run. The underscores' position is
    estimated from the character count, as in split_captions; the caption ends no later than the
    box begins. An option's label is never cut: a line right of a check box or radio, or one that
    starts with a box glyph ("□ Other (please add) ______"). Returns {id(line): [caption]}."""
    fields = [w for w in page.widgets if w.kind in ("text", "combo") and not w.comb]
    boxes = [w for w in page.widgets if w.kind in ("checkbox", "radio")]
    cuts: dict[int, list[Line]] = {}
    for line in lines:
        blank = BLANK_TAIL.search(line.text)
        if (blank is None or line.rotated or line.x1 <= line.x0
                or sum(c.isalnum() for c in line.text[:blank.start()]) < 2
                or OPTION_MARKER.match(line.text.lstrip())
                or any(-1 <= line.x0 - b.bbox[2] <= RIGHT_GAP
                       and b.bbox[1] - 3 <= (line.top + line.bottom) / 2 <= b.bbox[3] + 3
                       for b in boxes)):
            continue
        per_char = (line.x1 - line.x0) / max(len(line.text), 1)
        start = line.x0 + blank.start() * per_char
        over = [w for w in fields if line.top - 3 <= (w.bbox[1] + w.bbox[3]) / 2 <= line.bottom + 3
                and start - 2 * per_char <= centre(w.bbox)[0] <= line.x1 + 2]
        if len(over) != 1:
            continue
        edge = min(start, over[0].bbox[0])
        cuts[id(line)] = [replace(line.cut(0, blank.start(), strip=True),
                                  bbox=(line.x0, line.top, edge, line.bottom))]
    return cuts


def split_option_questions(page: PageContent, lines: Sequence[Line]) -> dict[int, list[Line]]:
    """A box's label printed with the question that all the options answer ("No Is this property
    ...?", after a "Yes" and a "No" box): cut into the label, which is the box's own word, and the
    question, which becomes the legend of the group (see _Planner.legend). A line is cut only when
    it starts with the tooltip of the box just left of it and the rest is a question: a sentence
    of at least MIN_QUESTION_WORDS words that starts with a capital and ends in "?". The cut is
    estimated from the character count. Returns {id(line): [label, question]}."""
    boxes = [w for w in page.widgets if w.kind in ("checkbox", "radio") and norm(w.tooltip)]
    cuts: dict[int, list[Line]] = {}
    for line in lines:
        if line.rotated or line.x1 <= line.x0:
            continue
        for w in boxes:
            if not (-1 <= line.x0 - w.bbox[2] <= RIGHT_GAP
                    and w.bbox[1] - 3 <= (line.top + line.bottom) / 2 <= w.bbox[3] + 3):
                continue
            words = norm(w.tooltip).split()
            head = re.match(r"\s*" + r"\s+".join(map(re.escape, words)) + r"\s+(?=\S)", line.text,
                            re.I)
            question = line.text[head.end():] if head else ""
            if (head and question.rstrip().endswith("?") and question[:1].isupper()
                    and len(question.split()) >= MIN_QUESTION_WORDS):
                per_char = (line.x1 - line.x0) / len(line.text)
                edge = line.x0 + head.end() * per_char
                cuts[id(line)] = [
                    replace(line.cut(0, head.end(), strip=True), bbox=(line.x0, line.top, edge, line.bottom)),
                    replace(line.cut(head.end(), len(line.text), strip=True),
                            bbox=(edge, line.top, line.x1, line.bottom))]
                break
    return cuts
