"""Reusing a conversion's model after a change that only the renderer reads.

Two identities describe a conversion:

  * the model key: what builds the model (names.MODEL_NAMES: document.json, review.json,
    model.json, trace/, figures/, working.pdf and its OCR word grades). It is the stage key of
    stagecache.STAGES["model"]: the code that import closure runs and the files beside it, the
    tool versions, and what the model reads: the source's name and bytes, the options that are
    not render settings, the OCR runner with its data, and the text witness's identity
    (ingest.text_trust.identity: its policy version, render scale and Tesseract data) with
    the font repair's vote policy (ingest.font_map.POLICY).
  * the render key: the model key, the code a whole conversion runs (reflow.convert's closure:
    output, render/, viewer/), the render settings (Options fields marked
    RENDER) and the renderer's packages.

With the stage cache on (Options.stage_cache), a conversion records both in BUILD_JSON, beside
document.json, with the digest of the model's files. The next conversion of the same source
looks for a saved model with its own model key: the previous output in out_dir first, then the
stage root's "model" entry (<root>/model/<source>/<key>, the layout every stage has, so locks and
eviction are stagecache's). It copies the files into its stage, and uses them only if they hash
to the recorded digest and document.json parses back to the same text; then only the renderer
runs. Anything it cannot prove is a miss and a full conversion, never an error. A conversion
whose model reads what no key can pin down (a model's answers, Docling's weights, a password, OCR
data that can't be identified, OCR that failed) records nothing and reuses nothing.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reflow import cachedir, identity, pipeline, stagecache
from reflow.ingest import font_map, text_trust
from reflow.ingest.ocr import LANGS_DEFAULT, make_runner
from reflow.names import BUILD_JSON, MODEL_NAMES, ORIGINAL_PDF, PAGES_DIR, WORKING_PDF
from reflow.pipeline import Model
from reflow.render.pages import page_filename
from reflow.run import RENDER, Options, Progress, Run, RunState, Workers
from reflow.steps import options_record, read_json, uncovered

SCHEMA = 1                        # of BUILD_JSON
KIND = "model"                    # the stage root's folder of saved models
RENDER_ENTRIES = ("reflow.convert",)   # what a whole conversion runs, the renderer included

type Origin = Literal["", "built", "previous output", "stage cache"]


def unprovable(opts: Options) -> list[str]:
    """Why a model built with `opts` can't be pinned down by a key, or [] when it can."""
    out = list(uncovered(opts))   # a model's answers, direct and polish among them
    if not opts.engine_info.provable:
        out.append(f"--engine {opts.engine}: Docling's model weights are in no key")
    if opts.password:
        out.append("a password is never part of a key")
    return out


def options_part(opts: Options, *, render: bool) -> dict:
    """The fields Options compares, split by their RENDER mark: the render settings, or every
    other field (a new option is a model option until it is marked)."""
    marked = {f.name for f in dataclasses.fields(opts) if f.metadata.get(RENDER)}
    return {k: v for k, v in options_record(opts).items() if (k in marked) == render}


@dataclass(frozen=True)
class Identity:
    """The keys a conversion of `source` with these options has: None when unprovable."""
    source: tuple[str, str]           # (file name, sha256)
    model: str | None
    render: str | None
    unprovable: tuple[str, ...] = ()
    # What the model key hashes, by name, so a refusal can say which one moved (rerender):
    # code, tools, options (the model's part) and ocr.
    inputs: dict[str, object] = dataclasses.field(default_factory=dict, compare=False)


class NotReusable(Exception):
    """A conversion that required a saved model (convert(must_reuse=True)) found none it could
    prove. Raised before anything is written; `reason` names what differs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def identify(pdf: Path, opts: Options) -> Identity:
    """The model key and the render key of converting `pdf` with `opts` in this checkout."""
    source = (pdf.name, identity.file_sha256(pdf))
    why = unprovable(opts)
    ocr = "none"
    if not why and (runner := make_runner(opts.ocr)) is not None:
        data = runner.environment(LANGS_DEFAULT)
        if data is None:
            why.append(f"the data of the {runner.name} OCR runner can't be identified")
        ocr = f"{runner.name}:{data}"
    if why:
        return Identity(source, None, None, tuple(why))
    model_options = options_part(opts, render=False)
    witness = {**text_trust.identity(), "font_map": font_map.POLICY}
    model = stagecache.stage_key(stagecache.STAGES[KIND], *source, model_options, ocr, witness)
    inputs = {"code": stagecache.code_hash(stagecache.STAGES[KIND]),
              "tools": identity.tool_key(), "options": model_options, "ocr": ocr,
              "witness": witness}
    h = hashlib.sha256(model.encode())
    h.update(stagecache.code_hash(RENDER_ENTRIES).encode())
    h.update(json.dumps([options_part(opts, render=True), identity.dependency_versions()],
                        sort_keys=True).encode())
    return Identity(source, model, h.hexdigest()[:20], inputs=inputs)


def digest(folder: Path) -> str:
    """The sha256 of the model's files in `folder` (names.MODEL_NAMES that are there), by
    relative path and bytes, in path order."""
    h = hashlib.sha256()
    files = []
    for name in MODEL_NAMES:
        path = folder / name
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file())
    for path in sorted(files, key=lambda p: p.relative_to(folder).as_posix()):
        data = path.read_bytes()
        h.update(b"\0%s\0%d\0" % (path.relative_to(folder).as_posix().encode(), len(data)))
        h.update(data)
    return h.hexdigest()


def _record(identity: Identity, model_digest: str, opts: Options) -> str:
    """BUILD_JSON. `inputs` and `options` are for reflow rerender: what the model key hashed,
    and the conversion's options (a record without them can't be rerendered)."""
    return json.dumps({"schema": SCHEMA,
                       "source": {"name": identity.source[0], "sha256": identity.source[1]},
                       "model": {"key": identity.model, "digest": model_digest,
                                 "inputs": identity.inputs},
                       "render": {"key": identity.render},
                       "options": options_record(opts)}, indent=1, sort_keys=True) + "\n"


def read_record(folder: Path) -> dict | None:
    """folder/BUILD_JSON as a dict, or None when it is absent, unreadable or not this schema."""
    value = read_json(folder / BUILD_JSON)
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


def _recorded(folder: Path) -> tuple[str, str] | None:
    """(model key, digest) of folder/BUILD_JSON, or None for anything else."""
    try:
        record = read_record(folder)
        key, made = record["model"]["key"], record["model"]["digest"]   # type: ignore[index]
    except (KeyError, TypeError):
        return None
    return (key, made) if isinstance(key, str) and isinstance(made, str) else None


_FICLONE = 0x40049409   # linux/fs.h


def clone(src: Path, dst: Path) -> None:
    """Copy a file or a tree, as a reflink where the file system shares extents (btrfs, xfs),
    else byte for byte. Never a hard link: a saved file must not change when a copy does."""
    if src.is_dir():
        dst.mkdir()
        for item in sorted(src.iterdir()):
            clone(item, dst / item.name)
        return
    with src.open("rb") as a, dst.open("wb") as b:
        try:
            fcntl.ioctl(b.fileno(), _FICLONE, a.fileno())
            return
        except OSError:
            pass
    shutil.copyfile(src, dst)


def _clone_model(src: Path, dst: Path) -> None:
    for name in MODEL_NAMES:
        if (src / name).exists():
            clone(src / name, dst / name)


def _drop_model(folder: Path) -> None:
    for name in MODEL_NAMES:
        path = folder / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


class Reuse:
    """One conversion's reuse of a saved model: `open` before the stage exists (it only reads),
    `restore` into the stage, `close` once published. Inert without the stage cache."""

    def __init__(self, pdf: Path, out_dir: Path, opts: Options, identity: Identity | None) -> None:
        self.pdf, self.out_dir, self.opts, self.identity = pdf, out_dir, opts, identity
        self.origin: Origin = "" if identity is None else "built"

    @classmethod
    def open(cls, pdf: Path, out_dir: Path, opts: Options) -> Reuse:
        # A conversion with a decisions file records its identity even without the stage cache, so
        # its own output can be rerendered when only the store changes (Apply decisions).
        keyed = opts.stage_cache is not None or opts.decisions is not None
        return cls(pdf, out_dir, opts, identify(pdf, opts) if keyed else None)

    @property
    def note(self) -> str:
        """What happened to the model, for the Summary: "" without the stage cache."""
        if self.identity is None:
            return ""
        if self.identity.model is None:
            return "built, not kept: " + "; ".join(self.identity.unprovable)
        if self.origin != "built":
            return self.origin
        return "built, kept" if self._entry() is not None else "built"   # kept in the stage root

    def _entry(self) -> Path | None:
        if self.identity is None or self.identity.model is None or self.opts.stage_cache is None:
            return None
        return (self.opts.stage_cache / KIND / self.identity.source[1][:16] / self.identity.model)

    def restore(self, stage: Path, workers: Workers, progress: Progress | None
                ) -> tuple[Run, Model] | None:
        """The Run and the Model of a saved model with this conversion's model key, copied into
        `stage` and proved there; None (and nothing of it left in the stage) when there is none."""
        if self.identity is None or self.identity.model is None or not self.opts.reuse_model:
            return None
        start = time.monotonic()
        model, origin = None, None
        if self._matches(self.out_dir):
            model = self._take(self.out_dir, stage)
            origin = "previous output"
        if model is None and (entry := self._entry()) is not None and entry.exists():
            with cachedir.try_lock(entry, shared=True) or nullcontext():
                if self._matches(entry):
                    model = self._take(entry, stage)
                    origin = "stage cache"
            if model is not None:
                stagecache.touch(entry)   # most recently used: eviction keeps it longest
            if model is None:   # another key's files, or damaged: no later run may read it
                stagecache.drop(entry)
        if model is None:
            return None
        self.origin = origin
        shutil.copyfile(self.pdf, stage / ORIGINAL_PDF)
        report: Progress = progress or (lambda *_: None)
        carried = origin == "previous output" and self._carry_pages(stage, len(model.doc.pages))
        run = Run(pdf=self.pdf, stage=stage, options=self.opts, workers=workers, progress=report,
                  doc=model.doc, working=stage / WORKING_PDF, ocr=model.facts.ocr,
                  seconds={"restore": round(time.monotonic() - start, 2)},
                  state=RunState(pages_carried=carried))
        return run, pipeline.redecide(run, model)

    def _carry_pages(self, stage: Path, count: int) -> bool:
        """Clone the previous output's page images into `stage` when they can't differ from a
        render: its render key is this run's (the model, so the working copy and the page count;
        the render settings; the renderer's code and packages), and its pages/ holds exactly the
        files this run would write. Else False, and the images are rendered as for any run."""
        src = self.out_dir / PAGES_DIR
        try:
            same = read_record(self.out_dir)["render"]["key"] == self.identity.render   # type: ignore[index,union-attr]
        except (KeyError, TypeError):
            return False
        if not (same and self.opts.page_images and src.is_dir()):
            return False
        widths = self.opts.written_widths(count)
        want = {page_filename(n, None if w == widths[0] else w)
                for n in range(1, count + 1) for w in widths}
        if {p.name for p in src.iterdir()} != want:
            return False
        try:
            clone(src, stage / PAGES_DIR)
        except OSError:
            shutil.rmtree(stage / PAGES_DIR, ignore_errors=True)
            return False
        return True

    def _matches(self, folder: Path) -> bool:
        recorded = _recorded(folder)
        return recorded is not None and recorded[0] == self.identity.model   # type: ignore[union-attr]

    def _take(self, folder: Path, stage: Path) -> Model | None:
        """Copy the model's files from `folder` into `stage`, and prove the copy: its digest is
        the recorded one, and the Document parses back to its own text. None when not."""
        recorded = _recorded(folder)
        if recorded is None:
            return None
        try:
            _clone_model(folder, stage)
            if digest(stage) != recorded[1]:
                raise ValueError("the saved model's files are not the ones recorded")
            return Model.read(stage)
        except (OSError, ValueError, KeyError, TypeError):
            _drop_model(stage)
            return None

    def close(self, run: Run, model: Model) -> None:
        """Record the identities in run.stage/BUILD_JSON, and keep a model built here in the
        stage root (best effort: a failure to keep it never fails the conversion)."""
        if self.identity is None or self.identity.model is None:
            return
        if model.facts.ocr.failed:   # an OCR failure under load: a later run must OCR again
            self.identity = dataclasses.replace(
                self.identity, model=None, render=None,
                unprovable=(f"OCR failed on pages {sorted(model.facts.ocr.failed)}",))
            return
        lost = sorted({n for report in (run.trust, run.state.missing) if report is not None
                       for n in text_trust.failed(report)})
        if lost:   # likewise the witness, and flag_missing's OCR
            self.identity = dataclasses.replace(
                self.identity, model=None, render=None,
                unprovable=(f"the text witness failed on pages {lost}",))
            return
        (run.stage / BUILD_JSON).write_text(_record(self.identity, digest(run.stage), self.opts),
                                            encoding="utf-8")
        entry = self._entry()
        if self.origin == "built" and entry is not None:
            def write(tmp: Path, _) -> None:
                tmp.mkdir()
                _clone_model(run.stage, tmp)
                clone(run.stage / BUILD_JSON, tmp / BUILD_JSON)
            stagecache.keep(entry, write)
