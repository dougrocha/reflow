"""An Options is a value: building one touches nothing. The device a model runs on is resolved by
`with_device`, once, where a conversion starts."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import reflow.run as run
from reflow import steps
from reflow.run import Options, OptionsError, with_device


def fake_resolver(monkeypatch, answer="cuda"):
    seen = []

    def resolve(requested):
        seen.append(requested)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(run, "resolve_device", resolve)
    return seen


def test_building_options_probes_no_device(monkeypatch):
    seen = fake_resolver(monkeypatch)
    for kwargs in ({}, {"engine": "hybrid"}, {"alt_text": "local"},
                   {"device": "cuda", "engine": "docling"}):
        opts = Options(**kwargs)
        assert opts.device == kwargs.get("device", "auto") == opts.device_requested
    assert seen == []


def test_building_options_loads_no_torch():
    code = ("import sys; from reflow.run import Options; Options(engine='hybrid', device='cuda'); "
            "sys.exit(int('torch' in sys.modules))")
    src = str(Path(run.__file__).parent.parent)      # the reflow under test
    env = {**os.environ, "PYTHONPATH": src}
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0


def test_with_device_resolves_only_when_a_model_runs(monkeypatch):
    seen = fake_resolver(monkeypatch)
    plain = Options()
    assert with_device(plain) is plain and seen == []
    hybrid = with_device(Options(engine="hybrid"))
    assert (hybrid.device, hybrid.device_requested, seen) == ("cuda", "auto", ["auto"])
    assert with_device(Options(alt_text="local")).device == "cuda"
    assert with_device(Options(assist="local")).device == "auto"     # Ollama: not in this process


def test_with_device_leaves_the_rest_alone_and_the_original_untouched(monkeypatch):
    fake_resolver(monkeypatch, "cpu")
    opts = Options(engine="hybrid", page_widths=(600, 900))
    resolved = with_device(opts)
    assert opts.device == "auto"
    assert resolved == Options(engine="hybrid", page_widths=(600, 900), device="cpu")


def test_a_missing_named_gpu_is_an_options_error(monkeypatch):
    fake_resolver(monkeypatch, ValueError("CUDA requested but not available"))
    with pytest.raises(OptionsError, match="CUDA requested"):
        with_device(Options(engine="docling", device="cuda"))
    assert Options(engine="docling", device="cuda").device == "cuda"


def test_the_record_names_the_resolved_device_for_a_model_run(monkeypatch):
    fake_resolver(monkeypatch, "mps")
    assert steps.options_record(Options())["device"] == "auto"
    record = steps.options_record(with_device(Options(engine="hybrid")))
    assert record["device"] == "mps" and "device_requested" not in record


def test_an_unknown_device_is_refused_on_construction():
    with pytest.raises(OptionsError, match="unknown device"):
        Options(device="tpu")
