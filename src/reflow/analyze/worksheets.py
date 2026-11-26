"""Worksheet rows: a label, then a column of answer values at a shared right edge."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from statistics import median

from reflow.analyze.constants import ALIGN_TOL, WORKSHEET_SUB_START
from reflow.extract.lines import Line


# A worksheet's line numbers: bare numbers down the left margin, each starting a line item whose
# text (and amount, on the right) sits on the same rows, up to the next number. The numbers are
# set apart from their text, so they are lines of their own; a run of consecutive numbers with
# text beside them is a worksheet, not a page reference or a year.
WORKSHEET_LABEL = re.compile(r"[0-9]{1,2}")
WORKSHEET_MIN_RUN = 3
WORKSHEET_EDGE_TOL = 1.5  # the numbers' right edges agree (a 10 and a 9 are both right-aligned)
WORKSHEET_ROW_TOL = 0.5  # × font size: lines this close in height share a row


def _on_row(label: Line, line: Line) -> bool:
    return (abs(line.top - label.top) <= WORKSHEET_ROW_TOL * label.size
            and bool(line.text.strip()) and not line.rotated
            and not WORKSHEET_LABEL.fullmatch(line.text.strip()))


def _hangs(label: Line, lines: list[Line]) -> bool:
    """Whether a worksheet number starts its row: text sits just right of it (within two font
    sizes, the hanging gap), and nothing is to its left. A table's amount in a middle column,
    with cells on its left, is not one."""
    row = [l for l in lines if l is not label and _on_row(label, l)]
    return (any(0 < l.x0 - label.x1 <= 2 * label.size for l in row)
            and not any(l.x1 <= label.x0 for l in row))


def _label_runs(lines: list[Line]) -> list[list[Line]]:
    """Runs of consecutive worksheet line numbers, top to bottom: each run's numbers go 1, 2,
    3… with one right edge, and most of them have text beside them."""
    labels = sorted((l for l in lines
                     if not l.rotated and WORKSHEET_LABEL.fullmatch(l.text.strip())),
                    key=lambda l: l.top)
    runs: list[list[Line]] = []
    for label in labels:
        value = int(label.text.strip())
        for run in runs:
            last = run[-1]
            if (int(last.text.strip()) + 1 == value
                    and abs(last.x1 - label.x1) <= WORKSHEET_EDGE_TOL and label.top > last.top):
                run.append(label)
                break
        else:
            runs.append([label])
    out = []
    for run in runs:
        if len(run) < WORKSHEET_MIN_RUN:
            continue
        hanging = sum(_hangs(label, lines) for label in run)
        if 3 * hanging >= 2 * len(run):
            out.append(run)
    return out


def _row_order(lines: list[Line]) -> list[Line]:
    """Lines in reading order by row: lines on one row (their tops within a font size) read
    left to right, so a row's amount ("1a $") comes after its text."""
    out: list[Line] = []
    row: list[Line] = []
    for line in sorted(lines, key=lambda l: (l.top, l.x0)):
        if row and line.top - row[0].top > WORKSHEET_ROW_TOL * row[0].size:
            out += sorted(row, key=lambda l: l.x0)
            row = []
        row.append(line)
    return out + sorted(row, key=lambda l: l.x0)


WORKSHEET_SUB_LETTER = re.compile(r"([a-z]) \S")  # "a Enter your total income": a sub-line


def _lettered_sub_lines(band: list[Line]) -> list[tuple[Line, str]]:
    """The lettered sub-lines of a worksheet item, in order from "a": each at the left edge of
    the item's text, two or more of them. A lone wrapped line that starts "a person" is not one."""
    texts = [l for l in band if l.text.strip()[:1] not in WORKSHEET_SUB_START]
    if not texts:
        return []
    edge = min(l.x0 for l in texts)
    out: list[tuple[Line, str]] = []
    want = "a"
    for line in sorted(texts, key=lambda l: l.top):
        m = WORKSHEET_SUB_LETTER.match(line.text.strip())
        if m and m[1] == want and abs(line.x0 - edge) <= ALIGN_TOL * line.size:
            out.append((line, want))
            want = chr(ord(want) + 1)
    return out if len(out) >= 2 else []


WORKSHEET_LETTER_ROW = re.compile(r"([A-Z])\. \S")  # "B. Multiply line A by 50%": a lettered row
WORKSHEET_STUB = re.compile(r"([A-Z])\.(?:\s+(\S.*))?")  # "B." or "A. $1,500": the answer box
WORKSHEET_VALUE = re.compile(r"[$(]?[0-9][0-9,.]*\)?%?|[-–]0[-–]")  # "750", "$18,450", "-0-"
WORKSHEET_STUB_GAP = 5.0  # × font size: the answer box sits this far right of the row's left edge


def _lettered_rows(lines: list[Line]) -> list[tuple[list[Line], list[Line]]]:
    """The rows of lettered worksheets whose lines end in an answer box that repeats the row's
    letter ("A. Enter the total amount ..." then, at the right edge, "A. $1,500", or "B." beside
    a "750"). Each row is (the lines it reads, label line first, with the box's value added to
    the line it sits beside; the lines it took from the page, box and value included). A row's
    lines run down to the next letter, a "Note:" line or a gap. A run of lettered lines with
    no answer boxes (an outline: "A. Introduction") is not a worksheet."""
    cands = sorted((l for l in lines if not l.rotated and WORKSHEET_LETTER_ROW.match(l.text.strip())),
                   key=lambda l: l.top)
    runs: list[list[Line]] = []
    for line in cands:
        for run in runs:
            last = run[-1]
            if (ord(last.text.strip()[0]) + 1 == ord(line.text.strip()[0])
                    and abs(last.x0 - line.x0) <= ALIGN_TOL * line.size and line.top > last.top):
                run.append(line)
                break
        else:
            if line.text.strip()[0] == "A":
                runs.append([line])
    out: list[tuple[list[Line], list[Line]]] = []
    for run in runs:
        if len(run) < WORKSHEET_MIN_RUN:
            continue
        ends = [r.top - WORKSHEET_ROW_TOL * r.size for r in run[1:]] + [math.inf]
        # Each row's answer box, and the value that follows it, nearest first.
        boxes: list[tuple[Line, list[Line]] | None] = []
        for label, end in zip(run, ends):
            letter = label.text.strip()[0]
            stubs = sorted((l for l in lines if not l.rotated and l is not label
                            and label.top - WORKSHEET_ROW_TOL * label.size <= l.top < end
                            and l.x0 > label.x0 + WORKSHEET_STUB_GAP * label.size
                            and (m := WORKSHEET_STUB.fullmatch(l.text.strip())) and m[1] == letter
                            and (not m[2] or WORKSHEET_VALUE.fullmatch(m[2]))),
                           key=lambda l: l.x0)
            if not stubs:
                boxes.append(None)
                continue
            stub, values = stubs[0], []
            reach = stub
            while not WORKSHEET_STUB.fullmatch(stub.text.strip())[2]:
                nxt = [l for l in lines if l is not reach and not l.rotated
                       and abs(l.top - stub.top) <= WORKSHEET_ROW_TOL * stub.size
                       and 0 <= l.x0 - reach.x1 <= 4 * stub.size
                       and WORKSHEET_VALUE.fullmatch(l.text.strip())]
                if not nxt:
                    break
                reach = min(nxt, key=lambda l: l.x0)
                values.append(reach)
            boxes.append((stub, values))
        if 3 * sum(b is not None for b in boxes) < 2 * len(run):
            continue
        out_rows = []
        right = max(max(l.x1 for l in [b[0], *b[1]]) for b in boxes if b is not None)
        for label, end, box in zip(run, ends, boxes):
            band: list[Line] = []
            prev = label
            for l in sorted((l for l in lines if l is not label and l.text.strip() and not l.rotated
                             and label.top - WORKSHEET_ROW_TOL * label.size <= l.top < end
                             and label.x0 - ALIGN_TOL * label.size <= l.x0 and l.x1 <= right + 1
                             and l.size >= 0.75 * label.size), key=lambda l: (l.top, l.x0)):
                if l.text.strip().startswith("Note") or l.top - prev.bottom > 1.5 * label.size:
                    break
                band.append(l)
                prev = l if l.top > prev.top else prev
            rest, text = [label, *band], ""
            if box is not None:
                stub, vals = box
                m = WORKSHEET_STUB.fullmatch(stub.text.strip())
                text = m[2] or " ".join(l.text.strip() for l in vals)
                drop = [stub, *vals]
                rest = [l for l in rest if all(l is not d for d in drop)]
                if text:  # the value joins the line it sits beside, or the row's last line
                    beside = [l for l in rest if abs(l.top - stub.top) <= WORKSHEET_ROW_TOL * stub.size
                              and l.x1 <= stub.x0 + 1]
                    at = max(beside, key=lambda l: l.x1) if beside else rest[-1]
                    frame = (at.x0, min(at.top, stub.top), max(at.x1, *(d.x1 for d in drop)),
                             max(at.bottom, stub.bottom))
                    joined = replace(at.retext(f"{at.text.rstrip()} {text}"), bbox=frame)
                    rest = [joined if l is at else l for l in rest]
                band += [d for d in drop if all(d is not b for b in band)]
            out_rows.append((rest, [label, *band]))
        out += out_rows
    return out


def worksheet_rows(lines: list[Line]) -> tuple[list[Line], dict[int, str], set[int]]:
    """Gather each worksheet line item in one piece: its number, then the lines below it down to
    the next number, in the rows of the item's band. Returns the lines in that order, the number
    text of each line number by id(line), and the ids of the lines that belong to a band. A line
    in the band that is left of the text, smaller, or outside the band stays where it was."""
    runs = _label_runs(lines)
    lettered = _lettered_rows(lines)
    if not runs and not lettered:
        return lines, {}, set()
    labels: dict[int, str] = {}
    members: set[int] = set()
    chunks: dict[int, list[Line]] = {}
    claimed: set[int] = set()
    for rest, taken in lettered:  # a lettered row is whole, at its label's place on the page
        labels[id(rest[0])] = ""
        members |= {id(l) for l in rest[1:]}
        chunks[id(taken[0])] = [rest[0], *sorted(rest[1:], key=lambda l: (l.top, l.x0))]
        claimed |= {id(l) for l in taken}
    numbers = {id(label) for run in runs for label in run}
    for run in runs:
        starts = [label.top - WORKSHEET_ROW_TOL * label.size for label in run]
        pitch = median(b.top - a.top for a, b in zip(run, run[1:]))
        ends = starts[1:] + [starts[-1] + pitch]
        bands: list[list[Line]] = [[] for _ in run]
        for line in lines:
            if id(line) in claimed or id(line) in numbers or line.rotated or not line.text.strip():
                continue
            # A bracket is tall: it belongs to the number its middle sits beside, not its top.
            at = ((line.top + line.bottom) / 2 if line.text.strip()[:1] in WORKSHEET_SUB_START
                  else line.top)
            for i, label in enumerate(run):
                if starts[i] <= at < ends[i]:
                    if line.x0 > label.x1 and line.size >= 0.9 * label.size:
                        bands[i].append(line)
                    break
        chunk: list[Line] = []
        for i, label in enumerate(run):
            labels[id(label)] = label.text.strip()
            for line, letter in _lettered_sub_lines(bands[i]):
                labels[id(line)] = letter  # a sub-item: its letter is its marker
            members |= {id(l) for l in bands[i]}
            # The item's own rows first; a bullet or bracket under it comes after, as its sub-list.
            text = [l for l in bands[i] if l.text.strip()[:1] not in WORKSHEET_SUB_START]
            subs = sorted((l for l in bands[i] if l.text.strip()[:1] in WORKSHEET_SUB_START),
                          key=lambda l: (l.top, l.x0))
            chunk += _row_order([label] + text) + subs
            claimed |= {id(label)} | {id(l) for l in bands[i]}
        chunks[id(run[0])] = chunk
    out: list[Line] = []
    for line in lines:
        if id(line) in chunks:
            out += chunks[id(line)]
        elif id(line) not in claimed:
            out.append(line)
    return out, labels, members
