"""What a robustness cell is scored on: the semantic projection of a conversion, the review facts
it raised, and the outcome of comparing a damaged conversion with the clean one.

The projection is what a reader gets from the published transcript, read the way the page checks
read it (tests/pages/checks.py): per page, the words in reading order, the table cells, the link
texts and the form fields (kind, name, value), and the document's language (its primary subtag:
"en-US" and "en" are one language). The page label and reflow's own notes are left out. Two projections are
the same only when every page's words come in the same order: the same words in another order is
a difference (an order-aware oracle, unlike the page checks' recall).

A damage kind that expects a flag is met by a review fact with its code that covers the damaged
region and that the clean conversion did not already raise there: an unrelated warning never
counts.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pages.checks import Node, norm, own_text, page_tokens, parse_html, text_of
from robustness.kinds import Box, Damage, Expect


@dataclass(frozen=True)
class PageView:
    tokens: tuple[str, ...]
    tables: tuple[tuple[tuple[str, ...], ...], ...] = ()
    links: tuple[str, ...] = ()
    fields: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Projection:
    lang: str
    pages: dict[int, PageView]


def _control_name(n: Node, labels: dict[str, str]) -> str:
    if n.attrs.get("aria-label"):
        return norm(n.attrs["aria-label"])
    return labels.get(n.attrs.get("id", ""), "")


def _generated(n: Node) -> bool:
    """The page label and the notes reflow writes itself ("Text on this page was read by OCR"):
    not the document's text. A note is a signal, and the facts carry it."""
    return n.has_class("pg-label") or "data-generated" in n.attrs


def view_of(pg: Node) -> PageView:
    """The projection of one page's `<div class="pg">`."""
    tables = tuple(tuple(tuple(own_text(c) for c in tr.children
                               if isinstance(c, Node) and c.tag in ("th", "td"))
                         for tr in table.find_all("tr"))
                   for table in pg.find_all("table"))
    links = tuple(text_of(a) for a in pg.find_all("a") if "href" in a.attrs)
    labels = {lab.attrs["for"]: text_of(lab) for lab in pg.find_all("label") if lab.attrs.get("for")}
    fields = []
    for n in pg.walk():
        if n.tag in ("input", "textarea", "select"):
            kind = n.attrs.get("type", "text") if n.tag == "input" else n.tag
            if n.tag == "select":   # the renderer marks the chosen options, not the select
                value = " ".join(o.attrs.get("value", "") for o in n.find_all("option")
                                 if "selected" in o.attrs)
            else:
                value = n.attrs.get("value", "") + (" checked" if "checked" in n.attrs else "")
            fields.append((kind, _control_name(n, labels), value.strip()))
    return PageView(tuple(page_tokens(text_of(pg, skip=_generated))), tables, links, tuple(fields))


def project(out: Path) -> Projection:
    """The projection of a conversion's output folder."""
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    pages: dict[int, PageView] = {}
    for chunk in manifest["chunks"]:
        root = parse_html((out / chunk["href"]).read_text(encoding="utf-8"))
        for n in root.walk():
            if n.tag == "div" and n.attrs.get("class", "") == "pg" and "data-page" in n.attrs:
                pages[int(n.attrs["data-page"])] = view_of(n)
    return Projection((manifest.get("lang") or "").split("-")[0].lower(), pages)


def _first_divergence(want: tuple[str, ...], got: tuple[str, ...]) -> str:
    i = next((k for k, (a, b) in enumerate(zip(want, got, strict=False)) if a != b), min(len(want), len(got)))
    show = lambda ts: " ".join(ts[max(0, i - 3):i + 5]) or "(end)"   # noqa: E731
    if Counter(want) == Counter(got):
        return f"the same words in another order, from word {i + 1}: {show(want)!r} became {show(got)!r}"
    lost = sorted((Counter(want) - Counter(got)).elements())
    added = sorted((Counter(got) - Counter(want)).elements())
    parts = []
    if lost:
        parts.append(f"{len(lost)} words lost ({' '.join(lost[:8])})")
    if added:
        parts.append(f"{len(added)} words added ({' '.join(added[:8])})")
    return f"{', '.join(parts)}; first change at word {i + 1}: {show(want)!r} became {show(got)!r}"


def differences(want: Projection, got: Projection) -> list[str]:
    """How `got` differs from `want`, the clean conversion's projection; empty when the same."""
    out = []
    if want.lang != got.lang:
        out.append(f"language {want.lang!r} became {got.lang!r}")
    if sorted(want.pages) != sorted(got.pages):
        out.append(f"pages {sorted(want.pages)} became {sorted(got.pages)}")
    for number in sorted(set(want.pages) & set(got.pages)):
        a, b = want.pages[number], got.pages[number]
        if a.tokens != b.tokens:
            out.append(f"page {number} text: {_first_divergence(a.tokens, b.tokens)}")
        for what in ("tables", "links", "fields"):
            x, y = getattr(a, what), getattr(b, what)
            if x != y:
                out.append(f"page {number} {what}: {len(x)} became {len(y)}"
                           + (f"; first change {_first(x, y)}" if x and y else ""))
    return out


def _first(x: tuple, y: tuple) -> str:
    i = next((k for k, (a, b) in enumerate(zip(x, y, strict=False)) if a != b), min(len(x), len(y)))
    return f"at {i + 1}: {x[i] if i < len(x) else None!r} became {y[i] if i < len(y) else None!r}"[:300]


# ---------------------------------------------------------------------------- facts

@dataclass(frozen=True)
class Fact:
    code: str
    page: int | None          # None: a document-level fact
    box: Box | None = None    # None: the whole page


def facts(out: Path) -> list[Fact]:
    """Every fact in a conversion's document.json: the document's, each page's and each node's."""
    doc = json.loads((out / "document.json").read_text(encoding="utf-8"))
    found = [Fact(f["code"], p["number"]) for p in doc.get("pages", []) for f in p.get("facts", [])]
    stack = [doc["root"]]
    while stack:
        node = stack.pop()
        box = tuple(node["bbox"]) if node.get("bbox") else None
        found += [Fact(f["code"], node.get("page"), box) for f in node.get("facts", [])]
        stack.extend(node.get("children", []))
    return found


def _overlaps(a: Box, b: Box) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def covers(fact: Fact, damage: Damage) -> bool:
    if fact.page is None:
        return True
    if fact.page not in damage.pages:
        return False
    box = damage.boxes.get(fact.page)
    return box is None or fact.box is None or _overlaps(fact.box, box)


def flag_found(code: str, clean: list[Fact], damaged: list[Fact], damage: Damage) -> bool:
    """A fact `code` in the damaged conversion over the damaged region, where the clean
    conversion raised none over it: a fact the clean file already has there is no evidence."""
    if any(f.code == code and covers(f, damage) for f in clean):
        return False
    return any(f.code == code and covers(f, damage) for f in damaged)


# ---------------------------------------------------------------------------- outcomes

class Outcome(Enum):
    PRESERVED = "preserved"       # the projection is the clean one's
    CAUGHT = "caught"             # changed, and the kind's review fact covers the damage
    SILENT_LOSS = "silent loss"   # changed, and nothing says so
    FALSE_ALARM = "false alarm"   # a clean control raised a fact a kind watches for
    INVALID = "invalid"           # the mutation is wrong: it changed nothing, or changed pixels it must not
    UNSUPPORTED = "unsupported"   # the document has nothing the kind can damage
    CRASHED = "crashed"           # the conversion of the damaged file failed


ORDER = list(Outcome)


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    met: bool | None              # whether the kind's expectation holds; None when not scored
    detail: str = ""
    diffs: tuple[str, ...] = field(default=())


def judge(expect: Expect, diffs: list[str], flagged: bool) -> Verdict:
    """The outcome of a damaged conversion against the clean one. `same` is met by an equal
    projection, `flagged` by the fact, and `repaired` only by both. A flagged kind whose
    projection stayed the same without the fact is preserved, but its expectation is unmet."""
    same = not diffs
    outcome = Outcome.PRESERVED if same else Outcome.CAUGHT if flagged else Outcome.SILENT_LOSS
    if expect.code is None:
        met = same
    elif expect.same:
        met = same and flagged
    else:
        met = flagged
    detail = "; ".join(diffs[:3]) + (f" (+{len(diffs) - 3} more)" if len(diffs) > 3 else "")
    if expect.code is not None:
        detail = (f"{' or '.join(expect.codes)} {'raised' if flagged else 'not raised'}"
                  + (f"; {detail}" if detail else ""))
    return Verdict(outcome, met, detail, tuple(diffs))
