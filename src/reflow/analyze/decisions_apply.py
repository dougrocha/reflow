"""Applying the decision store to a model: where each decision matches, what it changes, and the
ledger of what the pass did (docs/review-decisions.md). The store's wire format and its
fold are analyze.decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from reflow import geometry
from reflow.analyze import review
from reflow.analyze.anchors import (DOC_SCOPE, EXACT_EDGE_PT, MOVED_IOU, REBASED_IOU, Anchor,
                                    finding_id, rounded, text_sha)
from reflow.analyze.decisions import (
    AltText,
    Correct,
    Dismiss,
    Fold,
    FontMapVerdict,
    Kind,
    Keep,
    ProposalVerdict,
    Snapshot,
    Stackable,
    TextDecision,
    Verdict,
    eligible_pdfs,
    fold,
    invalid_now,
    read,
)
from reflow.ingest.font_map import FACT_MAP, LAYER_TEXT
from reflow.model import (AI_DRAFTED, BBox, Document, Fact, Mark, Node, Role, Source, Span, is_ai_draft,
                          mark_order)
from reflow.text_quality import squash


class Reason(StrEnum):
    """Why a decision is orphaned. Assigned when a decision is checked, matched or applied,
    never by the fold."""
    OTHER_PDF = "other-pdf"
    NO_NODE = "no-node"
    AMBIGUOUS = "ambiguous"
    RANGE_MISSING = "range-missing"
    CONFLICT = "conflict"
    NOT_OCR = "not-ocr"
    INVALID_NOW = "invalid-now"
    FIGURE_CHANGED = "figure-changed"
    REFUSED_ERROR = "refused-error"
    NOT_FLAGGED = "not-flagged"   # the range is not exactly a span reflow flagged (a doubt mark)




class Tier(StrEnum):
    EXACT = "exact"
    MOVED = "moved"
    REBASED = "rebased"


@dataclass(frozen=True)
class Target:
    """The one place in the text a correction or a keep applies: node.text[start:end]."""
    node: Node
    start: int
    end: int
    tier: Tier


@dataclass(frozen=True)
class FigureTarget:
    node: Node


@dataclass(frozen=True)
class Satisfied:
    """The converter already reads the corrected word: nothing to change."""
    node: Node


@dataclass(frozen=True)
class Orphan:
    reason: Reason


Match = Target | FigureTarget | Satisfied | Orphan


def _edges_close(a: BBox, b: BBox) -> bool:
    return all(abs(x - y) <= EXACT_EDGE_PT for x, y in zip(a, b))


def _count(text: str, needle: str) -> int:
    """How often `needle` occurs in `text`, overlapping occurrences included."""
    n, at = 0, text.find(needle)
    while at >= 0:
        n += 1
        at = text.find(needle, at + 1)
    return n


def _page_nodes(doc: Document, page: int) -> list[Node]:
    return [n for n in doc.root.walk() if n.page == page and n.bbox and n.text]


def match(doc: Document, decision: Correct | Keep | AltText) -> Match:
    """Where a decision applies in `doc`, or why nowhere. Fail-closed: anything but exactly one
    candidate is an Orphan. Reads the model; changes nothing. The caller checks the PDF identity
    first and matches against the model as it was before any decision changed it."""
    if isinstance(decision, AltText):
        return _match_figure(doc, decision.anchor)
    if isinstance(decision, Correct) and invalid_now(decision.now, decision.range.was):
        return Orphan(Reason.INVALID_NOW)
    nodes = _page_nodes(doc, decision.anchor.page)
    found = _find_node(nodes, decision.anchor)
    if isinstance(found, Orphan) and found.reason == Reason.AMBIGUOUS:
        return found
    if isinstance(found, Orphan):
        return _satisfied(nodes, decision) or found
    node, tier = found
    if node.source != Source.OCR:
        return Orphan(Reason.NOT_OCR)
    text, r = node.text or "", decision.range
    needle = r.before + r.was + r.after
    if (hits := _count(text, needle)) == 1:
        if isinstance(decision, Correct) and _count(text, r.before + decision.now + r.after):
            return Orphan(Reason.AMBIGUOUS)
        start = text.index(needle) + len(r.before)
        end = start + len(r.was)
        # Print only what the page says: a decision may change only a span reflow itself doubted.
        if not any(m.kind == "doubt" and (m.start, m.end) == (start, end) for m in node.marks):
            return Orphan(Reason.NOT_FLAGGED)
        return Target(node, start, end, tier)
    if hits == 0:
        return _satisfied(nodes, decision) or Orphan(Reason.RANGE_MISSING)
    return Orphan(Reason.RANGE_MISSING)


def _find_node(nodes: list[Node], anchor: Anchor) -> tuple[Node, Tier] | Orphan:
    """The tiers in order. One candidate ends the search with it, two or more with ambiguous."""
    def box(n: Node) -> BBox:
        return n.bbox or (0.0, 0.0, 0.0, 0.0)   # _page_nodes keeps only nodes with a box

    tiers = (
        (Tier.EXACT, lambda n: text_sha(n.text or "") == anchor.text_sha
         and _edges_close(box(n), anchor.bbox)),
        (Tier.MOVED, lambda n: text_sha(n.text or "") == anchor.text_sha
         and geometry.iou(box(n), anchor.bbox) >= MOVED_IOU),
        (Tier.REBASED, lambda n: geometry.iou(box(n), anchor.bbox) >= REBASED_IOU))
    for tier, test in tiers:
        candidates = [n for n in nodes if test(n)]
        if len(candidates) > 1:
            return Orphan(Reason.AMBIGUOUS)
        if candidates:
            return candidates[0], tier
    return Orphan(Reason.NO_NODE)


def _satisfied(nodes: list[Node], decision: Correct | Keep) -> Satisfied | Orphan | None:
    """The corrected reading, already in the text of the one node that passes the box test of
    the exact or moved tier (hash aside). None when it is not there."""
    if not isinstance(decision, Correct):
        return None
    r, anchor = decision.range, decision.anchor
    needle = r.before + decision.now + r.after
    near = [n for n in nodes if n.bbox and (_edges_close(n.bbox, anchor.bbox)
                                           or geometry.iou(n.bbox, anchor.bbox) >= MOVED_IOU)]
    holding = [n for n in near if _count(n.text or "", needle)]
    if not holding:
        return None
    if len(holding) > 1 or _count(holding[0].text or "", needle) > 1:
        return Orphan(Reason.AMBIGUOUS)
    return Satisfied(holding[0])


def _match_figure(doc: Document, anchor: Anchor) -> FigureTarget | Orphan:
    """A picture matches only by its page and its crop's hash, on exactly one figure."""
    same = [n for n in doc.root.walk() if n.role == Role.FIGURE and n.page == anchor.page
            and anchor.crop_sha and n.attrs.get("crop_sha") == anchor.crop_sha]
    if len(same) > 1:
        return Orphan(Reason.AMBIGUOUS)
    return FigureTarget(same[0]) if same else Orphan(Reason.FIGURE_CHANGED)


# -- applying ------------------------------------------------------------------------------------

LEDGER_VERSION = 1
UNDECIDED = "undecided"                 # node.attrs: the node as it was before a decision changed it
UNDECIDED_TITLE = "undecided_title"     # root.attrs: the title as it was
DOC_FACTS = frozenset({review.ORPHANED, "decisions-other-pdf", "decisions-unreadable"})
TEXT_NOW_CHARS = 200


def snapshot_of(run: Any) -> Snapshot | None:
    """The one read of a conversion's decisions file, kept in `run.state` so the pass and page
    vision see the same snapshot. None when the run was given no decisions file."""
    path = run.options.decisions
    if path is None:
        return None
    if run.state.decisions is None:
        run.state.decisions = read(path)
    return run.state.decisions


def proposal_verdicts(run: Any) -> tuple[ProposalVerdict, ...]:
    """The active proposal decisions of the run's store that were made on this PDF (or one
    reattached to it), in store order. Page vision layers them over its carried decisions."""
    snap = snapshot_of(run)
    if snap is None:
        return ()
    folded = fold(snap)
    sha = str(run.doc.meta.get("sha256") or "")
    eligible = eligible_pdfs(folded, sha) if sha else frozenset()
    return tuple(r for r in folded.active if isinstance(r, ProposalVerdict) and r.pdf in eligible)


def _scalar(v: object) -> object:
    """A JSON value of a store, as the ledger may repeat it: a short string, a number or None."""
    if isinstance(v, str):
        return v[:40]
    return v if isinstance(v, (int, float, bool)) or v is None else type(v).__name__


def _clip(text: str | None) -> str | None:
    return None if text is None else text[:64]


class _Ledger:
    """What the pass did, built from explicit field lists: no field of a record that is not
    listed here reaches a published file."""

    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []
        self.satisfied: list[dict[str, Any]] = []
        self.dismissed: list[dict[str, Any]] = []
        self.refused: list[dict[str, Any]] = []
        self.orphaned: list[dict[str, Any]] = []
        self.stale: list[dict[str, Any]] = []
        self.proposals: list[dict[str, Any]] = []
        self.fonts: list[dict[str, Any]] = []
        self.fingerprints: list[str] = []

    def orphan(self, doc: Document, rec: Stackable, reason: Reason) -> None:
        anchor = getattr(rec, "anchor", None)
        rng = getattr(rec, "range", None)
        now = rec.now if isinstance(rec, Correct) else rec.text if isinstance(rec, AltText) else ""
        self.orphaned.append({
            "id": rec.id, "kind": rec.kind.value,
            "page": anchor.page if anchor else None,
            "bbox": list(anchor.bbox) if anchor else None,
            "reason": reason.value, "was": rng.was if rng else "", "now": now,
            "text_now": _text_now(doc, anchor) if anchor and anchor.text_sha else ""})

    def as_dict(self, doc: Document, snapshot: Snapshot, folded: Fold) -> dict[str, Any]:
        return {
            "v": LEDGER_VERSION,
            "source": {"sha256": str(doc.meta.get("sha256") or "")},
            "store": {"id": snapshot.store, "bytes": snapshot.bytes, "lines": snapshot.lines,
                      "torn": snapshot.torn, "unreadable": len(snapshot.unreadable),
                      "duplicate": folded.duplicate,
                      "invalid": [{"id": _clip(i.id), "reason": i.reason} for i in folded.invalid],
                      "unsupported": [{"id": _clip(u.id), "v": _scalar(u.v), "kind": _scalar(u.kind)}
                                      for u in folded.unsupported],
                      "error": snapshot.error},
            "applied": self.applied, "satisfied": self.satisfied, "dismissed": self.dismissed,
            "refused": self.refused, "orphaned": self.orphaned, "stale": self.stale,
            "proposals": self.proposals,
            # only when a font-map answer was given, so a ledger without one is as it was
            **({"fonts": self.fonts} if self.fonts else {})}


def _text_now(doc: Document, anchor: Anchor) -> str:
    """What the page reads now where the decision pointed: the text of the node whose box overlaps
    the anchor's the most, when that is at least half. Empty when there is none."""
    best, at = 0.0, ""
    for node in _page_nodes(doc, anchor.page):
        score = geometry.iou(node.bbox or anchor.bbox, anchor.bbox)
        if score > best:
            best, at = score, node.text or ""
    return at[:TEXT_NOW_CHARS] if best >= MOVED_IOU else ""


def revert(doc: Document) -> None:
    """Undo what `apply` left in the model: every node that holds an `undecided` snapshot goes
    back to it, the title comes back, the root loses its decision facts and the model loses
    `meta["decisions"]`. A model no decision touched is left as it is. `revert(apply(m)) == m`
    by `Document.to_json`."""
    for node in doc.root.walk():
        prior = node.attrs.get(UNDECIDED)
        if prior is None:
            continue
        node.restore_text(prior["text"], [Span.from_dict(s) for s in prior["spans"]],
                          [Mark.from_dict(m) for m in prior["marks"]])
        node.facts = [Fact.from_dict(f) for f in prior["facts"]]
        node.source = Source(prior["source"])
        node.confidence = prior["confidence"]
        node.attrs.clear()
        node.attrs.update(prior["attrs"])
    if UNDECIDED_TITLE in doc.root.attrs:
        doc.root.attrs["title"] = doc.root.attrs.pop(UNDECIDED_TITLE)
    doc.root.facts = [f for f in doc.root.facts if f.code not in DOC_FACTS]
    doc.meta.pop("decisions", None)


def remember(node: Node) -> None:
    """Keep the node as it is in `attrs["undecided"]`, once: the first decision that changes a
    node stores its prior state (the later ones find it there)."""
    if UNDECIDED in node.attrs:
        return
    prior = {"text": node.text, "spans": [s.to_dict() for s in node.spans],
             "marks": [m.to_dict() for m in node.marks], "facts": [f.to_dict() for f in node.facts],
             "source": node.source.value, "confidence": node.confidence,
             "attrs": json.loads(json.dumps(node.attrs))}
    node.attrs[UNDECIDED] = prior


def _overlap(a: Target, b: Target) -> bool:
    return a.node is b.node and a.start < b.end and b.start < a.end


def _conflicts(found: list[tuple[TextDecision | AltText, Match]]) -> set[str]:
    """The ids of the decisions that cannot both apply: two ranges that overlap in one node
    (adjacent ranges are fine), or two alt texts for one figure."""
    out: set[str] = set()
    targets = [(r, m) for r, m in found if isinstance(m, (Target, FigureTarget))]
    for i, (ra, ma) in enumerate(targets):
        for rb, mb in targets[i + 1:]:
            if isinstance(ma, Target) and isinstance(mb, Target):
                clash = _overlap(ma, mb)
            else:
                clash = ma.node is mb.node
            if clash:
                out.update((ra.id, rb.id))
    return out


def apply(target: Any, snapshot: Snapshot) -> dict[str, int]:
    """Read a reviewer's decisions into the model (docs/review-decisions.md, "Applying"). `target`
    is a Run or a Document that confidence.apply has scored. In order: revert what an earlier pass
    left; check the PDF identity and match every active decision against the unchanged model;
    orphan the conflicting ones; settle the dismissals; write the ledger and the orphans' facts.
    Returns {"applied", "dismissed", "orphaned"} counts."""
    doc: Document = getattr(target, "doc", target)
    revert(doc)
    folded = fold(snapshot)
    sha = str(doc.meta.get("sha256") or "")
    eligible = eligible_pdfs(folded, sha) if sha else frozenset()
    ledger = _Ledger()
    found: list[tuple[TextDecision | AltText, Match]] = []
    dismissals: list[Dismiss] = []
    font_maps: list[FontMapVerdict] = []

    for rec in folded.active:
        if rec.pdf not in eligible:
            ledger.orphan(doc, rec, Reason.OTHER_PDF)
        elif isinstance(rec, Dismiss):
            dismissals.append(rec)
        elif isinstance(rec, ProposalVerdict):
            ledger.proposals.append({"id": rec.id, "proposal": rec.proposal,
                                     "verdict": rec.verdict.value})
        elif isinstance(rec, FontMapVerdict):
            font_maps.append(rec)
        else:
            found.append((rec, match(doc, rec)))

    clash = _conflicts(found)
    edits: dict[int, tuple[Node, list[tuple[TextDecision, Target]]]] = {}
    for rec, got in found:
        if isinstance(got, Orphan):
            ledger.orphan(doc, rec, got.reason)
        elif rec.id in clash:
            ledger.orphan(doc, rec, Reason.CONFLICT)
        elif isinstance(got, Satisfied):
            ledger.satisfied.append({"id": rec.id, "page": rec.anchor.page, "node": got.node.id,
                                     "now": rec.now if isinstance(rec, Correct) else ""})
        elif isinstance(got, FigureTarget) and isinstance(rec, AltText):
            _apply_alt(rec, got.node, ledger)
        elif isinstance(got, Target) and not isinstance(rec, AltText):
            edits.setdefault(id(got.node), (got.node, []))[1].append((rec, got))
    for node, node_edits in edits.values():
        _apply_text(doc, node, node_edits, ledger)
    for rec in font_maps:
        _apply_font_map(doc, rec, ledger)

    _warn(doc, snapshot, ledger)
    _settle(doc, dismissals, ledger)
    doc.meta["decisions"] = {"dismissed": ledger.fingerprints,
                             "ledger": ledger.as_dict(doc, snapshot, folded)}
    return {"applied": len(ledger.applied), "dismissed": len(ledger.dismissed),
            "orphaned": len(ledger.orphaned)}


def _drop_doubt(node: Node, start: int, end: int) -> None:
    """Take the doubt over node.text[start:end] off the node: a doubt inside the range goes, one
    that runs past it keeps the pieces outside (as the OCR-words pass placed them)."""
    text, kept = node.text or "", []
    for m in node.marks:
        if m.kind != "doubt" or m.end <= start or end <= m.start:
            kept.append(m)
            continue
        if m.start < start:
            kept.append(Mark(m.start, start, "doubt", text[m.start:start]))
        if end < m.end:
            kept.append(Mark(end, m.end, "doubt", text[end:m.end]))
    node.marks = kept


def _edit_lines(lines: list[str], edits: list[tuple[int, int, str]]) -> list[str] | None:
    """`lines` with each (start, end, new) of `edits` (offsets in the lines joined by one space,
    in order, none overlapping) put in the line that holds it; None when a range crosses a line."""
    out, at = list(lines), 0
    spans = []
    for line in lines:
        spans.append((at, at + len(line)))
        at += len(line) + 1
    for start, end, new in reversed(edits):
        for i, (a, b) in enumerate(spans):
            if a <= start and end <= b:
                out[i] = out[i][:start - a] + new + out[i][end - a:]
                break
        else:
            return None
    return out


def _apply_text(doc: Document, node: Node, edits: list[tuple[TextDecision, Target]],
                ledger: _Ledger) -> None:
    """All the corrections and keeps of one node at once: the new text from the last range to the
    first, one `set_text`, then the marks and facts."""
    edits = sorted(edits, key=lambda e: e[1].start)
    remember(node)
    old = node.text or ""
    for _, t in edits:
        _drop_doubt(node, t.start, t.end)
    new, shift, placed = old, 0, []
    for rec, t in edits:
        now = rec.now if isinstance(rec, Correct) else old[t.start:t.end]
        placed.append((rec, t, t.start + shift, t.start + shift + len(now), now))
        shift += len(now) - (t.end - t.start)
    for rec, t, *_ in reversed(placed):
        if isinstance(rec, Correct):
            new = new[:t.start] + rec.now + new[t.end:]

    lines = node.attrs.get("lines")
    new_lines = None
    if lines and old == " ".join(lines):
        new_lines = _edit_lines(lines, [(t.start, t.end, rec.now) for rec, t, *_ in placed
                                         if isinstance(rec, Correct)])
    # The edits are known exactly, so spans and marks move by them, not by a diff of the text.
    node.set_text(new, edits=[(t.start, t.end, len(now)) for rec, t, _, _, now in placed
                              if isinstance(rec, Correct)])   # a kept range is an equal stretch
    if new_lines is not None and " ".join(new_lines) == new:
        node.attrs["lines"] = new_lines

    spans = [(t.start, t.end) for _, t, *_ in placed]
    node.facts = [_moved(f, placed) for f in node.facts if _kept_fact(f, spans)]
    title_new = _title_after(doc, old, new)
    for rec, t, start, end, now in placed:
        if isinstance(rec, Correct):
            node.marks.append(Mark(start, end, "fix", now, rec.range.was))
            node.fact("reviewed-correction", Source.HUMAN,
                      f"A reviewer corrected ‘{rec.range.was}’ to ‘{now}’. OCR read "
                      f"‘{rec.range.was}’.", decision=rec.id, was=rec.range.was, now=now,
                      rule=rec.range.rule, tier=t.tier.value)
        else:
            node.fact("reviewed-kept", Source.HUMAN,
                      f"A reviewer kept OCR’s reading ‘{rec.range.was}’.", decision=rec.id,
                      was=rec.range.was, rule=rec.range.rule)
        ledger.applied.append({
            "id": rec.id, "kind": rec.kind.value, "page": rec.anchor.page,
            "bbox": list(rounded(node.bbox or rec.anchor.bbox)), "node": node.id,
            "was": rec.range.was, "now": now, "tier": t.tier.value,
            "title_unchanged": not (title_new and isinstance(rec, Correct))})
    node.marks.sort(key=mark_order)
    if title_new and any(isinstance(rec, Correct) for rec, *_ in placed):
        root = doc.root
        root.attrs.setdefault(UNDECIDED_TITLE, root.attrs.get("title", ""))
        root.attrs["title"] = title_new


def _kept_fact(fact: Fact, spans: list[tuple[int, int]]) -> bool:
    """Whether a fact stays: not an ocr-suggestion for a range a decision settled (or that a
    settled range overlaps)."""
    if fact.code != "ocr-suggestion":
        return True
    start, end = fact.data.get("start"), fact.data.get("end")
    return not any(isinstance(start, int) and isinstance(end, int) and start < b and a < end
                   for a, b in spans)


def _moved(fact: Fact, placed: list) -> Fact:
    """An ocr-suggestion the edits did not settle, with its offsets moved by the length change of
    every edit before it, so the queue still reads the right words."""
    if fact.code != "ocr-suggestion" or not isinstance(fact.data.get("start"), int):
        return fact
    shift = sum(len(now) - (t.end - t.start) for _, t, _, _, now in placed
                if t.end <= fact.data["start"])
    if not shift:
        return fact
    return Fact(fact.code, fact.source, fact.detail,
                {**fact.data, "start": fact.data["start"] + shift, "end": fact.data["end"] + shift})


def _title_after(doc: Document, old: str, new: str) -> str:
    """The document title after a node's text went from `old` to `new`: the new text when the
    title came from a heading (the `title` fact's origin) and is that node's text, else ""."""
    root = doc.root
    title = root.attrs.get("title")
    origin = next((f.data.get("origin") for f in root.facts if f.code == "title"), None)
    if (not isinstance(title, str) or origin not in ("heading", "docling")
            or squash(title) != squash(old) or UNDECIDED_TITLE in root.attrs):
        return ""
    return new if title == old else squash(new)


def _apply_font_map(doc: Document, rec: FontMapVerdict, ledger: _Ledger) -> None:
    """A reviewer's answer to a font-map item. Both settle its finding; a reject also puts the
    text layer's reading back on every node the map changed (`attrs["layer_text"]`, kept by
    ingest.font_map.mark_nodes), character for character, so every span and mark keeps its
    place, and the title with it. `revert` undoes it through `attrs["undecided"]`. A map this
    build did not decode is stale."""
    fact = next((f for f in doc.root.facts
                 if f.code == FACT_MAP and f.data.get("map") == rec.map), None)
    if fact is None:
        ledger.stale.append({"id": rec.id, "kind": rec.kind.value, "finding": rec.map})
        return
    nodes = 0
    if rec.verdict is Verdict.REJECT:
        for node in doc.root.walk():
            was, text = node.attrs.get(LAYER_TEXT), node.text or ""
            if not isinstance(was, str) or was == text or len(was) != len(text):
                continue
            remember(node)
            edits = [(i, i + 1, 1) for i, (a, b) in enumerate(zip(text, was, strict=True)) if a != b]
            lines = node.attrs.get("lines")
            title_new = _title_after(doc, text, was)
            node.set_text(was, edits=edits)   # moves no span or mark: one character for one
            if lines and " ".join(lines) == text:
                at, back = 0, []
                for line in lines:
                    back.append(was[at:at + len(line)])
                    at += len(line) + 1
                node.attrs["lines"] = back
            if title_new:
                root = doc.root
                root.attrs.setdefault(UNDECIDED_TITLE, root.attrs.get("title", ""))
                root.attrs["title"] = title_new
            nodes += 1
    ledger.fonts.append({"id": rec.id, "map": rec.map, "verdict": rec.verdict.value,
                         "nodes": nodes})
    fp = finding_id(DOC_SCOPE, fact.code, fact.detail, fact.data)   # the item is answered
    if fp not in ledger.fingerprints:
        ledger.fingerprints.append(fp)
        ledger.dismissed.append({"id": rec.id, "page": None, "code": fact.code, "finding": fp})


AI_FIGURE_FACTS = ("alt-missing", AI_DRAFTED, "decorative-guessed")


def _apply_alt(rec: AltText, node: Node, ledger: _Ledger) -> None:
    """A reviewer's alt text, or their word that the figure is decorative, in place of whatever
    the figure had: the PDF's, a draft or none. Nothing AI-made is left under their label."""
    remember(node)
    a = node.attrs
    was_by = str(a.get("provenance") or "")
    was_draft = is_ai_draft(node)
    was, was_kind = str(a.get("alt") or ""), str(a.get("kind") or "")
    a["alt"] = "" if rec.decorative else rec.text
    a["decorative"] = rec.decorative
    a["provenance"] = "reviewer"
    a["reviewed"] = True
    for key in ("needs", "warn", "description"):
        a.pop(key, None)
    if was_draft:
        a.pop("kind", None)
    if "figure_role" in a:
        a["figure_role"] = "decorative" if rec.decorative else (
            "content" if a["figure_role"] == "decorative" else a["figure_role"])
    node.facts = [f for f in node.facts
                  if f.code not in AI_FIGURE_FACTS and not f.code.startswith("suggested-")]
    node.fact("reviewed-alt", Source.HUMAN,
              "A reviewer " + ("marked this figure decorative." if rec.decorative
                               else "wrote this figure’s alt text."),
              decision=rec.id, was=was, was_by=was_by, was_kind=was_kind)
    node.source = Source.HUMAN
    ledger.applied.append({
        "id": rec.id, "kind": rec.kind.value, "page": rec.anchor.page,
        "bbox": list(rounded(node.bbox or rec.anchor.bbox)), "node": node.id, "was": was,
        "now": a["alt"], "tier": "crop", "title_unchanged": False})


def _warn(doc: Document, snapshot: Snapshot, ledger: _Ledger) -> None:
    """The root facts: a store that could not be read, decisions of another PDF, and one
    `decision-orphaned` per correction, keep or alt text that no longer matches."""
    root = doc.root
    if snapshot.error or snapshot.unreadable or snapshot.torn:
        parts = ([f"it has {snapshot.error.replace('-', ' ')}"] if snapshot.error else []) \
            + ([f"{len(snapshot.unreadable)} lines are not records"] if snapshot.unreadable else []) \
            + (["its last line is cut short"] if snapshot.torn else [])
        root.fact("decisions-unreadable", Source.HUMAN,
                  "Part of the decisions file could not be read: " + ", ".join(parts) + ".",
                  error=snapshot.error or "", unreadable=len(snapshot.unreadable),
                  torn=snapshot.torn)
    other = [o for o in ledger.orphaned if o["reason"] == Reason.OTHER_PDF]
    if other:
        n = len(other)
        root.fact("decisions-other-pdf", Source.HUMAN,
                  f"{n} decision{'s were' if n != 1 else ' was'} made on another PDF and "
                  f"{'were' if n != 1 else 'was'} not applied.", count=n)
    for o in ledger.orphaned:
        if o["kind"] not in (Kind.CORRECT, Kind.KEEP, Kind.ALT):
            continue
        root.fact(review.ORPHANED, Source.HUMAN, _orphan_text(o), decision=o["id"],
                  kind=o["kind"], page=o["page"], bbox=o["bbox"], reason=o["reason"],
                  was=o["was"], now=o["now"], text_now=o["text_now"])


def _orphan_text(o: dict[str, Any]) -> str:
    where = f" on page {o['page']}" if o["page"] else ""
    match o["kind"]:
        case Kind.CORRECT:
            said = f"A reviewer corrected ‘{o['was']}’ to ‘{o['now']}’{where}"
        case Kind.KEEP:
            said = f"A reviewer kept OCR’s reading ‘{o['was']}’{where}"
        case _:
            said = f"A reviewer wrote alt text for a figure{where}"
    reads = f": OCR now reads ‘{o['text_now']}’" if o["text_now"] else ""
    return f"{said}, and this no longer matches the page{reads}."


def _settle(doc: Document, dismissals: list[Dismiss], ledger: _Ledger) -> None:
    """Each fingerprint of the active dismissals: dismissed when a finding of the queue carries it,
    refused when that finding is an error or a decision warning, else stale. The queue is read
    from the model as it stands, with the root's decision facts in it."""
    if not dismissals:
        return
    known: dict[str, tuple[str, str | None, int | None]] = {}
    for item in review.items(doc):
        for f in item["findings"]:
            known.setdefault(f["id"], (f["code"], f["severity"], item["page"]))
    for rec in dismissals:
        for fp in dict.fromkeys(rec.findings):
            if fp not in known:
                ledger.stale.append({"id": rec.id, "kind": rec.kind.value, "finding": fp})
                continue
            code, severity, page = known[fp]
            if severity == "error" or code in DOC_FACTS:
                ledger.refused.append({"id": rec.id, "finding": fp,
                                       "reason": Reason.REFUSED_ERROR.value})
            elif fp not in ledger.fingerprints:
                ledger.fingerprints.append(fp)
                ledger.dismissed.append({"id": rec.id, "page": page, "code": code, "finding": fp})
