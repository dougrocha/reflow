import dataclasses

import pytest

from reflow import cli, steps
from reflow.convert import Summary
from reflow.run import Options


def test_every_step_has_a_summary_field_and_a_report_line():
    keys = [step.key for step in steps.STEPS]
    assert len(set(keys)) == len(keys)
    assert set(keys) == set(cli.REPORTS)
    assert set(keys) <= {f.name for f in dataclasses.fields(Summary)}


@pytest.mark.parametrize("options, mentions", [
    ({"assist": "local", "device": "cpu"}, ["transcriptions", "local"]),
    ({"assist_labels": True, "assist_scan_fields": True, "assist_provider": "anthropic"},
     ["form-field labels", "scanned-form fields", "anthropic", "assist cache"]),
    ({"engine": "direct"}, ["claude-haiku-5-5", "cache"]),
    ({"polish": True}, ["claude-haiku-5-5", "cache"]),
    ({"assist_form_repair": True}, ["form text", "ollama", "qwen3-vl:8b", "assist cache"]),
])
def test_what_a_record_cannot_pin_down_is_said_once_per_step(options, mentions):
    [said] = steps.uncovered(Options(**options))
    for word in mentions:
        assert word in said


def test_nothing_is_uncovered_by_default():
    assert steps.uncovered(Options()) == []
