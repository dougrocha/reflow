"""scripts/affected and the table in CONTRIBUTING.md that it generates.

The script is the source of truth for which check a change needs. These tests keep CONTRIBUTING.md equal
to its table, give every tracked file a rule, and keep its rules pointing at real files, specs and
check kinds.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("affected", str(ROOT / "scripts/affected"))
_spec = importlib.util.spec_from_loader("affected", _loader)
affected = importlib.util.module_from_spec(_spec)
sys.modules["affected"] = affected
_loader.exec_module(affected)

sys.path.insert(0, str(ROOT / "tests/pages"))
from checks import CHECKS, DOC_CHECKS, SHELL_CHECKS  # noqa: E402

KINDS = set(CHECKS) | set(DOC_CHECKS) | set(SHELL_CHECKS)


def tracked() -> list[str]:
    return subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True,
                          text=True).stdout.splitlines()


def test_contributing_md_holds_the_generated_table():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    begin = text.index(affected.TABLE_BEGIN) + len(affected.TABLE_BEGIN)
    end = text.index(affected.TABLE_END)
    assert text[begin:end].strip() == affected.table(), \
        "CONTRIBUTING.md is out of date: run scripts/affected --write-table"


def test_every_tracked_file_has_a_rule():
    assert [p for p in tracked() if affected.area_of(p) is None] == []


def test_every_rule_matches_a_tracked_file():
    files = tracked()
    for area in affected.AREAS:
        for glob in area.globs:
            pattern = affected.glob_re(glob)
            assert any(pattern.match(f) for f in files), f"{area.label}: {glob} matches nothing"


def test_named_files_kinds_and_checks_exist():
    for area in affected.AREAS:
        assert set(area.checks) <= set(affected.CHECKS), area.label
        for f in area.files:
            assert (ROOT / f).is_file(), f
    for feat in affected.FEATURES:
        for p in feat.paths:
            assert (ROOT / p).is_file(), p
        assert set(feat.kinds) <= KINDS, feat.label
        for word in feat.files:
            assert (ROOT / f"tests/pages/{word}.py").is_file(), word


def test_the_coverage_record_is_read_from_the_readme():
    files = affected.coverage_files()
    assert "src/reflow/convert.py" in files and "src/reflow/render/pages.py" in files
    for f in files:
        assert (ROOT / f).is_file(), f


def test_the_opt_in_rows_rest_on_the_harness_defaults():
    """The opt-in passes need no page checks because the harness converts with these defaults."""
    from reflow.run import Options

    o = Options()
    assert (o.engine, o.assist, o.alt_text) == ("heuristic", "none", "none")
    assert not (o.assist_labels or o.assist_structure or o.scan_fields or o.assist_scan_fields
                or o.assist_page_vision)
    corpus = (ROOT / "tests/pages/corpus.py").read_text(encoding="utf-8")
    for option in ("engine=", "assist", "alt_text", "scan_fields"):
        assert option not in corpus.split("convert_many(", 1)[1].split(")", 1)[0]


class FakeChange(affected.Change):
    def __init__(self, src: str, lines: list[int]):
        super().__init__("main", "HEAD")
        self.src, self.lines = src, lines

    def read(self, path):
        return self.src if path == affected.CHECKS_PY else super().read(path)

    def new_lines(self, path):
        return self.lines


CHECKS_SRC = (ROOT / affected.CHECKS_PY).read_text(encoding="utf-8")


def line_in(name: str) -> int:
    lines = CHECKS_SRC.splitlines()
    return next(i for i, line in enumerate(lines, 1) if line.startswith(f"def {name}(")) + 1


@pytest.mark.parametrize("func,kinds", [
    ("_links", {"links"}),
    ("_month", {"calendar_month"}),
    ("_controls", {"fields"}),   # a helper: the kinds whose check functions call it
])
def test_a_check_function_names_its_kinds(func, kinds):
    assert affected.check_kinds(FakeChange(CHECKS_SRC, [line_in(func)])) == kinds


def test_a_change_outside_the_check_functions_reaches_every_check():
    assert affected.check_kinds(FakeChange(CHECKS_SRC, [1])) is None
    assert affected.check_kinds(FakeChange(CHECKS_SRC, [line_in("run_check")])) is None
    assert affected.check_kinds(FakeChange(CHECKS_SRC, [line_in("parse_html")])) is None


def test_a_commit_reads_the_specs_and_samples_at_that_commit():
    known = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", "99f25c9^{commit}"],
                           capture_output=True)
    if known.returncode:
        pytest.skip("commit 99f25c9 is not in this repository's history")
    ch = affected.Change("99f25c9^1", "99f25c9")
    assert {s.id for s in affected.load_specs(ch)} >= {"fw9-1", "arxiv-1"}
    docs = affected.sample_docs(ch)
    assert docs["samples/fw9.pdf"] == "fw9"
    assert docs["samples/azblue/site/large-group-member-guide-with-rx.pdf"] == \
        "az-large-group-member-guide-with-rx"   # an entry split over two lines
    ch.files = {"samples/fw9.pdf": "M"}
    assert "fw9-1" in affected.plan(ch).pages_k


def test_count_checks_name_their_kinds():
    assert affected.check_kinds(FakeChange(CHECKS_SRC, [line_in("_count")])) >= {"tables", "ins_count"}


def test_a_spec_change_checks_only_its_page():
    ch = affected.Change("main", None, {"tests/pages/specs/fw9-1.json": "M"})
    p = affected.plan(ch)
    assert p.checks == {} and set(p.pages_k) == {"fw9-1"}


def test_converter_code_needs_the_quick_suite_and_no_filter():
    ch = affected.Change("main", None, {"src/reflow/analyze/headings.py": "M"})
    p = affected.plan(ch)
    assert set(p.checks) == {"unit", "pages", "lint"} and p.pages_k == {} and p.regime == "warm"


def test_the_stage_cache_sets_the_time_of_the_quick_suite():
    assert affected.reading(["src/reflow/analyze/headings.py"]) == "warm"
    assert affected.reading(["src/reflow/extract/parallel.py"]) == "extract"
    assert affected.reading(["src/reflow/ingest/ocr.py"]) == "cold"
    assert affected.reading(["src/reflow/model.py"]) == "cold"


def test_opt_in_code_needs_no_page_checks():
    ch = affected.Change("main", None, {"src/reflow/analyze/structure_assist.py": "M"})
    assert set(affected.plan(ch).checks) == {"unit", "eval-structure", "lint"}


def test_python_and_its_settings_get_ruff_and_mypy_and_nothing_else_does():
    ch = affected.Change("main", None, {"tests/test_cli.py": "M", "uv.lock": "M",
                                        "scripts/lint": "M", "web/src/docs.ts": "M",
                                        "docs/ask.md": "M"})
    assert affected.plan(ch).checks["lint"] == {"tests/test_cli.py", "uv.lock", "scripts/lint"}


def test_a_test_helper_runs_the_tests_that_import_it():
    ch = affected.Change("main", None, {"tests/pages/vision_gates.py": "M", "tests/pages/report.py": "M"})
    p = affected.plan(ch)
    assert set(p.page_files) == {"tests/pages/test_vision_gate_pages.py"}
    assert set(p.unit_files) == {"tests/pages/test_checks.py"}
    assert "unit" not in p.checks


def test_a_helper_reaches_the_tests_of_the_helpers_that_import_it():
    ch = affected.Change("main", None)
    assert "tests/pages/test_vision_gate_pages.py" in affected.test_files_using(ch, "tests/pages/corpus.py")


def test_a_helper_no_test_imports_runs_the_unit_suite():
    ch = affected.Change("main", None, {"tests/ocr/capture_plugin.py": "M"})
    assert set(affected.plan(ch).checks) == {"unit", "lint"}
