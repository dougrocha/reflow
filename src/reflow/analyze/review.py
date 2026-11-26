"""The review queue, read from the ledger. `queue` scores and reads it.

An item is produced for:
  * every node whose ledger holds a fact with a severity (CODES[code].severity);
  * every node whose score is below its role's threshold (confidence.Score.below);
  * every page with a fact that has a severity (role "page", id "page:N"): one item, labelled
    by its worst fact, whose targets are the boxes of all of them;
  * every document fact with a severity (role "doc", the root's id).

The order is the review walk: document items first, then the pages least trustworthy first (a
page with an error page item, then one with a warning page item, then the lowest node score on
the page, the larger share of its nodes below their threshold, and the page number), each page's
own item before its nodes' items in document order.

The item shape is review.json's, as web/src/lib/review.ts reads it:

    {"id", "severity", "role", "page", "confidence", "source",
     "needs": <error code or None>, "warn": <warning code or None>,   # read by the web
     "label": CODES[code].label, "reasons": [labels...], "text", "bbox", "pageSize",
     "targets": [{"code", "box"}...],                                  # targets_of
     "boxes": [[x0, top, x1, bottom]...],                              # page items only
     "findings": [{"code", "severity", "id"}...],                      # one per severe fact
     "anchor": {"page", "bbox", "text_sha", "crop_sha", "hint"},       # node and figure items
     "alt", "description",                                             # figures only
     "suggestions": [{"was", "now", "rule", "bbox",                    # OCR suggestions only
                      "anchor", "before", "after", "id"}],
     "group": {"nodeIds": [...], "boxes": [...]}}                      # collapsed items only

A reviewer's dismissal (decisions_apply.apply records it in the Document's decisions ledger) removes a
finding before an item is formed: its worst fact, `needs`/`warn`, reasons, targets and suggestions
are those of the findings left, and an item with none left is not published. A decision that no
longer matches the page is an item of its own, `decision:<id>`, role "decision", with the page and
box of its anchor and no node (`_decision_item`).

A finding is one fact a reviewer can settle, named by `anchors.finding_id`: a hash of the fact and
its scope (a node's page, box and text; a page; the document), so it changes with the evidence. A
low-confidence item has the finding "low-confidence", the language item the language issue. A
group item lists its members' findings. A node's `anchor` is what a decision copies to find the
node again (docs/review-decisions.md); a suggestion carries the node's anchor, up to
CONTEXT_CHARS characters of text on each side, and an `id` (16 hex over was|now|rule|before|after).

A target is a place a reviewer must look, tagged with the code of the fact that found it. A
page item's bbox is the union of its targets (None without any) and `boxes` lists the same
boxes for readers that do not know codes; a node item's bbox stays the node's own box.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from reflow.analyze import confidence
from reflow.analyze.anchors import (CONTEXT_CHARS, DOC_SCOPE, anchor_of, finding_id, node_scope,
                                    page_scope)
from reflow.analyze.confidence import CODES, score
from reflow.model import Document, Fact, Node, Role
from reflow.analyze.language import resolve_language

ReviewItem = dict[str, Any]
ReviewTarget = dict[str, Any]   # {"code": str, "box": [x0, top, x1, bottom]}, page points

# The short names of the error codes that `needs` carries.
NEEDS_NAMES = {"alt-missing": "alt", "label-missing": "label", "ocr-missing": "ocr",
               "title-suspect": "title", "extract-forbidden": "extract"}
LOW_CONFIDENCE = "low-confidence"
# A page's place in the walk by its own item: errors, then warnings, then pages without one.
PAGE_RANK = {"error": 0, "warning": 1}
# The errors of unread figures, grouped per page and code (_collapse).
UNREAD_LABELS = {CODES[code].label for code in ("unread-handwriting", "unread-text")}


def _severity(fact: Fact) -> str | None:
    return CODES[fact.code].severity


def _worst(facts: list[Fact]) -> Fact | None:
    """The first error fact, else the first warning fact, else None."""
    for wanted in ("error", "warning"):
        for fact in facts:
            if _severity(fact) == wanted:
                return fact
    return None


def _trust(page: int, severity: str | None,
           scores: list[tuple[float, bool]]) -> tuple[int, float, float, int]:
    """Where a page sorts in the walk, least trustworthy first: the severity of its own item
    (error, warning, none), the lowest score of its nodes (1.0 without nodes), the larger share
    of its nodes below their role's threshold, then the page number. `scores` holds (score,
    below) per non-artifact node. Read from the ledger each time and never stored: a stored
    fact would be added to the scores again (confidence.score)."""
    lowest = min((value for value, _ in scores), default=1.0)
    doubtful = sum(below for _, below in scores) / len(scores) if scores else 0.0
    return PAGE_RANK.get(severity or "", len(PAGE_RANK)), lowest, -doubtful, page


def dismissed_of(doc: Document) -> frozenset[str]:
    """The fingerprints a reviewer dismissed, from the ledger decisions_apply.apply put in the model."""
    ledger = doc.decisions_ledger
    return frozenset(d["finding"] for d in ledger["dismissed"]) if ledger else frozenset()


def _live(scope: list[Any], facts: Iterable[Fact], dismissed: frozenset[str]) -> list[Fact]:
    """The facts a dismissal has not settled: every fact without a severity (it is no finding)
    and each finding whose fingerprint is not dismissed."""
    if not dismissed:
        return list(facts)
    return [f for f in facts if not _severity(f)
            or finding_id(scope, f.code, f.detail, f.data) not in dismissed]


def _findings(scope: list[Any], facts: Iterable[Fact]) -> list[dict[str, str]]:
    """One finding per fact that has a severity, in fact order."""
    return [{"code": f.code, "severity": sev, "id": finding_id(scope, f.code, f.detail, f.data)}
            for f in facts if (sev := _severity(f))]


def targets_of(facts: Iterable[Fact]) -> list[ReviewTarget]:
    """The places the facts point at, in fact order then box order: each box of a fact's
    data["region_boxes"] (stored flat, four numbers per box) and its data["box"], tagged with
    the fact's code. A box list whose length is not a multiple of four is a corrupt ledger."""
    out: list[ReviewTarget] = []
    for fact in facts:
        flat = list(fact.data.get("region_boxes") or [])
        if (box := fact.data.get("box")) is not None:
            if len(box) != 4:
                raise ValueError(f"{fact.code}: data['box'] must hold four numbers, not {box!r}")
            flat += list(box)
        if len(flat) % 4:
            raise ValueError(f"{fact.code}: data['region_boxes'] must hold four numbers per box")
        out += [{"code": fact.code, "box": flat[i:i + 4]} for i in range(0, len(flat), 4)]
    return out


def _union(targets: list[ReviewTarget]) -> list[float] | None:
    if not targets:
        return None
    boxes = [t["box"] for t in targets]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
            max(b[3] for b in boxes)]


def _compat(facts: list[Fact]) -> tuple[str | None, str | None]:
    """The `needs` and `warn` fields, from a ledger."""
    needs = next((f.code for f in facts if _severity(f) == "error"), None)
    warn = next((f.code for f in facts if _severity(f) == "warning"), None)
    return (NEEDS_NAMES.get(needs) if needs else None), warn


def queue(doc: Document) -> list[ReviewItem]:
    """confidence.apply(doc), then items(doc). The one call that scores a document: the
    pipeline, the tests and the inspector use it. `items` stays the pure reader for an already
    scored document (review.json is checked against it on the reloaded document.json)."""
    confidence.apply(doc)
    return items(doc)


def items(doc: Document) -> list[ReviewItem]:
    """Every review item of the document: document items first, then, page by page, the
    page's own item followed by its nodes' items in document order, the pages least trustworthy
    first (`_trust`). Requires confidence.apply to have run (every node has a "prior" fact); raises RuntimeError
    otherwise, so the two can't drift apart."""
    sizes = {p.number: [p.width, p.height] for p in doc.pages}
    dismissed = dismissed_of(doc)
    out: list[ReviewItem] = []

    title_lines = (doc.root.attrs.get("title_evidence") or {}).get("lines") or []
    for fact in _live(DOC_SCOPE, doc.root.facts, dismissed):
        if fact.code == ORPHANED:
            out.append(_decision_item(fact, sizes))
        elif _severity(fact):
            item = _fact_item(doc.root.id, "doc", None, None, fact, scope=DOC_SCOPE)
            if fact.code == "title-suspect" and title_lines:   # point at the line that is the title
                first = title_lines[0]
                if first.get("page") in sizes and first.get("bbox"):
                    item.update(page=first["page"], bbox=list(first["bbox"]),
                                pageSize=sizes[first["page"]])
            out.append(item)
    choice = resolve_language(doc)
    language_id = finding_id(DOC_SCOPE, choice.issue, choice.declared, choice.detected) \
        if choice.issue else ""
    if (choice.issue and not any(f.code == choice.issue for f in doc.root.facts)
            and language_id not in dismissed):
        reads = (f"the text may read as {choice.detected}, but it is too short to be sure"
                 if choice.unsure and choice.detected else
                 "the text is in the Latin script, but it is too short to say which language"
                 if choice.unsure else f"the text reads as {choice.detected}"
                 if choice.detected else "the text is in the Latin script, which it is not "
                 "written in")
        text = (f"The PDF's language is {choice.declared or 'not set'}; {reads}. "
                f"Published as {choice.tag}. Check the document language.")
        out.append({"id": doc.root.id, "severity": CODES[choice.issue].severity,
                    "role": "doc", "page": None, "confidence": 1.0, "source": "heuristic",
                    "needs": None, "warn": choice.issue, "label": CODES[choice.issue].label,
                    "reasons": [], "text": text, "bbox": None, "pageSize": None,
                    "targets": [],
                    "findings": [{"code": choice.issue, "severity": CODES[choice.issue].severity,
                                  "id": language_id}]})

    paged: list[tuple[int, int, ReviewItem]] = []   # (page, 0 page item / 1 node, item)
    page_severity: dict[int, str] = {}               # page -> its own item's severity
    for page in doc.pages:
        severe = [f for f in _live(page_scope(page.number), page.facts, dismissed) if _severity(f)]
        if severe:
            item = _fact_item(f"page:{page.number}", "page", page.number,
                              sizes[page.number], _worst(severe), severe,
                              scope=page_scope(page.number))
            item["needs"], item["warn"] = _compat(severe)
            item["reasons"] = [CODES[f.code].label for f in severe]
            paged.append((page.number, 0, item))
            page_severity[page.number] = item["severity"]

    current = 0
    scored: dict[int, list[tuple[float, bool]]] = {}   # page -> (score, below) of each node
    page_facts = {p.number: p.facts for p in doc.pages}
    for node in doc.root.walk():
        if node is doc.root or node.role == Role.ARTIFACT:
            continue
        if node.page is not None:
            current = node.page
        if not any(f.code == "prior" for f in node.facts):
            raise RuntimeError(f"node {node.id} has no prior fact: run confidence.apply first")
        result = score(node.facts, confidence.page_facts_of(node, page_facts.get(node.page, ())))
        scored.setdefault(current, []).append((result.value, result.below(node.role)))
        scope = node_scope(node)
        live = _live(scope, node.facts, dismissed)
        worst = _worst(live)
        if worst is None and not result.below(node.role):
            continue
        if worst is None and finding_id(scope, LOW_CONFIDENCE, list(result.reasons),
                                        None) in dismissed:
            continue
        paged.append((current, 1, item_for(node, sizes.get(node.page), worst,
                                           list(result.reasons), live)))
    trust = {page: _trust(page, page_severity.get(page), scored.get(page, []))
             for page in {entry[0] for entry in paged}}
    paged.sort(key=lambda entry: (trust[entry[0]], entry[1]))   # stable: nodes keep document order
    return out + _collapse([item for _, _, item in paged])


def _collapse(items: list[ReviewItem]) -> list[ReviewItem]:
    """One warning per page and reason for repeated uncertain or unreadable text, and one error
    per page and code for unread figures, whose `group.boxes` keep every crop a target.

    Keep the first node's id and box so existing inspector links still select a real node.
    The member ids let the inspector associate every other block with the same queue item.
    """
    groups: dict[tuple, list[ReviewItem]] = {}
    for item in items:
        if item["page"] is None:
            continue
        if (item["severity"] == "warning"
                and item["warn"] in (LOW_CONFIDENCE, "ocr-image-text", "unicode-errors",
                                     "ocr-doubtful", "struck-word-unread")):
            key = (item["page"], item["warn"], tuple(item["reasons"]))
            groups.setdefault(key, []).append(item)
        elif item["severity"] == "error" and item["label"] in UNREAD_LABELS:
            groups.setdefault((item["page"], item["label"]), []).append(item)
    replacements = {}
    omitted = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        first = members[0]
        unread = first["label"] in UNREAD_LABELS
        replacements[id(first)] = {
            **first,
            "confidence": min(item["confidence"] for item in members),
            "text": (f"{len(members)} {'crops' if unread else 'blocks'} on this page. "
                     f"{first['text']}").rstrip(),
            "group": {"nodeIds": [item["id"] for item in members],
                      **({"boxes": [item["bbox"] for item in members]} if unread else {})},
            "targets": [t for item in members for t in item["targets"]],
            "findings": [f for item in members for f in item["findings"]],
        }
        omitted.update(id(item) for item in members[1:])
    return [replacements.get(id(item), item) for item in items if id(item) not in omitted]


def _fact_item(item_id: str, role: str, page: int | None, size: list[float] | None,
               fact: Fact | None, facts: list[Fact] | None = None,
               scope: list[Any] = DOC_SCOPE) -> ReviewItem:
    """An item for a document fact, or for a page whose worst fact is `fact`: its targets are
    those of `facts` (all of the page's severe facts), or of `fact` alone."""
    if fact is None:
        raise ValueError('review: expected fact is not None')
    spec = CODES[fact.code]
    needs, warn = _compat([fact])
    targets = targets_of(facts if facts is not None else [fact])
    return {"id": item_id, "severity": spec.severity, "role": role, "page": page,
            "confidence": 1.0, "source": fact.source.value, "needs": needs, "warn": warn,
            "label": spec.label, "reasons": [], "text": fact.detail or spec.label,
            "bbox": _union(targets), "pageSize": size, "targets": targets,
            "boxes": [t["box"] for t in targets],
            "findings": _findings(scope, facts if facts is not None else [fact])}


def item_for(node: Node, page_size: list[float] | None, worst: Fact | None,
             reasons: list[str], facts: list[Fact] | None = None) -> ReviewItem:
    """One node's item (see the module docstring for the shape). `worst` is the fact that
    makes it an item; None means the score fell below the role's threshold. `facts` are the
    node's facts a dismissal has not settled (all of them by default)."""
    facts = node.facts if facts is None else facts
    if worst is None:
        severity, label = "warning", CODES[LOW_CONFIDENCE].label
        needs, warn = None, LOW_CONFIDENCE
    else:
        severity, label = _severity(worst), CODES[worst.code].label
        needs, warn = _compat(facts)
        if severity == "warning" and warn is None:
            warn = worst.code
    item: ReviewItem = {
        "id": node.id, "severity": severity, "role": node.role.value, "page": node.page,
        "confidence": node.confidence, "source": node.source.value, "needs": needs,
        "warn": warn, "label": label, "reasons": reasons, "text": (node.text or "")[:200],
        # Where to draw it: [x0, top, x1, bottom] in points, and the page size.
        "bbox": list(node.bbox) if node.bbox else None, "pageSize": page_size,
        "targets": targets_of(facts)}
    scope = node_scope(node)
    item["findings"] = _findings(scope, facts)
    if worst is None:
        item["findings"] = [{"code": LOW_CONFIDENCE, "severity": "warning",
                             "id": finding_id(scope, LOW_CONFIDENCE, reasons, None)}]
    anchor = anchor_of(node)
    if anchor is not None:
        item["anchor"] = anchor.to_dict()
    if node.role == Role.FIGURE:
        item["alt"] = node.attrs.get("alt")
        item["description"] = node.attrs.get("description")
    # What OCR read and what the document suggests (analyze.ocr_words), with the line to draw.
    suggestions = [_suggestion(node, f, item) for f in facts if f.code == "ocr-suggestion"]
    if suggestions:
        item["suggestions"] = suggestions
        if severity == "warning":   # the specific reason, never collapsed into a page group
            item.update(warn="ocr-suggestion", label=CODES["ocr-suggestion"].label)
    return item


ORPHANED = "decision-orphaned"


def _decision_item(fact: Fact, sizes: dict[int, list[float]]) -> ReviewItem:
    """The item of a decision that no longer matches: it has no node, only the page and the box
    its anchor named. A page this document does not have gives an item with neither."""
    data = fact.data
    page, box = data.get("page"), data.get("bbox")
    here = page in sizes and isinstance(box, list) and len(box) == 4
    targets = [{"code": fact.code, "box": list(box)}] if here else []
    spec = CODES[fact.code]
    ident = str(data.get("decision", ""))
    return {"id": f"decision:{ident}", "severity": spec.severity, "role": "decision",
            "page": page if here else None, "confidence": 1.0, "source": fact.source.value,
            "needs": None, "warn": fact.code, "label": spec.label,
            "reasons": [str(data.get("reason", ""))], "text": fact.detail or spec.label,
            "bbox": list(box) if here else None, "pageSize": sizes[page] if here else None,
            "targets": targets, "boxes": [t["box"] for t in targets],
            "findings": _findings(["decision", ident], [fact])}


def _suggestion(node: Node, fact: Fact, item: ReviewItem) -> dict[str, Any]:
    """One OCR suggestion: the reading, the proposal, the line to draw, and what a decision on it
    copies: the node's anchor, the text on each side of the word, and an id of all of those."""
    data, text = fact.data, node.text or ""
    was, now, rule = data["was"], data["now"], data["rule"]
    start, end = data["start"], data["end"]
    before, after = text[max(0, start - CONTEXT_CHARS):start], text[end:end + CONTEXT_CHARS]
    key = "|".join([was, now, rule, before, after])
    return {"was": was, "now": now, "rule": rule, "bbox": data["line_box"] or item["bbox"],
            "anchor": item.get("anchor"), "before": before, "after": after,
            "id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]}


def summary(review: list[ReviewItem]) -> dict[str, int]:
    """{"errors": n, "warnings": n, "pages": distinct pages with an item} for the CLI and
    the conversion summary."""
    return {"errors": sum(r["severity"] == "error" for r in review),
            "warnings": sum(r["severity"] == "warning" for r in review),
            "pages": len({r["page"] for r in review if r["page"] is not None})}
