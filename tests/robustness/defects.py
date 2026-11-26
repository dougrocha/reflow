"""The defects of the injection harness: DEFECTS is the only list.

The damage kinds (kinds.py) break the input and score what reflow publishes from it. A defect
breaks the output instead: it starts from a correct conversion's model, changes one thing a
reader would get wrong, and asks which page checks and which review items notice. Equalify Iris
calibrates its verifier the same way.

A defect is `Document -> Document`, in place. Each one has

  id, blurb   a stable name, and one line on what it changes
  mutate      (doc, rng, pages) -> Damage: changes one target on one of `pages` (the pages the
              document's specs test), and says which page and region it changed. It raises
              Unsupported when those pages have nothing it can change, never a no-op
  codes       the review codes that describe this defect. A review item that was already on the
              page before the defect counts as a catch only when it has one of them and covers
              the changed region; a new item over the region always counts
  enforced    the documents on which a miss fails pytest: the cells caught today, so a check
              that stops catching shows. Every other miss is a scorecard entry

Text is edited only through Node.set_text, which keeps inline spans and marks on their words. A
mutation must round-trip through Model.read (the harness checks it), so the renderer and the
checks read the same model the defect wrote.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from robustness.kinds import Box, Damage, Unsupported

from reflow.analyze.sections import heading_of, nest_sections
from reflow.model import HEADING_ROLES, SUBHEADING_ROLES, Document, Node, Role

Mutate = Callable[[Document, random.Random, frozenset[int]], Damage]


@dataclass(frozen=True)
class Defect:
    id: str
    blurb: str
    mutate: Mutate
    codes: frozenset[str] = frozenset()
    enforced: frozenset[str] = frozenset()

    def enforced_on(self, doc: str) -> bool:
        return "*" in self.enforced or doc in self.enforced


# ---------------------------------------------------------------------------- the tree

def _with_parents(doc: Document) -> Iterator[tuple[Node, Node]]:
    """(node, parent) for every node under the root, in document order."""
    stack = [(c, doc.root) for c in reversed(doc.root.children)]
    while stack:
        node, parent = stack.pop()
        yield node, parent
        stack.extend((c, node) for c in reversed(node.children))


def _words(node: Node) -> int:
    return len((node.text or "").split())


def _pick(rng: random.Random, found: list, what: str, pages: frozenset[int]):
    if not found:
        raise Unsupported(f"the tested pages {sorted(pages)} have no {what}")
    return found[rng.randrange(len(found))]


def _union(*boxes) -> Box | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _box_of(node: Node) -> Box | None:
    """The node's box, or the union of its descendants' when it has none (a table row)."""
    if node.bbox:
        return tuple(node.bbox)
    return _union(*(tuple(n.bbox) for n in node.walk() if n.bbox))


def _damage(page: int, box: Box | None, note: str) -> Damage:
    return Damage(frozenset({page}), {page: box} if box else {}, note)


def _show(text: str | None, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return repr(text if len(text) <= limit else text[:limit - 1] + "…")


# ---------------------------------------------------------------------------- the defects

def _body_rows(table: Node) -> list[Node]:
    """The rows of `table` a defect may drop whole: below its header band (attrs
    "header_rows"), with no cell that spans rows, inside no span from a row above, and with no
    header cell another cell may name (an "id"). Dropping one leaves every other row as it was."""
    rows = [r for r in table.children if r.role is Role.TR]
    covered: set[int] = set()
    for i, row in enumerate(rows):
        for cell in row.children:
            covered.update(range(i + 1, i + int(cell.attrs.get("rowspan", 1))))
    return [row for i, row in enumerate(rows)
            if i >= max(1, int(table.attrs.get("header_rows", 1))) and i not in covered
            and all(int(c.attrs.get("rowspan", 1)) == 1 and "id" not in c.attrs
                    for c in row.children)]


def _drop_row(doc: Document, rng: random.Random, pages: frozenset[int]) -> Damage:
    """A body row with text in two cells or more goes, from a table with two such rows or
    more (`_body_rows`); the table's row count follows."""
    found = []
    for table, _ in _with_parents(doc):
        if table.role is not Role.TABLE:
            continue
        body = [r for r in _body_rows(table)
                if sum(bool((c.text or "").strip()) for c in r.children) >= 2]
        if len(body) >= 2:
            found += [(table, r) for r in body if r.page in pages]
    table, row = _pick(rng, found, "table with two body rows of text", pages)
    text = " | ".join((c.text or "").strip() for c in row.children)
    table.children.remove(row)
    if "rows" in table.attrs:
        table.attrs["rows"] = int(table.attrs["rows"]) - 1
    return _damage(row.page, _box_of(row), f"dropped row {text[:60]!r} of table {table.id}")


def _sections(doc: Document) -> dict[int, Node]:
    """Each SECTION node, by its heading node's identity."""
    return {id(heading_of(n)): n for n, _ in _with_parents(doc) if n.role is Role.SECTION}


def _renest(doc: Document, old: dict[int, Node]) -> None:
    """Nest the reading order under its headings again (analyze.sections), after a defect
    changed a heading. A section whose heading is still a heading keeps its node's id, attrs and
    facts (`old`, from _sections before the change), so only the sections the defect changed
    differ."""
    doc.root.children = nest_sections(list(doc.flow()))
    for node, _ in _with_parents(doc):
        if node.role is Role.SECTION and (was := old.get(id(heading_of(node)))) is not None:
            node.id, node.attrs, node.facts = was.id, was.attrs, was.facts


def _demote_heading(doc: Document, rng: random.Random, pages: frozenset[int]) -> Damage:
    """A section heading (h2 to h6, not the title) becomes a paragraph, and the reading order is
    nested again, as the converter would have nested it: its section is gone, and what it held
    joins the section before."""
    found = [n for n, _ in _with_parents(doc)
             if n.role in SUBHEADING_ROLES and n.page in pages and (n.text or "").strip()]
    node = _pick(rng, found, "section heading", pages)
    was, sections = node.role, _sections(doc)
    node.role = Role.P
    _renest(doc, sections)
    return _damage(node.page, _box_of(node), f"{was.value} {_show(node.text)} became a paragraph")


def _swap_paragraphs(doc: Document, rng: random.Random, pages: frozenset[int]) -> Damage:
    """Two adjacent paragraphs of one page, of five words or more and different texts, trade
    places in their parent."""
    found = []
    for parent, _ in [(doc.root, None), *_with_parents(doc)]:
        kids = parent.children
        for a, b in zip(kids, kids[1:], strict=False):
            if (a.role is b.role is Role.P and a.page == b.page and a.page in pages
                    and min(_words(a), _words(b)) >= 5 and a.text.split() != b.text.split()):
                found.append((parent, a, b))
    parent, a, b = _pick(rng, found, "two adjacent paragraphs", pages)
    i = parent.children.index(a)
    parent.children[i:i + 2] = [b, a]
    return _damage(a.page, _union(_box_of(a), _box_of(b)),
                   f"swapped {_show(a.text, 40)} and {_show(b.text, 40)}")


NUMBER = re.compile(r"\d+")
NUMBER_ROLES = frozenset({Role.P, Role.LI, Role.TD, Role.TH, Role.CAPTION}) | HEADING_ROLES


def _change_number(doc: Document, rng: random.Random, pages: frozenset[int]) -> Damage:
    """One digit of a number in the text becomes another digit."""
    found = [(n, m) for n, _ in _with_parents(doc)
             if n.role in NUMBER_ROLES and n.page in pages
             for m in NUMBER.finditer(n.text or "")]
    node, match = _pick(rng, found, "number in the text", pages)
    at = match.start() + rng.randrange(len(match.group()))
    old = node.text[at]
    new = str((int(old) + 1 + rng.randrange(9)) % 10)
    before = node.text
    node.set_text(before[:at] + new + before[at + 1:], edits=[(at, at + 1, 1)])
    number = match.group()
    changed = number[:at - match.start()] + new + number[at - match.start() + 1:]
    return _damage(node.page, _box_of(node),
                   f"{number} became {changed} in {node.role.value} {_show(before, 50)}")


def _drop_paragraph(doc: Document, rng: random.Random, pages: frozenset[int]) -> Damage:
    """A paragraph of five words or more goes."""
    found = [(n, p) for n, p in _with_parents(doc)
             if n.role is Role.P and n.page in pages and _words(n) >= 5]
    node, parent = _pick(rng, found, "paragraph of five words", pages)
    parent.children.remove(node)
    return _damage(node.page, _box_of(node), f"dropped {_show(node.text)}")


# The documents of inject.DOCS: every check-scored cell there is caught since #330.
ALL_DOCS = frozenset({"fw9", "resnet", "uscis-i9", "irs-w4-2026", "cdc-antibiotics-trifold"})

DEFECTS: list[Defect] = [
    Defect("drop-table-row", "a body row of a table is gone", _drop_row,
           codes=frozenset({"table-collapsed", "unheld-lines"}),
           enforced=frozenset({"fw9", "resnet", "uscis-i9"})),
    Defect("demote-heading", "a section heading is published as a paragraph", _demote_heading,
           codes=frozenset({"demoted-heading", "level-disagreement"}),
           enforced=ALL_DOCS),
    Defect("swap-paragraphs", "two adjacent paragraphs trade places", _swap_paragraphs,
           codes=frozenset({"order-disagreement", "ruled-box-doubt"}),
           enforced=ALL_DOCS),
    Defect("change-number", "one digit of a number is another digit", _change_number,
           enforced=ALL_DOCS),
    Defect("drop-paragraph", "a paragraph is gone: the defect recall exists to catch",
           _drop_paragraph, codes=frozenset({"empty-transcript"}),
           enforced=ALL_DOCS),
]
