"""A figure's alt text and role, decided in one place.

The transcript's `<img alt>` (render.html) and the Ask passages (render.passages) both read
`figure_alt`, so the rule that says which alt a figure carries, and who wrote it, is written
once. A figure has one stored alt (`attrs["alt"]`); its provenance, not the string, says
whose it is. The drafting guard (analyze.enrich) never drafts over an authored alt, so the
resolver never chooses between two stored alternatives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from reflow.model import (AI_DRAFTED, Document, FigureRole, HEADING_ROLES, Node, Role, figure_role,
                          is_ai_draft)
from reflow.text_quality import fold, squash

type AltSource = Literal["reviewer", "reflow", "ai-draft", "decorative", "caption-kind", "author",
                         "fallback"]



@dataclass(frozen=True)
class Alt:
    text: str
    source: AltSource
    role: FigureRole


def same_words(a: str, b: str) -> bool:
    """Whether two texts are the same words, whatever their spacing and case."""
    return fold(a) == fold(b)


def figure_alt(node: Node, page_label: str, heading: str | None = None) -> Alt:
    """One figure's alt, from its one stored slot and its provenance, in this order:

    1. a reviewer's alt: "" when they marked it decorative, else their text, with the
       caption rule of 5;
    2. an unread figure: reflow's own note, never sent to a model;
    3. an AI draft: everything the model gave is AI, a decorative answer and the kind too;
       a distinct drafted alt shows even on a captioned figure, and the badge labels it;
    4. decorative (the tags, or the figure pass's evidence): "";
    5. captioned, and the alt is missing or only repeats the caption: the kind, else
       "Figure" (the caption itself is printed beside the picture);
    6. the PDF's own alt;
    7. else where the picture is, under the nearest printed heading, and that it has no
       description yet. Only printed text goes into it.
    """
    a = node.attrs
    role = figure_role(node)
    alt = squash(str(a.get("alt") or ""))
    caption = next((c.text or "" for c in node.children if c.role == Role.CAPTION), "")

    def kind() -> str:
        k = a.get("kind")
        return k.capitalize() if k and k != "other" else "Figure"

    repeats = bool(caption) and (not alt or same_words(alt, caption))
    provenance = a.get("provenance")
    if provenance == "reviewer":
        text = "" if a.get("decorative") else (kind() if repeats else alt)
        return Alt(text, "reviewer", role)
    if a.get("unread"):
        return Alt(alt, "reflow", role)
    if provenance == AI_DRAFTED:
        text = "" if a.get("decorative") else (kind() if repeats else alt)
        return Alt(text, "ai-draft", role)
    if a.get("decorative"):
        return Alt("", "decorative", role)
    if repeats:
        return Alt(kind(), "caption-kind", role)
    if alt:
        return Alt(alt, "author", role)
    under = f" under “{squash(heading)}”" if heading and heading.strip() else ""
    return Alt(f"Picture on page {page_label}{under}; no description yet", "fallback", role)


def figure_headings(doc: Document) -> dict[str, str]:
    """The nearest heading before each figure in reading order, for the fallback alt."""
    out: dict[str, str] = {}
    heading = ""
    for node in doc.flow():
        if node.role in HEADING_ROLES and not is_ai_draft(node):
            heading = node.text or ""
        elif node.role == Role.FIGURE and heading:
            out[node.id] = heading
    return out
