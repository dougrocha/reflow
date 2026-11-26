"""What the benchmark scripts share: percentiles, an nvidia-smi sampler, Ollama's HTTP API and
loading and unloading models."""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


def percentile(values: list[float], q: float) -> float:
    """The q-quantile (0 to 1) of `values`, interpolated between the two nearest ranks."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


class GpuSampler:
    """One nvidia-smi process started (and stopped) by the harness, sampling `fields` (nvidia-smi's
    --query-gpu names, such as "memory.used"). `samples` holds (time, *values) per reading."""

    def __init__(self, fields: str, interval_ms: int, command: list[str] | None = None) -> None:
        self.samples: list[tuple[float, ...]] = []
        self.proc = subprocess.Popen(
            command or ["nvidia-smi", "-lms", str(interval_ms), f"--query-gpu={fields}",
                        "--format=csv,noheader,nounits"], stdout=subprocess.PIPE, text=True)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                self.samples.append((time.time(), *(float(x) for x in line.split(","))))
            except ValueError:
                continue

    def window(self, t0: float, t1: float) -> list[tuple[float, ...]]:
        return [s for s in list(self.samples) if t0 <= s[0] <= t1]

    def values(self, t0: float, t1: float) -> list[float]:
        """The first field's readings in the window."""
        return [s[1] for s in self.window(t0, t1)]

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


def ollama_host(host: str | None = None) -> str:
    """The server URL the product itself would use for `host` (else OLLAMA_HOST, else the
    local default), without a path."""
    from reflow.analyze.vision import OllamaVision

    return OllamaVision("", host=host).url().removesuffix("/api/chat")


def ollama_api(host: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    """GET (no body) or POST `body` as JSON to `host` + `path`, and the JSON answer."""
    request = urllib.request.Request(
        host + path, json.dumps(body).encode() if body is not None else None,
        {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def ollama_loaded(host: str, tag: str) -> dict | None:
    """The running model `tag`; Ollama names a tag given without one `TAG:latest`."""
    names = {tag, tag if ":" in tag.rpartition("/")[2] else f"{tag}:latest"}
    return next((m for m in ollama_api(host, "/api/ps").get("models", []) if m["name"] in names), None)


def ollama_stop(host: str, tag: str | None = None) -> None:
    """Unload the model `tag` (every running model when it is None) with keep_alive 0, and wait
    until Ollama reports it gone."""
    if tag is None:
        names = [m["name"] for m in ollama_api(host, "/api/ps").get("models", [])]
    else:
        names = [tag] if ollama_loaded(host, tag) else []
    for name in names:
        ollama_api(host, "/api/generate", {"model": name, "keep_alive": 0}, timeout=120)
    for _ in range(120):
        if not any(ollama_loaded(host, name) for name in names):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{', '.join(names)} still loaded after keep_alive 0")


def ollama_load(host: str, tag: str) -> float:
    """Load `tag` and keep it for 30 minutes; the seconds Ollama says the load took."""
    reply = ollama_api(host, "/api/generate", {"model": tag, "keep_alive": "30m"}, timeout=900)
    return reply.get("load_duration", 0) / 1e9


def idle_mib(sampler: GpuSampler, host: str, unload_tag: str | None = None) -> float:
    """The mean GPU memory in MiB with no model of the run loaded: `unload_tag` is stopped first
    (None leaves the server alone), then 3 s are sampled."""
    time.sleep(1.0)
    if unload_tag is not None:
        ollama_stop(host, unload_tag)
        time.sleep(1.0)
    start = time.time()
    time.sleep(3.0)
    return statistics.fmean(sampler.values(start, time.time()))


def gpu_processes() -> list[str]:
    """`pid name MiB` for each process on the GPU, to show what else was running."""
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                          "--format=csv,noheader"], capture_output=True, text=True).stdout
    procs = []
    for line in filter(str.strip, out.splitlines()):
        pid, rest = line.split(",", 1)
        command, mib = rest.rsplit(",", 1)   # the command line may hold commas
        procs.append(f"{pid.strip()} {Path(command.split()[0]).name} {mib.strip()}")
    return procs


def track_usage(client) -> dict:
    """Wrap `client.ask` so `stats["last"]` holds what the server reported for its latest reply."""
    stats: dict = {"last": {}}
    ask = client.ask

    def asked(*args, **kwargs):
        reply = ask(*args, **kwargs)
        stats["last"] = dict(reply.usage.stats)
        return reply

    client.ask = asked
    return stats


def route_dir(work: Path, route: str) -> Path:
    """The output folder of one route (`ollama:TAG`, `local`) under `work`."""
    return work / route.replace("/", "_").replace(":", "_")


def fresh_route(work: Path, route: str) -> tuple[Path, dict]:
    """The route's output folder, emptied, and its run record started with the GPU processes
    already running (printed too)."""
    base = route_dir(work, route)
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    record: dict = {"route": route, "gpu_processes_before": gpu_processes(),
                    "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"{route}: GPU processes before: {record['gpu_processes_before']}", flush=True)
    return base, record
