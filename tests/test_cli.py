import pytest
import json

from typer.testing import CliRunner

from conftest import make_pdf
from reflow.cli import app

runner = CliRunner()


def test_inspect_summary(mixed_pdf):
    result = runner.invoke(app, ["inspect", str(mixed_pdf)])
    assert result.exit_code == 0, result.output
    assert "Annual Report" in result.output
    assert "scanned 1" in result.output


def test_inspect_json(mixed_pdf, tmp_path):
    out = tmp_path / "doc.json"
    result = runner.invoke(app, ["inspect", str(mixed_pdf), "--json", "-o", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["schema"] == 1
    assert len(data["pages"]) == 4


def test_inspect_needs_password(tmp_path):
    path = make_pdf(tmp_path / "locked.pdf", ["text"], password="pw")
    assert runner.invoke(app, ["inspect", str(path)]).exit_code == 2
    assert runner.invoke(app, ["inspect", str(path), "--password", "pw"]).exit_code == 0


def test_inspect_not_a_pdf(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    assert runner.invoke(app, ["inspect", str(junk)]).exit_code == 1


def test_convert_not_a_pdf_exits_1_with_a_readable_message(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    result = runner.invoke(app, ["convert", str(junk), "-o", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "not a PDF, or too damaged to repair" in " ".join(result.output.split())  # Rich wraps
    assert not (tmp_path / "out").exists()


def test_convert_needs_password_exits_2(tmp_path):
    path = make_pdf(tmp_path / "locked.pdf", ["text"], password="pw")
    result = runner.invoke(app, ["convert", str(path), "-o", str(tmp_path / "out")])
    assert result.exit_code == 2, result.output
    assert "is encrypted; pass --password" in " ".join(result.output.split())
    assert not (tmp_path / "out").exists()
    assert runner.invoke(app, ["convert", str(path), "-o", str(tmp_path / "out"),
                               "--password", "pw", "--no-page-images"]).exit_code == 0


def test_convert_writes_a_site(tmp_path):
    from conftest import make_text_pdf
    pdf = make_text_pdf(tmp_path / "doc.pdf", [
        [(72, 60, 20, "Annual Report", "B"), (72, 100, 10, "The year in review.")],
        [(72, 60, 14, "Finances", "B"), (72, 100, 10, "Revenue grew.")],
    ])
    out = tmp_path / "out"
    result = runner.invoke(app, ["convert", str(pdf), "-o", str(out)])
    assert result.exit_code == 0, result.output
    for name in ("index.html", "original.pdf", "toc.json", "review.json", "document.json",
                 "assets/viewer.js", "assets/viewer.css", "pages/0001.webp", "pages/0002.webp"):
        assert (out / name).exists(), name
    index = (out / "index.html").read_text()
    assert "<h1>Annual Report</h1>" in index and "Revenue grew." in index


def test_convert_rejects_unknown_alt_text_provider(tmp_path):
    from conftest import make_text_pdf
    pdf = make_text_pdf(tmp_path / "doc.pdf", [[(72, 60, 12, "Hello")]])
    result = runner.invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "out"),
                                 "--alt-text", "cloud"])
    assert result.exit_code == 2
    assert not (tmp_path / "out").exists()


def test_audit_table_and_exit_code(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["text", "scan"], title="T", lang="en")
    result = runner.invoke(app, ["audit", str(pdf)])
    assert result.exit_code == 1, result.output
    text = " ".join(result.output.split())
    assert "SRC-TXT-001" in text and "image-only" in text and "checks failed" in text


def test_audit_json_round_trips_to_dict(tmp_path):
    from reflow.checks.source import audit as run_audit
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan"])
    result = runner.invoke(app, ["audit", str(pdf), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output) == run_audit(pdf).to_dict()


def test_audit_not_a_pdf_exits_1_with_a_report(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    result = runner.invoke(app, ["audit", str(junk), "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["passed"] is False and data["filename"] == "junk.pdf"


def test_audit_password(tmp_path):
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], password="pw")
    result = runner.invoke(app, ["audit", str(pdf), "--password", "pw", "--json"])
    txt = next(f for f in json.loads(result.stdout)["findings"] if f["id"] == "SRC-TXT-001")
    assert txt["status"] == "pass"


def test_doctor_prints_every_probe():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for name in ("qpdf", "tesseract", "ocrmypdf", "docling", "cuda", "chromium"):
        assert name in result.output


def test_doctor_survives_a_raising_import(monkeypatch):
    import importlib

    def boom(name, package=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(importlib, "import_module", boom)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "broken" in result.output and "chromium" in result.output


def test_convert_refuses_an_owner_locked_pdf_with_exit_3(tmp_path):
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], owner_only=True)
    out = tmp_path / "out"
    result = runner.invoke(app, ["convert", str(pdf), "-o", str(out)])
    assert result.exit_code == 3, result.output
    assert "forbids text extraction" in " ".join(result.output.split())
    assert not out.exists()

    result = runner.invoke(app, ["convert", str(pdf), "-o", str(out), "--allow-restricted",
                                 "--no-page-images"])
    assert result.exit_code == 0, result.output
    assert any(i["needs"] == "extract" for i in json.loads((out / "review.json").read_text()))


def test_convert_reports_image_only_pages_when_ocr_is_off(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan", "scan", "text"])
    out = tmp_path / "out"
    result = runner.invoke(app, ["convert", str(pdf), "-o", str(out), "--ocr", "none",
                                 "--no-page-images"])
    assert result.exit_code == 0, result.output
    assert "2 image-only pages without text" in " ".join(result.output.split())
    items = json.loads((out / "review.json").read_text())
    ocr = [i for i in items if i["needs"] != "title"]   # the file-name title is flagged too
    assert sum(i["severity"] == "error" for i in ocr) == 3   # 2 pages + 1 doc


def test_convert_exits_2_when_the_required_ocr_engine_is_missing(tmp_path, monkeypatch):
    from reflow.ingest.ocr import OcrmypdfRunner
    monkeypatch.setattr(OcrmypdfRunner, "available", staticmethod(lambda: False))
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan"])
    out = tmp_path / "out"
    result = runner.invoke(app, ["convert", str(pdf), "-o", str(out), "--ocr", "ocrmypdf"])
    assert result.exit_code == 2, result.output
    assert "ocrmypdf is not installed" in " ".join(result.output.split())
    assert not out.exists()
    assert runner.invoke(app, ["convert", str(pdf), "-o", str(out), "--ocr", "tesseract"]).exit_code == 2


def test_convert_many_accepts_ocr(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan"])
    out = tmp_path / "out"
    result = runner.invoke(app, ["convert-many", str(pdf), str(out), "--ocr", "none",
                                 "--no-page-images", "--jobs", "1"])
    assert result.exit_code == 0, result.output
    assert (out / "review.json").exists()


def test_convert_many_passes_the_assist_options_to_every_document(tmp_path, monkeypatch):
    """docs:build's variants convert through convert-many with the assists on."""
    import reflow.cli as cli
    seen = {}
    monkeypatch.setattr(cli, "convert_many", lambda docs, **kw: seen.update(kw) or [])
    pdf = make_pdf(tmp_path / "form.pdf", ["form"])
    result = runner.invoke(app, ["convert-many", str(pdf), str(tmp_path / "out"), "--jobs", "1",
                                 "--assist-labels", "--assist-structure", "--assist-scan-fields",
                                 "--assist-provider", "anthropic", "--assist-model", "claude-haiku-5-5"])
    assert result.exit_code == 0, result.output
    assert seen["assist_labels"] and seen["assist_structure"] and seen["assist_scan_fields"]
    assert not seen["assist_page_vision"]
    assert (seen["assist_provider"], seen["assist_model"]) == ("anthropic", "claude-haiku-5-5")


def _stdout_lines(result):
    return result.stdout.splitlines()


def test_convert_many_progress_json_adds_json_lines_and_keeps_the_rest(tmp_path):
    pdf = _two_page_pdf(tmp_path)
    plain = runner.invoke(app, ["convert-many", str(pdf), str(tmp_path / "a"), "--jobs", "1"])
    asked = runner.invoke(app, ["convert-many", str(pdf), str(tmp_path / "b"), "--jobs", "1",
                                "--progress-json"])
    assert plain.exit_code == 0 and asked.exit_code == 0, asked.output
    assert not any(line.startswith("{") for line in _stdout_lines(plain))
    everything = [json.loads(line) for line in _stdout_lines(asked) if line.startswith("{")]
    events = [e for e in everything if e["event"] == "progress"]
    assert events, asked.stdout
    out = str(tmp_path / "b")
    timings = [e for e in everything if e["event"] == "timings"]
    assert len(timings) == 1 and timings[0]["out"] == out and timings[0]["seconds"]
    for event in events:
        assert event["event"] == "progress" and event["out"] == out
        assert isinstance(event["stage"], str) and event["stage"]
        assert isinstance(event["done"], int)
        assert event["total"] is None or event["done"] <= event["total"]
    assert any(e["stage"] == "Rendering page images" and e["done"] == e["total"] == 2 for e in events)
    # The tab-separated lines are the same with and without the option, up to the output path.
    tabbed = lambda r, d: [line.replace(str(d), "OUT").split("\t")[:2]
                           for line in _stdout_lines(r) if "\t" in line]
    assert tabbed(asked, tmp_path / "b") == tabbed(plain, tmp_path / "a")


def _two_page_pdf(tmp_path):
    from conftest import make_text_pdf
    return make_text_pdf(tmp_path / "doc.pdf", [
        [(72, 60, 20, "Annual Report", "B"), (72, 100, 10, "The year in review.")],
        [(72, 60, 14, "Finances", "B"), (72, 100, 10, "Revenue grew.")],
    ])


def test_convert_writes_srcset_widths_and_lists_them_in_the_manifest(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["convert", str(_two_page_pdf(tmp_path)), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "pages/0002-1200.webp").exists()
    assert json.loads((out / "manifest.json").read_text())["imageWidths"] == [800, 1200]


def test_srcset_max_pages_drops_the_large_width(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["convert", str(_two_page_pdf(tmp_path)), "-o", str(out),
                                 "--srcset-max-pages", "1"])
    assert result.exit_code == 0, result.output
    assert (out / "pages/0001.webp").exists() and not list((out / "pages").glob("*-1200.webp"))
    assert json.loads((out / "manifest.json").read_text())["imageWidths"] == [800]


def test_page_widths_flag_sets_the_candidates_and_rejects_bad_lists(tmp_path):
    pdf = _two_page_pdf(tmp_path)
    out = tmp_path / "out"
    assert runner.invoke(app, ["convert", str(pdf), "-o", str(out), "--page-widths", "600,900"]
                         ).exit_code == 0
    assert (out / "pages/0001-900.webp").exists()
    assert json.loads((out / "manifest.json").read_text())["imageWidths"] == [600, 900]
    for bad in ("900,600", "x", "0"):
        assert runner.invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "bad"),
                                   "--page-widths", bad]).exit_code != 0


def test_page_widths_rejects_empty_entries(tmp_path):
    result = runner.invoke(app, ["convert", str(_two_page_pdf(tmp_path)), "-o", str(tmp_path / "o"),
                                 "--page-widths", "800,,1200"])
    assert result.exit_code != 0 and not (tmp_path / "o").exists()


def test_summary_says_when_the_cutoff_dropped_the_large_width(tmp_path):
    pdf = _two_page_pdf(tmp_path)
    capped = runner.invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "a"),
                                 "--srcset-max-pages", "1"])
    assert "Page images: 800 px only (document over 1 page; --srcset-max-pages to change)" in \
        " ".join(capped.output.split())
    full = runner.invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "b")])
    assert "Page images:" not in full.output


@pytest.mark.parametrize("command", ["inspect", "convert", "audit"])
@pytest.mark.parametrize("source", ["env", "prompt", "explicit"])
def test_password_sources(command, source, tmp_path, monkeypatch):
    import reflow.cli as cli
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], password="secret")
    received = []

    def capture(*args, **kwargs):
        received.append(kwargs.get("password", args[1] if len(args) > 1 else ""))
        raise ValueError("stop after password capture")

    if command == "inspect":
        monkeypatch.setattr(cli, "ingest", capture)
    elif command == "convert":
        monkeypatch.setattr(cli, "run_convert", capture)
    else:
        monkeypatch.setattr(cli, "run_audit", capture)
    monkeypatch.delenv("REFLOW_PDF_PASSWORD", raising=False)
    args = [command, str(pdf)]
    if source == "env":
        monkeypatch.setenv("REFLOW_PDF_PASSWORD", "secret")
    else:
        args += ["--password"]
        if source == "explicit":
            args += ["secret"]
    result = runner.invoke(app, args, input="secret\n" if source == "prompt" else None)
    assert received == ["secret"], (result.output, result.exception)
    assert "secret" not in result.output
    assert ("visible in the process list" in result.output) == (source == "explicit")


def test_explicit_password_overrides_environment(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], password="explicit")
    monkeypatch.setenv("REFLOW_PDF_PASSWORD", "wrong")
    result = runner.invoke(app, ["inspect", str(pdf), "--password", "explicit"])
    assert result.exit_code == 0, result.output
    assert "visible in the process list" in result.stderr


def test_password_value_may_start_with_a_dash(tmp_path):
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], password="-secret")
    result = runner.invoke(app, ["inspect", str(pdf), "--password", "-secret"])
    assert result.exit_code == 0, result.output


def test_bare_password_before_help_shows_help(tmp_path):
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], password="pw")
    result = runner.invoke(app, ["inspect", str(pdf), "--password", "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def _text_pdf(tmp_path):
    from conftest import make_text_pdf
    return make_text_pdf(tmp_path / "doc.pdf", [[(72, 60, 12, "Hello")]])


def test_convert_a_valueerror_from_inside_the_run_is_not_reported_as_a_bad_option(
        tmp_path, monkeypatch):
    import reflow.pipeline

    def broken(*args, **kwargs):
        raise ValueError("node p1: spans or marks but no text")

    monkeypatch.setattr(reflow.pipeline, "read_pages", broken)
    result = runner.invoke(app, ["convert", str(_text_pdf(tmp_path)), "-o", str(tmp_path / "out")])
    assert result.exit_code not in (0, 2)
    assert type(result.exception) is ValueError   # surfaced, with its traceback
    assert not (tmp_path / "out").exists()


def test_convert_a_pdf_library_error_from_inside_the_run_is_not_reported_as_unreadable(
        tmp_path, monkeypatch):
    import pikepdf

    import reflow.pipeline

    def broken(*args, **kwargs):
        raise pikepdf.PdfError("a page tree that is gone")

    monkeypatch.setattr(reflow.pipeline, "read_pages", broken)
    result = runner.invoke(app, ["convert", str(_text_pdf(tmp_path)), "-o", str(tmp_path / "out")])
    assert isinstance(result.exception, pikepdf.PdfError)
    assert "Could not open" not in result.output


@pytest.mark.parametrize("args", [
    ["--alt-text", "cloud"],
    ["--alt-text-model", "m"],
    ["--assist-labels-max-calls", "-1"],
])
def test_convert_a_bad_option_exits_2_with_its_message(tmp_path, args):
    result = runner.invoke(app, ["convert", str(_text_pdf(tmp_path)), "-o", str(tmp_path / "out"),
                                 *args])
    assert result.exit_code == 2, result.output
    assert not isinstance(result.exception, ValueError)
    assert not (tmp_path / "out").exists()


def test_convert_an_output_folder_with_foreign_files_exits_2(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "mine.txt").write_text("keep")
    result = runner.invoke(app, ["convert", str(_text_pdf(tmp_path)), "-o", str(out)])
    assert result.exit_code == 2, result.output
    assert "holds files a conversion didn't write" in " ".join(result.output.split())
    assert (out / "mine.txt").read_text() == "keep"


def test_options_error_is_a_valueerror_and_the_password_is_not_in_a_repr():
    from reflow.run import Options, OptionsError
    with pytest.raises(ValueError) as caught:
        Options(engine="nope")
    assert type(caught.value) is OptionsError
    options = Options(password="hunter2-secret")
    assert options.password == "hunter2-secret"
    assert "hunter2" not in repr(options)
