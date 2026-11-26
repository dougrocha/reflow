"""Page vision: a local vision model's answers become proposals, and only a reviewer's
accept lets one change the output. Nothing here loads a model: the provider is a stub or the
cache is replayed. These test the contract (closed answers, proposals, review, the text check),
not what a model says."""

import json
from pathlib import Path

import pytest
from conftest import make_scan_pdf, make_text_pdf
from typer.testing import CliRunner

from reflow.analyze import page_vision as pv
from reflow.analyze import proposals as pr
from reflow.analyze.decisions import Verdict
from reflow.analyze import review
from reflow.analyze import vision_gates as vg
from reflow.analyze.confidence import CODES
from reflow.analyze.sections import nest_sections
from reflow.cli import app
from reflow.convert import convert
from reflow.model import Document, Fact, Mark, Node, PageInfo, PageKind, Role, Source
from reflow.names import ASSIST_CACHE, PROPOSAL_DECISIONS_JSON, PROPOSALS_JSON, WORKING_PDF
from reflow.run import Options
from testkit import para


def head(text, box, role=Role.H2, page=1):
    return Node(role, page=page, bbox=box, text=text)


def document(*nodes):
    return Document(Node(Role.DOC, children=nest_sections(list(nodes))),
                    pages=[PageInfo(1, 612, 792)], meta={"sha256": "abc"})


def sample():
    return document(head("Report", (72, 40, 300, 60), Role.H1),
                    head("Introduction", (72, 80, 300, 96)),
                    para("This report covers the year.", (72, 100, 500, 112)),
                    head("Form 12-B rev 3", (72, 130, 300, 142)),
                    para("Results", (72, 160, 200, 172)),
                    para("Sales rose in every region.", (72, 180, 500, 192)))


def question(doc):
    return pv.question_for(doc, doc.pages[0])


def ids_by_text(q):
    return {b.snippet: b.id for b in q.blocks}


CALM = {c.value: False for c in pv.Complexity}   # no complexity flag


def reply(blocks, *flags):
    """A well-formed answer: these block words, and `flags` the page's complexity."""
    return {"blocks": blocks, "complexity": {**CALM, **{f: True for f in flags}}}


# -- the answer is parsed once, and anything outside the closed vocabulary rejects it ------------


def test_a_well_formed_answer_parses_to_block_ids_and_fixed_words():
    q = question(sample())
    b = ids_by_text(q)
    answer = pv.parse_answer(q, reply([{"id": b["Introduction"], "role": "heading"},
                                            {"id": b["Results"], "role": "heading"}]))
    assert answer.roles == {b["Introduction"]: pv.Word.HEADING, b["Results"]: pv.Word.HEADING}


@pytest.mark.parametrize("bad", [
    {**reply([{"id": "b2", "role": "heading"}]), "note": "Results is a heading"},   # extra key
    reply([{"id": "b2", "role": "Heading"}]),                 # not a word of the list
    reply([{"id": "b2", "role": "a section title"}]),         # free text
    reply([{"id": "b99", "role": "heading"}]),                # an id that was not offered
    reply([{"id": "b2", "role": "heading", "text": "Results"}]),   # a rewrite attempt
    reply([{"id": "b2", "role": "heading"}, {"id": "b2", "role": "paragraph"}]),
    reply([{"id": 2, "role": "heading"}]),
    reply("b2 heading"),
    reply([["b2", "heading"]]),
    {"blocks": [{"id": "b2", "role": "heading"}]},            # no complexity
    {"blocks": [], "complexity": {**CALM, "diagram": "yes"}},     # a flag that is not a bool
    {"blocks": [], "complexity": {**CALM, "diagram": 1}},         # 1 is not true
    {"blocks": [], "complexity": {"diagram": True}},              # a flag left out
    {"blocks": [], "complexity": {**CALM, "busy": True}},         # a flag not in the list
    {"blocks": [], "complexity": ["diagram"]},
    {},
    ["b2", "heading"],
    "heading",
])
def test_any_other_string_key_or_shape_rejects_the_whole_answer(bad):
    q = question(sample())
    with pytest.raises(pv.AnswerRejected):
        pv.parse_answer(q, bad)


def test_a_rejected_answer_keeps_none_of_its_valid_parts():
    q = question(sample())
    b = ids_by_text(q)
    with pytest.raises(pv.AnswerRejected):
        pv.parse_answer(q, reply([{"id": b["Results"], "role": "heading"},
                                  {"id": b["Introduction"], "role": "subtitle"}], "diagram"))


def test_the_schema_the_model_answers_in_has_no_free_string():
    q = question(sample())

    def strings(node):
        if isinstance(node, dict):
            if node.get("type") == "string":
                yield node
            for v in node.values():
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)

    found = list(strings(q.schema()))
    assert found and all("enum" in s for s in found)


# -- answers become proposals; the document is left alone ----------------------------------------


def test_heading_disagreements_become_proposals_and_change_nothing():
    doc = sample()
    before = doc.to_json()
    q = question(doc)
    b = ids_by_text(q)
    answer = pv.parse_answer(q, reply([
        {"id": b["Report"], "role": "paragraph"},             # the title (h1) is never proposed
        {"id": b["Introduction"], "role": "heading"},         # agrees
        {"id": b["Form 12-B rev 3"], "role": "label"},        # a heading it reads as a label
        {"id": b["Results"], "role": "heading"},              # a paragraph it reads as a heading
        {"id": b["Sales rose in every region."], "role": "list"}]))   # not about headings
    got = pv.proposals_for(doc, q, answer, model="stub")
    assert sorted((p.change, p.was, p.word, p.text) for p in got) == [
        (pr.Change.DEMOTE_HEADING, Role.H2, pv.Word.LABEL, "Form 12-B rev 3"),
        (pr.Change.PROMOTE_HEADING, Role.P, pv.Word.HEADING, "Results")]
    assert all(p.document_text == pr.text_digest(doc) for p in got)
    assert doc.to_json() == before


def test_a_promotion_a_check_rejects_is_not_proposed_and_is_listed(monkeypatch):
    # legislation p9's `court.` and appeal p1's form identifier, as the page-vision model read them
    doc = document(para("BOE-305-AH (P1) REV. 12 (05-24)", (72, 40, 300, 52)),
                   para("a writ of garnishment returnable to the clerk's", (72, 60, 500, 72)),
                   para("court.", (72, 80, 120, 92)),
                   para("Results", (72, 100, 200, 112)))
    q = question(doc)
    answer = pv.parse_answer(q, reply([{"id": b.id, "role": "heading"} for b in q.blocks
                                       if b.snippet != "a writ of garnishment returnable to the clerk's"]))
    gated: list[pv.Gated] = []
    got = pv.proposals_for(doc, q, answer, model="stub", gated=gated)
    assert [p.text for p in got] == ["Results"]
    assert [(g.text, g.gates) for g in gated] == [
        ("BOE-305-AH (P1) REV. 12 (05-24)", (vg.Gate.FORM_ID,)),
        ("court.", (vg.Gate.CONTINUATION,))]
    monkeypatch.setattr(vg, "ON", frozenset())   # a check that is off rejects nothing
    assert len(pv.proposals_for(doc, q, answer, model="stub")) == 3


def test_a_proposal_id_is_stable_across_runs_and_names_its_change():
    a = pr.Proposal.make(pr.Change.DEMOTE_HEADING, "n1", 1, Role.H2, pv.Word.LABEL, "x",
                         pr.TextDigest("d1"), "m")
    b = pr.Proposal.make(pr.Change.DEMOTE_HEADING, "n1", 1, Role.H2, pv.Word.NOISE, "x",
                         pr.TextDigest("d2"), "m")
    c = pr.Proposal.make(pr.Change.PROMOTE_HEADING, "n1", 1, Role.H2, pv.Word.HEADING, "x",
                         pr.TextDigest("d1"), "m")
    assert a.id == b.id != c.id


# -- only a reviewer's accept makes an applicable proposal ---------------------------------------


def demotion(doc):
    q = question(doc)
    b = ids_by_text(q)
    answer = pv.parse_answer(q, reply([{"id": b["Form 12-B rev 3"], "role": "label"}]))
    (proposal,) = pv.proposals_for(doc, q, answer, model="stub")
    return proposal


def test_an_accepted_proposal_cannot_be_made_without_a_decision():
    proposal = demotion(sample())
    with pytest.raises(TypeError):
        pr.Accepted(proposal, proposal.document_text, object())
    with pytest.raises(TypeError):
        pr.Accepted(proposal, proposal.document_text)   # type: ignore[call-arg]


def test_review_sorts_proposals_by_decision():
    doc = sample()
    proposal = demotion(doc)
    accept = pr.Decisions({proposal.id: pr.Decided(Verdict.ACCEPT, proposal.document_text)})
    reject = pr.Decisions({proposal.id: pr.Decided(Verdict.REJECT, proposal.document_text)})
    assert pr.review([proposal], pr.Decisions({})).accepted == []
    assert pr.review([proposal], reject).accepted == []
    assert pr.review([proposal], reject).status[proposal.id] is pr.Status.REJECTED
    assert pr.review([proposal], pr.Decisions({})).status[proposal.id] is pr.Status.PENDING
    (accepted,) = pr.review([proposal], accept).accepted
    assert pr.review([proposal], accept).status == {}
    assert accepted.proposal is proposal


def test_an_accepted_proposal_applies_only_when_the_text_is_unchanged():
    doc = sample()
    proposal = demotion(doc)
    text = pr.text_digest(doc)
    (accepted,) = pr.review([proposal], pr.Decisions(
        {proposal.id: pr.Decided(Verdict.ACCEPT, text)})).accepted
    assert pr.apply(doc, accepted) is pr.Status.APPLIED
    node = next(n for n in doc.root.walk() if n.text == "Form 12-B rev 3")
    assert node.role == Role.P
    assert [f.code for f in node.facts] == ["vision-accepted"]
    assert pr.text_digest(doc) == text


def test_an_accepted_proposal_on_changed_text_is_stale_and_changes_nothing():
    doc = sample()
    proposal = demotion(doc)
    (accepted,) = pr.review([proposal], pr.Decisions(
        {proposal.id: pr.Decided(Verdict.ACCEPT, proposal.document_text)})).accepted
    para_node = next(n for n in doc.root.walk() if n.text == "This report covers the year.")
    para_node.set_text("This report covers the year 2025.")   # OCR read the page differently
    before = doc.to_json()
    assert pr.apply(doc, accepted) is pr.Status.STALE
    assert doc.to_json() == before


def test_an_accepted_proposal_whose_block_changed_role_is_stale():
    doc = sample()
    proposal = demotion(doc)
    (accepted,) = pr.review([proposal], pr.Decisions(
        {proposal.id: pr.Decided(Verdict.ACCEPT, proposal.document_text)})).accepted
    next(n for n in doc.root.walk() if n.text == "Form 12-B rev 3").role = Role.P
    assert pr.apply(doc, accepted) is pr.Status.STALE


def test_a_promotion_takes_the_level_of_the_heading_before_it():
    doc = sample()
    q = question(doc)
    b = ids_by_text(q)
    answer = pv.parse_answer(q, reply([{"id": b["Results"], "role": "heading"}]))
    (proposal,) = pv.proposals_for(doc, q, answer, model="stub")
    (accepted,) = pr.review([proposal], pr.Decisions(
        {proposal.id: pr.Decided(Verdict.ACCEPT, proposal.document_text)})).accepted
    assert pr.apply(doc, accepted) is pr.Status.APPLIED
    sections = [c for c in doc.root.children if c.role == Role.SECTION]
    assert [s.children[0].text for s in sections] == ["Introduction", "Form 12-B rev 3",
                                                       "Results"]
    assert sections[-1].children[0].role == Role.H2


# -- the decisions file is parsed at its boundary too ---------------------------------------------


def test_decisions_round_trip_and_reject_anything_else(tmp_path):
    path = tmp_path / "d.json"
    decided = pr.Decisions({pr.ProposalId("pv-1"): pr.Decided(Verdict.ACCEPT,
                                                              pr.TextDigest("a" * 64))})
    pr.save_decisions(path, decided)
    assert pr.load_decisions(path) == decided
    assert pr.load_decisions(tmp_path / "missing.json") == pr.Decisions({})
    for bad in ({"pv-1": {"verdict": "yes", "text": "a" * 64}},
                {"pv-1": {"verdict": "accept"}},
                {"pv-1": {"verdict": "accept", "text": "a" * 64, "note": "looks right"}},
                ["pv-1"]):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            pr.load_decisions(path)


def test_options_check_the_cap():
    with pytest.raises(ValueError):
        Options(assist_page_vision_max_pages=-1)


# -- the pass end to end, with a stub model ------------------------------------------------------


class StubProvider:
    """Calls every heading a label and every paragraph a heading. Not a model."""
    cache_id = "stub-vision"

    def __init__(self):
        self.calls = 0

    def check(self, question, image):
        self.calls += 1
        assert image.startswith(b"\x89PNG")
        return reply([{"id": b.id, "role": "label" if b.role == "heading" else "heading"}
                      for b in question.blocks])


def make_pdf(path: Path) -> Path:
    return make_text_pdf(path, [[
        (72, 60, 22, "Annual Report", "B"),
        (72, 120, 15, "Background", "B"),
        (72, 150, 11, "The council met four times this year to review the budget and the plan."),
        (72, 166, 11, "Each meeting was open to the public and recorded in the minutes kept."),
        (72, 210, 15, "Findings", "B"),
        (72, 240, 11, "Spending stayed within the budget, and the reserve grew by a small amount."),
        (72, 256, 11, "No new borrowing was needed, and every project finished within its year."),
    ]])


IGNORED = {WORKING_PDF, ASSIST_CACHE, PROPOSALS_JSON, PROPOSAL_DECISIONS_JSON}


VISION_FACTS = {"vision-block", "vision-complexity", "vision-dense-tables"}


def _strip(value):
    """`value` without the facts the pass records for review, and without their codes'
    labels (a layout page lists the label of each code it uses). A page leaves out an empty
    list of facts, so one emptied here goes too."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "facts" and isinstance(v, list):
                v = [_strip(f) for f in v if f.get("code") not in VISION_FACTS]
                if v:
                    out[k] = v
            elif k not in VISION_FACTS:
                out[k] = _strip(v)
        return out
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def files(out: Path, version: str | None = None) -> dict[str, object]:
    """Every output file, the page-vision facts left out: the bytes, with this output's version
    stamp read as `version` (the stamp is a digest of the outputs, the facts included), and a
    JSON file parsed. With the facts gone, nothing of the pass may be left."""
    own = json.loads((out / "manifest.json").read_text())["v"]
    out_files: dict[str, object] = {}
    for p in sorted(out.rglob("*")):
        name = p.relative_to(out).as_posix()
        if not p.is_file() or p.relative_to(out).parts[0] in IGNORED:
            continue
        body = p.read_bytes()
        if version:
            body = body.replace(own.encode(), version.encode())
        out_files[name] = _strip(json.loads(body)) if name.endswith(".json") else body
    return out_files


def version(out: Path) -> str:
    return json.loads((out / "manifest.json").read_text())["v"]


def test_unreviewed_or_rejected_proposals_leave_the_output_byte_identical(tmp_path):
    """Done when: a rejected or unreviewed proposal leaves the output identical to a run
    without the pass, apart from the facts the pass records for review, which change no
    role, text or order; an accepted one is applied after the text check."""
    pdf = make_pdf(tmp_path / "report.pdf")
    off = tmp_path / "off"
    convert(pdf, off, page_images=False)
    baseline = files(off)

    on = tmp_path / "on"
    stub = StubProvider()
    summary = convert(pdf, on, page_images=False, assist_page_vision=True,
                      page_vision_provider=stub)
    assert stub.calls == 1
    record = json.loads((on / PROPOSALS_JSON).read_text())
    assert summary.page_vision["proposals"] == len(record["proposals"]) > 0
    assert {p["status"] for p in record["proposals"]} == {"pending"}
    assert files(on, version(off)) == baseline                      # unreviewed

    runner = CliRunner()
    ids = [p["id"] for p in record["proposals"]]
    result = runner.invoke(app, ["review", str(on), *sum((["--reject", i] for i in ids), [])])
    assert result.exit_code == 0, result.output
    convert(pdf, on, page_images=False, assist_page_vision=True)   # replayed from the cache
    assert {p["status"] for p in json.loads((on / PROPOSALS_JSON).read_text())["proposals"]} \
        == {"rejected"}
    assert files(on, version(off)) == baseline                      # rejected

    demote = next(p for p in record["proposals"] if p["change"] == "demote-heading"
                  and p["text"] == "Findings")
    result = runner.invoke(app, ["review", str(on), "--accept", demote["id"]])
    assert result.exit_code == 0, result.output
    convert(pdf, on, page_images=False, assist_page_vision=True)
    statuses = {p["id"]: p["status"]
                for p in json.loads((on / PROPOSALS_JSON).read_text())["proposals"]}
    assert statuses[demote["id"]] == "applied"
    doc = Document.from_json((on / "document.json").read_text())
    node = next(n for n in doc.root.walk() if n.text == "Findings")
    assert node.role == Role.P
    assert pr.text_digest(doc) == pr.text_digest(Document.from_json(
        (off / "document.json").read_text()))
    assert files(on, version(off)) != baseline


def test_an_accept_given_on_other_text_is_not_applied(tmp_path):
    pdf = make_pdf(tmp_path / "report.pdf")
    off, on = tmp_path / "off", tmp_path / "on"
    convert(pdf, off, page_images=False)
    convert(pdf, on, page_images=False, assist_page_vision=True,
            page_vision_provider=StubProvider())
    record = json.loads((on / PROPOSALS_JSON).read_text())
    first = record["proposals"][0]["id"]
    pr.save_decisions(on / PROPOSAL_DECISIONS_JSON, pr.Decisions(
        {pr.ProposalId(first): pr.Decided(Verdict.ACCEPT, pr.TextDigest("0" * 64))}))
    convert(pdf, on, page_images=False, assist_page_vision=True)
    statuses = {p["id"]: p["status"]
                for p in json.loads((on / PROPOSALS_JSON).read_text())["proposals"]}
    assert statuses[first] == "stale"
    assert files(on, version(off)) == files(off)


def test_a_rejected_answer_makes_no_proposal_and_is_counted(tmp_path):
    class Chatty(StubProvider):
        def check(self, question, image):
            answer = super().check(question, image)
            answer["blocks"][0]["role"] = "the title of the report"
            return answer

    pdf = make_pdf(tmp_path / "report.pdf")
    summary = convert(pdf, tmp_path / "on", page_images=False, assist_page_vision=True,
                      page_vision_provider=Chatty())
    assert summary.page_vision["proposals"] == 0
    assert summary.page_vision["answers_rejected"] == 1


def test_review_lists_proposals_and_refuses_an_unknown_id(tmp_path):
    pdf = make_pdf(tmp_path / "report.pdf")
    on = tmp_path / "on"
    convert(pdf, on, page_images=False, assist_page_vision=True,
            page_vision_provider=StubProvider())
    runner = CliRunner()
    listed = runner.invoke(app, ["review", str(on)])
    assert listed.exit_code == 0 and "Findings" in listed.output and "pending" in listed.output
    bad = runner.invoke(app, ["review", str(on), "--accept", "pv-nope"])
    assert bad.exit_code != 0
    assert not (on / PROPOSAL_DECISIONS_JSON).exists()


# -- the reply text itself, and blocks apply can't reach --------------------------


@pytest.mark.parametrize("content", [
    'Here is my answer: {"blocks": [{"id": "b2", "role": "heading"}]}',
    '{"blocks": [{"id": "b2", "role": "the title", "role": "heading"}]}',
])
def test_the_ollama_reply_is_read_whole_and_strictly(content, monkeypatch):
    provider = pv.OllamaPageProvider("qwen3-vl:8b")
    monkeypatch.setattr(provider.vision, "post",
                        lambda payload, local_only=False: {"message": {"content": content},
                                                               "done_reason": "stop"})
    with pytest.raises(pv.ProviderError):
        provider.check(question(sample()), b"png")


def test_a_block_inside_a_box_is_offered_but_never_proposed():
    boxed = head("Notice", (72, 210, 300, 222))
    doc = sample()
    doc.root.children.append(Node(Role.BOX, page=1, bbox=(60, 200, 520, 260), children=[boxed]))
    q = question(doc)
    b = ids_by_text(q)
    answer = pv.parse_answer(q, reply([{"id": b["Notice"], "role": "label"}]))
    assert pv.proposals_for(doc, q, answer, model="stub") == []


# -- complexity flags as one review item per page; block readings for the inspector only ----


def pages_doc(*kinds):
    """A paragraph on each page, page i of kind kinds[i - 1]."""
    paras = [para(f"Text on page {i}.", (72, 100, 500, 112), page=i)
             for i in range(1, len(kinds) + 1)]
    return Document(Node(Role.DOC, children=paras),
                    pages=[PageInfo(i, 612, 792, kind=k) for i, k in enumerate(kinds, 1)],
                    meta={"sha256": "abc"})


def flagged(doc, flags_by_page, words=None):
    """flag() with each page answered by `flags_by_page[page]` (and block words by text)."""
    answered = []
    for info in doc.pages:
        q = pv.question_for(doc, info)
        blocks = [{"id": b.id, "role": (words or {}).get(b.snippet, b.role.value)}
                  for b in q.blocks]
        answered.append((q, pv.parse_answer(q, reply(blocks, *flags_by_page.get(info.number, ())))))
    return pv.flag(doc, answered, model="stub")


def complexity(doc):
    return {p.number: f.data["kinds"] for p in doc.pages for f in p.facts
            if f.code == "vision-complexity"}


def test_handwriting_and_poor_scan_count_only_on_a_scanned_or_mixed_page():
    doc = pages_doc(PageKind.BORN_DIGITAL, PageKind.SCANNED, PageKind.MIXED)
    every = ("handwriting", "poor_scan", "diagram")
    got = flagged(doc, {1: every, 2: every, 3: every})
    assert complexity(doc) == {1: ["diagram"], 2: ["diagram", "poor_scan", "handwriting"],
                               3: ["diagram", "poor_scan", "handwriting"]}
    assert got.refused == {1: (pv.Complexity.POOR_SCAN, pv.Complexity.HANDWRITING)}


def test_a_born_digital_page_with_only_scan_flags_gets_no_item():
    doc = pages_doc(PageKind.BORN_DIGITAL)
    flagged(doc, {1: ("handwriting",)})
    assert complexity(doc) == {}
    assert review.queue(doc) == []


def test_dense_tables_on_more_than_a_third_of_the_pages_are_one_document_item():
    doc = pages_doc(*[PageKind.BORN_DIGITAL] * 6)
    got = flagged(doc, {1: ("dense_table",), 2: ("dense_table", "diagram"), 3: ("dense_table",)})
    assert got.dense_pages == (1, 2, 3)
    assert complexity(doc) == {2: ["diagram"]}
    (fact,) = [f for f in doc.root.facts if f.code == "vision-dense-tables"]
    assert fact.data["pages"] == [1, 2, 3] and "3 of the 6 pages" in fact.detail
    items = review.queue(doc)
    assert [(i["role"], i["page"], i["warn"]) for i in items] == [
        ("doc", None, "vision-dense-tables"), ("page", 2, "vision-complexity")]


def test_dense_tables_on_a_third_of_the_pages_or_fewer_stay_per_page():
    doc = pages_doc(*[PageKind.BORN_DIGITAL] * 6)
    got = flagged(doc, {1: ("dense_table",), 4: ("dense_table",)})
    assert got.dense_pages == ()
    assert complexity(doc) == {1: ["dense_table"], 4: ["dense_table"]}
    assert not any(f.code == "vision-dense-tables" for f in doc.root.facts)
    assert [i["page"] for i in review.queue(doc)] == [1, 4]


def test_a_flagged_page_is_one_item_beside_the_page_facts_it_already_has():
    doc = pages_doc(PageKind.SCANNED)
    doc.pages[0].facts.append(Fact("ocr-missing", Source.HEURISTIC, "no OCR"))
    flagged(doc, {1: ("handwriting", "diagram")})
    (item,) = [i for i in review.queue(doc) if i["role"] == "page"]
    assert item["needs"] == "ocr"                     # the worst fact still labels the item
    assert item["reasons"] == [CODES["ocr-missing"].label, CODES["vision-complexity"].label]


def test_a_block_reading_is_for_the_inspector_and_never_a_review_item():
    doc = sample()
    before = {n.id: n.confidence for n in doc.root.walk()}
    got = flagged(doc, {}, words={"Sales rose in every region.": "handwriting",
                                  "Form 12-B rev 3": "label"})
    assert got.blocks == 2
    readings = {n.text: f.data["word"] for n in doc.root.walk() for f in n.facts
                if f.code == "vision-block"}
    assert readings == {"Sales rose in every region.": "handwriting", "Form 12-B rev 3": "label"}
    assert review.queue(doc) == []
    assert {n.id: n.confidence for n in doc.root.walk()} == before


class Flagging(StubProvider):
    """The stub, calling the page a diagram with handwriting."""

    def check(self, question, image):
        answer = super().check(question, image)
        answer["complexity"].update(diagram=True, handwriting=True)
        return answer


def test_a_flagged_page_adds_one_review_item_and_leaves_the_transcript_alone(tmp_path):
    pdf = make_pdf(tmp_path / "report.pdf")
    off, on = tmp_path / "off", tmp_path / "on"
    convert(pdf, off, page_images=False)
    summary = convert(pdf, on, page_images=False, assist_page_vision=True,
                      page_vision_provider=Flagging())
    # a born-digital page: the diagram counts, the handwriting is refused
    assert summary.page_vision["complexity_pages"] == 1
    assert summary.page_vision["complexity_refused"] == 1
    added = [i for i in json.loads((on / "review.json").read_text())
             if i not in json.loads((off / "review.json").read_text())]
    assert [(i["role"], i["page"], i["source"], i["warn"]) for i in added] == [
        ("page", 1, "assist", "vision-complexity")]
    assert "diagram" in added[0]["text"] and "handwriting" not in added[0]["text"]
    record = json.loads((on / PROPOSALS_JSON).read_text())["complexity"]
    assert record == {"pages": {"1": ["diagram"]}, "refused": {"1": ["handwriting"]},
                      "dense_table_pages": []}
    # nothing but the review facts and their item: the same document text, roles and transcript
    assert {k: v for k, v in files(on, version(off)).items() if k != "review.json"} == \
        {k: v for k, v in files(off).items() if k != "review.json"}


# -- an unread crop, when the model calls OCR text junk and a rule already doubts it -------


def ocr_para(text, box, page=1, **attrs):
    return Node(Role.P, page=page, bbox=box, text=text, source=Source.OCR,
                attrs={"ocr": "ungraded", **attrs})


def junk_doc(*extra):
    return document(head("Report", (72, 40, 300, 60), Role.H1),
                    ocr_para("The council met to review the budget.", (72, 100, 500, 112)),
                    ocr_para("Spending stayed within the budget.", (72, 120, 500, 132)),
                    *extra)


def crops(doc, words, unread=True):
    """proposals_for's unread crops, with each block called the word `words` gives its text."""
    q = question(doc)
    answer = pv.parse_answer(q, reply([{"id": b.id, "role": words.get(b.snippet, b.role.value)}
                                       for b in q.blocks]))
    return [p for p in pv.proposals_for(doc, q, answer, model="stub", unread=unread)
            if p.change is pr.Change.UNREAD_CROP]


def test_an_unread_crop_needs_the_models_word_and_a_rules_doubt():
    graded = ocr_para("Budget", (72, 140, 200, 152))   # a word printed elsewhere, nothing doubtful
    doubtful = ocr_para("xq Zr kk w", (72, 160, 200, 172))        # the lexical gate doubts it
    doc = junk_doc(graded, doubtful)
    assert crops(doc, {"Budget": "noise", "xq Zr kk w": "noise"}, unread=False) == []
    found = crops(doc, {"Budget": "noise", "xq Zr kk w": "noise"})
    assert [(p.node, p.word, p.doubt) for p in found] == [
        (doubtful.id, "noise", "OCR text starts in lowercase: a piece of a sentence")]
    # a rule's doubt alone, or a word outside handwriting, noise and figure label: no crop
    assert crops(doc, {"xq Zr kk w": "paragraph"}) == []
    assert crops(doc, {"xq Zr kk w": "label"}) == []
    for word in ("handwriting", "figure_label"):
        assert [p.word for p in crops(doc, {"xq Zr kk w": word})] == [word]


def test_born_digital_text_is_never_cropped():
    doc = junk_doc(para("xq Zr kk w", (72, 160, 200, 172)))
    assert crops(doc, {"xq Zr kk w": "handwriting"}) == []


def test_the_second_witnesses_beside_the_ocr_grade():
    unique = ocr_para("Quetzal Wombat", (72, 160, 200, 172))
    doc = junk_doc(unique)
    lexicon = pv.document_lexicon(doc.flow())
    assert pv.unread_doubt(unique, [], lexicon) == \
        "no word of it is printed elsewhere in the document"
    known = ocr_para("The budget", (72, 300, 200, 312))
    doc = junk_doc(known)
    lexicon = pv.document_lexicon(doc.flow())
    assert pv.unread_doubt(known, [], lexicon) is None
    assert pv.unread_doubt(known, [(210, 290, 400, 400)], lexicon) == "it is beside a figure"
    assert pv.unread_doubt(known, [(240, 290, 400, 400)], lexicon) is None   # 28 pt away
    marked = ocr_para("The budget", (72, 300, 200, 312))
    marked.marks = [Mark(4, 10, "doubt", "budget")]
    assert pv.unread_doubt(marked, [], lexicon) == "OCR is unsure of the word ‘budget’"


def test_an_accepted_crop_takes_out_only_its_paragraphs_text():
    doubtful = ocr_para("xq Zr kk w", (72, 160, 200, 172))
    doc = junk_doc(doubtful)
    [p] = crops(doc, {"xq Zr kk w": "handwriting"})
    expected = pr.text_digest(doc, without=doubtful.id)
    accepted = pr.review([p], pr.Decisions({p.id: pr.Decided(Verdict.ACCEPT,
                                                              p.document_text)})).accepted[0]
    assert pr.apply(doc, accepted) is pr.Status.APPLIED
    node = next(n for n in doc.root.walk() if n.id == doubtful.id)
    assert node.role == Role.FIGURE and node.text is None and node.bbox == (72, 160, 200, 172)
    assert node.attrs["unread"] and node.attrs["kind"] == "handwriting"
    assert node.attrs["ocr_guess"] == "xq Zr kk w"
    assert {f.code for f in node.facts} == {"unread-handwriting", "vision-accepted"}
    assert pr.text_digest(doc) == expected
    # applied again (a rebuild that already holds the crop): the block is no paragraph, stale
    assert pr.apply(doc, pr.review([p], pr.Decisions({p.id: pr.Decided(
        Verdict.ACCEPT, pr.text_digest(doc))})).accepted[0]) is pr.Status.STALE


def test_an_accepted_crop_on_changed_text_is_stale():
    doubtful = ocr_para("xq Zr kk w", (72, 160, 200, 172))
    doc = junk_doc(doubtful)
    [p] = crops(doc, {"xq Zr kk w": "noise"})
    accepted = pr.review([p], pr.Decisions({p.id: pr.Decided(Verdict.ACCEPT,
                                                              p.document_text)})).accepted[0]
    doc.root.children[-1].set_text("xq Zr kk")
    assert pr.apply(doc, accepted) is pr.Status.STALE
    assert doc.root.children[-1].role == Role.P


class Junk(StubProvider):
    """Calls the scrawl noise, the title line a heading, and every other block what reflow calls
    it. Not a model."""

    def check(self, question, image):
        self.calls += 1
        words = {SCRAWL: "noise", "Annual Report of the Council": "heading"}
        return reply([{"id": b.id, "role": words.get(b.snippet, b.role.value)}
                      for b in question.blocks])


SCRAWL = "xq Zr kk w"


def make_scan(path: Path) -> Path:
    return make_scan_pdf(path, [[(60, "Annual Report of the Council"),
                                 (100, "The council met four times this year to review the budget."),
                                 (116, "Each meeting was open to the public and recorded."),
                                 (180, SCRAWL),
                                 (220, "Spending stayed within the budget this year.")]])


def layer_text(pdf: Path) -> str:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        return "".join(page.get_textpage().get_text_range() for page in doc)
    finally:
        doc.close()


def test_an_unaccepted_crop_leaves_the_output_byte_identical_and_an_accepted_one_keeps_the_layer(
        tmp_path):
    """Done when: an unreviewed or rejected unread crop leaves every
    output file as a run without the pass writes it; an accepted one takes out exactly its
    paragraph's text, which stays in the PDF's text layer and in the crop's `ocr_guess`."""
    pdf = make_scan(tmp_path / "scan.pdf")
    off, on = tmp_path / "off", tmp_path / "on"
    convert(pdf, off, page_images=False)
    baseline = files(off)
    assert SCRAWL in layer_text(off / "working.pdf")

    stub = Junk()
    convert(pdf, on, page_images=False, assist_page_vision=True, assist_page_vision_unread=True,
            page_vision_provider=stub)
    assert stub.calls == 1
    [crop] = [p for p in json.loads((on / PROPOSALS_JSON).read_text())["proposals"]
              if p["change"] == "unread-crop"]
    assert (crop["text"], crop["word"], crop["status"]) == (SCRAWL, "noise", "pending")
    assert crop["doubt"]
    assert files(on, version(off)) == baseline                      # unreviewed

    runner = CliRunner()
    listing = runner.invoke(app, ["review", str(on)], env={"COLUMNS": "300"})
    assert "noise; and " + crop["doubt"] in listing.output
    assert runner.invoke(app, ["review", str(on), "--reject", crop["id"]]).exit_code == 0
    convert(pdf, on, page_images=False, assist_page_vision=True, assist_page_vision_unread=True)
    assert files(on, version(off)) == baseline                      # rejected

    accepted = runner.invoke(app, ["review", str(on), "--accept", crop["id"]],
                             env={"COLUMNS": "300"})
    assert accepted.exit_code == 0
    assert "--assist-page-vision --assist-page-vision-unread" in accepted.output
    other = next(p for p in json.loads((on / PROPOSALS_JSON).read_text())["proposals"]
                 if p["change"] != "unread-crop")
    later = runner.invoke(app, ["review", str(on), "--reject", other["id"]],
                          env={"COLUMNS": "300"})   # the saved accept still needs the flag
    assert "--assist-page-vision --assist-page-vision-unread" in later.output
    convert(pdf, on, page_images=False, assist_page_vision=True, assist_page_vision_unread=True)
    statuses = {p["id"]: p["status"]
                for p in json.loads((on / PROPOSALS_JSON).read_text())["proposals"]}
    assert statuses[crop["id"]] == "applied"
    before = Document.from_json((off / "document.json").read_text())
    after = Document.from_json((on / "document.json").read_text())
    node = next(n for n in after.root.walk() if n.id == crop["node"])
    assert node.role == Role.FIGURE and node.attrs["ocr_guess"] == SCRAWL
    assert pr.text_digest(after) == pr.text_digest(before, without=crop["node"])
    # out of the transcript, Find, Ask's passages and the review queue; kept for the inspector,
    # in the crop's ocr_guess, and in the proposal a reviewer reads
    holding = {p.relative_to(on).as_posix() for p in on.rglob("*")
               if p.is_file() and SCRAWL.encode() in p.read_bytes()}
    assert holding == {"document.json", "layout/0001.json", "trace/0001.json", PROPOSALS_JSON}
    assert any((on / "figures").glob("*.webp"))                     # the crop shows the writing
    # the PDF and its text layer are what the run without the pass wrote
    assert (on / "working.pdf").read_bytes() == (off / "working.pdf").read_bytes()
    assert (on / "original.pdf").read_bytes() == (off / "original.pdf").read_bytes()
    assert SCRAWL in layer_text(on / "working.pdf")


def test_every_accepted_crop_applies_though_each_takes_text_out():
    """The text check is against the document as the reviewer saw it, before this run's
    accepted changes: the second crop must not read the first one's change as changed text."""
    first = ocr_para("xq Zr kk w", (72, 160, 200, 172))
    second = ocr_para("zz Qp", (72, 200, 200, 212))
    doc = junk_doc(first, second)
    found = crops(doc, {"xq Zr kk w": "noise", "zz Qp": "handwriting"})
    assert len(found) == 2
    expected = pr.text_digest(doc, without=first.id)
    decisions = pr.Decisions({p.id: pr.Decided(Verdict.ACCEPT, p.document_text) for p in found})
    status = pv.settle(doc, found, decisions)
    assert set(status.values()) == {pr.Status.APPLIED}
    roles = {n.id: n.role for n in doc.root.walk()}
    assert roles[first.id] == roles[second.id] == Role.FIGURE
    assert pr.text_digest(doc) != expected   # both paragraphs' text left, not only the first's
