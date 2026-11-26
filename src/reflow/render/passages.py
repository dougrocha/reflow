"""Retrievable passages of a Document, for "Ask this document".

One owner of the answer to "what is a passage": a block the reader could be pointed at (a
paragraph, note, caption, list item, or a group of table rows), with its page, its box and the
headings it sits under. The web server reads passages.jsonl and retrieves from it; it does not
look at document.json or at the HTML.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reflow.analyze.sections import heading_of
from reflow.model import BBox, Document, HEADING_ROLES, Node, Role, is_model_text
from reflow.names import PASSAGES_JSONL
from reflow.render.alt import figure_alt
from reflow.render.annotations import annotated_text
from reflow.render.tables import header_rows
from reflow.text_quality import squash

PASSAGES_SCHEMA = 1
TABLE_GROUP_CHARS = 900
TRAIL_CHARS = 160
LEAD_CHARS = 200

type PassageKind = Literal["p", "li", "cap", "tbl", "fig", "h"]


@dataclass(frozen=True)
class Passage:
    id: str
    """Node id (the transcript's data-node); a table row group adds "#r<k>"."""
    page: int
    bbox: BBox
    kind: PassageKind
    text: str
    """The full text, the only quotable field."""
    trail: str
    """Enclosing headings joined by " > ", innermost last, at most TRAIL_CHARS."""
    lead: str
    """Lead-in sentence or table caption, at most LEAD_CHARS, "" if none."""
    doubt: tuple[tuple[int, int], ...] = ()
    """[start, end) ranges of `text` that OCR is unsure of (doubt marks)."""
    fixed: tuple[tuple[int, int, str], ...] = ()
    """[start, end, was) of each word in `text` a reviewer corrected, with what OCR read."""

    def to_line(self) -> str:
        """One JSON line, keys id n b k t h c in that order, then u when there are doubtful
        words, then f when a reviewer corrected words; ensure_ascii=False, no spaces."""
        line: dict[str, object] = {"id": self.id, "n": self.page, "b": list(self.bbox),
                                   "k": self.kind, "t": self.text, "h": self.trail, "c": self.lead}
        if self.doubt:
            line["u"] = [list(r) for r in self.doubt]
        if self.fixed:
            line["f"] = [list(r) for r in self.fixed]
        return json.dumps(line, ensure_ascii=False, separators=(",", ":"))


_TEXT_KINDS: dict[Role, PassageKind] = {
    Role.P: "p", Role.NOTE: "p", Role.CAPTION: "cap", Role.LI: "li",
}


def passages(doc: Document) -> Iterator[Passage]:
    """Every passage in reading order (doc.flow()): P/NOTE/CAPTION/LI (nested lists included),
    TABLE row groups (the TABLE's bbox and id), FIGURE alt text that is not AI-drafted, and
    headings (kind "h", whose trail is the headings above it), with the heading trail from
    enclosing SECTION nodes. Skips artifacts, form controls, nodes without a page or bbox, and
    text with no letter or digit. Pure and deterministic."""
    yield from _walk(doc.root.children, [], "")


def _walk(nodes: list[Node], headings: list[str], lead: str) -> Iterator[Passage]:
    """`nodes` are siblings; `lead` is the lead-in of the list they belong to, if any."""
    trail = _trail(headings)
    for i, node in enumerate(nodes):
        role = node.role
        if role == Role.SECTION:
            # The first child is the section's heading: a passage under the headings above it,
            # then it joins the trail of everything the section holds.
            heading, rest = heading_of(node), node.children[1:]
            title = squash(heading.text or "") if heading is not None else ""
            if heading is not None and heading.role in HEADING_ROLES:
                yield from _text_passage(heading, "h", trail, "")
            yield from _walk(rest, [*headings, title] if title else headings, "")
        elif role in HEADING_ROLES:
            yield from _text_passage(node, "h", trail, "")
        elif role in _TEXT_KINDS:
            yield from _text_passage(node, _TEXT_KINDS[role], trail, lead)
            if role == Role.LI:
                yield from _walk([c for c in node.children if c.role == Role.LIST],
                                 headings, lead)
        elif role == Role.LIST:
            yield from _walk(node.children, headings, _lead_in(nodes, i) or lead)
        elif role == Role.BOX:
            yield from _walk(node.children, headings, lead)
        elif role == Role.TABLE:
            yield from _table_passages(node, trail, _table_lead(node, nodes, i))
        elif role == Role.FIGURE:
            yield from _figure_passage(node, trail)


def _text_passage(node: Node, kind: PassageKind, trail: str, lead: str) -> Iterator[Passage]:
    """A model's transcription (assist.py) is not the document's own words, so it is not quoted."""
    if is_model_text(node):
        return
    full = annotated_text(node)
    text = full.strip()
    if node.page is not None and node.bbox is not None and _has_content(text):
        cut = len(full) - len(full.lstrip())
        doubt = tuple((m.start - cut, m.end - cut) for m in node.marks
                      if m.kind == "doubt" and m.start >= cut and m.end - cut <= len(text))
        fixed = tuple((m.start - cut, m.end - cut, m.alt) for m in node.marks
                      if m.kind == "fix" and m.start >= cut and m.end - cut <= len(text))
        yield Passage(node.id, node.page, node.bbox, kind, text, trail,
                      lead if kind == "li" else "", doubt, fixed)


def _figure_passage(node: Node, trail: str) -> Iterator[Passage]:
    """The figure's alt text when the PDF's author wrote it (render.alt decides whose an alt
    is): a reviewer's, reflow's own note, a model's draft, the kind named for a caption and the
    no-description fallback are never quoted."""
    if node.page is None or node.bbox is None:
        return
    resolved = figure_alt(node, str(node.page))
    if resolved.source != "author" or not _has_content(resolved.text):
        return
    caption = next((c for c in node.children if c.role == Role.CAPTION), None)
    caption_text = squash(caption.text or "") if caption is not None else ""
    yield Passage(node.id, node.page, node.bbox, "fig", resolved.text, trail,
                  _clip_end(caption_text, LEAD_CHARS))


def _table_passages(table: Node, trail: str, lead: str) -> Iterator[Passage]:
    """Row groups carry the TABLE's own box and id: rows and cells have no boxes."""
    if table.page is None or table.bbox is None:
        return
    for k, text in table_groups(table):
        if _has_content(text):
            yield Passage(f"{table.id}#r{k}", table.page, table.bbox, "tbl", text, trail, lead)


def table_groups(table: Node) -> Iterator[tuple[int, str]]:
    """(k, text) per row group of a TABLE node. Leading TH-only rows (render.tables.header_rows)
    repeat at the top of every group. Cells joined by " | ", rows by "\\n", up to
    TABLE_GROUP_CHARS per group; a single longer row is its own group, never cut."""
    rows = [c for c in table.children if c.role == Role.TR]
    n_header = len(header_rows(rows))
    lines = [_row_text(tr) for tr in rows]
    header = [line for line in lines[:n_header] if line]
    body = [line for line in lines[n_header:] if line]
    if not body:
        if header:
            yield 0, "\n".join(header)
        return
    base = sum(len(line) + 1 for line in header)
    k, group, size = 0, [], 0
    for line in body:
        if group and base + size + len(line) + 1 > TABLE_GROUP_CHARS:
            yield k, "\n".join([*header, *group])
            k, group, size = k + 1, [], 0
        group.append(line)
        size += len(line) + 1
    yield k, "\n".join([*header, *group])


def _row_text(tr: Node) -> str:
    """A row's cells joined by " | ", each with its annotations (render.annotations): a
    calendar day reads "02 — Closed / Holiday — Labor Day"."""
    cells = [squash(annotated_text(c)) for c in tr.children]
    return " | ".join(cells) if any(cells) else ""


def _lead_in(siblings: list[Node], i: int) -> str:
    """The paragraph just before a list or table, when it ends in a colon."""
    before = siblings[i - 1] if i else None
    if before is not None and before.role == Role.P:
        text = squash(before.text or "")
        if text.endswith(":"):
            return _clip_start(text, LEAD_CHARS)
    return ""


def _table_lead(table: Node, siblings: list[Node], i: int) -> str:
    """The table's caption (its own, else a caption just before it), else a lead-in paragraph."""
    caption = next((c for c in table.children if c.role == Role.CAPTION), None)
    before = siblings[i - 1] if i else None
    if caption is None and before is not None and before.role == Role.CAPTION:
        caption = before
    text = squash(caption.text or "") if caption is not None else ""
    return _clip_end(text, LEAD_CHARS) if text else _lead_in(siblings, i)


def _clip_end(text: str, limit: int) -> str:
    """The first `limit` characters: a caption starts with its label."""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _trail(headings: list[str]) -> str:
    """The headings joined by " > ", the outermost dropped until it fits TRAIL_CHARS."""
    shown = list(headings)
    while len(shown) > 1 and len(" > ".join(shown)) > TRAIL_CHARS:
        shown.pop(0)
    text = " > ".join(shown)
    return text if len(text) <= TRAIL_CHARS else text[:TRAIL_CHARS - 1].rstrip() + "…"


def _clip_start(text: str, limit: int) -> str:
    """The last `limit` characters, from a word start: a lead-in ends in its colon."""
    if len(text) <= limit:
        return text
    tail = text[-(limit - 1):]
    return "…" + (tail.split(" ", 1)[1] if " " in tail else tail)


def _has_content(text: str) -> bool:
    return any(c.isalnum() for c in text)


def _number(v: float) -> float | int:
    return int(v) if float(v).is_integer() else v


def write_passages(doc: Document, out_dir: Path, version: str) -> int:
    """Write out_dir/PASSAGES_JSONL: the header {"schema", "v": version, "pages": [[w, h], ...]},
    then one line per passage. `version` is the manifest's "v". Returns the passage count."""
    header = {"schema": PASSAGES_SCHEMA, "v": version,
              "pages": [[_number(p.width), _number(p.height)] for p in doc.pages]}
    count = 0
    with (out_dir / PASSAGES_JSONL).open("w", encoding="utf-8", errors="replace",
                                         newline="\n") as out:
        out.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")
        for passage in passages(doc):
            out.write(passage.to_line() + "\n")
            count += 1
    return count
