"""Words a node carries beside its own text, for every reader: the transcript, Ask's passages
and the review queue all get them from `annotations`, so none of them shows a word the others
do not.

A calendar day's cell holds the printed number ("02"); `attrs["day"]` adds what the page says
about the day elsewhere (analyze.calendar.annotate): the colour key's labels for its colours and
marks ("status") and the dated list's text for it ("notes"), each with the boxes its words are
printed in. An icon legend's meaning (`attrs["status"]`, analyze.figures.find_legend) is printed
by the legend, not beside the line, so it has no source box.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from reflow.model import BBox, Node

SEPARATOR = " — "  # between a node's text and each annotation; it adds no words


class Annotation(NamedTuple):
    kind: Literal["status", "note"]
    text: str
    sources: tuple[BBox, ...]   # where the words are printed; () for the icon legend's status
    origin: Literal["key", "list", "legend"]


def annotations(node: Node) -> list[Annotation]:
    """The day's statuses, then its notes, then the icon legend's status."""
    day = node.attrs.get("day") or {}
    found = [Annotation("status", s["label"], tuple(tuple(b) for b in s["label_boxes"]), "key")
             for s in day.get("status", [])]
    found += [Annotation("note", n["text"], (tuple(n["box"]),), "list")
              for n in day.get("notes", [])]
    if legend := node.attrs.get("status"):
        found.append(Annotation("status", legend, (), "legend"))
    return found


def annotated_text(node: Node) -> str:
    """The node's text, then each annotation's text after SEPARATOR: "02 — Closed / Holiday —
    Labor Day"."""
    return "".join([node.text or "", *(SEPARATOR + a.text for a in annotations(node))])
