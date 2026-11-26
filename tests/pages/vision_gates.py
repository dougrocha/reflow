"""Scores the page-vision checks (reflow.analyze.vision_gates) on real conversions: how many
of the wrong cases each check rejects (fixtures/vision-gates.json), and how many of the
headings and row headers the page specs require it rejects too (false rejections). A check may
be on only with no false rejection.

Each block is judged as the pass would judge a promotion of it: the node in the page-check
harness's conversion, with the block before it in the page's reading order. A required heading
is judged whatever role reflow gave it, since the model might be the one to propose it.

    PYTHONPATH=src:tests/pages .venv/bin/python tests/pages/vision_gates.py [--list] [--whole]

tests/pages/test_vision_gate_pages.py asserts the same verdicts one case at a time.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from corpus import Corpus, DocOutput
from specs import load_specs

from reflow.analyze import page_vision as pv
from reflow.analyze import vision_gates as vg
from reflow.model import Document, Node, Role

FIXTURE = Path(__file__).parent / "fixtures" / "vision-gates.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Heading:
    """A block to judge as a promotion: a wrong case (`wrong`) or a heading a spec requires."""
    id: str
    doc: str
    page: int
    text: str
    wrong: bool
    contains: bool = False
    gate: str | None = None      # a wrong case: the check meant to reject it
    xfail: str | None = None     # a wrong case: why that check does not reject it here


@dataclass(frozen=True)
class Column:
    """A table column to judge as row headers: a wrong case, or a spec's required row headers."""
    id: str
    doc: str
    page: int
    wrong: bool
    column: int = 0
    first_cells: tuple[str, ...] = ()   # a spec's table: cells it holds, to find it on the page


def wrong_headings() -> list[Heading]:
    return [Heading(f"{c['doc']}-{c['page']}-{c['text'][:24]}", c["doc"], c["page"], c["text"],
                    True, gate=c["gate"], xfail=c.get("xfail")) for c in CASES["headings"]]


def required_headings() -> list[Heading]:
    """Every heading a spec requires, except an expected failure (xfail)."""
    out = []
    for spec in load_specs():
        for a in spec.assertions:
            if a.check == "heading" and not a.xfail:
                out.append(Heading(f"{spec.id}-{a.index}", spec.doc, spec.page, a.value["text"],
                                   False, contains=bool(a.value.get("contains"))))
    return out


def wrong_columns() -> list[Column]:
    return [Column(f"{c['doc']}-{c['page']}-col{c['column']}", c["doc"], c["page"], True,
                   c["column"]) for c in CASES["row_headers"]]


def required_columns() -> list[Column]:
    out = []
    for spec in load_specs():
        for a in spec.assertions:
            if a.check == "table" and a.value.get("row_headers") and not a.xfail:
                out.append(Column(f"{spec.id}-{a.index}", spec.doc, spec.page, False,
                                  first_cells=tuple(a.value.get("cells", ())[:4])))
    return out


def pages_needed() -> dict[str, set[int]]:
    """The pages each document's checks read: every spec's page, and the wrong cases'."""
    needed: dict[str, set[int]] = {}
    for spec in load_specs():
        needed.setdefault(spec.doc, set()).add(spec.page)
    for c in CASES["headings"] + CASES["row_headers"]:
        needed.setdefault(c["doc"], set()).add(c["page"])
    return needed


# -- finding the blocks -----------------------------------------------------------------------------


def norm(text: str | None) -> str:
    text = re.sub(r"[☐☑☒]", "", text or "").replace("’", "'")
    return " ".join(text.split()).casefold()


class Converted:
    """One converted document, read as the pass reads it."""

    def __init__(self, out: DocOutput) -> None:
        self.out = out
        self.doc = Document.from_dict(out.document())
        self._blocks: dict[int, list[Node]] = {}

    def blocks(self, page: int) -> list[Node]:
        """The blocks the pass offers on the source page `page`, in its order."""
        n = self.out.excerpt_page(page)
        if n not in self._blocks:
            self._blocks[n] = [b for b in pv.page_blocks(self.doc.root.children, n, [])
                               if b.bbox is not None]
        return self._blocks[n]

    def find(self, h: Heading) -> tuple[Node, Node | None] | None:
        """The block holding the heading's text, and the block before it; None when no block
        does. Equal text first, then a block that starts with it (the wrong cases are the start
        of their block), then, for a `contains` heading, a block holding it."""
        want = norm(h.text)
        blocks = self.blocks(h.page)
        # a directory heading's spec text ends in its status ("— Accepting New Patients"),
        # which the transcript shows beside the name (attrs["status"])
        named = want.split(" — ")[0]
        tests = [lambda t: t in (want, named), lambda t: t.startswith(want) or want.startswith(t)
                 and len(t) >= 0.6 * len(want)]
        if h.contains:
            tests.append(lambda t: want in t)
        for test in tests:
            for i, b in enumerate(blocks):
                if b.text and test(norm(b.text)):
                    # the nearest block with text before it, as page_vision.proposals_for reads it
                    return b, next((x for x in reversed(blocks[:i]) if (x.text or "").strip()),
                                   None)
        return None

    def tables(self, page: int) -> list[tuple[list[list[str]], int]]:
        """Each table on the page as rows of cell texts, and its count of header rows."""
        return [([[c.text or "" for c in r.children] for r in t.children if r.role is Role.TR],
                 int(t.attrs.get("header_rows", 0)))
                for t in self.blocks(page) if t.role is Role.TABLE]


@dataclass
class Judged:
    case: Heading | Column
    found: bool
    gates: tuple[vg.Gate, ...] = ()
    seen: str = ""        # what was judged: the block's text, or the column's cells


def judge_heading(c: Converted, h: Heading) -> Judged:
    hit = c.find(h)
    if hit is None:
        return Judged(h, False)
    node, before = hit
    return Judged(h, True, vg.heading_gates(node, before),
                  f"{node.role.value} {node.text!r} after {(before.text if before else None)!r}")


def judge_columns(c: Converted, col: Column) -> list[Judged]:
    """One verdict per table the case names on its page (a spec's: the table with its cells)."""
    out = []
    for rows, header_rows in c.tables(col.page):
        if col.first_cells:
            held = {norm(x) for r in rows for x in r}
            if not all(norm(x) in held for x in col.first_cells if x):
                continue
        cells = [r[col.column] for r in rows[header_rows:] if len(r) > col.column]
        rejected = (vg.Gate.DATA_COLUMN,) if vg.data_column(cells) else ()
        out.append(Judged(col, True, rejected, " | ".join(cells)))
    return out or [Judged(col, False)]


# -- scoring --------------------------------------------------------------------------------------


def judge_all(corpus: Corpus) -> tuple[list[Judged], list[Judged]]:
    """(headings, columns): every wrong case and every required case, judged."""
    converted: dict[str, Converted] = {}

    def doc(slug: str) -> Converted:
        if slug not in converted:
            converted[slug] = Converted(corpus.doc(slug))
        return converted[slug]

    headings = [judge_heading(doc(h.doc), h) for h in wrong_headings() + required_headings()]
    columns = [j for col in wrong_columns() + required_columns()
               for j in judge_columns(doc(col.doc), col)]
    return headings, columns


def score(judged: list[Judged], gate: str) -> dict[str, int]:
    """The counts behind a check's score: wrong cases found and rejected, required ones found
    and wrongly rejected."""
    wrong = [j for j in judged if j.case.wrong and j.found]
    right = [j for j in judged if not j.case.wrong and j.found]
    return {"wrong": len(wrong), "rejected": sum(gate in j.gates for j in wrong),
            "targeted": sum(getattr(j.case, "gate", gate) == gate for j in wrong),
            "targeted_rejected": sum(getattr(j.case, "gate", gate) == gate and gate in j.gates
                                     for j in wrong),
            "required": len(right), "false_rejections": sum(gate in j.gates for j in right),
            "not_found": sum(not j.found for j in judged)}


def rules_row_headers(corpus: Corpus) -> list[list[str]]:
    """The body cells of each column the rules made row headers (th scope=row) on a spec's page:
    not checked by a person, so a second count of false rejections beside the specs' few."""
    out = []
    for spec in load_specs():
        c = Converted(corpus.doc(spec.doc))
        for t in (n for n in c.blocks(spec.page) if n.role is Role.TABLE):
            rows = [r.children for r in t.children if r.role is Role.TR]
            body = rows[int(t.attrs.get("header_rows", 0)):]
            for k in sorted({i for r in body for i, x in enumerate(r)
                             if x.role is Role.TH and x.attrs.get("scope") == "row"}):
                out.append([r[k].text or "" for r in body if len(r) > k])
    return out


def report(headings: list[Judged], columns: list[Judged], listing: bool,
           rules: list[list[str]] = ()) -> Iterator[str]:
    gates = [g.value for g in vg.Gate if g is not vg.Gate.DATA_COLUMN]
    yield "check          on   wrong rejected (of its targets)   required falsely rejected"
    for gate, judged in [*((g, headings) for g in gates), ("data-column", columns)]:
        s = score(judged, gate)
        on = ("n/a" if gate == vg.Gate.DATA_COLUMN    # the pass asks no row-header question yet
              else "on" if gate in {g.value for g in vg.ON} else "off")
        yield (f"{gate:14} {on:4} {s['rejected']:>3} of {s['wrong']:<3} "
               f"({s['targeted_rejected']} of {s['targeted']})"
               f"{'':16}{s['false_rejections']:>3} of {s['required']}")
    yield (f"data-column on the rules' own row-header columns of the spec pages: "
           f"{sum(vg.data_column(c) for c in rules)} of {len(rules)} rejected")
    on = {g.value for g in vg.ON}
    wrong = [j for j in headings if j.case.wrong and j.found]
    yield (f"checks on: {sum(bool(on & set(j.gates)) for j in wrong)} of {len(wrong)} wrong "
           f"headings rejected; {sum(bool(on & set(j.gates)) for j in headings if not j.case.wrong and j.found)} "
           "required headings rejected")
    missing = [j.case.id for j in headings + columns if not j.found]
    yield f"not found in the conversion ({len(missing)}): " + ", ".join(missing)
    if listing:
        for j in headings + columns:
            if j.found:
                mark = "WRONG" if j.case.wrong else "req  "
                yield f"{mark} {j.case.id}: {[g.value for g in j.gates]} {j.seen[:160]}"
        for cells in rules:
            if vg.data_column(cells):
                yield f"rules' row headers rejected: {' | '.join(cells)[:160]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="every case, its checks and its block")
    ap.add_argument("--whole", action="store_true", help="convert whole documents, not excerpts")
    args = ap.parse_args(argv)
    corpus = Corpus(quick=not args.whole)
    needed = pages_needed()
    slugs = set(needed)
    corpus.prepare(sorted(slugs), pages={s: needed[s] for s in slugs})
    for line in report(*judge_all(corpus), args.list, rules_row_headers(corpus)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
