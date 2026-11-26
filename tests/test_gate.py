"""scripts/gate: runs the checks scripts/affected prints and keeps one record per commit."""
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("gate", str(ROOT / "scripts/gate"))
gate = importlib.util.module_from_spec(importlib.util.spec_from_loader("gate", _loader))
_loader.exec_module(gate)

COMMIT = "a" * 40


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """The gate on a fake change: `commands` are the checks affected prints, `unknown` the files
    it has no rule for, `dirty` what git status reports."""
    state = SimpleNamespace(commands=[], unknown=[], dirty="")
    monkeypatch.setattr(gate, "ROOT", tmp_path / "gate")

    def git(*args):
        if args[0] == "status":
            return state.dirty
        return COMMIT

    monkeypatch.setattr(gate, "git", git)
    fake = SimpleNamespace(
        Change=SimpleNamespace(of=lambda _: SimpleNamespace(old="b" * 40)),
        plan=lambda ch: SimpleNamespace(unknown=state.unknown),
        main_checkout=lambda _: Path(sys.prefix),
        rows=lambda ch, p, py: [(1, name, cmd, "why") for name, cmd in state.commands])
    monkeypatch.setattr(gate, "load_affected", lambda: fake)
    return state


def record(tmp_path) -> dict:
    [path] = (tmp_path / "gate" / COMMIT).glob("*/record.json")
    return json.loads(path.read_text())


def test_passing_checks_leave_a_passing_record(setup, tmp_path, capsys):
    setup.commands = [("unit tests", "echo ok"), ("web typecheck and lint", "true")]
    assert gate.main([]) == 0
    r = record(tmp_path)
    assert r["result"] == "passed" and r["state"] == "complete" and r["base"] == "b" * 40
    assert [(c["name"], c["state"]) for c in r["checks"]] == [("unit tests", "passed"),
                                                             ("web typecheck and lint", "passed")]
    assert "ok" in (Path(r["checks"][0]["run"]) / "log").read_text()
    assert gate.main(["status"]) == 0
    assert "passed on aaaaaaaaaaaa" in capsys.readouterr().out


def test_one_failing_check_fails_the_gate_and_the_rest_still_run(setup, tmp_path):
    setup.commands = [("unit tests", "exit 3"), ("crossing tests", "true")]
    assert gate.main([]) == 1
    r = record(tmp_path)
    assert r["result"] == "failed"
    assert [(c["state"], c["exit"]) for c in r["checks"]] == [("failed", 3), ("passed", 0)]
    assert gate.main(["status", "HEAD"]) == 1


def test_a_file_without_a_rule_fails_the_gate(setup, tmp_path):
    setup.unknown = ["new/thing.txt"]
    assert gate.main([]) == 1
    assert record(tmp_path)["unknown"] == ["new/thing.txt"]


def test_uncommitted_changes_are_refused_and_leave_no_record(setup, tmp_path):
    setup.dirty = " M src/reflow/cli.py"
    assert gate.main([]) == 2
    assert not (tmp_path / "gate").exists()


def test_no_record_and_a_run_in_progress_are_not_a_pass(setup, tmp_path, capsys):
    assert gate.main(["status"]) == 1
    folder = tmp_path / "gate" / COMMIT / "20260101-000000"
    folder.mkdir(parents=True)
    (folder / "record.json").write_text(json.dumps({"state": "running", "checks": []}))
    assert gate.main(["status"]) == 1
    assert "in progress" in capsys.readouterr().out


def test_the_newest_complete_record_decides(setup, tmp_path):
    for stamp, result in (("20260101-000000", "passed"), ("20260102-000000", "failed")):
        folder = tmp_path / "gate" / COMMIT / stamp
        folder.mkdir(parents=True)
        (folder / "record.json").write_text(json.dumps(
            {"state": "complete", "result": result, "base": "b" * 40, "checks": []}))
    assert gate.main(["status"]) == 1
