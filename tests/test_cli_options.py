"""`convert` and `convert-many` take their settings from the fields of Options (reflow.run.option):
a field cannot be added without saying whether it is a setting, and a setting cannot be missing
from a command that takes it."""

import dataclasses

import pytest
import typer.main
from typer.testing import CliRunner

import reflow.cli as cli
from conftest import make_pdf
from reflow.run import FLAG, Options

runner = CliRunner()

# Fields that are not command-line settings, and why.
NOT_SETTINGS = {
    "password": "read from REFLOW_PDF_PASSWORD or a prompt (cli.PasswordOpt)",
    "chunk_pages": "a tuning knob of the page pool",
    "direct_jobs": "requests in flight; a library setting",
    "direct_cache": "the answer cache folder; a library setting",
    "device_requested": "what `device` was before it was resolved",
    "reuse_model": "the page-check harness's timing runs",
}
COMMANDS = ("convert", "convert-many")


def _params(command):
    return {p.name: p for p in typer.main.get_command(cli.app).commands[command].params}


def test_every_field_is_a_setting_or_says_why_not():
    undeclared = {f.name for f in dataclasses.fields(Options) if FLAG not in f.metadata}
    assert undeclared == set(NOT_SETTINGS)


@pytest.mark.parametrize("command", COMMANDS)
def test_a_command_takes_every_setting_declared_for_it(command):
    params = _params(command)
    for f in dataclasses.fields(Options):
        flag = f.metadata.get(FLAG)
        if flag is None:
            continue
        taken = command in flag.commands
        assert (f.name in params) == taken, f.name
        if taken:
            default = ",".join(map(str, f.default)) if isinstance(f.default, tuple) else f.default
            assert params[f.name].default == default, f.name
            assert params[f.name].help == flag.help


def test_convert_many_takes_what_convert_takes_but_the_decisions_file():
    """It takes a folder of stores instead (--decisions-dir)."""
    assert set(_params("convert")) - set(_params("convert-many")) == {"pdf", "output", "decisions"}


def test_the_defaults_on_the_command_line_are_the_defaults_of_options(tmp_path, monkeypatch):
    seen = {}
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    monkeypatch.setattr(cli, "run_convert", lambda *a, **kw: seen.update(kw) or (_ for _ in ()).throw(
        typer.Exit(0)))
    runner.invoke(cli.app, ["convert", str(pdf), "-o", str(tmp_path / "out")])
    for key in ("jobs", "lease", "on_progress"):
        seen.pop(key)
    assert Options(**seen) == Options()
    seen.clear()
    monkeypatch.setattr(cli, "convert_many", lambda docs, **kw: seen.update(kw) or [])
    runner.invoke(cli.app, ["convert-many", str(pdf), str(tmp_path / "out"), "--jobs", "1"])
    for key in ("jobs", "max_docs", "lease", "on_start", "on_done", "on_progress", "decisions_dir"):
        seen.pop(key)
    assert Options(**seen) == Options()


def test_convert_many_takes_the_settings_it_used_to_lack(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "convert_many", lambda docs, **kw: seen.update(kw) or [])
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    result = runner.invoke(cli.app, [
        "convert-many", str(pdf), str(tmp_path / "out"), "--jobs", "1", "--allow-restricted",
        "--assist-form-repair", "--assist-form-repair-max-calls", "3",
        "--assist-page-vision-max-pages", "7", "--assist-labels-max-calls", "2",
        "--password", "pw"])
    assert result.exit_code == 0, result.output
    assert seen["allow_restricted"] and seen["assist_form_repair"] and seen["password"] == "pw"
    assert (seen["assist_form_repair_max_calls"], seen["assist_page_vision_max_pages"],
            seen["assist_labels_max_calls"]) == (3, 7, 2)
