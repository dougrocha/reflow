"""A conversion that is given a decisions file: the option, the pass in the pipeline, the
ledger and manifest it publishes, the summary line, convert-many's per-document stores, and page
vision's use of the store's proposal decisions."""

from __future__ import annotations

import json
from pathlib import Path

import pikepdf
import pytest
from typer.testing import CliRunner

import decisions_kit as kit
from conftest import make_text_pdf
from reflow import saved, steps
from reflow.analyze import proposals as pr
from reflow.analyze.decisions import Verdict
from reflow.cli import app
from reflow.convert import convert, convert_many
from reflow.names import DECISIONS_LEDGER, PROPOSAL_DECISIONS_JSON, PROPOSALS_JSON
from reflow.run import Options
from testkit import ocr_available, walk_json

ENGLISH = ("the and of to is are with for that this from which have has were was be it a in "
           "on as by an at or not but all can their there would about more other into than "
           ).split() * 3


def spanish_pdf(path: Path, title: str = "Annual report") -> Path:
    """English text under a Spanish declaration: one language-mismatch warning."""
    lines = [" ".join(ENGLISH[i:i + 10]) for i in range(0, len(ENGLISH), 10)]
    make_text_pdf(path, [[(72, 72, 18, title, "B")]
                         + [(72, 110 + 16 * i, 11, line) for i, line in enumerate(lines)]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.Lang = "es"
        pdf.docinfo["/Title"] = title
        pdf.save(path)
    return path


def sha_of(out: Path) -> str:
    return json.loads((out / "document.json").read_text())["meta"]["sha256"]


def language_finding(out: Path) -> str:
    [item] = [i for i in json.loads((out / "review.json").read_text()) if i["warn"] ==
              "language-mismatch"]
    return item["findings"][0]["id"]


def tree(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes()
            for p in sorted(out.rglob("*")) if p.is_file()}


# -- the option ----------------------------------------------------------------------------------


def test_a_decisions_path_that_is_not_a_file_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a file"):
        Options(decisions=tmp_path)
    assert Options(decisions=tmp_path / "missing.jsonl").decisions == tmp_path / "missing.jsonl"
    assert Options().decisions is None


def test_the_decisions_file_is_not_part_of_the_options_that_name_the_output(tmp_path):
    assert Options(decisions=tmp_path / "a.jsonl") == Options()


def test_a_decisions_file_does_not_make_a_conversion_unprovable(tmp_path):
    """A saved model is a base: the store is replayed on it (tests/test_rerender.py)."""
    opts = Options(decisions=tmp_path / "a.jsonl")
    assert not steps.uncovered(opts) and not saved.unprovable(opts)


def test_a_bad_path_fails_the_command_before_any_work(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    result = CliRunner().invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "out"),
                                      "--decisions", str(tmp_path)])
    assert result.exit_code == 2
    assert not (tmp_path / "out").exists()


# -- without a store, nothing changes ------------------------------------------------------------


def test_a_missing_store_changes_only_the_ledger_and_the_manifest(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    plain, with_missing = tmp_path / "plain", tmp_path / "missing"
    convert(pdf, plain, page_images=False)
    convert(pdf, with_missing, page_images=False, decisions=tmp_path / "nope.jsonl")
    assert DECISIONS_LEDGER not in {p.name for p in plain.iterdir()}
    assert DECISIONS_LEDGER in {p.name for p in with_missing.iterdir()}

    def comparable(out: Path) -> dict[str, bytes]:
        files = tree(out)
        version = json.loads(files["manifest.json"])["v"].encode()
        doc = json.loads(files.pop("document.json"))
        doc["meta"].pop("decisions", None)
        files = {k: v.replace(version, b"V") for k, v in files.items()
                 if k not in ("manifest.json", DECISIONS_LEDGER, "model.json", "working.pdf",
                              "build.json")}
        files["document.json"] = json.dumps(doc).encode()
        return files

    assert comparable(plain) == comparable(with_missing)


# -- a dismissal, end to end ---------------------------------------------------------------------


def test_a_dismissed_warning_is_gone_from_review_json_and_listed_in_the_ledger(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    first = tmp_path / "first"
    convert(pdf, first, page_images=False)
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "a")
    kit.append(path, kit.record(sha_of(first), "dismiss", findings=[language_finding(first)]))

    out = tmp_path / "out"
    summary = convert(pdf, out, page_images=False, decisions=path)

    assert not [i for i in json.loads((out / "review.json").read_text())
                if i["warn"] == "language-mismatch"]
    ledger = json.loads((out / DECISIONS_LEDGER).read_text())
    assert [d["code"] for d in ledger["dismissed"]] == ["language-mismatch"]
    assert ledger["source"] == {"sha256": sha_of(first)}
    assert summary.decisions == {"applied": 0, "dismissed": 1, "orphaned": 0}
    assert summary.review["warnings"] == 0
    assert (out / "document.json").read_text() != (first / "document.json").read_text()


def test_the_pass_runs_after_the_queue_is_scored_and_before_it_is_built(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "a")
    kit.append(path, kit.record("f" * 64, "dismiss", findings=["e" * 16]))
    out = tmp_path / "out"
    convert(pdf, out, page_images=False, decisions=path)
    review = json.loads((out / "review.json").read_text())
    assert "decisions-other-pdf" in [i["warn"] for i in review]
    doc = json.loads((out / "document.json").read_text())
    assert all(any(f["code"] == "prior" for f in n.get("facts", []))
               for n in walk_json(doc["root"]) if n["role"] != "doc")


def test_the_cli_prints_the_decisions_line_beside_the_review_line(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    first = tmp_path / "first"
    convert(pdf, first, page_images=False)
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "a")
    kit.append(path, kit.record(sha_of(first), "dismiss", findings=[language_finding(first)]))
    kit.append(path, kit.record("f" * 64, "dismiss", findings=["e" * 16]))
    result = CliRunner().invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "out"),
                                      "--no-page-images", "--decisions", str(path)])
    assert result.exit_code == 0, result.output
    assert "decisions: 0 applied, 1 dismissed, 1 orphaned" in result.output


def test_no_decisions_option_prints_no_decisions_line(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    result = CliRunner().invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "out"),
                                      "--no-page-images"])
    assert "decisions:" not in result.output


# -- convert-many --------------------------------------------------------------------------------


@pytest.mark.parametrize("isolate", [False, True])
def test_convert_many_reads_one_store_per_output_folder(tmp_path, isolate):
    pdfs = [spanish_pdf(tmp_path / f"{n}.pdf", title=f"Report {n}") for n in "ab"]
    firsts = [tmp_path / f"first-{n}" for n in "ab"]
    convert_many(list(zip(pdfs, firsts)), jobs=1, page_images=False)
    stores = tmp_path / "stores"
    stores.mkdir()
    # only the document in out-a has a store, and it dismisses its language warning
    kit.new_store(stores / "out-a.jsonl", "a")
    kit.append(stores / "out-a.jsonl", kit.record(sha_of(firsts[0]), "dismiss",
                                              findings=[language_finding(firsts[0])]))
    outs = [tmp_path / "out-a", tmp_path / "out-b"]
    results = convert_many(list(zip(pdfs, outs)), jobs=1, isolate=isolate, page_images=False,
                           decisions_dir=stores)
    assert [r.ok for r in results] == [True, True], [r.error for r in results]
    assert [r.summary.decisions["dismissed"] for r in results] == [1, 0]
    assert (outs[0] / DECISIONS_LEDGER).exists() and (outs[1] / DECISIONS_LEDGER).exists()


def test_convert_many_without_a_directory_writes_no_ledger(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    [r] = convert_many([(pdf, tmp_path / "out")], jobs=1, page_images=False)
    assert not (tmp_path / "out" / DECISIONS_LEDGER).exists() and r.summary.decisions == {}


# -- page vision ---------------------------------------------------------------------------------


def test_the_stores_proposal_decision_applies_and_wins_over_the_carried_one(tmp_path):
    from test_page_vision import StubProvider, make_pdf as vision_pdf
    pdf = vision_pdf(tmp_path / "report.pdf")
    out = tmp_path / "on"
    convert(pdf, out, page_images=False, assist_page_vision=True,
            page_vision_provider=StubProvider())
    proposals = json.loads((out / PROPOSALS_JSON).read_text())["proposals"]
    demote = next(p for p in proposals if p["change"] == "demote-heading"
                  and p["text"] == "Findings")
    sha = sha_of(out)
    # the carried file rejects it; the store accepts it, and the store wins
    pr.save_decisions(out / PROPOSAL_DECISIONS_JSON, pr.Decisions(
        {pr.ProposalId(demote["id"]): pr.Decided(Verdict.REJECT, pr.TextDigest(
            demote["document_text"]))}))
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "report")
    kit.append(path, kit.record(sha, "proposal", proposal=demote["id"], verdict="accept",
                            text=demote["document_text"]))
    convert(pdf, out, page_images=False, assist_page_vision=True, decisions=path)
    status = {p["id"]: p["status"]
              for p in json.loads((out / PROPOSALS_JSON).read_text())["proposals"]}
    assert status[demote["id"]] == "applied"
    ledger = json.loads((out / DECISIONS_LEDGER).read_text())
    assert [p["proposal"] for p in ledger["proposals"]] == [demote["id"]]


def test_a_proposal_decision_made_on_another_pdf_does_not_apply(tmp_path):
    from test_page_vision import StubProvider, make_pdf as vision_pdf
    pdf = vision_pdf(tmp_path / "report.pdf")
    out = tmp_path / "on"
    convert(pdf, out, page_images=False, assist_page_vision=True,
            page_vision_provider=StubProvider())
    demote = next(p for p in json.loads((out / PROPOSALS_JSON).read_text())["proposals"]
                  if p["change"] == "demote-heading" and p["text"] == "Findings")
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "report")
    kit.append(path, kit.record("f" * 64, "proposal", proposal=demote["id"], verdict="accept",
                            text=demote["document_text"]))
    convert(pdf, out, page_images=False, assist_page_vision=True, decisions=path)
    status = {p["id"]: p["status"]
              for p in json.loads((out / PROPOSALS_JSON).read_text())["proposals"]}
    assert status[demote["id"]] == "pending"
    ledger = json.loads((out / DECISIONS_LEDGER).read_text())
    assert [o["reason"] for o in ledger["orphaned"]] == ["other-pdf"]


# -- no reviewer name ----------------------------------------------------------------------------


def test_a_reviewers_name_in_the_store_reaches_no_published_file(tmp_path):
    pdf = spanish_pdf(tmp_path / "a.pdf")
    first = tmp_path / "first"
    convert(pdf, first, page_images=False)
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "a")
    kit.append(path, kit.record(sha_of(first), "dismiss", findings=[language_finding(first)],
                            by="reviewer@example.com", email="reviewer@example.com"))
    kit.append(path, kit.record("f" * 64, "dismiss", findings=["e" * 16], by="reviewer@example.com"))
    future = kit.record(sha_of(first), "future", by="reviewer@example.com")
    future["v"] = 2
    kit.append(path, future)
    out = tmp_path / "out"
    convert(pdf, out, page_images=False, decisions=path)
    ledger = (out / DECISIONS_LEDGER).read_text(encoding="utf-8")
    assert "@" not in ledger and '"by"' not in ledger and '"email"' not in ledger
    for name in ("review.json", "manifest.json", "passages.jsonl", "toc.json", "index.html"):
        assert "reviewer@example.com" not in (out / name).read_text(encoding="utf-8")
    assert not any("reviewer@example.com" in p.read_text(encoding="utf-8") for p in
                   (out / "chunks").iterdir())


# -- end to end, on a scanned sample -------------------------------------------------------------

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.mark.pages
@pytest.mark.skipif(not ocr_available(), reason="No OCR runner (xfail_without_ocr in the page specs)")
def test_a_scanned_page_takes_a_correction_a_kept_reading_and_a_dismissal(tmp_path):
    """samples/scan.pdf has no OCR suggestion, so the decisions answer words its OCR graded as
    doubtful (a doubt mark is a span reflow itself flagged): one corrected, one kept."""
    from reflow.analyze import anchors, decisions as dec, decisions_apply as da
    from reflow.model import Document, Role, Source
    from reflow.render.passages import passages as quotable

    cache = tmp_path / "cache"
    pdf = SAMPLES / "scan.pdf"
    first = tmp_path / "first"
    convert(pdf, first, page_images=False, stage_cache=cache)
    doc = Document.from_json((first / "document.json").read_text(encoding="utf-8"))
    sha = doc.meta["sha256"]

    def target(node, mark, kind, **extra):
        text = node.text
        before, after = text[max(0, mark.start - 20):mark.start], text[mark.end:mark.end + 20]
        body = kit.record(sha, kind, target={"anchor": anchors.anchor_of(node).to_dict(), "range": {
            "was": mark.text, "before": before, "after": after, "rule": "ocr-doubt"}}, **extra)
        parsed = dec.parse_record(body)
        return body if isinstance(da.match(doc, parsed), da.Target) else None

    chosen, used = [], set()
    ids = {p.id for p in quotable(doc)}     # a paragraph Ask can quote
    for node in doc.root.walk():
        if (node.source != Source.OCR or node.role != Role.P or node.page in used
                or node.id not in ids):
            continue
        for mark in node.marks:
            if mark.kind != "doubt":
                continue
            kind = "correct" if not chosen else "keep"
            extra = {"now": mark.text + "."} if kind == "correct" else {}
            if (body := target(node, mark, kind, **extra)) is not None:
                chosen.append((node, mark, body))
                used.add(node.page)
                break
        if len(chosen) == 2:
            break
    assert len(chosen) == 2, "the sample should hold two matchable doubtful words on two pages"
    (fixed, fixed_mark, correction), (kept, kept_mark, keep_body) = chosen
    language = next(i for i in json.loads((first / "review.json").read_text())
                    if i["warn"] == "language-missing")
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "scan")
    for body in (correction, keep_body,
                 kit.record(sha, "dismiss", findings=[language["findings"][0]["id"]])):
        kit.append(path, body)

    out = tmp_path / "out"
    result = CliRunner().invoke(app, ["convert", str(pdf), "-o", str(out), "--no-page-images",
                                      "--stage-cache", str(cache), "--decisions", str(path)])
    assert result.exit_code == 0, result.output
    assert "decisions: 2 applied, 1 dismissed, 0 orphaned" in " ".join(result.output.split())

    html = "".join(p.read_text(encoding="utf-8") for p in [out / "index.html",
                                                           *sorted((out / "chunks").iterdir())])
    assert f'data-was="{fixed_mark.text}"' in html and "review-note" in html
    after = Document.from_json((out / "document.json").read_text(encoding="utf-8"))
    by_id = {n.id: n for n in after.root.walk()}
    assert by_id[fixed.id].text.count(fixed_mark.text + ".") >= 1
    assert [m.kind for m in by_id[fixed.id].marks if m.kind == "fix"] == ["fix"]
    assert len([m for m in by_id[kept.id].marks if m.kind == "doubt"]) == len(
        [m for m in kept.marks if m.kind == "doubt"]) - 1
    assert by_id[kept.id].text == kept.text
    review = json.loads((out / "review.json").read_text(encoding="utf-8"))
    assert not [i for i in review if i["warn"] == "language-missing"]
    ledger = json.loads((out / DECISIONS_LEDGER).read_text(encoding="utf-8"))
    assert [e["kind"] for e in ledger["applied"]] == ["correct", "keep"]
    assert ledger["orphaned"] == [] and len(ledger["dismissed"]) == 1
    passages = [json.loads(line) for line in (out / "passages.jsonl").read_text().splitlines()[1:]]
    assert [p["f"] for p in passages if "f" in p][0][0][2] == fixed_mark.text


def test_jobs_2_with_a_decisions_file_publishes_what_jobs_1_publishes(tmp_path):
    """verify-parallel-conversion's promise with a store: a dismissal and an orphan on fw9."""
    pdf = Path(__file__).parent.parent / "samples" / "fw9.pdf"
    first = tmp_path / "first"
    convert(pdf, first, page_images=False)
    items = json.loads((first / "review.json").read_text())
    warning = next(f["id"] for i in items for f in i.get("findings", [])
                   if f["severity"] == "warning")
    node = next(i for i in items if i.get("anchor"))
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "fw9")
    kit.append(path, kit.record(sha_of(first), "dismiss", findings=[warning]))
    kit.append(path, kit.record(sha_of(first), "keep", target={
        "anchor": node["anchor"], "range": {"was": "zz", "before": "", "after": "", "rule": "x"}}))

    convert(pdf, tmp_path / "one", page_images=False, jobs=1, decisions=path)
    convert(pdf, tmp_path / "two", page_images=False, jobs=2, chunk_pages=2, decisions=path)

    ledger = json.loads((tmp_path / "one" / DECISIONS_LEDGER).read_text())
    assert len(ledger["dismissed"]) == 1 and len(ledger["orphaned"]) == 1
    one, two = tree(tmp_path / "one"), tree(tmp_path / "two")
    assert one.keys() == two.keys()
    # working.pdf's /ID is fresh on every run, and build.json holds the model's digest with it
    assert [k for k in one if one[k] != two[k] and k not in ("working.pdf", "build.json")] == []


def test_a_manifest_with_a_ledger_validates_against_the_published_schema(tmp_path):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).parent.parent / "schema" / "manifest.schema.json")
                        .read_text())
    pdf = spanish_pdf(tmp_path / "a.pdf")
    out = tmp_path / "out"
    convert(pdf, out, page_images=False, decisions=tmp_path / "missing.jsonl")
    manifest = json.loads((out / "manifest.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(manifest)) == []
