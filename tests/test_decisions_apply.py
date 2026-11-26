"""Applying a reviewer's decisions to the model: identity, dismissals, orphans, the ledger,
and the corrections, keeps and alt texts that still match. Built from real Node and Document
objects and stores written by the test writer, so a record is read the way a real one is."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import decisions_kit as kit
from reflow.analyze import confidence, decisions as d, decisions_apply as da, review
from reflow.analyze.anchors import anchor_of
from reflow.analyze.ocr_words import flag
from reflow.model import Document, Fact, Node, PageInfo, Role, Source, make_id

PDF = "a1" * 32
OTHER = "b2" * 32
BOX = (72.0, 100.0, 300.0, 112.0)
ZIP = "Englewood, CO 8011O"


def build(*nodes: Node, pages: int = 3, sha: str = PDF, **meta) -> Document:
    """A scored document (confidence.apply has run), as the pass finds it in the pipeline."""
    doc = Document(root=Node(Role.DOC, children=list(nodes)),
                   pages=[PageInfo(number=n, width=612, height=792) for n in range(1, pages + 1)],
                   meta={"lang": "en-US", "sha256": sha, **meta})
    confidence.apply(doc)
    return doc


def zip_node(text: str = ZIP, box=BOX, page: int = 1) -> Node:
    return Node(Role.P, page=page, bbox=box, text=text, source=Source.OCR)


def flagged(*nodes: Node, **kw) -> Document:
    doc = Document(root=Node(Role.DOC, children=list(nodes)),
                   pages=[PageInfo(number=n, width=612, height=792) for n in range(1, 4)],
                   meta={"lang": "en-US", "sha256": PDF, **kw})
    flag(doc, None)
    confidence.apply(doc)
    return doc


def store(tmp_path: Path, *records: dict, name: str = "s.jsonl") -> d.Snapshot:
    path = tmp_path / name
    kit.new_store(path, "doc")
    for record in records:
        kit.append(path, record)
    return d.read(path)


def rec(kind: str, pdf: str = PDF, **fields) -> dict:
    return kit.record(pdf, kind, **fields)


def dismiss(*findings: str, pdf: str = PDF) -> dict:
    return rec("dismiss", pdf, findings=list(findings))


def finding_ids(doc: Document) -> dict[str, str]:
    """code -> fingerprint of every finding in the queue, per the published items."""
    return {f["code"]: f["id"] for item in review.items(doc) for f in item["findings"]}


def correction(doc: Document, now: str = "80110", pdf: str = PDF, **extra) -> dict:
    """A correction copied from the published suggestion, as the web writes it."""
    [item] = [i for i in review.items(doc) if i.get("suggestions")]
    s = item["suggestions"][0]
    return rec("correct", pdf, target={"anchor": s["anchor"], "range": {
        "was": s["was"], "before": s["before"], "after": s["after"], "rule": s["rule"]}},
        now=now, **extra)


def unchanged_but_decisions(doc: Document) -> dict:
    """The document as JSON without the decision records the pass adds (the ledger, the root's
    decision facts), to assert that an orphan changed nothing else."""
    out = json.loads(doc.to_json())
    out["meta"].pop("decisions", None)
    out["root"]["facts"] = [f for f in out["root"].get("facts", [])
                            if f["code"] not in ("decision-orphaned", "decisions-other-pdf",
                                                 "decisions-unreadable")]
    if not out["root"]["facts"]:
        out["root"].pop("facts")
    return out


# -- the ledger --------------------------------------------------------------------------------


def test_a_missing_store_gives_an_empty_ledger_and_changes_nothing_else(tmp_path):
    doc = build(zip_node())
    before = unchanged_but_decisions(doc)
    da.apply(doc, d.read(tmp_path / "none.jsonl"))
    ledger = doc.meta["decisions"]["ledger"]
    assert ledger["v"] == 1 and ledger["source"] == {"sha256": PDF}
    assert all(ledger[k] == [] for k in ("applied", "satisfied", "dismissed", "refused",
                                         "orphaned", "stale", "proposals"))
    assert ledger["store"]["id"] is None and ledger["store"]["error"] is None
    assert unchanged_but_decisions(doc) == before


def test_the_ledger_counts_what_the_store_holds(tmp_path):
    snap = store(tmp_path, rec("dismiss", findings=["f" * 16]),
                 rec("future", v=2), {"v": 1, "id": "x", "kind": "keep"})
    doc = build()
    da.apply(doc, snap)
    s = doc.meta["decisions"]["ledger"]["store"]
    assert s["id"] == snap.store and s["bytes"] == snap.bytes and s["lines"] == snap.lines
    assert s["unsupported"] == [{"id": s["unsupported"][0]["id"], "v": 2, "kind": "future"}]
    assert len(s["invalid"]) == 1


def test_a_store_that_could_not_be_read_is_one_warning(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("not a header\n", encoding="utf-8")
    doc = build()
    da.apply(doc, d.read(path))
    assert doc.meta["decisions"]["ledger"]["store"]["error"] == "no-header"
    [fact] = [f for f in doc.root.facts if f.code == "decisions-unreadable"]
    assert fact.source == Source.HUMAN
    assert [i["warn"] for i in review.items(doc)] == ["decisions-unreadable"]


def test_a_torn_tail_or_an_unreadable_line_is_the_same_warning(tmp_path):
    for junk in (b'{"v": 1, "id"', b"[1, 2]\n"):
        path = tmp_path / f"s{len(junk)}.jsonl"
        kit.new_store(path, "doc")
        with open(path, "ab") as f:
            f.write(junk)
        doc = build()
        da.apply(doc, d.read(path))
        assert [f.code for f in doc.root.facts] == ["decisions-unreadable"]


def test_the_ledger_is_written_from_explicit_fields_so_a_reviewer_name_never_reaches_it(tmp_path):
    doc = build(zip_node())
    other = rec("dismiss", OTHER, findings=["f" * 16], by="reviewer@example.com", email="x@y.z")
    future = rec("future", by="reviewer@example.com")
    future["v"] = 2
    da.apply(doc, store(tmp_path, other, future))
    text = json.dumps(doc.meta["decisions"]["ledger"])
    assert "@" not in text and '"by"' not in text and '"email"' not in text


# -- the PDF identity --------------------------------------------------------------------------


def test_a_decision_made_on_another_pdf_is_orphaned_and_the_document_warned_once(tmp_path):
    doc = flagged(zip_node())
    [fp] = [f["id"] for i in review.items(doc) for f in i["findings"]][:1]
    records = [correction(doc, pdf=OTHER), rec("keep", OTHER, target={
        "anchor": anchor_of(zip_node()).to_dict(), "range": {
            "was": "8011O", "before": "CO ", "after": "", "rule": "zip-digit"}}),
        dismiss(fp, pdf=OTHER)]
    before = unchanged_but_decisions(doc)
    da.apply(doc, store(tmp_path, *records))
    ledger = doc.meta["decisions"]["ledger"]
    assert [o["reason"] for o in ledger["orphaned"]] == ["other-pdf"] * 3
    assert ledger["applied"] == [] and ledger["dismissed"] == []
    assert [f.code for f in doc.root.facts].count("decisions-other-pdf") == 1
    assert unchanged_but_decisions(doc) == before


def test_other_pdf_orphans_are_in_the_queue_with_their_own_items(tmp_path):
    doc = flagged(zip_node())
    c = correction(doc, pdf=OTHER)
    da.apply(doc, store(tmp_path, c))
    items = review.items(doc)
    [orphan] = [i for i in items if i["role"] == "decision"]
    assert orphan["id"] == f"decision:{c['id']}" and orphan["warn"] == "decision-orphaned"
    assert orphan["severity"] == "warning" and orphan["page"] == 1
    assert orphan["bbox"] == list(BOX) and orphan["pageSize"] == [612, 792]
    assert "8011O" in orphan["text"] and "80110" in orphan["text"]
    assert orphan["reasons"] == ["other-pdf"]
    assert [i["warn"] for i in items if i["role"] == "doc"] == ["decisions-other-pdf"]


def test_an_orphan_on_a_page_the_document_lacks_still_has_an_item(tmp_path):
    doc = flagged(zip_node())
    c = correction(doc, pdf=OTHER)
    c["target"]["anchor"]["page"] = 99
    da.apply(doc, store(tmp_path, c))
    [orphan] = [i for i in review.items(doc) if i["role"] == "decision"]
    assert orphan["page"] is None and orphan["bbox"] is None and orphan["targets"] == []


def test_one_matching_record_among_twenty_others_applies(tmp_path):
    doc = build(Node(Role.P, page=1, bbox=BOX, text="Fee schedule",
                     facts=[Fact("unfinished-sentence", Source.HEURISTIC, "stops")]))
    [fp] = finding_ids(doc).values()
    many = [dismiss(f"{i:016x}", pdf=OTHER) for i in range(20)]
    da.apply(doc, store(tmp_path, *many[:10], dismiss(fp), *many[10:]))
    ledger = doc.meta["decisions"]["ledger"]
    assert [x["finding"] for x in ledger["dismissed"]] == [fp]
    assert len(ledger["orphaned"]) == 20
    assert not [i for i in review.items(doc) if i["role"] == "p"]


def test_a_reattach_makes_the_old_pdfs_records_eligible_again(tmp_path):
    doc = build(Node(Role.P, page=1, bbox=BOX, text="Fee schedule",
                     facts=[Fact("unfinished-sentence", Source.HEURISTIC, "stops")]))
    [fp] = finding_ids(doc).values()
    link = rec("reattach", PDF, **{"from": OTHER, "to": PDF})
    da.apply(doc, store(tmp_path, dismiss(fp, pdf=OTHER), link))
    ledger = doc.meta["decisions"]["ledger"]
    assert [x["finding"] for x in ledger["dismissed"]] == [fp] and ledger["orphaned"] == []
    assert not [f for f in doc.root.facts if f.code == "decisions-other-pdf"]


def test_a_document_without_a_hash_applies_nothing(tmp_path):
    doc = build(sha="")
    doc.meta.pop("sha256")
    da.apply(doc, store(tmp_path, dismiss("f" * 16)))
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == ["other-pdf"]


# -- dismissals --------------------------------------------------------------------------------


def stops(text="Fee schedule", page=1, box=BOX) -> Node:
    return Node(Role.P, page=page, bbox=box, text=text,
                facts=[Fact("unfinished-sentence", Source.HEURISTIC, "stops mid-sentence")])


def test_a_dismissed_warning_leaves_the_queue_and_the_ledger_names_it(tmp_path):
    doc = build(stops())
    [fp] = finding_ids(doc).values()
    da.apply(doc, store(tmp_path, dismiss(fp)))
    assert review.items(doc) == []
    assert doc.meta["decisions"]["dismissed"] == [fp]
    [entry] = doc.meta["decisions"]["ledger"]["dismissed"]
    assert entry["finding"] == fp and entry["code"] == "unfinished-sentence"
    assert entry["page"] == 1 and entry["id"]


def test_a_dismissal_settles_one_finding_not_the_whole_item(tmp_path):
    node = stops()
    node.fact("ocr-image-text", Source.OCR, "from an image")
    doc = build(node)
    ids = finding_ids(doc)
    da.apply(doc, store(tmp_path, dismiss(ids["unfinished-sentence"])))
    [item] = review.items(doc)
    assert item["warn"] == "ocr-image-text"
    assert [f["code"] for f in item["findings"]] == ["ocr-image-text"]
    assert item["targets"] == []


def test_a_dismissed_warning_on_a_node_with_an_error_leaves_the_error_item(tmp_path):
    node = stops()
    node.fact("dropped-ligature", Source.HEURISTIC, "missing fi")
    doc = build(node)
    ids = finding_ids(doc)
    da.apply(doc, store(tmp_path, dismiss(ids["unfinished-sentence"])))
    [item] = review.items(doc)
    assert item["severity"] == "error"


def test_an_error_cannot_be_dismissed(tmp_path):
    node = Node(Role.P, page=1, bbox=BOX, text="Fee schedule",
                facts=[Fact("dropped-ligature", Source.HEURISTIC, "missing fi")])
    doc = build(node)
    [fp] = finding_ids(doc).values()
    da.apply(doc, store(tmp_path, dismiss(fp)))
    [item] = review.items(doc)
    assert item["severity"] == "error"
    ledger = doc.meta["decisions"]["ledger"]
    assert ledger["refused"] == [{"id": ledger["refused"][0]["id"], "finding": fp,
                                  "reason": "refused-error"}]
    assert ledger["dismissed"] == [] and doc.meta["decisions"]["dismissed"] == []


def test_a_decision_warning_cannot_be_dismissed(tmp_path):
    doc = build()
    da.apply(doc, store(tmp_path, dismiss("f" * 16, pdf=OTHER)))
    [fp] = [f["id"] for i in review.items(doc) for f in i["findings"]]
    da.apply(doc, store(tmp_path, dismiss("f" * 16, pdf=OTHER), dismiss(fp), name="t.jsonl"))
    ledger = doc.meta["decisions"]["ledger"]
    assert [r["reason"] for r in ledger["refused"]] == ["refused-error"]
    assert [i["warn"] for i in review.items(doc)] == ["decisions-other-pdf"]


def test_a_finding_whose_evidence_changed_comes_back_and_the_old_dismissal_is_stale(tmp_path):
    first = build(stops())
    [fp] = finding_ids(first).values()
    again = build(Node(Role.P, page=1, bbox=BOX, text="Fee schedule",
                       facts=[Fact("unfinished-sentence", Source.HEURISTIC, "stops at a comma")]))
    snap = store(tmp_path, dismiss(fp))
    da.apply(again, snap)
    assert [i["warn"] for i in review.items(again)] == ["unfinished-sentence"]
    [entry] = again.meta["decisions"]["ledger"]["stale"]
    assert entry["finding"] == fp and entry["kind"] == "dismiss"


def test_a_page_with_two_warnings_and_one_dismissed_keeps_the_other_reason(tmp_path):
    facts = [Fact("scan-noisy", Source.HEURISTIC, "grainy"),
             Fact("ocr-band-missing", Source.HEURISTIC, "band")]
    doc = build()
    doc.pages[0].facts = facts
    ids = finding_ids(doc)
    da.apply(doc, store(tmp_path, dismiss(ids["scan-noisy"])))
    [item] = review.items(doc)
    assert item["warn"] == "ocr-band-missing" and item["reasons"] == [
        confidence.CODES["ocr-band-missing"].label]
    assert item["targets"] == [] and [f["code"] for f in item["findings"]] == ["ocr-band-missing"]


def test_a_dismissed_group_leaves_its_members_and_a_new_member_comes_back(tmp_path):
    def member(i, text=None):
        return Node(Role.P, page=1, bbox=(72.0, 100.0 + 30 * i, 300.0, 112.0 + 30 * i),
                    text=text or f"Block number {i}", source=Source.OCR,
                    facts=[Fact("ocr-image-text", Source.OCR, "from an image")])
    doc = build(member(0), member(1))
    [group] = review.items(doc)
    assert "group" in group
    da.apply(doc, store(tmp_path, dismiss(*[f["id"] for f in group["findings"]])))
    assert review.items(doc) == []
    later = build(member(0), member(1), member(2))
    da.apply(later, store(tmp_path, dismiss(*[f["id"] for f in group["findings"]]), name="t"))
    [back] = review.items(later)
    assert [f["code"] for f in back["findings"]] == ["ocr-image-text"]
    assert "group" not in back and back["bbox"][1] == 160.0


def test_a_dismissed_low_confidence_item_leaves_the_queue(tmp_path):
    table = Node(Role.TABLE, page=1, bbox=(10.0, 20.0, 110.0, 40.0), confidence=0.4)
    doc = build(table)
    [item] = review.items(doc)
    assert item["warn"] == "low-confidence"
    da.apply(doc, store(tmp_path, dismiss(item["findings"][0]["id"])))
    assert review.items(doc) == []


ENGLISH = ("the and of to is are with for that this from which have has were was be it a in "
           "on as by an at or not but all can their there would about more other into than ") * 6


def test_a_dismissed_language_item_leaves_the_queue(tmp_path):
    doc = build(Node(Role.P, page=1, text=ENGLISH), lang="es")
    [item] = review.items(doc)
    assert item["warn"] == "language-mismatch"
    da.apply(doc, store(tmp_path, dismiss(item["findings"][0]["id"])))
    assert review.items(doc) == []


def test_an_undone_dismissal_does_not_apply(tmp_path):
    doc = build(stops())
    [fp] = finding_ids(doc).values()
    first = dismiss(fp)
    da.apply(doc, store(tmp_path, first, rec("undo", of=first["id"])))
    assert [i["warn"] for i in review.items(doc)] == ["unfinished-sentence"]
    assert doc.meta["decisions"]["ledger"]["stale"] == []


# -- revert, and applying twice ----------------------------------------------------------------


def test_apply_twice_equals_apply_once(tmp_path):
    doc = build(stops())
    [fp] = finding_ids(doc).values()
    snap = store(tmp_path, dismiss(fp), dismiss("e" * 16, pdf=OTHER))
    da.apply(doc, snap)
    once = doc.to_json()
    da.apply(doc, snap)
    assert doc.to_json() == once


def test_revert_of_apply_is_the_model_it_started_from(tmp_path):
    doc = flagged(zip_node(), stops("Fee schedule", box=(72.0, 200.0, 300.0, 212.0)))
    [fp] = [f["id"] for i in review.items(doc) for f in i["findings"]
            if f["code"] == "unfinished-sentence"]
    start = doc.to_json()
    da.apply(doc, store(tmp_path, dismiss(fp), correction(doc, pdf=OTHER)))
    assert doc.to_json() != start
    da.revert(doc)
    assert doc.to_json() == start


def test_a_non_matching_decision_changes_nothing_but_its_orphan_record(tmp_path):
    doc = flagged(zip_node())
    c = correction(doc)
    c["target"]["range"]["was"] = "gone"        # the word is no longer there
    c["target"]["range"]["before"] = c["target"]["range"]["after"] = ""
    before = unchanged_but_decisions(doc)
    da.apply(doc, store(tmp_path, c))
    assert unchanged_but_decisions(doc) == before
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == ["range-missing"]
    assert [f.code for f in doc.root.facts] == ["decision-orphaned"]


# -- corrections and kept readings -------------------------------------------------------------


def suggested(text: str, was: str, now: str, *, box=BOX, page: int = 1, rule: str = "lexicon",
              nth: int = 0, **kw) -> Node:
    """An OCR node that carries what the OCR-words pass leaves: a doubt mark with the suggestion
    as its alt, and an ocr-suggestion fact for that range."""
    from reflow.model import Mark
    at = -1
    for _ in range(nth + 1):
        at = text.index(was, at + 1)
    node = Node(Role.P, page=page, bbox=box, text=text, source=Source.OCR,
                marks=[Mark(at, at + len(was), "doubt", was, now)], **kw)
    node.fact("ocr-suggestion", Source.OCR, f"OCR read ‘{was}’; the document suggests ‘{now}’",
              was=was, now=now, rule=rule, start=at, end=at + len(was), line_box=None)
    return node


def corrections(doc: Document, *nows: str, **extra) -> list[dict]:
    """One correction per suggestion of the published item, in order, as the web writes them."""
    [item] = [i for i in review.items(doc) if i.get("suggestions")]
    return [rec("correct", target={"anchor": s["anchor"], "range": {
        "was": s["was"], "before": s["before"], "after": s["after"], "rule": s["rule"]}},
        now=now, **extra) for s, now in zip(item["suggestions"], nows)]


def keeps(doc: Document) -> list[dict]:
    [item] = [i for i in review.items(doc) if i.get("suggestions")]
    return [rec("keep", target={"anchor": s["anchor"], "range": {
        "was": s["was"], "before": s["before"], "after": s["after"], "rule": s["rule"]}})
        for s in item["suggestions"]]


def general(doc_text: str = "Ge neral rules apply here") -> Document:
    node = suggested(doc_text, "Ge neral", "General")
    node.facts.append(Fact("unfinished-sentence", Source.HEURISTIC, "stops"))
    return build(node)


def test_a_correction_replaces_the_flagged_word_and_keeps_ocrs_reading_on_the_page(tmp_path):
    doc = general()
    [c] = corrections(doc, "General")
    da.apply(doc, store(tmp_path, c))
    [node] = [n for n in doc.root.walk() if n.role == Role.P]
    assert node.text == "General rules apply here" and node.source == Source.OCR
    assert [(m.kind, m.text, m.alt) for m in node.marks] == [("fix", "General", "Ge neral")]
    codes = [f.code for f in node.facts]
    assert "ocr-suggestion" not in codes and "unfinished-sentence" in codes
    [fact] = [f for f in node.facts if f.code == "reviewed-correction"]
    assert fact.source == Source.HUMAN
    assert fact.data == {"decision": c["id"], "was": "Ge neral", "now": "General",
                         "rule": "lexicon", "tier": "exact"}
    [entry] = doc.meta["decisions"]["ledger"]["applied"]
    assert entry == {"id": c["id"], "kind": "correct", "page": 1, "bbox": list(BOX),
                     "node": node.id, "was": "Ge neral", "now": "General", "tier": "exact",
                     "title_unchanged": True}


def test_the_suggestions_finding_is_settled_but_another_doubt_stays(tmp_path):
    doc = general()
    [c] = corrections(doc, "General")
    da.apply(doc, store(tmp_path, c))
    [item] = review.items(doc)
    assert item["warn"] == "unfinished-sentence" and "suggestions" not in item


def test_a_correction_may_be_the_reviewers_own_reading_of_the_span(tmp_path):
    doc = general()
    [c] = corrections(doc, "Genera1")
    da.apply(doc, store(tmp_path, c))
    [node] = [n for n in doc.root.walk() if n.role == Role.P]
    assert node.text == "Genera1 rules apply here"


def test_a_link_after_the_corrected_word_keeps_its_text(tmp_path):
    from reflow.model import Span
    node = suggested("Ge neral rules at example.com today", "Ge neral", "General")
    at = node.text.index("example.com")
    node.spans = [Span(at, at + len("example.com"), "link", "https://example.com")]
    doc = build(node)
    [c] = corrections(doc, "General")
    da.apply(doc, store(tmp_path, c))
    [link] = node.spans
    assert node.text[link.start:link.end] == "example.com"


def test_the_line_that_holds_the_word_is_edited_in_the_lines_attribute(tmp_path):
    node = suggested("1 Main St Ge neral Hospital", "Ge neral", "General",
                     attrs={"lines": ["1 Main St", "Ge neral Hospital"]})
    doc = build(node)
    [c] = corrections(doc, "General")
    da.apply(doc, store(tmp_path, c))
    assert node.attrs["lines"] == ["1 Main St", "General Hospital"]
    assert node.text == " ".join(node.attrs["lines"])


def test_two_adjacent_corrections_in_one_node_both_apply(tmp_path):
    text = "Ge neral Hospita1 here"
    node = suggested(text, "Ge neral", "General")
    from reflow.model import Mark
    at = text.index("Hospita1")
    node.marks.append(Mark(at, at + 8, "doubt", "Hospita1", "Hospital"))
    node.marks.sort(key=lambda m: m.start)
    node.fact("ocr-suggestion", Source.OCR, "second", was="Hospita1", now="Hospital",
              rule="lexicon", start=at, end=at + 8, line_box=None)
    doc = build(node)
    cs = corrections(doc, "General", "Hospital")
    da.apply(doc, store(tmp_path, *cs))
    assert node.text == "General Hospital here"
    assert [(m.kind, m.text, m.alt) for m in node.marks] == [
        ("fix", "General", "Ge neral"), ("fix", "Hospital", "Hospita1")]
    assert len(doc.meta["decisions"]["ledger"]["applied"]) == 2


def test_a_shorter_first_correction_does_not_move_the_second(tmp_path):
    # the edits are built from the last range to the first, so offsets stay valid
    text = "Ge  neral and Hospita1"
    node = suggested(text, "Ge  neral", "General")
    from reflow.model import Mark
    at = text.index("Hospita1")
    node.marks.append(Mark(at, at + 8, "doubt", "Hospita1", "Hospital"))
    node.fact("ocr-suggestion", Source.OCR, "second", was="Hospita1", now="Hospital",
              rule="lexicon", start=at, end=at + 8, line_box=None)
    doc = build(node)
    da.apply(doc, store(tmp_path, *corrections(doc, "General", "Hospital")))
    assert node.text == "General and Hospital"
    assert [m.text for m in node.marks if m.kind == "fix"] == ["General", "Hospital"]


def test_two_overlapping_corrections_are_both_conflicts_and_change_nothing(tmp_path):
    text = "Ge neral rules"
    node = suggested(text, "Ge neral", "General")
    doc = build(node)
    [a] = corrections(doc, "General")
    b = json.loads(json.dumps(a))
    b["id"] = kit.mint_id()
    b["target"]["range"]["before"] = ""             # a different family, the same flagged span
    b["target"]["range"]["after"] = ""
    b["now"] = "Gen eral"
    before = unchanged_but_decisions(doc)
    da.apply(doc, store(tmp_path, a, b))
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == ["conflict"] * 2
    assert unchanged_but_decisions(doc) == before


def test_text_that_already_reads_the_correction_is_satisfied(tmp_path):
    doc = general()
    [c] = corrections(doc, "General")
    fresh = build(Node(Role.P, page=1, bbox=BOX, text="General rules apply here",
                       source=Source.OCR))
    before = unchanged_but_decisions(fresh)
    da.apply(fresh, store(tmp_path, c))
    ledger = fresh.meta["decisions"]["ledger"]
    assert [(s["id"], s["page"], s["now"]) for s in ledger["satisfied"]] == [(c["id"], 1, "General")]
    assert ledger["applied"] == [] and ledger["orphaned"] == []
    assert unchanged_but_decisions(fresh) == before


@pytest.mark.parametrize("now", ["x" * 201, "line\nbreak", "bidi‮word", "tab\there", "x" * 40])
def test_a_now_over_the_limits_is_invalid_now(tmp_path, now):
    doc = general()
    [c] = corrections(doc, now)
    before = unchanged_but_decisions(doc)
    da.apply(doc, store(tmp_path, c))
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == ["invalid-now"]
    assert unchanged_but_decisions(doc) == before


def test_a_correction_applied_twice_is_applied_once(tmp_path):
    doc = general()
    [c] = corrections(doc, "General")
    snap = store(tmp_path, c)
    da.apply(doc, snap)
    once = doc.to_json()
    da.apply(doc, snap)
    assert doc.to_json() == once
    [node] = [n for n in doc.root.walk() if n.role == Role.P]
    assert [f.code for f in node.facts].count("reviewed-correction") == 1
    assert [m.kind for m in node.marks] == ["fix"]


def test_revert_restores_a_corrected_model(tmp_path):
    node = suggested("1 Main St Ge neral Hospital", "Ge neral", "General",
                     attrs={"lines": ["1 Main St", "Ge neral Hospital"], "italic": False})
    doc = build(node, Node(Role.H1, page=1, text="Plain", bbox=(1.0, 1.0, 2.0, 2.0)))
    doc.root.attrs["title"] = "Plain"
    start = doc.to_json()
    [c] = corrections(doc, "General")
    da.apply(doc, store(tmp_path, c))
    assert doc.to_json() != start and node.attrs["undecided"]["text"] == "1 Main St Ge neral Hospital"
    da.revert(doc)
    assert doc.to_json() == start


@pytest.mark.parametrize("origin,heading,updated", [
    ("heading", True, True), ("docling", True, True), ("meta", True, False),
    ("heading", False, False)])
def test_the_title_follows_a_correction_of_the_heading_it_came_from(tmp_path, origin, heading,
                                                                      updated):
    node = suggested("Ge neral Report", "Ge neral", "General")
    doc = build(node)
    doc.root.attrs["title"] = "Ge neral Report" if heading else "Another Title"
    doc.root.facts.append(Fact("title", Source.HEURISTIC, "from the heading", {"origin": origin}))
    [c] = corrections(doc, "General")
    da.apply(doc, store(tmp_path, c))
    [entry] = doc.meta["decisions"]["ledger"]["applied"]
    assert entry["title_unchanged"] is (not updated)
    assert doc.root.attrs["title"] == ("General Report" if updated else
                                       ("Ge neral Report" if heading else "Another Title"))
    if updated:
        assert doc.root.attrs["undecided_title"] == "Ge neral Report"
    else:
        assert "undecided_title" not in doc.root.attrs
    da.revert(doc)
    assert doc.root.attrs["title"] == ("Ge neral Report" if heading else "Another Title")


def test_a_keep_removes_the_doubt_and_the_suggestion_and_leaves_the_text(tmp_path):
    doc = general()
    [k] = keeps(doc)
    da.apply(doc, store(tmp_path, k))
    [node] = [n for n in doc.root.walk() if n.role == Role.P]
    assert node.text == "Ge neral rules apply here" and node.marks == []
    codes = [f.code for f in node.facts]
    assert "ocr-suggestion" not in codes and "unfinished-sentence" in codes
    [fact] = [f for f in node.facts if f.code == "reviewed-kept"]
    assert fact.source == Source.HUMAN
    assert fact.data == {"decision": k["id"], "was": "Ge neral", "rule": "lexicon"}
    [entry] = doc.meta["decisions"]["ledger"]["applied"]
    assert entry["kind"] == "keep" and entry["was"] == "Ge neral"


def test_a_keep_after_a_correction_of_the_same_range_replaces_it(tmp_path):
    doc = general()
    [c] = corrections(doc, "General")
    k = json.loads(json.dumps(c))
    k.update(id=kit.mint_id(), kind="keep")
    del k["now"]
    da.apply(doc, store(tmp_path, c, k))
    [node] = [n for n in doc.root.walk() if n.role == Role.P]
    assert node.text == "Ge neral rules apply here"
    assert [f.code for f in node.facts if f.code.startswith("reviewed")] == ["reviewed-kept"]


@pytest.mark.parametrize("reason", ["no-node", "ambiguous", "range-missing", "not-ocr"])
def test_a_decision_that_does_not_match_exactly_one_target_never_changes_the_output(
        tmp_path, reason):
    doc = general()
    [c] = corrections(doc, "General")
    if reason == "no-node":
        c["target"]["anchor"]["page"] = 2
    elif reason == "range-missing":
        c["target"]["range"]["after"] = " nowhere"
    elif reason == "ambiguous":
        twin = suggested("Ge neral rules apply here", "Ge neral", "General")
        doc.root.children.append(twin)
        confidence.apply(doc)
    else:
        doc.root.children[0].source = Source.HEURISTIC
    before = unchanged_but_decisions(doc)
    da.apply(doc, store(tmp_path, c))
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == [reason]
    assert unchanged_but_decisions(doc) == before
    [item] = [i for i in review.items(doc) if i["role"] == "decision"]
    assert item["id"] == f"decision:{c['id']}" and item["reasons"] == [reason]


# -- alt text ----------------------------------------------------------------------------------


def drafted_figure(sha: str = "c" * 16) -> Node:
    fig = Node(Role.FIGURE, page=2, bbox=(50.0, 60.0, 250.0, 200.0), source=Source.LLM,
               attrs={"alt": "A bar chart", "provenance": "ai-drafted", "kind": "chart",
                      "description": "Bars of rising height", "warn": "ai-drafted",
                      "crop_sha": sha})
    for code in ("ai-drafted", "suggested-alt", "suggested-description", "alt-missing"):
        fig.fact(code, Source.LLM, "x")
    fig.fact("tag-alt", Source.TAGS, "kept")
    return fig


def alt_record(fig: Node, text: str = "Sales rose each quarter", decorative: bool = False,
               pdf: str = PDF) -> dict:
    return rec("alt", pdf, target={"anchor": anchor_of(fig).to_dict()},
               alt={"text": text, "decorative": decorative})


def test_a_reviewers_alt_replaces_the_drafts_and_every_trace_of_it(tmp_path):
    fig = drafted_figure()
    doc = build(fig)
    a = alt_record(fig)
    da.apply(doc, store(tmp_path, a))
    assert fig.attrs["alt"] == "Sales rose each quarter" and fig.attrs["decorative"] is False
    assert fig.attrs["provenance"] == "reviewer" and fig.attrs["reviewed"] is True
    for key in ("needs", "warn", "description", "kind"):
        assert key not in fig.attrs
    assert [f.code for f in fig.facts if f.code not in ("prior", "reviewed-alt")] == ["tag-alt"]
    [fact] = [f for f in fig.facts if f.code == "reviewed-alt"]
    assert fact.source == Source.HUMAN
    assert fact.data == {"decision": a["id"], "was": "A bar chart", "was_by": "ai-drafted",
                         "was_kind": "chart"}
    assert fig.source == Source.HUMAN
    [entry] = doc.meta["decisions"]["ledger"]["applied"]
    assert entry["kind"] == "alt" and entry["node"] == fig.id and entry["now"] == a["alt"]["text"]
    assert [i for i in review.items(doc) if i["role"] == "figure"] == []


def test_a_decorative_alt_is_empty(tmp_path):
    fig = drafted_figure()
    doc = build(fig)
    da.apply(doc, store(tmp_path, alt_record(fig, text="", decorative=True)))
    assert fig.attrs["alt"] == "" and fig.attrs["decorative"] is True
    from reflow.render.alt import figure_alt
    resolved = figure_alt(fig, "2")
    assert (resolved.text, resolved.source) == ("", "reviewer")


def test_an_alt_for_a_changed_picture_is_figure_changed_and_the_draft_stays(tmp_path):
    fig = drafted_figure()
    doc = build(fig)
    a = alt_record(fig)
    fig.attrs["crop_sha"] = "d" * 16
    before = unchanged_but_decisions(doc)
    da.apply(doc, store(tmp_path, a))
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == ["figure-changed"]
    assert unchanged_but_decisions(doc) == before
    [item] = [i for i in review.items(doc) if i["role"] == "decision"]
    assert item["page"] == 2 and item["bbox"] == [50.0, 60.0, 250.0, 200.0]


def test_two_equal_crops_on_one_page_make_an_alt_ambiguous(tmp_path):
    fig = drafted_figure()
    twin = drafted_figure()
    twin.bbox = (300.0, 60.0, 500.0, 200.0)
    twin.id = make_id(twin.page, twin.bbox, twin.text)
    doc = build(fig, twin)
    da.apply(doc, store(tmp_path, alt_record(fig)))
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == ["ambiguous"]
    assert fig.attrs["provenance"] == "ai-drafted"


def test_revert_restores_a_figure_a_reviewer_described(tmp_path):
    fig = drafted_figure()
    doc = build(fig)
    start = doc.to_json()
    da.apply(doc, store(tmp_path, alt_record(fig)))
    da.apply(doc, store(tmp_path, alt_record(fig), name="again.jsonl"))
    da.revert(doc)
    assert doc.to_json() == start


def test_a_suggestion_the_reviewer_has_not_answered_still_points_at_its_words(tmp_path):
    from reflow.model import Mark
    text = "Ge neral and Hospita1 here"
    node = suggested(text, "Ge neral", "General")
    at = text.index("Hospita1")
    node.marks.append(Mark(at, at + 8, "doubt", "Hospita1", "Hospital"))
    node.fact("ocr-suggestion", Source.OCR, "second", was="Hospita1", now="Hospital",
              rule="lexicon", start=at, end=at + 8, line_box=None)
    doc = build(node)
    [first, _] = corrections(doc, "General", "Hospital")
    da.apply(doc, store(tmp_path, first))
    [item] = [i for i in review.items(doc) if i.get("suggestions")]
    [s] = item["suggestions"]
    assert (s["was"], s["before"], s["after"]) == ("Hospita1", "General and ", " here")
    assert [(m.kind, m.text) for m in node.marks] == [("fix", "General"), ("doubt", "Hospita1")]


# -- on the page, and in Ask -------------------------------------------------------------------


def rendered(doc: Document, tmp_path: Path, **kw) -> str:
    from reflow.render.site import render_html
    render_html(doc, "Report", tmp_path / "site", **{"page_widths": (800,), **kw})
    return (tmp_path / "site" / "index.html").read_text(encoding="utf-8")


def test_a_corrected_word_renders_with_ocrs_reading_and_the_page_says_so(tmp_path):
    doc = build(suggested("Englewood, CO 8011O", "8011O", "80110", rule="zip-digit"))
    [c] = corrections(doc, "80110")
    da.apply(doc, store(tmp_path, c))
    html = rendered(doc, tmp_path)
    assert ('<span class="reviewed-fix" data-was="8011O" title="Corrected by a reviewer. '
            'OCR read: 8011O">80110</span>') in html
    note = ('<p class="review-note" role="note" data-generated>A reviewer corrected 1 word on '
            'this page. OCR read ‘8011O’; the page shows ‘80110’.</p>')
    assert note in html
    assert html.index('<p class="pg-label">') < html.index(note) < html.index("Englewood")


def test_the_page_note_lists_five_words_then_a_count_and_only_on_its_page(tmp_path):
    words = [("Hospita1", "Hospital"), ("Genera1", "General"), ("Servic3", "Service"),
             ("Offic3", "Office"), ("Bo4rd", "Board"), ("Repor7", "Report"), ("Budge7", "Budget")]
    nodes = [suggested(f"The {was} of all", was, now, rule="lexicon",
                       box=(72.0, 100.0 + 30 * i, 300.0, 112.0 + 30 * i))
             for i, (was, now) in enumerate(words)]
    other = Node(Role.P, page=2, bbox=BOX, text="Nothing changed here.")
    doc = build(*nodes, other)
    cs = [c for n in nodes for c in
          [rec("correct", target={"anchor": anchor_of(n).to_dict(), "range": {
              "was": n.marks[0].text, "before": "The ", "after": " of all", "rule": "lexicon"}},
              now=n.marks[0].alt)]]
    da.apply(doc, store(tmp_path, *cs))
    html = rendered(doc, tmp_path)
    [note] = re.findall(r'<p class="review-note"[^>]*>(.*?)</p>', html)
    assert note.startswith("A reviewer corrected 7 words on this page. OCR read ‘Hospita1’, "
                           "‘Genera1’, ‘Servic3’, ‘Offic3’, ‘Bo4rd’ and 2 more; the page shows "
                           "‘Hospital’, ‘General’, ‘Service’, ‘Office’, ‘Board’ and 2 more.")
    assert html.count("review-note") == 1 and html.index("review-note") < html.index('data-page="2"')


def test_a_page_with_no_correction_has_no_note(tmp_path):
    doc = build(Node(Role.P, page=1, bbox=BOX, text="Plain text."))
    assert "review-note" not in rendered(doc, tmp_path)


def test_a_reviewers_alt_is_marked_and_has_no_ai_badge(tmp_path):
    fig = drafted_figure()
    doc = build(fig)
    da.apply(doc, store(tmp_path, alt_record(fig)))
    html = rendered(doc, tmp_path, figures={fig.id: ("p0002-1.webp", 200, 140)})
    assert 'alt="Sales rose each quarter"' in html and 'data-alt-by="reviewer"' in html
    assert "ai-badge" not in html and "AI-drafted" not in html and "Bars of rising" not in html


def test_the_figure_resolver_gives_the_reviewer_source_first(tmp_path):
    from reflow.render.alt import figure_alt
    fig = drafted_figure()
    doc = build(fig)
    assert figure_alt(fig, "2").source == "ai-draft"
    da.apply(doc, store(tmp_path, alt_record(fig)))
    resolved = figure_alt(fig, "2")
    assert (resolved.source, resolved.text) == ("reviewer", "Sales rose each quarter")


def test_ask_quotes_the_corrected_word_with_its_range_but_never_a_reviewers_alt(tmp_path):
    from reflow.render.passages import passages
    node = suggested("Englewood, CO 8011O", "8011O", "80110", rule="zip-digit")
    fig = drafted_figure()
    plain = Node(Role.P, page=1, bbox=(72.0, 300.0, 300.0, 312.0), text="Nothing changed")
    doc = build(node, fig, plain)
    [c] = corrections(doc, "80110")
    da.apply(doc, store(tmp_path, c, alt_record(fig)))
    got = list(passages(doc))
    assert [p.text for p in got] == ["Englewood, CO 80110", "Nothing changed"]
    corrected = got[0]
    assert corrected.fixed == ((14, 19, "8011O"),)
    assert corrected.to_line().count('"f":[[14,19,"8011O"]]') == 1
    assert '"f"' not in got[1].to_line()
    assert corrected.text[14:19] == "80110"


def test_the_passage_range_is_inside_the_stripped_text(tmp_path):
    from reflow.render.passages import passages
    node = suggested("  Englewood, CO 8011O", "8011O", "80110", rule="zip-digit")
    doc = build(node)
    [c] = corrections(doc, "80110")
    da.apply(doc, store(tmp_path, c))
    [p] = passages(doc)
    assert p.text == "Englewood, CO 80110" and p.fixed == ((14, 19, "8011O"),)


def test_a_correction_moves_no_other_mark_or_link_of_the_node(tmp_path):
    from reflow.model import Mark, Span
    text = "cat cat cat cat"
    node = Node(Role.P, page=1, bbox=BOX, text=text, source=Source.OCR,
                marks=[Mark(4, 7, "doubt", "cat", "dog"), Mark(8, 11, "doubt", "cat")],
                spans=[Span(12, 15, "link", "https://x.test")])
    doc = build(node)
    snap = store(tmp_path, rec("correct", target={"anchor": anchor_of(node).to_dict(), "range": {
        "was": "cat", "before": "cat ", "after": " cat cat", "rule": "split-word"}}, now="dog"))
    da.apply(doc, snap)
    assert node.text == "cat dog cat cat"
    assert [(m.start, m.end, m.kind) for m in node.marks] == [(4, 7, "fix"), (8, 11, "doubt")]
    assert [(s.start, s.end, s.kind) for s in node.spans] == [(12, 15, "link")]


def test_an_empty_correction_is_invalid_and_changes_nothing(tmp_path):
    doc = flagged(zip_node())
    before = unchanged_but_decisions(doc)
    da.apply(doc, store(tmp_path, correction(doc, now="")))
    assert unchanged_but_decisions(doc) == before
    assert [o["reason"] for o in doc.meta["decisions"]["ledger"]["orphaned"]] == ["invalid-now"]


def test_a_kept_range_beside_a_correction_keeps_its_links_where_they_are(tmp_path):
    from reflow.model import Mark, Span
    text = "cat cat cat cat"
    node = Node(Role.P, page=1, bbox=BOX, text=text, source=Source.OCR,
                marks=[Mark(0, 7, "doubt", "cat cat"), Mark(8, 11, "doubt", "cat", "dog")],
                spans=[Span(0, 3, "link", "https://a.test"), Span(4, 7, "link", "https://b.test")])
    doc = build(node)
    anchor = anchor_of(node).to_dict()
    snap = store(tmp_path,
                 rec("keep", target={"anchor": anchor, "range": {
                     "was": "cat cat", "before": "", "after": " cat cat", "rule": "x"}}),
                 rec("correct", target={"anchor": anchor, "range": {
                     "was": "cat", "before": "cat cat ", "after": " cat", "rule": "x"}}, now="dog"))
    da.apply(doc, snap)
    assert node.text == "cat cat dog cat"
    assert [(s.start, s.end) for s in node.spans] == [(0, 3), (4, 7)]
    node.check_inline()
