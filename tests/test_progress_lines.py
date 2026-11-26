"""The lines convert-many and rerender print for web/scripts/build-docs.ts to read
(reflow.progress_lines). web/src/lib/builds/lines.test.ts checks the reader against these."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from conftest import make_pdf
from reflow import progress_lines as lines
from reflow.cli import app


def test_a_status_line_is_status_directory_and_details_between_tabs():
    assert lines.status_line(lines.OK, "/out/a", "2 pages") == "ok\t/out/a\t2 pages"
    assert lines.status_line(lines.START, "/out/a", "") == "start\t/out/a\t"


def test_a_progress_line_is_one_json_object():
    event = json.loads(lines.progress_line("/out/a", "Reading", 1, None))
    assert event == {"event": "progress", "out": "/out/a", "stage": "Reading", "done": 1, "total": None}


def test_a_timings_line_is_one_json_object():
    event = json.loads(lines.timings_line("/out/a", {"extract": 0.85}, {"extract": 0.2}))
    assert event == {"event": "timings", "out": "/out/a", "seconds": {"extract": 0.85},
                     "gpuWait": {"extract": 0.2}}


def test_the_stage_times_line_keeps_only_the_stages_that_took_time():
    from types import SimpleNamespace

    from reflow.cli import _stage_times
    summary = SimpleNamespace(seconds={"ingest": 0.04, "extract": 0.93, "docling": 14.2, "publish": 1.03},
                              gpu_wait={"docling": 9.0, "extract": 0.01})
    assert _stage_times(summary) == "extract 0.9s, docling 14.2s (9.0s on the GPU lease), publish 1.0s"
    assert _stage_times(SimpleNamespace(seconds={"ingest": 0.04}, gpu_wait={})) == ""


@pytest.mark.parametrize("fail", [False, True])
def test_every_stdout_line_of_convert_many_is_a_status_or_progress_line(tmp_path, fail):
    good = make_pdf(tmp_path / "good.pdf", ["text"])
    pairs = [str(good), str(tmp_path / "g")]
    if fail:
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"not a pdf")
        pairs += [str(bad), str(tmp_path / "b")]
    result = CliRunner().invoke(app, ["convert-many", *pairs, "--jobs", "1", "--progress-json"])
    seen = {lines.START: set(), lines.OK: set(), lines.FAILED: set(), lines.PROGRESS: set(),
            lines.TIMINGS: set()}
    order = []
    for line in result.stdout.splitlines():
        if line.startswith("{"):
            event = json.loads(line)
            assert event["event"] in (lines.PROGRESS, lines.TIMINGS)
            seen[event["event"]].add(event["out"])
            if event["event"] == lines.TIMINGS:
                assert event["seconds"] and all(isinstance(v, float) for v in event["seconds"].values())
                order.append(("timings", event["out"]))
            continue
        status, out, *details = line.split("\t")
        assert status in seen and len(details) == 1, line
        seen[status].add(out)
        order.append((status, out))
    outs = {str(tmp_path / "g")} | ({str(tmp_path / "b")} if fail else set())
    assert seen[lines.START] == outs
    assert seen[lines.OK] | seen[lines.FAILED] == outs
    assert seen[lines.FAILED] == (outs - {str(tmp_path / "g")} if fail else set())
    assert str(tmp_path / "g") in seen[lines.PROGRESS]
    # Only a document that converted has timings, and they come before its ok line.
    assert seen[lines.TIMINGS] == {str(tmp_path / "g")}
    good_out = str(tmp_path / "g")
    assert order.index(("timings", good_out)) < order.index((lines.OK, good_out))
