"""The lines `reflow convert-many` and `reflow rerender --progress-json` print on stdout, and
that web/scripts/build-docs.ts reads (parsed by web/src/lib/builds/lines.ts).

  <status> TAB <output directory> TAB <details>   status is START, OK or FAILED
  {"event": "progress", "out": ..., "stage": ..., "done": n, "total": n or null}
  {"event": "timings", "out": ..., "seconds": {stage: s}, "gpuWait": {stage: s}}   before its OK line

tests/test_progress_lines.py and web/src/lib/builds/lines.test.ts check the two sides against
each other. Standard library only: the web tests' converter fixtures import it."""

from __future__ import annotations

import json

START = "start"
OK = "ok"
FAILED = "FAILED"
PROGRESS = "progress"
TIMINGS = "timings"
CANNOT_RERENDER = "CannotRerender"   # the details of a FAILED line `rerender` prints when it refuses


def status_line(status: str, out_dir: str, details: str) -> str:
    return f"{status}\t{out_dir}\t{details}"


def progress_line(out_dir: str, stage: str, done: int, total: int | None) -> str:
    return json.dumps({"event": PROGRESS, "out": out_dir, "stage": stage, "done": done, "total": total})


def timings_line(out_dir: str, seconds: dict[str, float], gpu_wait: dict[str, float]) -> str:
    """The seconds each stage of a finished conversion took (Summary.seconds), and the part of
    each spent waiting for the GPU lease (Summary.gpu_wait)."""
    return json.dumps({"event": TIMINGS, "out": out_dir, "seconds": seconds, "gpuWait": gpu_wait})
