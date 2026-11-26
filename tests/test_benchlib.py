"""scripts/benchlib: what the benchmark scripts share."""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import benchlib  # noqa: E402


def test_percentile_interpolates_between_ranks():
    assert benchlib.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert benchlib.percentile([5.0], 0.95) == 5.0
    assert math.isnan(benchlib.percentile([], 0.5))


def test_the_sampler_keeps_each_reading_with_its_time_and_skips_junk():
    code = "print('12.5, 300'); print('N/A'); print('13, 310')"
    sampler = benchlib.GpuSampler("power.draw,memory.used", 100, command=[sys.executable, "-c", code])
    sampler.proc.wait(timeout=10)
    sampler.thread.join(timeout=10)
    readings = sampler.window(0, time.time() + 1)
    assert [r[1:] for r in readings] == [(12.5, 300.0), (13.0, 310.0)]
    assert sampler.window(0, 1) == []
    sampler.close()


def test_the_ollama_host_is_the_one_the_product_uses(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5")
    assert benchlib.ollama_host() == "http://10.0.0.5:11434"
    assert benchlib.ollama_host("box:9") == "http://box:9"
    monkeypatch.delenv("OLLAMA_HOST")
    assert benchlib.ollama_host() == "http://localhost:11434"
