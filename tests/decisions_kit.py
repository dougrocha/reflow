"""A writer for the decision store, for tests: the write protocol of docs/review-decisions.md.

The converter never writes a store. The web app does; this is the same protocol in
Python, so tests can build stores that look like a real writer's, and race two writers.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_LINE = 8 * 1024
BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def mint_id(now_ms: int | None = None) -> str:
    """`<milliseconds since the epoch, base 36>-<8 random hex>`."""
    n = int(time.time() * 1000) if now_ms is None else now_ms
    digits = ""
    while n:
        n, r = divmod(n, 36)
        digits = BASE36[r] + digits
    return f"{digits or '0'}-{secrets.token_hex(4)}"


def _line(record: Mapping[str, Any]) -> bytes:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(line) > MAX_LINE:
        raise ValueError(f"a store line is at most {MAX_LINE} bytes, not {len(line)}")
    return line


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    with open(path.parent / "coordination.lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write(path: Path, data: bytes, flags: int) -> None:
    fd = os.open(path, os.O_WRONLY | flags, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def new_store(path: Path, slug: str) -> str:
    """Create the store with its header (under the lock, O_CREAT | O_EXCL). Returns the store id.
    FileExistsError when it already exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    store_id = secrets.token_hex(8)
    header = {"v": 1, "kind": "store", "id": store_id, "slug": slug,
              "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    with _locked(path):
        _write(path, _line(header), os.O_CREAT | os.O_EXCL)
    return store_id


def append(path: Path, record: Mapping[str, Any]) -> None:
    """Append one record: take the lock; end a torn last line with one `\\n`; write the record
    with one write on an O_APPEND descriptor; fsync. Durable when it returns."""
    path = Path(path)
    data = _line(record)
    with _locked(path):
        repair = b""
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    repair = b"\n"
        _write(path, repair + data, os.O_APPEND)


NOW = "2026-10-28T09:14:03Z"


def record(sha: str, kind: str, **fields) -> dict:
    """A decision record of `kind` for the PDF `sha`, minted now and stamped NOW."""
    return {"v": 1, "id": mint_id(), "at": NOW, "pdf": sha, "kind": kind, **fields}
