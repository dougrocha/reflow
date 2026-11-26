"""`reflow audit` reports in Markdown and HTML (checks.report) and the veraPDF merge
(checks.verapdf)."""

import json
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import make_pdf
from reflow.checks import verapdf
from reflow.checks.report import to_html, to_markdown
from reflow.checks.source import audit
from reflow.checks.verapdf import parse_report
from reflow.cli import app

runner = CliRunner()
SAMPLE = Path(__file__).parent.parent / "samples/robustness/files/verapdf-ua1-7.21.7-t01-fail-a.pdf"

NON_COMPLIANT = """<?xml version="1.0" encoding="utf-8"?>
<report><jobs><job>
  <validationReport profileName="PDF/UA-1 validation profile" isCompliant="false">
    <details passedRules="105" failedRules="2">
      <rule clause="7.21.7" testNumber="1" status="failed" failedChecks="8">
        <description>The Font dictionary of all fonts shall define
          the map | to Unicode</description></rule>
      <rule clause="7.1" testNumber="3" status="failed" failedChecks="1">
        <description>Content shall be <![CDATA[<tagged>]]></description></rule>
      <rule clause="7.2" testNumber="2" status="passed" failedChecks="0">
        <description>passed rules are not listed</description></rule>
    </details>
  </validationReport>
</job></jobs></report>"""

COMPLIANT = """<report><jobs><job>
  <validationReport profileName="PDF/UA-1 validation profile" isCompliant="true">
    <details passedRules="106" failedRules="0"/></validationReport>
</job></jobs></report>"""

PARSE_ERROR = """<report><jobs><job><taskException type="PARSE">
  <exceptionMessage>Couldn't parse stream</exceptionMessage></taskException>
</job></jobs></report>"""


def test_parse_non_compliant_lists_failed_rules_only():
    r = parse_report(NON_COMPLIANT)
    assert r.status == "fail"
    assert r.detail == "2 of 107 rules of the PDF/UA-1 validation profile failed"
    assert [(x.clause, x.test, x.failed_checks) for x in r.rules] == [("7.21.7", "1", 8),
                                                                      ("7.1", "3", 1)]
    assert r.rules[0].description == ("The Font dictionary of all fonts shall define the map | "
                                      "to Unicode")


def test_parse_compliant_and_unvalidated_reports():
    assert parse_report(COMPLIANT).status == "pass"
    err = parse_report(PARSE_ERROR)
    assert err.status == "skip" and "Couldn't parse stream" in err.detail
    assert parse_report("not xml").status == "skip"


def test_missing_verapdf_is_a_skip_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(verapdf.shutil, "which", lambda _: None)
    pdf = make_pdf(tmp_path / "a.pdf", ["text"], title="T", lang="en", tagged=True)
    plain = audit(pdf)
    report = audit(pdf, verapdf=True)
    assert report.verapdf.status == "skip" and "not found" in report.verapdf.detail
    assert report.failed == plain.failed
    assert "verapdf" not in plain.to_dict()
    assert report.to_dict()["verapdf"]["status"] == "skip"


def test_the_password_is_never_put_on_verapdfs_command_line(tmp_path, monkeypatch):
    monkeypatch.setattr(verapdf.shutil, "which", lambda _: "/bin/verapdf")
    monkeypatch.setattr(verapdf.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran verapdf with a password"))
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], password="pw")
    assert audit(pdf, "pw", verapdf=True).verapdf.status == "skip"


def _fake_verapdf(tmp_path: Path, monkeypatch, xml: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "report.xml").write_text(xml)
    exe = bin_dir / "verapdf"
    exe.write_text(f"#!/bin/sh\ncat '{bin_dir / 'report.xml'}'\nexit 1\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


def test_a_non_compliant_verapdf_result_fails_the_audit(tmp_path, monkeypatch):
    _fake_verapdf(tmp_path, monkeypatch, NON_COMPLIANT)
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    result = runner.invoke(app, ["audit", str(pdf), "--verapdf", "--format", "json", "-j", "1"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["passed"] is False
    assert [r["clause"] for r in data["verapdf"]["rules"]] == ["7.21.7", "7.1"]


@pytest.mark.parametrize("fmt", ["markdown", "html", "text"])
def test_each_format_shows_the_verapdf_rules(tmp_path, monkeypatch, fmt):
    _fake_verapdf(tmp_path, monkeypatch, NON_COMPLIANT)
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    result = runner.invoke(app, ["audit", str(pdf), "--verapdf", "--format", fmt, "-j", "1"])
    assert result.exit_code == 1
    out = " ".join(result.stdout.split())
    assert "SRC-TXT-001" in out and "7.21.7" in out and "2 of 107 rules" in out


def test_markdown_escapes_table_cells(tmp_path):
    report = audit(make_pdf(tmp_path / "a|b.pdf", ["scan"]))
    report.findings[0].detail = "x | y\nz"
    md = to_markdown(report)
    assert md.startswith("# Accessibility audit: a\\|b.pdf\n")
    assert "| x \\| y z |" in md
    rows = [line for line in md.splitlines() if line.startswith("| SRC-")]
    assert len(rows) == len(report.findings)
    assert "**Failed.**" in md


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_html_is_a_standalone_escaped_accessible_page(tmp_path):
    report = audit(make_pdf(tmp_path / "<b>.pdf", ["text"]))
    report.findings[0].detail = "<script>alert(1)</script>"
    report.verapdf = parse_report(NON_COMPLIANT)
    page = to_html(report)
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "&lt;tagged&gt;" in page
    parser = _Collect()
    parser.feed(page)
    tags = parser.tags
    assert tags[0] == ("html", {"lang": "en"})
    assert [t for t, _ in tags].count("h1") == 1
    assert [t for t, _ in tags].count("caption") == 2
    assert all(a.get("scope") in ("col", "row") for t, a in tags if t == "th")
    assert "<title>Accessibility audit: &lt;b&gt;.pdf</title>" in page


@pytest.mark.skipif(not shutil.which("verapdf"), reason="verapdf not on PATH")
def test_real_verapdf_finds_the_missing_tounicode_the_src_checks_pass():
    report = audit(SAMPLE, verapdf=True)
    assert not any(f.status == "fail" for f in report.findings)
    assert report.verapdf.status == "fail" and report.failed
    assert [(r.clause, r.test) for r in report.verapdf.rules] == [("7.21.7", "1")]
