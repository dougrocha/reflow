"""Provenance of published documents: what a record holds, and the verdict of each kind of
document (current, stale, missing, unverifiable), including old outputs and dirty source."""

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from reflow import identity, output, provenance, steps
from reflow.provenance import Published, check, environment, inputs, make_record
from reflow.pageimages import LARGE_WIDTH_MAX_PAGES
from reflow.run import Options

VERSION = "0123456789ab"


def record_path(data: Path, slug: str) -> Path:
    return data / ".provenance" / f"{slug}.json"


def _package(root: Path) -> Path:
    pkg = root / "pkg" / "reflow"
    (pkg / "render" / "templates").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "render" / "html.py").write_text("def render(): return 1\n")
    (pkg / "render" / "templates" / "index.html").write_text("<html></html>\n")
    return pkg


class Doc:
    """One document laid out as docs:build leaves it: the conversion, its public copy, the PDF
    copy and the record, built from a package of our own."""

    def __init__(self, root: Path, options: Options | None = None) -> None:
        self.root, self.options = root, options or Options()
        self.pkg = _package(root)
        self.pdf = root / "samples" / "a.pdf"
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(b"%PDF-1.7 first")
        data = root / "web" / ".reflow"
        self.doc = Published("a", self.pdf, data / "a", record_path(data, "a"),
                             root / "web" / "public" / "docs" / "a",
                             root / "web" / "public" / "pdfs" / "a.pdf")

    def env(self) -> dict:
        return environment(self.pkg)

    def now(self, options: Options | None = None):
        return inputs(self.pdf, options or self.options, self.env()) if self.pdf.exists() else None

    def build(self, version: str = VERSION, *, after=None) -> None:
        """Convert (a manifest with `version`), publish, copy the PDF and record."""
        before = self.now()
        for folder in (self.doc.data, self.doc.site):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "manifest.json").write_text(json.dumps({"v": version}))
        self.doc.pdf_copy.parent.mkdir(parents=True, exist_ok=True)
        self.doc.pdf_copy.write_bytes(self.pdf.read_bytes())
        provenance.write_record(self.doc.record,
                                make_record(before, after if after is not None else self.now(),
                                            version))

    def verdict(self, options: Options | None = None):
        return check(self.doc, self.now(options))


def test_the_source_digest_is_the_page_check_cache_code_hash():
    """The page-check cache, shared by every worktree, keys on this digest: it must not move."""
    repo = Path(provenance.__file__).resolve().parents[2]
    h = hashlib.sha256()
    files = (p for p in (repo / "src" / "reflow").rglob("*")
             if p.is_file() and "__pycache__" not in p.parts)
    for path in sorted(files):
        h.update(str(path.relative_to(repo)).encode())
        h.update(path.read_bytes())
    assert provenance.source_identity(repo / "src" / "reflow")[0] == h.hexdigest()


def test_the_source_identity_sees_edits_additions_and_removals_but_not_bytecode(tmp_path):
    pkg = _package(tmp_path)
    digest, files = provenance.source_identity(pkg)
    assert set(files) == {"src/reflow/__init__.py", "src/reflow/render/html.py",
                          "src/reflow/render/templates/index.html"}
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"bytecode")
    assert provenance.source_identity(pkg)[0] == digest
    (pkg / "render" / "html.py").write_text("def render(): return 2\n")   # an uncommitted edit
    edited, edited_files = provenance.source_identity(pkg)
    assert edited != digest
    assert edited_files["src/reflow/render/html.py"] != files["src/reflow/render/html.py"]
    (pkg / "new.py").write_text("")
    assert provenance.source_identity(pkg)[0] not in (digest, edited)


def test_the_options_record_holds_what_changes_the_output_and_never_the_password(tmp_path):
    record = steps.options_record(Options(password="hunter2", stage_cache=tmp_path,
                                          engine="heuristic"))
    assert record["password"] is True
    assert "stage_cache" not in record and "device_requested" not in record
    assert record["engine"] == "heuristic"
    assert isinstance(record["page_widths"], list)
    assert "hunter2" not in json.dumps(record)


def test_model_answers_are_inputs_no_record_covers():
    assert steps.uncovered(Options()) == []
    assert "ollama" in steps.uncovered(Options(alt_text="ollama"))[0]
    assert steps.uncovered(Options(assist_labels=True))


def test_a_document_built_from_these_inputs_is_current(tmp_path):
    d = Doc(tmp_path)
    d.build()
    v = d.verdict()
    assert v.status == "current", v.reasons


def test_nothing_converted_or_nothing_published_is_missing(tmp_path):
    d = Doc(tmp_path)
    assert d.verdict().status == "missing"
    d.build()
    (d.doc.site / "manifest.json").unlink()
    v = d.verdict()
    assert v.status == "missing" and "not published" in v.reasons[0]


def test_an_old_output_without_a_record_is_unverifiable_never_current(tmp_path):
    d = Doc(tmp_path)
    d.build()
    d.doc.record.unlink()   # as main's documents are, from before records were kept
    v = d.verdict()
    assert v.status == "unverifiable" and "no provenance record" in v.reasons[0]
    d.doc.record.write_text("{not json")
    assert d.verdict().status == "unverifiable"
    d.doc.record.write_text(json.dumps({"schema": 0}))
    assert d.verdict().status == "unverifiable"


def test_a_conversion_replaced_without_a_new_record_is_unverifiable(tmp_path):
    """An older docs:build, or a plain reflow convert, rebuilds without writing a record."""
    d = Doc(tmp_path)
    d.build()
    for folder in (d.doc.data, d.doc.site):
        (folder / "manifest.json").write_text(json.dumps({"v": "ffffffffffff"}))
    v = d.verdict()
    assert v.status == "unverifiable" and "another build" in v.reasons[0]


def test_dirty_source_makes_it_stale_and_names_the_file(tmp_path):
    d = Doc(tmp_path)
    d.build()
    (d.pkg / "render" / "html.py").write_text("def render(): return 3\n")
    v = d.verdict()
    assert v.status == "stale"
    assert "render/html.py" in v.reasons[0] and "1 changed" in v.reasons[0]
    (d.pkg / "render" / "html.py").write_text("def render(): return 1\n")   # undone
    assert d.verdict().status == "current"


def test_a_build_from_dirty_source_is_current_while_the_source_is_the_same(tmp_path):
    d = Doc(tmp_path)
    (d.pkg / "render" / "html.py").write_text("def render(): return 'uncommitted'\n")
    d.build()
    assert d.verdict().status == "current"


def test_a_changed_pdf_options_or_dependency_makes_it_stale(tmp_path, monkeypatch):
    d = Doc(tmp_path)
    d.build()
    assert d.verdict(Options(engine="hybrid", device="cpu")).status == "stale"
    reasons = d.verdict(Options(srcset_max_pages=10)).reasons
    assert reasons == (f"the conversion options differ: srcset_max_pages {LARGE_WIDTH_MAX_PAGES} -> 10",)
    real = identity.dependency_versions()
    monkeypatch.setattr(identity, "dependency_versions", lambda: {**real, "pdfplumber": "0.0.1"})
    v = d.verdict()
    assert v.status == "stale" and "pdfplumber" in v.reasons[0] and "0.0.1" in v.reasons[0]
    monkeypatch.undo()
    d.pdf.write_bytes(b"%PDF-1.7 second")
    v = d.verdict()
    assert v.status == "stale" and v.reasons[0] == "the source PDF changed since the build"


def test_a_publish_that_did_not_finish_or_another_pdf_copy_is_stale(tmp_path):
    d = Doc(tmp_path)
    d.build()
    (d.doc.site / "manifest.json").write_text(json.dumps({"v": "ffffffffffff"}))
    assert "published copy" in d.verdict().reasons[0]
    (d.doc.site / "manifest.json").write_text(json.dumps({"v": VERSION}))
    d.doc.pdf_copy.write_bytes(b"%PDF-1.7 other")
    v = d.verdict()
    assert v.status == "stale" and "public/pdfs/a.pdf" in v.reasons[0]


def test_inputs_that_moved_during_the_build_make_it_unverifiable(tmp_path):
    d = Doc(tmp_path)
    after = d.now()
    after = {**after, "source": {**after["source"], "digest": "edited mid-build"}}
    d.build(after=after)
    record = json.loads(d.doc.record.read_text())
    assert record["changed_during_build"] == ["source"]
    v = d.verdict()
    assert v.status == "unverifiable" and "changed while it was built" in v.reasons[0]


def test_model_answers_make_it_unverifiable_and_stale_wins_over_unverifiable(tmp_path):
    d = Doc(tmp_path, Options(alt_text="ollama"))
    d.build()
    v = d.verdict()
    assert v.status == "unverifiable" and "figure descriptions" in v.reasons[0]
    d.pdf.write_bytes(b"%PDF-1.7 second")
    v = d.verdict()
    assert v.status == "stale" and len(v.reasons) == 2


def test_no_source_pdf_to_compare_is_unverifiable(tmp_path):
    d = Doc(tmp_path)
    d.build()
    d.pdf.unlink()
    v = d.verdict()
    assert v.status == "unverifiable" and "source PDF is not here" in v.reasons[0]


def test_the_record_lives_outside_the_conversion_so_any_converter_can_replace_it(tmp_path):
    """Older converters refuse an output folder holding a file they did not write, and a seeded
    document reaches worktrees on older branches: the record must stay out of the folder."""
    d = Doc(tmp_path)
    d.build()
    assert d.doc.data not in d.doc.record.parents
    assert d.doc.record.parent.name.startswith(".")   # never taken for a slug
    output.check_replaceable(d.doc.data)


def test_writing_a_record_leaves_no_temp_files_and_replaces_the_old_one(tmp_path):
    d = Doc(tmp_path)
    d.build()
    d.build("abcdefabcdef")
    assert [p.name for p in d.doc.record.parent.iterdir()] == ["a.json"]
    assert json.loads(d.doc.record.read_text())["output"]["v"] == "abcdefabcdef"


def _main(command: str, spec: dict, tmp_path: Path):
    path = tmp_path / f"{command}.json"
    path.write_text(json.dumps(spec))
    out = io.StringIO()
    with redirect_stdout(out):
        assert provenance.main([command, str(path)]) == 0
    return json.loads(out.getvalue())


def test_the_command_line_snapshots_records_and_checks(tmp_path):
    """The three steps docs:build runs, against this process's own reflow package."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    data = tmp_path / ".reflow"
    docs = [{"slug": "a", "pdf": str(pdf), "data": str(data / "a"),
             "record": str(record_path(data, "a"))},
            {"slug": "b", "pdf": str(tmp_path / "gone.pdf"), "data": str(data / "b"),
             "record": str(record_path(data, "b"))}]
    spec = {"src": str(provenance.PACKAGE_DIR), "options": {}, "docs": docs}
    before = _main("snapshot", spec, tmp_path)
    assert list(before) == ["a"]   # no PDF, no snapshot
    (data / "a").mkdir(parents=True)
    (data / "a" / "manifest.json").write_text(json.dumps({"v": VERSION}))
    written = _main("record", {**spec, "before": before}, tmp_path)
    assert (written["a"]["written"], written["a"]["changed"]) == (True, [])
    assert "error" in written["b"]
    verdicts = _main("check", spec, tmp_path)
    assert [(v["slug"], v["status"]) for v in verdicts] == [("a", "current"), ("b", "missing")]


def test_the_command_line_refuses_to_check_another_checkout_s_source(tmp_path):
    with pytest.raises(SystemExit, match="imports reflow from"):
        _main("check", {"src": str(tmp_path / "src" / "reflow"), "docs": []}, tmp_path)


def test_a_build_through_the_stage_cache_records_its_working_copy_and_stays_current(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    data = tmp_path / ".reflow"
    docs = [{"slug": "a", "pdf": str(pdf), "data": str(data / "a"),
             "record": str(record_path(data, "a"))}]
    spec = {"src": str(provenance.PACKAGE_DIR), "options": {}, "docs": docs}
    before = _main("snapshot", spec, tmp_path)
    (data / "a").mkdir(parents=True)
    (data / "a" / "manifest.json").write_text(json.dumps({"v": VERSION}))
    (data / "a" / "working.pdf").write_bytes(b"%PDF-1.7 working")
    _main("record", {**spec, "before": before, "stageCache": True}, tmp_path)
    record = json.loads(record_path(data, "a").read_text())
    assert record["stage_cache"] == {"working": hashlib.sha256(b"%PDF-1.7 working").hexdigest()}
    [verdict] = _main("check", spec, tmp_path)
    assert verdict["status"] == "current" and "stage cache" in verdict["reasons"][0]
    _main("record", {**spec, "before": before}, tmp_path)   # rebuilt without it
    assert "stage_cache" not in json.loads(record_path(data, "a").read_text())


def test_a_document_s_own_options_go_over_the_spec_s_and_changing_them_makes_it_stale(tmp_path):
    """docs:build's variants (SampleDoc.options): a document with options of its own is recorded
    with them, and is stale once they change."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    data = tmp_path / ".reflow"
    doc = {"slug": "a", "pdf": str(pdf), "data": str(data / "a"), "record": str(record_path(data, "a")),
           "options": {"engine": "docling", "ocr": "none"}}
    spec = {"src": str(provenance.PACKAGE_DIR), "options": {"engine": "hybrid"}, "docs": [doc]}
    before = _main("snapshot", spec, tmp_path)
    assert before["a"]["options"]["engine"] == "docling" and before["a"]["options"]["ocr"] == "none"
    (data / "a").mkdir(parents=True)
    (data / "a" / "manifest.json").write_text(json.dumps({"v": VERSION}))
    _main("record", {**spec, "before": before}, tmp_path)
    assert _main("check", spec, tmp_path)[0]["status"] == "current"
    moved = {**spec, "docs": [{**doc, "options": {"engine": "docling"}}]}
    [verdict] = _main("check", moved, tmp_path)
    assert verdict["status"] == "stale" and "ocr none -> auto" in verdict["reasons"][0]


def test_build_info_is_the_public_part_of_a_build(tmp_path):
    """What the viewer shows ("How this transcript was made"): resolved models, answers against
    the cap, OCR pages, decisions applied, commit and time; never a path or the record."""
    from datetime import UTC, datetime
    data = tmp_path / "doc"
    data.mkdir()
    (data / "model.json").write_text(json.dumps({
        "alt_text": {"provider": "anthropic", "described": 3, "cached": 1},
        "ocr": {"runner": "ocrmypdf", "done": [3, 1, 2]}}))
    meta = {"label_assist": {"model": "claude-haiku-5-5", "asked": 4, "calls": 3, "skipped": 0},
            "structure_assist": {"model": "claude-haiku-5-5", "asked": 2, "calls": 2, "skipped": 1},
            "producer": "Adobe PDF Library 17.0", "creator": "Adobe InDesign 18.1 (Macintosh)"}
    (data / "document.json").write_text(json.dumps({"schema": 1, "meta": meta, "pages": [], "root": {}}))
    (data / "decisions.json").write_text(json.dumps({"applied": [{"id": "a"}, {"id": "b"}]}))
    opts = steps.options_record(Options(
        alt_text="anthropic", assist_labels=True, assist_structure=True, assist_provider="anthropic"))
    record = {"options": opts, "source": {"git": {"commit": "8d36858b7fb8aaaa", "uncommitted": ["x"]}}}
    info = provenance.build_info(record, data, built=datetime(2026, 11, 4, 20, 14, tzinfo=UTC))
    assert info["engine"] == "heuristic"
    assert info["ocr"] == {"mode": "auto", "runner": "ocrmypdf", "pages": [1, 2, 3]}
    assert info["ai"] == [
        {"step": "Form-field labels", "provider": "anthropic", "model": "claude-haiku-5-5",
         "answers": 4, "sent": 3, "cap": 25, "unasked": 0},
        {"step": "Headings", "provider": "anthropic", "model": "claude-haiku-5-5",
         "answers": 2, "sent": 2, "cap": 25, "unasked": 1}]
    # No model asked for: the provider's default, by name.
    assert info["altText"] == {"provider": "anthropic", "model": "claude-haiku-5-5", "described": 3, "cached": 1}
    assert info["decisionsApplied"] == 2
    assert info["converter"] == {"commit": "8d36858b7fb8", "edits": True}
    assert info["built"] == "2026-11-04T20:14:00Z"
    assert info["pdf"] == {"producer": "Adobe PDF Library 17.0", "creator": "Adobe InDesign 18.1 (Macintosh)"}
    assert str(tmp_path) not in json.dumps(info)


def test_build_info_lists_every_step_that_ran_in_registry_order_form_repair_included(tmp_path):
    data = tmp_path / "doc"
    data.mkdir()
    meta = {"form_repair": {"model": "qwen3-vl:8b", "proposed": 3, "unresolved": 1, "calls": 5,
                            "skipped": 2}}
    (data / "document.json").write_text(json.dumps({"schema": 1, "meta": meta, "pages": [], "root": {}}))
    opts = steps.options_record(Options(
        assist_page_vision=True, assist_form_repair=True, assist_scan_fields=True,
        assist_provider="anthropic", assist_form_repair_max_calls=9))
    info = provenance.build_info({"options": opts, "source": {}}, data)
    assert [step["step"] for step in info["ai"]] == ["Scanned-form fields", "Page vision", "Form repair"]
    assert info["ai"][-1] == {"step": "Form repair", "provider": "ollama", "model": "qwen3-vl:8b",
                              "answers": 4, "sent": 5, "cap": 9, "unasked": 2}


def test_a_direct_build_records_what_its_transcript_cost(tmp_path):
    """--engine direct: the model, the pages read and left unread, and the tokens, dollars
    and seconds per page and in all, in build_info and in the record docs:build writes."""
    data = tmp_path / "doc"
    data.mkdir()
    meta = {"direct": {"model": "claude-haiku-5-5", "provider": "anthropic", "cached": 1,
                       "pages": {"2": {"in": 10, "out": 5, "usd": 0.002, "s": 1.5, "cached": True},
                                 "1": {"in": 20, "out": 7, "usd": 0.003, "s": 2.0, "cached": False}},
                       "unread": {"3": "the run reached its spending cap"}, "tokens_in": 30,
                       "tokens_out": 12, "usd": 0.005, "seconds": 3.5, "spent": 0.003,
                       "stopped": True}}
    (data / "document.json").write_text(json.dumps({"schema": 1, "meta": meta, "pages": [], "root": {}}))
    record = {"options": steps.options_record(Options(engine="direct")), "source": {"git": None}}
    direct = provenance.build_info(record, data)["direct"]
    assert direct == {
        "model": "claude-haiku-5-5", "provider": "anthropic", "prompt": None, "pages": 2, "cached": 1, "unread": [3],
        "tokensIn": 30, "tokensOut": 12, "dollars": 0.005, "seconds": 3.5, "spent": 0.003,
        "stopped": True,
        "perPage": [{"page": 1, "tokensIn": 20, "tokensOut": 7, "dollars": 0.003, "seconds": 2.0,
                     "cached": False},
                    {"page": 2, "tokensIn": 10, "tokensOut": 5, "dollars": 0.002, "seconds": 1.5,
                     "cached": True}]}
    assert "direct" not in provenance.build_info(record, tmp_path)
    assert any("claude-haiku-5-5" in why for why in steps.uncovered(Options(engine="direct")))


def test_build_info_of_a_build_with_no_ai_says_so(tmp_path):
    record = {"options": steps.options_record(Options()), "source": {"git": None}}
    info = provenance.build_info(record, tmp_path)   # no model.json, document.json or ledger
    assert info["ai"] == [] and info["altText"] is None
    assert info["ocr"]["pages"] == [] and info["decisionsApplied"] == 0 and info["converter"] is None
    assert info["pdf"] == {"producer": None, "creator": None}
    assert "timings" not in info   # no timings seen: none claimed


def test_build_info_holds_the_seconds_each_stage_took(tmp_path):
    record = {"options": steps.options_record(Options()), "source": {"git": None}}
    seen = {"seconds": {"extract": 0.854, "publish": 1.03, "bad": "x", "flag": True},
            "gpuWait": {"extract": 0.2}}
    info = provenance.build_info(record, tmp_path, timings=seen)
    assert info["timings"] == {"seconds": {"extract": 0.85, "publish": 1.03}, "gpuWait": {"extract": 0.2}}
    for nothing in (None, {}, {"seconds": {}}, {"seconds": "x"}, "x"):
        assert "timings" not in provenance.build_info(record, tmp_path, timings=nothing)


def test_record_publishes_the_timings_the_build_saw(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    data = tmp_path / ".reflow"
    docs = [{"slug": "a", "pdf": str(pdf), "data": str(data / "a"), "record": str(record_path(data, "a"))}]
    spec = {"src": str(provenance.PACKAGE_DIR), "options": {}, "docs": docs}
    before = _main("snapshot", spec, tmp_path)
    (data / "a").mkdir(parents=True)
    (data / "a" / "manifest.json").write_text(json.dumps({"v": VERSION}))
    timings = {"a": {"seconds": {"extract": 1.5}, "gpuWait": {}}}
    written = _main("record", {**spec, "before": before, "timings": timings}, tmp_path)
    assert written["a"]["info"]["timings"] == timings["a"]


def test_record_returns_each_document_s_build_info(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    data = tmp_path / ".reflow"
    docs = [{"slug": "a", "pdf": str(pdf), "data": str(data / "a"), "record": str(record_path(data, "a"))}]
    spec = {"src": str(provenance.PACKAGE_DIR), "options": {}, "docs": docs}
    before = _main("snapshot", spec, tmp_path)
    (data / "a").mkdir(parents=True)
    (data / "a" / "manifest.json").write_text(json.dumps({"v": VERSION}))
    written = _main("record", {**spec, "before": before}, tmp_path)
    assert written["a"]["info"]["engine"] == "heuristic" and written["a"]["info"]["ai"] == []
