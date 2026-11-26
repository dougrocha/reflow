"""Rendering a saved model after a render-only change: a conversion with
the stage cache renders the model it can prove, from the previous output or the stage root,
and publishes what a full conversion publishes; anything it cannot prove is built again."""

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from reflow import identity, pipeline, saved
from reflow.convert import convert
from reflow.names import BUILD_JSON, DOCUMENT_JSON, MODEL_JSON
from reflow.pipeline import Model
from reflow.run import Options
from testkit import needs, no_build, sample, walk_json

RESNET = sample("resnet.pdf")   # 12 pages, 7 figures
pytestmark = needs(RESNET)


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    """An uncached conversion, and one with the stage cache that kept its model."""
    work = tmp_path_factory.mktemp("saved")
    convert(RESNET, work / "uncached", page_images=False)
    summary = convert(RESNET, work / "kept", page_images=False, stage_cache=work / "root")
    assert summary.model == "built, kept"
    return work


def _same_as_full(out: Path, full: Path) -> None:
    """Every file of a full conversion; build.json is written only with the stage cache."""
    got, want = _files(out), _files(full / "uncached")
    assert set(got) - set(want) <= {BUILD_JSON}
    assert {k: v for k, v in got.items() if k != BUILD_JSON} == want


def test_the_previous_output_is_rendered_again_and_publishes_what_a_full_conversion_does(
        full, tmp_path, monkeypatch):
    out = tmp_path / "out"
    shutil.copytree(full / "kept", out)
    no_build(monkeypatch)
    summary = convert(RESNET, out, page_images=False, stage_cache=tmp_path / "empty-root")
    assert summary.model == "previous output" and set(summary.seconds) == {"restore", "publish"}
    assert summary.figures == 7 and summary.ocr.attempted == [] and not summary.ocr.failed
    _same_as_full(out, full)
    assert _files(out) == _files(full / "kept")


def test_the_stage_root_serves_a_fresh_folder(full, tmp_path, monkeypatch):
    root = tmp_path / "root"
    shutil.copytree(full / "root", root)
    no_build(monkeypatch)
    summary = convert(RESNET, tmp_path / "out", jobs=2, page_images=False, stage_cache=root)
    assert summary.model == "stage cache"
    _same_as_full(tmp_path / "out", full)


def test_a_render_setting_reuses_the_model_and_publishes_what_a_full_conversion_does(
        full, tmp_path, monkeypatch):
    convert(RESNET, tmp_path / "uncached", page_widths=(600,), srcset_max_pages=4)
    out = tmp_path / "out"
    shutil.copytree(full / "kept", out)
    real = pipeline.read_pages
    no_build(monkeypatch)
    summary = convert(RESNET, out, page_widths=(600,), srcset_max_pages=4,
                      stage_cache=tmp_path / "root")
    monkeypatch.setattr(pipeline, "read_pages", real)
    assert summary.model == "previous output"
    got, want = _files(out), _files(tmp_path / "uncached")
    assert {k: v for k, v in got.items() if k != BUILD_JSON} == want


@pytest.mark.parametrize("damage", ["document", "figure gone", "figure added", "key", "schema",
                                    "no record", "round trip"])
def test_a_saved_model_that_cannot_be_proved_is_built_again(full, tmp_path, damage):
    out = tmp_path / "out"
    shutil.copytree(full / "kept", out)
    record = json.loads((out / BUILD_JSON).read_text())
    if damage == "document":
        body = (out / DOCUMENT_JSON).read_text()
        (out / DOCUMENT_JSON).write_text(body.replace('"title"', '"title" ', 1))
    elif damage == "figure gone":
        next((out / "figures").iterdir()).unlink()
    elif damage == "figure added":
        (out / "figures" / "p0099-0.webp").write_bytes(b"RIFF")
    elif damage == "key":
        record["model"]["key"] = "0" * 20
    elif damage == "schema":
        record["schema"] = 0
    elif damage == "no record":
        (out / BUILD_JSON).unlink()
    elif damage == "round trip":   # the same model, written in another form: digest and all
        doc = json.loads((out / DOCUMENT_JSON).read_text())
        (out / DOCUMENT_JSON).write_text(json.dumps(doc, indent=1))
        record["model"]["digest"] = saved.digest(out)
    if damage in ("key", "schema", "round trip"):
        (out / BUILD_JSON).write_text(json.dumps(record))
    summary = convert(RESNET, out, page_images=False, stage_cache=tmp_path / "root")
    assert summary.model == "built, kept"
    _same_as_full(out, full)
    assert not list(tmp_path.glob(".out.tmp-*"))   # no stage left behind


def test_a_stage_root_entry_of_another_model_is_refused_and_removed(full, tmp_path):
    root = tmp_path / "root"
    shutil.copytree(full / "root", root)
    [entry] = list((root / "model").glob("*/*"))
    (entry / DOCUMENT_JSON).write_text("{}")
    summary = convert(RESNET, tmp_path / "out", page_images=False, stage_cache=root)
    assert summary.model == "built, kept"
    _same_as_full(tmp_path / "out", full)
    assert _files(entry) == _files(next((full / "root" / "model").glob("*/*")))   # kept anew


def test_reuse_model_false_builds_the_model_and_keeps_it(full, tmp_path):
    out = tmp_path / "out"
    shutil.copytree(full / "kept", out)
    summary = convert(RESNET, out, page_images=False, stage_cache=tmp_path / "root",
                      reuse_model=False)
    assert summary.model == "built, kept" and (tmp_path / "root" / "model").is_dir()
    _same_as_full(out, full)


def test_without_the_stage_cache_nothing_is_recorded(full):
    assert not (full / "uncached" / BUILD_JSON).exists()
    assert (full / "uncached" / MODEL_JSON).exists()


def test_the_figures_map_is_saved_with_the_crop_sizes(full):
    model = Model.read(full / "kept")
    assert len(model.figures) == 7
    for name, w, h in model.figures.values():
        with Image.open(full / "kept" / "figures" / name) as im:
            assert im.size == (w, h)


def test_a_document_that_does_not_round_trip_is_not_read(full, tmp_path):
    folder = tmp_path / "m"
    shutil.copytree(full / "kept", folder)
    doc = json.loads((folder / DOCUMENT_JSON).read_text())
    (folder / DOCUMENT_JSON).write_text(json.dumps(doc, indent=1))
    with pytest.raises(ValueError, match="round-trip"):
        Model.read(folder)


def test_a_model_option_changes_the_model_key_and_a_render_setting_only_the_render_key():
    base = saved.identify(RESNET, Options())
    widths = saved.identify(RESNET, Options(page_widths=(600,), page_images=False))
    ocr = saved.identify(RESNET, Options(ocr="none"))
    assert base.model and base.render
    assert widths.model == base.model and widths.render != base.render
    assert ocr.model != base.model and ocr.render != base.render


@pytest.mark.parametrize("options,why", [
    ({"alt_text": "ollama"}, "figure descriptions"),
    ({"assist_labels": True}, "form-field labels"),
    ({"assist_page_vision": True}, "heading changes"),
    ({"password": "secret"}, "password"),
])
def test_what_no_key_can_pin_down_is_never_kept_or_reused(options, why):
    identity = saved.identify(RESNET, Options(**options))
    assert identity.model is None and any(why in r for r in identity.unprovable)


def test_an_ocr_runner_whose_data_is_unknown_makes_the_model_unprovable(monkeypatch):
    class Runner:
        name = "mystery"

        def environment(self, langs):
            return None
    monkeypatch.setattr(saved, "make_runner", lambda mode: Runner())
    identity = saved.identify(RESNET, Options())
    assert identity.model is None and "can't be identified" in identity.unprovable[0]


def test_a_model_whose_ocr_failed_is_not_kept(full, tmp_path, monkeypatch):
    real = pipeline.finish

    def failing(run, backbone, providers):
        run.ocr.failed[1] = "timed out"
        return real(run, backbone, providers)
    monkeypatch.setattr(pipeline, "finish", failing)
    summary = convert(RESNET, tmp_path / "out", page_images=False, stage_cache=tmp_path / "root")
    assert summary.model.startswith("built, not kept: OCR failed on pages [1]")
    assert not (tmp_path / "out" / BUILD_JSON).exists()
    assert not (tmp_path / "root" / "model").exists()


def test_the_saved_model_has_the_stage_root_layout(full):
    [entry] = list((full / "root" / "model").glob("*/*"))
    assert entry.parent.name == identity.file_sha256(RESNET)[:16]
    assert json.loads((entry / BUILD_JSON).read_text())["model"]["key"] == entry.name


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="the text witness needs Tesseract")
def test_a_suspect_page_s_witness_facts_are_the_same_fresh_and_reused(tmp_path, monkeypatch):
    """A shifted /ToUnicode (tests/robustness) makes page 1 suspect, so the text witness reads it,
    and the font repair decodes what the witness's votes decide (font-map). A reused model
    publishes what a fresh conversion does."""
    from conftest import make_text_pdf
    from testkit import shifted_pdf
    clean = make_text_pdf(tmp_path / "clean.pdf", [[
        (72, 72, 11, "The staff filed five official files after the final fit check of the year."),
        (72, 90, 11, "Our office flew the flag of the field team in the fall, as it does often."),
        (72, 108, 11, "A brief note of thanks for the effort of everyone who offered to help.")]])
    pdf = shifted_pdf(clean, tmp_path / "suspect.pdf")
    convert(pdf, tmp_path / "uncached", page_images=False)
    doc = json.loads((tmp_path / "uncached" / DOCUMENT_JSON).read_text())
    assert any(f["code"] == "font-map" for f in doc["root"].get("facts", []))
    assert any("layer_text" in n.get("attrs", {}) for n in walk_json(doc["root"]))
    root = tmp_path / "root"
    assert convert(pdf, tmp_path / "kept", page_images=False, stage_cache=root).model == "built, kept"
    no_build(monkeypatch)
    summary = convert(pdf, tmp_path / "reused", jobs=2, page_images=False, stage_cache=root)
    assert summary.model == "stage cache"
    _same_as_full(tmp_path / "reused", tmp_path)
