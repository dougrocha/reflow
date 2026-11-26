"""What a stage-cache key holds: the whole import closure of the stage's code, its
resources, the OCR engine's data, and the artifacts the stage reads. Each hole has a test that a
change to the missing input misses, and each stage's closure is pinned to an allow-list."""

import shutil
import time
from functools import partial
from pathlib import Path

import pikepdf
import pytest
from conftest import extract_cached

from reflow import stagecache
from reflow.ingest.ocr import OcrmypdfRunner, OcrPage, OcrPlan
from reflow.stagecache import CachedRunner, ClosureError, code_hash
from testkit import blank_pdf


def _tree(root: Path, files: dict[str, str]) -> Path:
    """A package tree under `root`: {"reflow/extract/a.py": "source"}; every directory on a
    module's path gets an empty __init__.py unless `files` gives one."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for path in list(root.rglob("*.py")):
        for parent in path.relative_to(root).parents:
            if parent != Path(".") and not (root / parent / "__init__.py").exists():
                (root / parent / "__init__.py").write_text("")
    return root


def _changes(root: Path, entries: tuple[str, ...], name: str, text: str) -> bool:
    before = code_hash(entries, root)
    (root / name).write_text(text)
    return code_hash(entries, root) != before


# Hole 1: relative imports.

def test_a_relative_import_is_in_the_key(tmp_path):
    root = _tree(tmp_path, {"reflow/extract/a.py": "from . import b\nfrom .c import f\n",
                            "reflow/extract/b.py": "X = 1\n", "reflow/extract/c.py": "def f(): pass\n"})
    assert _changes(root, ("reflow.extract.a",), "reflow/extract/b.py", "X = 2\n")
    assert _changes(root, ("reflow.extract.a",), "reflow/extract/c.py", "def f(): return 1\n")


def test_a_parent_relative_import_is_in_the_key(tmp_path):
    root = _tree(tmp_path, {"reflow/extract/a.py": "from ..model import BBox\n",
                            "reflow/model.py": "BBox = tuple\n"})
    assert _changes(root, ("reflow.extract.a",), "reflow/model.py", "BBox = list\n")


# Hole 2: a parent package's own imports run whenever one of its modules is imported.

def test_a_parent_package_s_imports_are_in_the_key(tmp_path):
    root = _tree(tmp_path, {"reflow/extract/__init__.py": "from reflow import helper\n",
                            "reflow/extract/a.py": "X = 1\n", "reflow/helper.py": "Y = 1\n"})
    assert _changes(root, ("reflow.extract.a",), "reflow/helper.py", "Y = 2\n")


def test_the_else_of_a_type_checking_block_is_in_the_key(tmp_path):
    root = _tree(tmp_path, {
        "reflow/a.py": "if TYPE_CHECKING:\n    from reflow import t\nelse:\n    from reflow import e\n",
        "reflow/t.py": "", "reflow/e.py": "Z = 1\n"})
    assert not _changes(root, ("reflow.a",), "reflow/t.py", "Z = 2\n")
    assert _changes(root, ("reflow.a",), "reflow/e.py", "Z = 2\n")


# Hole 3: imports the scanner can't resolve fail closed.

@pytest.mark.parametrize("source", [
    "from reflow.missing import x\n",
    "import reflow.missing\n",
    "from ...outside import x\n",            # climbs out of the package
    "import importlib\nm = importlib.import_module(name)\n",
    "from importlib import import_module\nm = import_module('.b', 'reflow')\n",
    "m = __import__(name)\n",
    "from importlib import import_module as load\nm = load(name)\n",
])
def test_an_import_the_scanner_cannot_resolve_fails_closed(tmp_path, source):
    root = _tree(tmp_path, {"reflow/extract/a.py": source})
    with pytest.raises(ClosureError):
        code_hash(("reflow.extract.a",), root)


def test_a_literal_dynamic_import_is_followed_and_one_outside_reflow_is_not(tmp_path):
    root = _tree(tmp_path, {
        "reflow/a.py": "import importlib\nm = importlib.import_module('reflow.b')\n"
                       "o = importlib.import_module('ocrmypdf')\n",
        "reflow/b.py": "X = 1\n"})
    assert _changes(root, ("reflow.a",), "reflow/b.py", "X = 2\n")


def test_an_aliased_literal_dynamic_import_is_followed(tmp_path):
    root = _tree(tmp_path, {
        "reflow/a.py": "from importlib import import_module as load\nm = load('reflow.b')\n",
        "reflow/b.py": "X = 1\n"})
    assert _changes(root, ("reflow.a",), "reflow/b.py", "X = 2\n")


def test_the_body_of_if_not_type_checking_is_in_the_key(tmp_path):
    root = _tree(tmp_path, {"reflow/a.py": "if not TYPE_CHECKING:\n    from reflow import e\n",
                            "reflow/e.py": "Z = 1\n"})
    assert _changes(root, ("reflow.a",), "reflow/e.py", "Z = 2\n")


def test_an_entry_that_does_not_exist_fails_closed(tmp_path):
    root = _tree(tmp_path, {"reflow/a.py": ""})
    with pytest.raises(ClosureError):
        code_hash(("reflow.gone",), root)


# Hole 4: the non-Python files beside the stage's code.

def test_a_resource_beside_the_stage_s_code_is_in_the_key(tmp_path):
    root = _tree(tmp_path, {"reflow/extract/a.py": "X = 1\n", "reflow/extract/rules.json": "{}",
                            "reflow/extract/data/table.txt": "1", "reflow/render/__init__.py": "",
                            "reflow/render/t.html": "<p>"})
    assert _changes(root, ("reflow.extract.a",), "reflow/extract/rules.json", '{"a": 1}')
    assert _changes(root, ("reflow.extract.a",), "reflow/extract/data/table.txt", "2")
    assert not _changes(root, ("reflow.extract.a",), "reflow/render/t.html", "<b>")


# Holes 5 and 6: the OCR engine's data and the copy OCR reads.

class Runner:
    name = "fake"
    grades = False

    def __init__(self) -> None:
        self.calls, self.env = 0, "data-1"

    def environment(self, langs):
        return self.env

    def run(self, src, dst, plan, langs, evidence) -> None:
        self.calls += 1
        with pikepdf.new() as out:
            out.add_blank_page()
            out.save(dst)
        for n in plan.pages:
            (Path(evidence) / f"{n}.json").write_text("{}")


PLAN = OcrPlan({1: OcrPage("no-text", "redo")})


def _ocr(runner, tmp_path, src, n):
    evidence = tmp_path / f"e{n}"
    evidence.mkdir()
    runner.run(src, tmp_path / f"o{n}.pdf", PLAN, ("eng",), evidence)


def test_the_ocr_entry_is_keyed_on_the_working_copy_it_reads(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    inner = Runner()
    runner = CachedRunner(inner, root, pdf)
    working = tmp_path / "working.pdf"
    shutil.copyfile(pdf, working)
    _ocr(runner, tmp_path, working, 1)
    _ocr(runner, tmp_path, working, 2)
    assert inner.calls == 1
    working.write_bytes(working.read_bytes() + b"\n% made again\n")   # same source, other copy
    _ocr(runner, tmp_path, working, 3)
    assert inner.calls == 2


def test_the_ocr_entry_is_keyed_on_the_engine_s_data(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    inner = Runner()
    runner = CachedRunner(inner, root, pdf)
    _ocr(runner, tmp_path, pdf, 1)
    inner.env = "data-2"
    _ocr(runner, tmp_path, pdf, 2)
    assert inner.calls == 2


def test_ocr_is_not_cached_when_its_data_cannot_be_identified(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    inner = Runner()
    inner.env = None
    runner = CachedRunner(inner, root, pdf)
    _ocr(runner, tmp_path, pdf, 1)
    _ocr(runner, tmp_path, pdf, 2)
    assert inner.calls == 2 and not root.exists()


def _tessdata(path: Path) -> Path:
    (path / "configs").mkdir(parents=True)
    (path / "eng.traineddata").write_bytes(b"english model 1")
    (path / "deu.traineddata").write_bytes(b"german model 1")
    (path / "configs" / "hocr").write_text("tessedit_create_hocr 1\n")
    return path


def test_the_ocr_environment_holds_traineddata_configs_and_tessdata_prefix(tmp_path, monkeypatch):
    data = _tessdata(tmp_path / "tessdata")
    monkeypatch.setenv("TESSDATA_PREFIX", str(data))
    env = OcrmypdfRunner().environment(("eng",))
    assert env is not None
    assert OcrmypdfRunner().environment(("eng",)) == env
    (data / "deu.traineddata").write_bytes(b"german model 2")   # a language not asked for
    assert OcrmypdfRunner().environment(("eng",)) == env
    (data / "eng.traineddata").write_bytes(b"english model 2")
    second = OcrmypdfRunner().environment(("eng",))
    assert second != env
    (data / "configs" / "hocr").write_text("tessedit_create_hocr 0\n")
    third = OcrmypdfRunner().environment(("eng",))
    assert third != second
    other = shutil.copytree(data, tmp_path / "other")
    monkeypatch.setenv("TESSDATA_PREFIX", str(other))
    assert OcrmypdfRunner().environment(("eng",)) != third


def test_the_ocr_environment_is_unknown_without_the_language_s_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSDATA_PREFIX", str(_tessdata(tmp_path / "tessdata")))
    assert OcrmypdfRunner().environment(("fra",)) is None


# Hole 6 continued: extraction reads the OCR word grades beside the working copy.

def test_extraction_is_keyed_on_the_word_grades_beside_the_working_copy(tmp_path):
    from reflow.ingest.ocr_trust import sidecar_path
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    working = blank_pdf(tmp_path / "working.pdf")
    assert extract_cached(root, pdf, working)[1] is False
    assert extract_cached(root, pdf, working)[1] is True
    sidecar_path(working).write_text('{"pages": {}}')
    assert extract_cached(root, pdf, working)[1] is False
    assert extract_cached(root, pdf, working)[1] is True
    sidecar_path(working).write_text('{"pages": {"1": []}}')
    assert extract_cached(root, pdf, working)[1] is False


def test_the_working_copy_is_the_same_bytes_every_time_it_is_made(tmp_path):
    from conftest import make_pdf

    from reflow.ingest.load import normalize
    pdf = make_pdf(tmp_path / "a.pdf", ["text", "text"])
    normalize(pdf, tmp_path / "one.pdf")
    time.sleep(1 - time.time() % 1 + 0.01)   # qpdf's random /ID changes with the clock second
    normalize(pdf, tmp_path / "two.pdf")
    assert (tmp_path / "one.pdf").read_bytes() == (tmp_path / "two.pdf").read_bytes()


# Hole 7: the code a stage builds with must be in its key.

def test_a_build_outside_the_stage_s_closure_is_refused(tmp_path):
    from reflow.analyze import headings
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    outside = next(v for v in vars(headings).values()
                   if callable(v) and getattr(v, "__module__", "") == headings.__name__)
    for build in (outside, partial(outside)):
        with pytest.raises(ClosureError):
            stagecache.cached_extract(root, pdf, pdf, build)
    with pytest.raises(ClosureError):
        stagecache.cached(root, "pages", stagecache.STAGES["pages"], pdf, partial(outside), "part")


def test_the_real_builds_are_inside_their_stage_s_closure(tmp_path):
    from reflow.extract.parallel import extract_document
    from reflow.ingest.pages import read_source
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    assert stagecache.cached_extract(root, pdf, pdf, partial(extract_document, pdf, 1))[1] is False
    with pytest.raises(ClosureError):
        stagecache.cached_extract(root, pdf, pdf, partial(read_source, pdf, tmp_path / "w.pdf"))


# The boundary: each stage's closure, by name. A module joining one must be added here on
# purpose, after checking that an edit to it should rebuild the stage.

ALLOWED = {
    "ingest": {
        "reflow/__init__.py", "reflow/identity.py", "reflow/model.py", "reflow/pdfobjects.py",
        "reflow/ingest/__init__.py", "reflow/ingest/classify.py", "reflow/ingest/frame.py",
        "reflow/ingest/load.py",
        "reflow/ingest/pages.py", "reflow/pool.py",
    },
    "ocr": {
        "reflow/__init__.py", "reflow/identity.py", "reflow/model.py", "reflow/pdfobjects.py",
        "reflow/ingest/__init__.py", "reflow/ingest/ocr.py", "reflow/ingest/ocr_trust.py",
        "reflow/tessdata.py",
    },
    # The text witness renders a page and runs Tesseract: none of replacement OCR, none of the
    # trust judge (reflow.ingest.text_trust), which reads its words.
    "witness": {
        "reflow/__init__.py", "reflow/identity.py", "reflow/model.py", "reflow/tessdata.py",
        "reflow/ingest/__init__.py", "reflow/ingest/witness.py",
    },
    "extract": {
        "reflow/__init__.py", "reflow/model.py", "reflow/pdfobjects.py", "reflow/leaders.py",
        "reflow/cachedir.py", "reflow/stagecache.py", "reflow/names.py", "reflow/identity.py",
        "reflow/geometry.py", "reflow/text_quality.py", "reflow/patterns.py", "reflow/pool.py",
        "reflow/fontcensus.py",   # the "f" width a font declares, read at its encoding's code
        "reflow/extract/__init__.py", "reflow/extract/chars.py", "reflow/extract/drawings.py",
        "reflow/extract/fonts.py", "reflow/extract/forms.py", "reflow/extract/grid.py",
        "reflow/extract/images.py", "reflow/extract/lines.py", "reflow/extract/ocrwords.py",
        "reflow/extract/pagecontent.py", "reflow/extract/pages.py", "reflow/extract/parallel.py",
        "reflow/extract/redraws.py", "reflow/extract/rtl.py", "reflow/extract/rulings.py", "reflow/extract/scan.py",
        "reflow/extract/tables/__init__.py", "reflow/extract/tables/cells.py",
        "reflow/extract/tables/ruled.py", "reflow/extract/text.py", "reflow/extract/weights.py",
        "reflow/ingest/__init__.py", "reflow/ingest/classify.py", "reflow/ingest/ocr_trust.py",
    },
    # The pdfplumber parse reads none of the text extractor (extract/__init__.py is empty).
    "pages": {
        "reflow/__init__.py", "reflow/model.py", "reflow/cachedir.py", "reflow/stagecache.py",
        "reflow/names.py", "reflow/identity.py", "reflow/geometry.py", "reflow/pool.py",
        "reflow/extract/__init__.py", "reflow/extract/pagecontent.py", "reflow/extract/redraws.py",
        "reflow/extract/weights.py", "reflow/ingest/__init__.py", "reflow/ingest/ocr_trust.py",
    },
}


def test_each_stage_s_closure_is_its_allow_list():
    closures = {kind: {p.relative_to(stagecache.PACKAGE_ROOT).as_posix()
                       for p in stagecache.closure(entries)}
                for kind, entries in stagecache.STAGES.items() if kind != "model"}
    assert closures == ALLOWED
    assert all(not stagecache.resources(stagecache.closure(entries))
               for entries in stagecache.STAGES.values())


def test_every_reflow_module_s_imports_resolve():
    """The scanner fails closed, so an import it cannot resolve anywhere in the package would
    break the cache the day a stage reaches it."""
    package = stagecache.PACKAGE_ROOT / "reflow"
    modules = tuple(".".join(p.relative_to(stagecache.PACKAGE_ROOT).with_suffix("").parts)
                    .removesuffix(".__init__") for p in package.rglob("*.py"))
    assert len(stagecache.closure(modules)) == len(modules)


READING = ("ingest", "ocr", "extract", "pages")   # the stages that read the PDF


def test_an_analysis_or_rendering_edit_keeps_every_stage_s_key(tmp_path):
    copy = tmp_path / "src"
    shutil.copytree(stagecache.PACKAGE_ROOT / "reflow", copy / "reflow",
                    ignore=shutil.ignore_patterns("__pycache__"))
    before = {k: code_hash(stagecache.STAGES[k], copy) for k in READING}
    for name in ("reflow/analyze/headings.py", "reflow/render/html.py", "reflow/convert.py",
                 "reflow/analyze/engine.py", "reflow/render/shell.py"):
        with (copy / name).open("a") as f:
            f.write("\n# edited\n")
    assert {k: code_hash(stagecache.STAGES[k], copy) for k in READING} == before
    with (copy / "reflow/extract/__init__.py").open("a") as f:
        f.write("\n# edited\n")
    after = {k: code_hash(stagecache.STAGES[k], copy) for k in READING}
    assert after["ingest"] == before["ingest"] and after["ocr"] == before["ocr"]
    assert after["extract"] != before["extract"] and after["pages"] != before["pages"]
    with (copy / "reflow/extract/text.py").open("a") as f:
        f.write("\n# edited\n")
    edited = {k: code_hash(stagecache.STAGES[k], copy) for k in READING}
    assert edited["extract"] != after["extract"] and edited["pages"] == after["pages"]


# The model stage: the model's key holds every pass and none of the renderer.

RENDERER = ("reflow/output.py", "reflow/convert.py", "reflow/saved.py", "reflow/cli.py")


def test_the_model_s_closure_holds_no_renderer_and_no_resources():
    files = [p.relative_to(stagecache.PACKAGE_ROOT).as_posix()
             for p in stagecache.closure(stagecache.STAGES["model"])]
    assert not [f for f in files if f.startswith(("reflow/render/", "reflow/viewer/"))
                or f in RENDERER]
    assert {"reflow/pipeline.py", "reflow/prepare.py", "reflow/analyze/enrich.py",
            "reflow/extract/crops.py", "reflow/analyze/language.py"} <= set(files)
    assert stagecache.resources(stagecache.closure(stagecache.STAGES["model"])) == []


def _copy(tmp_path: Path) -> Path:
    copy = tmp_path / "src"
    shutil.copytree(stagecache.PACKAGE_ROOT / "reflow", copy / "reflow",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return copy


@pytest.mark.parametrize("name", ["reflow/viewer/viewer.css", "reflow/viewer/transcript.css",
                                  "reflow/viewer/viewer.js", "reflow/render/shell.py",
                                  "reflow/render/html.py", "reflow/output.py", "reflow/convert.py",
                                  "reflow/render/pages.py"])
def test_a_render_edit_keeps_the_model_key_and_changes_the_render_key(tmp_path, name):
    from reflow.saved import RENDER_ENTRIES
    copy = _copy(tmp_path)
    model, render = code_hash(stagecache.STAGES["model"], copy), code_hash(RENDER_ENTRIES, copy)
    with (copy / name).open("a") as f:
        f.write("\n# edited\n" if name.endswith(".py") else "\n/* edited */\n")
    assert code_hash(stagecache.STAGES["model"], copy) == model
    assert code_hash(RENDER_ENTRIES, copy) != render


@pytest.mark.parametrize("name", ["reflow/pipeline.py", "reflow/prepare.py",
                                  "reflow/analyze/headings.py", "reflow/analyze/language.py",
                                  "reflow/extract/crops.py", "reflow/model.py", "reflow/run.py"])
def test_a_model_edit_changes_both_keys(tmp_path, name):
    from reflow.saved import RENDER_ENTRIES
    copy = _copy(tmp_path)
    model, render = code_hash(stagecache.STAGES["model"], copy), code_hash(RENDER_ENTRIES, copy)
    with (copy / name).open("a") as f:
        f.write("\n# edited\n")
    assert code_hash(stagecache.STAGES["model"], copy) != model
    assert code_hash(RENDER_ENTRIES, copy) != render


def test_a_resource_read_is_followed_and_a_computed_one_fails_closed(tmp_path):
    root = _tree(tmp_path, {
        "reflow/a.py": "from importlib import resources\nresources.files('reflow.assets')\n",
        "reflow/assets/__init__.py": "", "reflow/assets/style.css": "a {}",
        "reflow/b.py": "import importlib.resources\nimportlib.resources.files('reflow.assets')\n",
        "reflow/c.py": "from importlib.resources import files as f\nf('reflow.assets')\n",
        "reflow/d.py": "from importlib import resources\nname = 'reflow.assets'\nresources.files(name)\n",
    })
    for module in ("reflow.a", "reflow.b", "reflow.c"):
        assert _changes(root, (module,), "reflow/assets/style.css", f"/* {module} */")
    with pytest.raises(ClosureError):
        stagecache.closure(("reflow.d",), root)
