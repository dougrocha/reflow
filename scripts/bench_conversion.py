"""Alternating whole-conversion runs of one PDF on several source trees, with the time of each pass.

    scripts/bench_conversion.py PDF JOBS RUNS LABEL=SRC[::free-tag-pages] ... [--json OUT]

SRC is a `src` folder (the worktree's, or a `git archive <commit> src` extract). Run r of every
side happens before run r+1 (A B C A B C ...), each in a fresh process, so drift, warm caches and
the other work on the machine fall on every side alike. The report is the median and the range of
the wall time, the extract step and the passes that matter to the tag read, and the load average
before each run.

`::free-tag-pages` is a ceiling, not a configuration: the pages that only the tag tree wants (no
link annotation on them) cost nothing to read. It is the most that reading marked-content ids from
another source could save, since the pages with links are parsed for their words anyway.
"""

from __future__ import annotations

import functools
import importlib
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

PASSES = [("analyze.engine", "build"), ("analyze.prefetch", "finish"), ("analyze.evidence", "witness"),
          ("analyze.order_evidence", "apply"), ("analyze.structure", "finish"),
          ("analyze.enrich", "describe"), ("output", "publish"), ("extract.tags", "read_tags"),
          ("extract.tags", "read_bookmarks"), ("extract.links", "read_links")]

times: dict[str, float] = {}
linked: set[int] = set()
read_pages_of_main = None


def free_read_pages(path, numbers, words=False):
    """pagecontent.read_pages, except that a page without a link annotation is not parsed."""
    from reflow.extract import pagecontent
    found = read_pages_of_main(path, [n for n in numbers if n in linked], words)
    return {n: found.get(n, pagecontent.PlumberPage(0, {}, {})) for n in numbers}


def patch(src: str, pdf: str, free: bool) -> None:
    """Runs in the parent and, because a spawned worker re-imports this file, in every worker."""
    global read_pages_of_main
    sys.path.insert(0, src)
    import reflow

    assert reflow.__file__.startswith(src), reflow.__file__
    for module_name, name in PASSES:
        module = importlib.import_module(f"reflow.{module_name}")
        original = getattr(module, name)
        label = f"{module_name.rsplit('.', 1)[-1]}.{name}"

        @functools.wraps(original)
        def timed(*a, _fn=original, _label=label, **k):
            start = time.monotonic()
            try:
                return _fn(*a, **k)
            finally:
                times[_label] = round(times.get(_label, 0) + time.monotonic() - start, 2)

        setattr(module, name, timed)
    if free:
        from reflow.extract import pagecontent
        from reflow.extract.links import scan_links

        if "BENCH_LINKED" not in os.environ:   # the parent scans once; its workers inherit the result
            os.environ["BENCH_LINKED"] = ",".join(str(n) for n in scan_links(Path(pdf)))
        linked.update(int(n) for n in os.environ["BENCH_LINKED"].split(",") if n)
        read_pages_of_main = pagecontent.read_pages
        pagecontent.read_pages = free_read_pages


def convert_once(pdf: str, out: str, jobs: int) -> None:
    from reflow.convert import convert

    start = time.monotonic()
    summary = convert(Path(pdf), Path(out), jobs=jobs)
    print(json.dumps({"wall": round(time.monotonic() - start, 2), "steps": summary.seconds,
                      "passes": times}))


def median_range(values: list[float]) -> str:
    return f"{statistics.median(values):6.2f} ({min(values):.2f}-{max(values):.2f})"


def orchestrate(args: list[str]) -> None:
    json_out = None
    if "--json" in args:
        i = args.index("--json")
        json_out = args[i + 1]
        del args[i:i + 2]
    pdf, jobs, runs = args[0], int(args[1]), int(args[2])
    sides = []
    for spec in args[3:]:
        label, rest = spec.split("=", 1)
        src, _, option = rest.partition("::")
        sides.append((label, src, option == "free-tag-pages"))
    results: dict[str, list[dict]] = {label: [] for label, _, _ in sides}
    print(f"cores {os.cpu_count()}, jobs {jobs}, {runs} runs per side, {pdf}")
    for run in range(runs):
        for label, src, free in sides:
            load = Path("/proc/loadavg").read_text().split()[0]
            command = [sys.executable, __file__, "--child", src, pdf, f"/tmp/bench-conversion-{label}",
                       str(jobs), "free" if free else "plain"]
            done = subprocess.run(command, capture_output=True, text=True)
            lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
            if done.returncode or not lines:
                print(label, "FAILED", done.stderr[-400:])
                continue
            data = json.loads(lines[-1])
            data["load"] = load
            results[label].append(data)
            print(f"run {run + 1} {label:8s} wall {data['wall']:6.2f}  extract {data['steps']['extract']:5.2f}"
                  f"  load {load}", flush=True)
    print(f"\nmedian (range) over {runs} runs, seconds")
    for label, rows in results.items():
        if rows:
            def passes(key):
                return [r["passes"].get(key, 0.0) for r in rows]
            print(f"{label:8s} wall {median_range([r['wall'] for r in rows])}"
                  f" | extract {median_range([r['steps']['extract'] for r in rows])}"
                  f" | prefetch.finish {median_range(passes('prefetch.finish'))}"
                  f" | witness {median_range(passes('evidence.witness'))}"
                  f" | read_tags {median_range(passes('tags.read_tags'))}"
                  f" | read_links {median_range(passes('links.read_links'))}")
    if json_out:
        Path(json_out).write_text(json.dumps({"pdf": pdf, "jobs": jobs, "cores": os.cpu_count(),
                                              "results": results}, indent=1))
    if any(len(rows) < runs for rows in results.values()):
        sys.exit("some runs failed")


if len(sys.argv) > 6 and sys.argv[1] == "--child":
    patch(sys.argv[2], sys.argv[3], sys.argv[6] == "free")

if __name__ == "__main__":
    if len(sys.argv) > 6 and sys.argv[1] == "--child":
        convert_once(sys.argv[3], sys.argv[4], int(sys.argv[5]))
    else:
        orchestrate(sys.argv[1:])
