"""Applying a decisions store to a saved model without converting again: a conversion with
`--decisions` and the stage cache reuses the model it saved, replays the store on it (revert, then
the same pass a full build runs) and publishes what a full build with that store publishes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import decisions_kit as kit
from reflow import identity, pipeline, saved
from reflow.analyze import anchors
from reflow.analyze import decisions as dec, decisions_apply as da
from reflow.convert import convert
from reflow.model import Document, Role, Source
from reflow.names import BUILD_JSON, DOCUMENT_JSON, PAGES_DIR
from reflow.run import Options
from testkit import NOT_COMMITTED, no_build, ocr_available, sample

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
SAME = ("document.json", "review.json", "decisions.json", "passages.jsonl", "index.html",
        "manifest.json", "toc.json", "chunks", "layout", "figures", "trace")


def needs_ocr(test):
    test = pytest.mark.skipif(not ocr_available(),
                              reason="No OCR runner (xfail_without_ocr)")(test)
    return pytest.mark.pages(test)


def files(root: Path, names=SAME) -> dict[str, bytes]:
    out = {}
    for name in names:
        path = root / name
        if path.is_file():
            out[name] = path.read_bytes()
        elif path.is_dir():
            out.update({p.relative_to(root).as_posix(): p.read_bytes()
                        for p in sorted(path.rglob("*")) if p.is_file()})
    return out


def doc_of(out: Path) -> Document:
    return Document.from_json((out / DOCUMENT_JSON).read_text(encoding="utf-8"))


def figure_alts(doc: Document) -> dict[str, tuple[str, str]]:
    return {n.id: (n.attrs.get("alt", ""), n.attrs.get("provenance", ""))
            for n in doc.root.walk() if n.role == Role.FIGURE and n.bbox}


# -- the born-digital document, with figures -----------------------------------------------------

RESNET = sample("resnet.pdf")


@pytest.fixture(scope="module")
def resnet(tmp_path_factory):
    """S0 = one alt text. S = S0, a dismissal, another alt, and an undo of S0's alt."""
    if not RESNET.exists():
        pytest.skip(f"samples/resnet.pdf {NOT_COMMITTED}")
    work = tmp_path_factory.mktemp("resnet")
    plain = work / "plain"
    convert(RESNET, plain, page_images=False)
    doc = doc_of(plain)
    sha = doc.meta["sha256"]
    figs = [n for n in doc.root.walk() if n.role == Role.FIGURE and n.bbox]
    assert len(figs) >= 2
    items = json.loads((plain / "review.json").read_text())
    warning = next(f["id"] for i in items for f in i.get("findings", [])
                   if f["severity"] == "warning")

    def alt(fig, text):
        return kit.record(sha, "alt", target={"anchor": anchors.anchor_of(fig).to_dict()},
                      alt={"text": text, "decorative": False})

    first, second = alt(figs[0], "A reviewer's first reading"), alt(figs[1], "Another reading")
    s0, s = work / "s0.jsonl", work / "s.jsonl"
    for path in (s0, s):
        kit.new_store(path, "resnet")
        kit.append(path, first)
    for body in (kit.record(sha, "dismiss", findings=[warning]), second,
                 kit.record(sha, "undo", of=first["id"])):
        kit.append(s, body)
    return work, figs, s0, s


def test_a_store_replays_on_the_saved_model_and_publishes_what_a_full_build_does(
        resnet, tmp_path, monkeypatch):
    work, figs, s0, s = resnet
    root, out = tmp_path / "root", tmp_path / "out"
    first = convert(RESNET, out, page_images=False, stage_cache=root, decisions=s0)
    assert first.model == "built, kept" and first.decisions["applied"] == 1
    assert figure_alts(doc_of(out))[figs[0].id] == ("A reviewer's first reading", "reviewer")

    real = pipeline.read_pages
    no_build(monkeypatch)
    again = convert(RESNET, out, page_images=False, stage_cache=root, decisions=s)
    monkeypatch.setattr(pipeline, "read_pages", real)
    assert again.model == "previous output"
    assert again.decisions == {"applied": 1, "dismissed": 1, "orphaned": 0}

    fresh = tmp_path / "fresh"
    convert(RESNET, fresh, page_images=False, decisions=s)
    assert files(out) == files(fresh)
    assert (json.loads((out / "manifest.json").read_text())["v"]
            == json.loads((fresh / "manifest.json").read_text())["v"])
    # the undone alt is the draft's again, as in a build that never saw it
    plain_alts = figure_alts(doc_of(work / "plain"))
    assert figure_alts(doc_of(out))[figs[0].id] == plain_alts[figs[0].id]
    assert figure_alts(doc_of(out))[figs[1].id] == ("Another reading", "reviewer")


def test_the_replay_is_recorded_so_the_next_conversion_reuses_the_decided_model(
        resnet, tmp_path, monkeypatch):
    work, _, s0, s = resnet
    root, out = tmp_path / "root", tmp_path / "out"
    convert(RESNET, out, page_images=False, stage_cache=root, decisions=s0)
    no_build(monkeypatch)
    convert(RESNET, out, page_images=False, stage_cache=root, decisions=s)
    record_ = json.loads((out / BUILD_JSON).read_text())
    assert record_["model"]["digest"] == saved.digest(out)   # what the replay left in the stage
    # and again, from the stage root or the output: the same bytes
    first = files(out)
    shutil.rmtree(out)
    third = convert(RESNET, out, page_images=False, stage_cache=root, decisions=s)
    assert third.model == "stage cache" and files(out) == first


def test_a_model_saved_with_decisions_restored_without_any_equals_a_build_without(
        resnet, tmp_path, monkeypatch):
    work, _, s0, _ = resnet
    root, out = tmp_path / "root", tmp_path / "out"
    convert(RESNET, out, page_images=False, stage_cache=root, decisions=s0)
    no_build(monkeypatch)
    summary = convert(RESNET, out, page_images=False, stage_cache=root)
    assert summary.model == "previous output" and summary.decisions == {}
    assert files(out, [n for n in SAME if n != "manifest.json"]) == files(
        work / "plain", [n for n in SAME if n != "manifest.json"])
    assert not (out / "decisions.json").exists()


def test_the_decisions_file_is_not_in_the_model_key(tmp_path):
    opts = Options(decisions=tmp_path / "a.jsonl")
    assert saved.identify(FW9, opts).model == saved.identify(FW9, Options()).model


# -- a scanned page ------------------------------------------------------------------------------


@needs_ocr
def test_a_scanned_page_replays_a_store_and_an_undo_brings_ocrs_reading_back(
        tmp_path, monkeypatch):
    pdf = SAMPLES / "scan.pdf"
    root, base = tmp_path / "root", tmp_path / "base"
    convert(pdf, base, page_images=False, stage_cache=root)
    doc = doc_of(base)
    sha = doc.meta["sha256"]
    from reflow.render.passages import passages as quotable
    ids = {p.id for p in quotable(doc)}

    def target(node, mark, kind, **extra):
        body = kit.record(sha, kind, target={"anchor": anchors.anchor_of(node).to_dict(), "range": {
            "was": mark.text, "before": node.text[max(0, mark.start - 20):mark.start],
            "after": node.text[mark.end:mark.end + 20], "rule": "ocr-doubt"}}, **extra)
        return body if isinstance(da.match(doc, dec.parse_record(body)), da.Target) else None

    picks, used = [], set()
    for node in doc.root.walk():
        if (node.source != Source.OCR or node.role != Role.P or node.page in used
                or node.id not in ids):
            continue
        for mark in node.marks:
            if mark.kind != "doubt":
                continue
            kind = ("correct", "keep", "correct")[len(picks)]
            extra = {"now": mark.text + "."} if kind == "correct" else {}
            if (body := target(node, mark, kind, **extra)) is not None:
                picks.append((node, mark, body))
                used.add(node.page)
                break
        if len(picks) == 3:
            break
    assert len(picks) == 3
    (first, first_mark, correction), (_, _, keep), (_, _, other) = picks
    s0, s = tmp_path / "s0.jsonl", tmp_path / "s.jsonl"
    for path in (s0, s):
        kit.new_store(path, "scan")
        kit.append(path, correction)
    for body in (keep, other, kit.record(sha, "undo", of=correction["id"])):
        kit.append(s, body)

    out = tmp_path / "out"
    convert(pdf, out, page_images=False, stage_cache=root, decisions=s0)
    mid = {n.id: n for n in doc_of(out).root.walk()}[first.id]
    assert [m.kind for m in mid.marks].count("fix") == 1

    real = pipeline.read_pages
    no_build(monkeypatch)
    again = convert(pdf, out, page_images=False, stage_cache=root, decisions=s)
    monkeypatch.setattr(pipeline, "read_pages", real)
    assert again.model == "previous output"
    fresh = tmp_path / "fresh"
    convert(pdf, fresh, page_images=False, decisions=s)
    assert files(out) == files(fresh)
    # the undone correction: OCR's reading, its doubt mark and its review item
    back = {n.id: n for n in doc_of(out).root.walk()}[first.id]
    assert back.text == first.text
    assert [m.kind for m in back.marks if m.kind == "doubt"] == [
        m.kind for m in first.marks if m.kind == "doubt"]
    assert not [m for m in back.marks if m.kind == "fix"]
    items = json.loads((out / "review.json").read_text())
    assert any(i.get("page") == first.page or i.get("anchor", {}).get("page") == first.page
               for i in items)


# -- the page images ----------------------------------------------------------------------------

FW9 = SAMPLES / "fw9.pdf"


def _store(tmp_path: Path, pdf: Path, out: Path) -> Path:
    doc = doc_of(out)
    items = json.loads((out / "review.json").read_text())
    warning = next(f["id"] for i in items for f in i.get("findings", [])
                   if f["severity"] == "warning")
    path = tmp_path / "s.jsonl"
    kit.new_store(path, "fw9")
    kit.append(path, kit.record(doc.meta["sha256"], "dismiss", findings=[warning]))
    return path


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    """Page images rendered anywhere are counted here."""
    calls = []
    from reflow import output
    real = output.render_pages
    monkeypatch.setattr(output, "render_pages", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    return calls


def test_a_decisions_only_change_carries_the_page_images_and_renders_none(tmp_path, rendered):
    root, out = tmp_path / "root", tmp_path / "out"
    convert(FW9, out, stage_cache=root)
    assert len(rendered) == 1
    before = files(out, ("pages",))
    path = _store(tmp_path, FW9, out)
    again = convert(FW9, out, stage_cache=root, decisions=path)
    assert again.model == "previous output" and len(rendered) == 1   # no new render
    fresh = tmp_path / "fresh"
    convert(FW9, fresh, decisions=path)
    assert len(rendered) == 2
    assert files(out, (*SAME, PAGES_DIR)) == files(fresh, (*SAME, PAGES_DIR))
    assert files(out, ("pages",)) == before


def test_a_render_setting_or_a_damaged_pages_folder_renders_the_images_again(tmp_path, rendered):
    root, out = tmp_path / "root", tmp_path / "out"
    convert(FW9, out, stage_cache=root)
    convert(FW9, out, stage_cache=root, page_widths=(600,))
    assert len(rendered) == 2
    assert {p.name for p in (out / "pages").iterdir()} == {f"{n:04d}.webp" for n in range(1, 7)}
    next((out / "pages").iterdir()).unlink()
    convert(FW9, out, stage_cache=root, page_widths=(600,))
    assert len(rendered) == 3
    assert len(list((out / "pages").iterdir())) == 6


def test_the_stage_root_never_carries_images(tmp_path, rendered):
    root = tmp_path / "root"
    convert(FW9, tmp_path / "a", stage_cache=root)
    summary = convert(FW9, tmp_path / "b", stage_cache=root)
    assert summary.model == "stage cache" and len(rendered) == 2


# -- reflow rerender -----------------------------------------------------------------------------

from typer.testing import CliRunner  # noqa: E402

from reflow import stagecache  # noqa: E402
from reflow.cli import app  # noqa: E402
from reflow.rerender import CannotRerender, NoConversion, rerender  # noqa: E402


def tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """fw9, under another file name, converted with the stage cache, and a store of one dismissal."""
    work = tmp_path_factory.mktemp("rerender")
    pdf = work / "Form W-9 (2024).pdf"
    shutil.copyfile(FW9, pdf)
    out = work / "out"
    convert(pdf, out, page_images=False, stage_cache=work / "root")
    return work, pdf, out, _store(work, pdf, out)


def fresh_out(built, tmp_path) -> Path:
    shutil.copytree(built[2], tmp_path / "out")
    return tmp_path / "out"


def leftovers(out: Path) -> list[str]:
    return sorted(p.name for p in out.parent.iterdir() if p.name.startswith(f".{out.name}"))


def test_rerender_publishes_what_a_full_conversion_with_the_store_publishes(
        built, tmp_path, monkeypatch):
    work, pdf, _, store = built
    out = fresh_out(built, tmp_path)
    real = pipeline.read_pages
    no_build(monkeypatch)
    summary = rerender(out, store)
    monkeypatch.setattr(pipeline, "read_pages", real)
    assert summary.model == "previous output" and summary.decisions["dismissed"] == 1
    full = tmp_path / "full"
    convert(pdf, full, page_images=False, decisions=store)
    assert files(out) == files(full)
    assert not leftovers(out)
    # build.json says the same as it did: the decided model is a base for the next rerender
    assert rerender(out, store).model == "previous output"


def test_the_cli_prints_the_decisions_line_and_the_progress_lines(built, tmp_path):
    out = fresh_out(built, tmp_path)
    result = CliRunner().invoke(app, ["rerender", str(out), "--decisions", str(built[3]),
                                      "--progress-json"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split("\t")[:2] == ["start", str(out)]
    ok = [line for line in lines if line.startswith("ok\t")]
    assert len(ok) == 1 and ok[0].split("\t")[1] == str(out)
    everything = [json.loads(line) for line in lines if line.startswith("{")]
    events = [e for e in everything if e["event"] == "progress"]
    assert events and all(e["out"] == str(out) for e in everything)
    assert [e["event"] for e in everything if e["event"] != "progress"] == ["timings"]
    assert "decisions: 0 applied, 1 dismissed, 0 orphaned" in " ".join(result.stdout.split())


class _Runner:
    name = "fake-ocr"

    def environment(self, _langs):
        return "data-v2"


@pytest.mark.parametrize("change", ["code", "tools", "ocr", "options", "pdf"])
def test_a_conversion_the_current_build_would_not_make_is_refused_and_left_as_it_was(
        built, tmp_path, monkeypatch, change):
    out = fresh_out(built, tmp_path)
    if change == "ocr":  # the OCR data moved, and no tool's version did
        monkeypatch.setattr(saved, "make_runner", lambda *_: _Runner())
    elif change == "code":
        monkeypatch.setattr(stagecache, "code_hash", lambda *_, **__: "0" * 64)
    elif change == "tools":
        monkeypatch.setattr(identity, "tool_key", lambda: "python=0.0.0")
    elif change == "options":
        record = json.loads((out / BUILD_JSON).read_text())
        record["options"]["chunk_pages"] = 3
        (out / BUILD_JSON).write_text(json.dumps(record))
    else:
        (out / "original.pdf").write_bytes(SAMPLES.joinpath("scan.pdf").read_bytes())
    before = tree(out)
    result = CliRunner().invoke(app, ["rerender", str(out), "--decisions", str(built[3]),
                                      "--progress-json"])
    assert result.exit_code == 4, result.output
    failed = [line for line in result.stdout.splitlines() if line.startswith("FAILED\t")]
    reason = "tools" if change == "ocr" else change
    assert len(failed) == 1 and f"CannotRerender: {reason}:" in failed[0]
    assert tree(out) == before and not leftovers(out)


def test_with_a_source_it_runs_only_for_the_pdf_and_options_a_full_build_would_use(
        built, tmp_path):
    work, pdf, _, store = built
    out = fresh_out(built, tmp_path)
    assert rerender(out, store, source=pdf).model == "previous output"
    assert rerender(out, store, source=pdf, options={"engine": "heuristic"}).decisions[
        "dismissed"] == 1
    changed = tmp_path / pdf.name           # the sample was replaced since the conversion
    changed.write_bytes(SAMPLES.joinpath("scan.pdf").read_bytes())
    renamed = tmp_path / "fw9.pdf"
    shutil.copyfile(pdf, renamed)
    before = tree(out)
    for source, options, reason in ((changed, {}, "pdf"), (renamed, {}, "pdf"),
                                    (pdf, {"engine": "docling"}, "options"),
                                    (pdf, {"alt_text": "local"}, "options")):
        with pytest.raises(CannotRerender) as e:
            rerender(out, store, source=source, options=options)
        assert e.value.reason == reason, (source, options)
    assert tree(out) == before and not leftovers(out)
    with pytest.raises(ValueError):
        rerender(out, store, options={"engine": "heuristic"})
    with pytest.raises(ValueError):
        rerender(out, store, source=pdf, options={"chunk_pages": "3"})
    result = CliRunner().invoke(app, ["rerender", str(out), "--decisions", str(store),
                                      "--source", str(pdf), "--alt-text", "local",
                                      "--progress-json"])
    assert result.exit_code == 4, result.output
    assert "CannotRerender: options:" in result.stdout
    assert tree(out) == before


def test_a_folder_without_a_record_or_a_store_that_is_not_a_file_exits_2(built, tmp_path):
    out = fresh_out(built, tmp_path)
    (out / BUILD_JSON).unlink()
    assert CliRunner().invoke(app, ["rerender", str(out), "--decisions", str(built[3])]
                              ).exit_code == 2
    with pytest.raises(NoConversion):
        rerender(out, built[3])
    out = tmp_path / "again" / "out"
    shutil.copytree(built[2], out)
    result = CliRunner().invoke(app, ["rerender", str(out), "--decisions",
                                      str(tmp_path / "nope.jsonl")])
    assert result.exit_code == 2
    assert CliRunner().invoke(app, ["rerender", str(tmp_path / "empty"), "--decisions",
                                    str(built[3])]).exit_code == 2


def test_a_record_from_before_rerender_is_refused_not_misread(built, tmp_path):
    out = fresh_out(built, tmp_path)
    record = json.loads((out / BUILD_JSON).read_text())
    del record["options"]
    (out / BUILD_JSON).write_text(json.dumps(record))
    with pytest.raises(CannotRerender) as e:
        rerender(out, built[3])
    assert e.value.reason == "record"


def test_a_failure_during_the_replay_leaves_the_output_and_no_stage(
        built, tmp_path, monkeypatch):
    out = fresh_out(built, tmp_path)
    before = tree(out)

    def boom(*_):
        raise RuntimeError("the replay broke")
    monkeypatch.setattr(da, "apply", boom)
    with pytest.raises(RuntimeError, match="replay broke"):
        rerender(out, built[3])
    assert tree(out) == before and not leftovers(out)


def test_the_page_images_are_carried_and_the_carried_files_are_the_same_bytes(tmp_path, rendered):
    out = tmp_path / "out"
    convert(FW9, out, stage_cache=tmp_path / "root")
    before = files(out, ("pages",))
    path = _store(tmp_path, FW9, out)
    rerender(out, path)
    assert len(rendered) == 1 and files(out, ("pages",)) == before


def test_a_build_with_a_store_and_no_stage_cache_can_be_rerendered_from_its_own_output(
        tmp_path, monkeypatch):
    """The web's builds pass a store but no stage cache: the output's own build.json proves the
    model, so Apply decisions needs no stage root and keeps nothing outside the output."""
    store = tmp_path / "s.jsonl"
    out = tmp_path / "out"
    first = convert(FW9, out, page_images=False, decisions=store)
    assert (out / BUILD_JSON).is_file() and first.model == "built"
    no_build(monkeypatch)
    again = convert(FW9, out, page_images=False, decisions=store)
    assert again.model == "previous output"
    assert not list(tmp_path.glob("**/model/*"))   # no stage root was written


def test_a_build_without_a_store_or_a_stage_cache_records_nothing(tmp_path):
    out = tmp_path / "out"
    assert convert(FW9, out, page_images=False).model == ""
    assert not (out / BUILD_JSON).exists()
