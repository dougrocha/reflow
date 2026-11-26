"""Applying a reviewer's decisions to a conversion without converting it again.

`rerender(out, store)` replays the store on the model the conversion in `out` saved, and
publishes the result in its place, through the same `convert` a full build runs: the saved model
is restored (reflow.saved), the store replayed on it (pipeline.redecide) and the site rendered.
It runs only when the model a full conversion would build now is the one saved: the same code,
tools, options and PDF. Else it raises `CannotRerender` before writing anything, and the caller
converts in full. `out` is as it was after any failure (output.staged)."""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
from pathlib import Path

from reflow import budget, identity, saved
from reflow.convert import Summary, convert
from reflow.names import DOCUMENT_JSON, ORIGINAL_PDF
from reflow.run import Options, Progress, with_device

# The options a caller can say a full conversion would run with (`reflow rerender --source`):
# the ones docs:build sets (REFLOW_ENGINE, REFLOW_ALT_TEXT).
EXPECTED = ("engine", "alt_text")


class NoConversion(Exception):
    """`out` holds no conversion this command can read (exit 2)."""


class CannotRerender(Exception):
    """The saved model is not the one a full conversion would build (exit 4): `reason` is one of
    pdf, options, code, tools, record or model, `detail` says what moved."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


def _options(recorded: dict, store: Path, cache: Path | None) -> dict:
    """The keyword options of the recorded conversion, with this store and stage root."""
    kwargs = dict(recorded)
    if kwargs.pop("password", False):
        raise CannotRerender("options", "the conversion needed a password")
    kwargs.update(decisions=store, stage_cache=cache)
    try:
        with_device(Options(**kwargs))
    except (TypeError, ValueError) as e:
        raise CannotRerender("options", f"the recorded options are not the converter's ({e})")
    return kwargs


def _refusal(record: dict, kwargs: dict, pdf: Path) -> CannotRerender | None:
    """What differs between the recorded model and the one a conversion would build now."""
    if identity.file_sha256(pdf) != record["source"]["sha256"]:
        return CannotRerender("pdf", f"{ORIGINAL_PDF} is not the PDF that was converted")
    now = saved.identify(pdf, with_device(Options(**kwargs)))
    if now.model is None:
        return CannotRerender("options", "; ".join(now.unprovable))
    if now.model == record["model"]["key"]:
        return None
    before = record["model"].get("inputs") or {}
    for reason, name, what in (("options", "options", "the options"),
                               ("code", "code", "the converter's code"),
                               ("tools", "tools", "the tools' versions"),
                               ("tools", "ocr", "the OCR data"),
                               ("tools", "witness", "the text witness's policy or OCR data")):
        if before.get(name) != now.inputs.get(name):
            return CannotRerender(reason, f"{what} changed since the conversion")
    return CannotRerender("model", "the saved model's key is not the one a conversion makes now")


def _expected(record: dict, recorded: dict, source: Path, options: dict[str, str]) -> None:
    """Raise CannotRerender when a full conversion of `source` with `options` (the flags it would
    be given; an option not given is Options' default) is not the conversion recorded."""
    if not source.is_file():
        raise ValueError(f"{source} is not a file")
    if source.name != Path(record["source"]["name"]).name:
        raise CannotRerender("pdf", f"the conversion was of {record['source']['name']}, "
                             f"not {source.name}")
    if identity.file_sha256(source) != record["source"]["sha256"]:
        raise CannotRerender("pdf", f"{source} is not the PDF that was converted")
    defaults = {f.name: f.default for f in dataclasses.fields(Options)}
    for name in EXPECTED:
        want = options.get(name, defaults[name])
        if recorded.get(name) != want:
            raise CannotRerender("options", f"the conversion ran with {name} "
                                 f"{recorded.get(name)!r}, not {want!r}")


def rerender(out: Path, store: Path, *, stage_cache: Path | None = None, jobs: int = 1,
             lease: budget.Lease | None = None, on_progress: Progress | None = None, source: Path | None = None,
             options: dict[str, str] | None = None) -> Summary:
    """Replay `store` on the conversion in `out`. With `source`, it runs only when a full
    conversion of that PDF with `options` (EXPECTED's keys; one not given is Options' default)
    would be the conversion recorded. Raises NoConversion, CannotRerender, ValueError (the store
    or the source is not a readable file, or `out` holds foreign files). With a `lease`
    (reflow.budget) the render's page workers hold slots only while they run."""
    out, store = Path(out), Path(store)
    if not store.is_file() or not os.access(store, os.R_OK):
        raise ValueError(f"{store} is not a readable file")
    if options and source is None:
        raise ValueError("options are checked only against a source")
    if options and (extra := set(options) - set(EXPECTED)):
        raise ValueError(f"only {', '.join(EXPECTED)} are checked, not {', '.join(sorted(extra))}")
    record = saved.read_record(out)
    original = out / ORIGINAL_PDF
    if record is None or not (out / DOCUMENT_JSON).is_file() or not original.is_file():
        raise NoConversion(f"{out} holds no conversion with a {saved.BUILD_JSON}, "
                           f"{DOCUMENT_JSON} and {ORIGINAL_PDF}")
    try:
        recorded, name = record["options"], Path(record["source"]["name"]).name
        record["model"]["key"], record["source"]["sha256"]
    except (KeyError, TypeError):
        raise CannotRerender("record", f"the {saved.BUILD_JSON} was written before rerender "
                             "recorded the conversion's options")
    if not isinstance(recorded, dict) or not name:
        raise CannotRerender("record", f"the {saved.BUILD_JSON} holds no options")
    if source is not None:
        _expected(record, recorded, Path(source), options or {})
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{out.name}.rerender-", dir=out.parent))
    try:
        pdf = work / "source" / name      # the conversion's own file name is part of its identity
        pdf.parent.mkdir()
        try:
            os.link(original, pdf)
        except OSError:
            shutil.copyfile(original, pdf)
        # The output's own build.json proves the model; a stage root is only used when given.
        kwargs = _options(recorded, store, stage_cache)
        if (refusal := _refusal(record, kwargs, pdf)) is not None:
            raise refusal
        try:
            return convert(pdf, out, jobs=jobs, lease=lease, on_progress=on_progress,
                           must_reuse=True, **kwargs)
        except saved.NotReusable as e:
            raise CannotRerender("model", e.reason) from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
