"""Several runs of the quick page checks at once, each on a cold conversion cache of its own.

    scripts/bench_parallel_checks.py REPO STAGES RUNS... [--rounds R] [--json OUT] [-- PYTEST ARGS]

REPO is the checkout whose code runs (`-o pythonpath=src` from there). STAGES is a warm stage
cache (`reflow-page-checks-stages`), copied with reflinks into each run's own cache root, so
every run misses the whole conversions and hits OCR and extraction: the cost of a run
after an edit to analysis or rendering code. The runs share no cache key, as runs on
different branches don't. For each count in RUNS, that many pytest runs start together; the
report is each one's wall time, and the 1-minute load average before and at the end. The default
pytest arguments are `-m pages --quick -q -p no:randomly`.

The roots live in ~/.cache/reflow-bench, not /tmp, so the reflinks work on btrfs.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import checkout  # noqa: E402

BENCH = Path.home() / ".cache" / "reflow-bench"


def run_round(repo: Path, stages: Path, runs: int, pytest_args: list[str]) -> dict:
    roots = []
    for i in range(runs):
        root = BENCH / f"run-{i}"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        subprocess.run(["cp", "-a", "--reflink=always", str(stages),
                        str(root / "reflow-page-checks-stages")], check=True)
        roots.append(root)
    python = checkout.main_checkout(repo) / ".venv/bin/python"
    load_before = os.getloadavg()[0]
    procs = []
    start = time.monotonic()
    for i, root in enumerate(roots):
        env = {**os.environ, "REFLOW_PAGE_CHECKS_CACHE": str(root)}
        log = open(BENCH / f"run-{i}.log", "w")
        procs.append((subprocess.Popen([str(python), "-m", "pytest", "-o", "pythonpath=src", *pytest_args],
                                       cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT), log))
    seconds, codes = [None] * runs, [None] * runs
    peak = 0.0
    while any(s is None for s in seconds):
        for i, (proc, log) in enumerate(procs):
            if seconds[i] is None and proc.poll() is not None:
                seconds[i], codes[i] = time.monotonic() - start, proc.returncode
                log.close()
        peak = max(peak, os.getloadavg()[0])
        time.sleep(0.5)
    return {"runs": runs, "seconds": [round(s, 1) for s in seconds], "codes": codes,
            "median": round(statistics.median(seconds), 1), "max": round(max(seconds), 1),
            "load_before": round(load_before, 1), "load_peak": round(peak, 1)}


def main(argv: list[str]) -> None:
    pytest_args = ["-m", "pages", "--quick", "-q", "-p", "no:randomly"]
    if "--" in argv:
        cut = argv.index("--")
        argv, pytest_args = argv[:cut], argv[cut + 1:]
    out = None
    if "--json" in argv:
        i = argv.index("--json")
        out = Path(argv[i + 1])
        del argv[i:i + 2]
    rounds = 1
    if "--rounds" in argv:
        i = argv.index("--rounds")
        rounds = int(argv[i + 1])
        del argv[i:i + 2]
    repo, stages, *counts = argv
    results = []
    for _ in range(rounds):
        for n in map(int, counts):
            r = run_round(Path(repo), Path(stages), n, pytest_args)
            print(json.dumps(r), flush=True)
            results.append(r)
    if out:
        out.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
