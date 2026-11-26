"""What identifies the inputs of a conversion: file digests, the installed packages and the
programs it runs. The stage cache, the saved model and the provenance records all key on these,
so they are read here and nowhere else."""

from __future__ import annotations

import hashlib
import importlib.metadata
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# The installed packages a conversion runs. A cached stage or a cached conversion made with
# other versions is stale. The page-check harness (tests/pages/corpus.py) keys on these too.
CONVERSION_PACKAGES = ("pypdfium2", "pikepdf", "pillow", "pdfplumber", "pdfminer.six", "ocrmypdf",
                       "docling", "docling-core", "docling-parse", "docling-ibm-models",
                       "torch", "transformers")
# The renderer escapes text through markupsafe. No cached stage renders, so
# its version is in the provenance record and the render key, not in the stage keys.
RENDER_PACKAGES = ("markupsafe",)
# OCR runs these programs when the ocrmypdf module is not installed, and tesseract either way.
OCR_PROGRAMS = ("ocrmypdf", "tesseract")


@lru_cache(maxsize=None)
def _file_sha256(path: str, size: int, mtime: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    """The sha256 of a file's bytes, read again only when its size or mtime changes."""
    st = path.stat()
    return _file_sha256(str(path.resolve()), st.st_size, st.st_mtime_ns)


@lru_cache(maxsize=None)
def program_output(*command: str) -> str | None:
    """What `command` printed (stdout then stderr), or None when it is not on PATH or fails to
    run. One run per command in this process."""
    if shutil.which(command[0]) is None:
        return None
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or "") + (done.stderr or "")


def program_version(name: str) -> str:
    """The first line of `name --version`; "absent" off PATH, "unknown" when it printed nothing."""
    if shutil.which(name) is None:
        return "absent"
    text = (program_output(name, "--version") or "").strip()
    return text.splitlines()[0] if text else "unknown"


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "absent"


def tool_versions() -> dict[str, str]:
    """{name: version} of Python, the installed CONVERSION_PACKAGES and the OCR programs."""
    out = {"python": ".".join(map(str, sys.version_info[:3]))}
    out.update({name: _package_version(name) for name in CONVERSION_PACKAGES})
    out.update({f"{name}-program": program_version(name) for name in OCR_PROGRAMS})
    return out


def tool_key() -> str:
    """`tool_versions` as one string, for a stage key."""
    return ";".join(f"{name}={version}" for name, version in tool_versions().items())


def dependency_versions() -> dict[str, str]:
    """`tool_versions` and the RENDER_PACKAGES: everything a record of a build pins."""
    return {**tool_versions(), **{name: _package_version(name) for name in RENDER_PACKAGES}}
