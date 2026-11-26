"""Tesseract's data: where its models are, and a digest of the ones a reading uses. Read by
replacement OCR (reflow.ingest.ocr) and by the text witness (reflow.ingest.witness), whose stage
keys and the model key hold it."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from reflow.identity import file_sha256, program_output


def tessdata_dir(prefix: str | None) -> Path | None:
    """The directory tesseract reads its models and configs from: TESSDATA_PREFIX when set
    (tesseract 4 and later read the models straight from it), else the one `tesseract
    --list-langs` names."""
    if prefix:
        return Path(prefix) if Path(prefix).is_dir() else None
    listed = program_output("tesseract", "--list-langs") or ""
    found = re.search(r'languages in "([^"]+)"', listed)
    return Path(found.group(1)) if found and Path(found.group(1)).is_dir() else None


def data_digest(langs: tuple[str, ...]) -> str | None:
    """A digest of the tessdata directory and TESSDATA_PREFIX, each language's traineddata and
    every file in configs/ and tessconfigs/. None when the directory or a language's model can't
    be found."""
    prefix = os.environ.get("TESSDATA_PREFIX")
    data = tessdata_dir(prefix)
    if data is None:
        return None
    h = hashlib.sha256(f"prefix={prefix};dir={data.resolve()}".encode())

    def add(path: Path) -> None:
        h.update(f";{path.relative_to(data)}=".encode())
        h.update(file_sha256(path).encode())
    for lang in langs:
        model = data / f"{lang}.traineddata"
        if not model.is_file():
            return None
        add(model)
    for sub in ("configs", "tessconfigs"):
        if (data / sub).is_dir():
            for path in sorted(p for p in (data / sub).iterdir() if p.is_file()):
                add(path)
    return h.hexdigest()


def installed(langs: tuple[str, ...]) -> tuple[str, ...]:
    """The languages of `langs` whose traineddata is in the tessdata directory, in that order."""
    data = tessdata_dir(os.environ.get("TESSDATA_PREFIX"))
    if data is None:
        return ()
    return tuple(lang for lang in langs if (data / f"{lang}.traineddata").is_file())
