"""What a published document was converted from, and whether a rebuild here would publish the same.

docs:build (web/scripts/build-docs.ts) records the inputs of each document it converts in
DATA_ROOT/.provenance/<slug>.json, beside the conversion in DATA_ROOT/<slug>. `check` compares a
record with the inputs a rebuild in this checkout would use, and says whether the document is
current, stale, missing or unverifiable, and why. docs/provenance.md describes the workflow.

A record holds:
  * source: the digest of every file in the reflow package that ran (code and viewer
    assets; bytecode caches skipped). This is the page-check cache's code hash, so an uncommitted
    edit counts as much as a commit. The record also keeps each file's hash, to name the files
    that changed, and the git commit and uncommitted files, which describe the build but decide
    nothing.
  * pdf: the source PDF's sha256 and size.
  * options: every field of reflow.run.Options that can change the output.
  * dependencies: identity.dependency_versions() (Python, the parser, OCR and model packages, the
    OCR programs: the list both caches key on, and the renderer's packages).
  * output: the "v" of the conversion's manifest.json (reflow.output.stamp_version). It ties the
    record to the one build it describes, so a conversion replaced without a new record (by an
    older docs:build, or a plain `reflow convert`) no longer matches it.
  * changed_during_build: the inputs that differed between the start and the end of the build.
  * stage_cache: present when the build read the PDF through the stage cache (`docs:build
    --stage-cache`, reflow.stagecache), with the sha256 of the working copy that keyed its
    extraction and page reads. The cache's keys hold the code and versions each stage runs, so
    such a build has the inputs the record names; `check` says it used the cache.

Inputs no record covers, so `check` cannot see them change: Python packages outside the
dependency list (numpy, charset-normalizer, ...), Docling's model weights in the Hugging Face
cache, what the OCR programs run with (ghostscript, tesseract's language data, the system
Python's Pillow under the grading plugin, TESSDATA_PREFIX, OMP_THREAD_LIMIT), other system
libraries and fonts, and an edit made and undone while a build ran. "Current" means the same
inputs, not the same bytes: OCR is not reproducible run to run. A conversion that asks a model for
figure descriptions or assist answers is never current: the answers, and the caches carried over
from the previous output, are not inputs a record can pin down (`reflow.steps.uncovered`).

`build_info` is the public part of a build, for the reader ("How this transcript was made"): the
engine, the OCR, each AI step with its provider, resolved model and answers, the alt text, the
reviewer's decisions applied, and the converter commit and build time. Never a path, a key or the
record itself. docs:build publishes it as BUILD_INFO beside the document.

Run as `python -m reflow.provenance check|snapshot|record SPEC`; see `main`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reflow import identity
from reflow.names import DECISIONS_LEDGER, DOCUMENT_JSON, MANIFEST_JSON, MODEL_JSON, WORKING_PDF
from reflow.providers import ALT_TEXT_MODELS
from reflow.steps import STEPS, options_record, read_json, uncovered

if TYPE_CHECKING:
    from reflow.run import Options

SCHEMA = 1
PACKAGE_DIR = Path(__file__).resolve().parent   # the reflow package this process runs
BUILD_INFO = "build-info.json"   # build_info, published in the site's copy of a document only


def source_identity(package: Path = PACKAGE_DIR) -> tuple[str, dict[str, str]]:
    """(digest, {name: sha256 prefix}) of every file in the reflow package at `package`, with
    bytecode caches skipped. A file is named "src/reflow/<path>" wherever the package is, so the
    digest is the page-check cache's code hash (tests/pages/corpus.py) of the same files."""
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    paths = (p for p in package.rglob("*")
             if p.is_file() and "__pycache__" not in p.relative_to(package).parts)
    for path in sorted(paths):
        name = "src/reflow/" + path.relative_to(package).as_posix()
        data = path.read_bytes()
        digest.update(name.encode())
        digest.update(data)
        files[name] = hashlib.sha256(data).hexdigest()[:16]
    return digest.hexdigest(), files


def git_state(package: Path = PACKAGE_DIR) -> dict | None:
    """The commit of the checkout that holds `package`, and its uncommitted files under it. None
    outside git. It describes a build; `check` never decides on it."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=package, capture_output=True, text=True,
                              timeout=10, check=True).stdout
    try:
        commit = git("rev-parse", "HEAD").strip()
        status = git("status", "--porcelain=v1", "--untracked-files=all", "--", ".")
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": commit, "uncommitted": [line[3:] for line in status.splitlines()]}


def environment(package: Path = PACKAGE_DIR) -> dict[str, Any]:
    """The inputs every document of a build shares: the source and the dependencies."""
    from reflow.ingest.text_trust import identity as witness_identity
    digest, files = source_identity(package)
    return {"source": {"digest": digest, "files": files, "git": git_state(package)},
            "dependencies": {**identity.dependency_versions(),
                             "text-witness": _witness(witness_identity())}}


def _witness(value: dict[str, Any]) -> str:
    """The text witness's identity (ingest.text_trust.identity) as one short string: its policy
    and each language's Tesseract data, so a traineddata change shows as a dependency change."""
    data = "; ".join(f"{lang} {(d or 'unknown')[:12]}" for lang, d in sorted(value["data"].items()))
    return f"policy {value['policy']}, scale {value['scale']:.3f}; {data}"


def inputs(pdf: Path, options: Options, env: dict[str, Any]) -> dict[str, Any]:
    """What a conversion of `pdf` with `options` reads, given the shared `env` (`environment`)."""
    return {"schema": SCHEMA, **env,
            "pdf": {"sha256": identity.file_sha256(pdf), "bytes": pdf.stat().st_size},
            "options": options_record(options), "uncovered": uncovered(options)}


def changed(before: dict[str, Any], after: dict[str, Any] | None) -> list[str]:
    """The inputs that differ between two `inputs` of one document."""
    if after is None:
        return ["pdf"]
    pairs = (("source", before["source"]["digest"], after["source"]["digest"]),
             ("pdf", before["pdf"], after["pdf"]),
             ("options", before["options"], after["options"]),
             ("dependencies", before["dependencies"], after["dependencies"]))
    return [name for name, a, b in pairs if a != b]


def make_record(before: dict[str, Any], after: dict[str, Any] | None, version: str, *,
                stage_cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """The record of a build that read `before` at its start and `after` at its end, and wrote
    the conversion whose manifest "v" is `version`; `stage_cache` when it read the PDF through
    the stage cache."""
    record = {**before, "output": {"v": version}}
    if moved := changed(before, after):
        record["changed_during_build"] = moved
    if stage_cache is not None:
        record["stage_cache"] = stage_cache
    return record


def write_record(path: Path, record: dict[str, Any]) -> None:
    """Write `record` under a name of this writer's own, then rename it in, so a reader sees the
    old record or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def output_version(out_dir: Path) -> str | None:
    """The "v" of the conversion's manifest.json: the digest of what it published. None when
    there is no manifest or it has no digest."""
    manifest = read_json(out_dir / MANIFEST_JSON)
    v = manifest.get("v") if isinstance(manifest, dict) else None
    return v if isinstance(v, str) else None


def _sha256_or_none(path: Path) -> str | None:
    try:
        return identity.file_sha256(path)
    except OSError:
        return None


@dataclass(frozen=True)
class Published:
    """Where docs:build keeps one document. `site` (public/docs/<slug>) and `pdf_copy`
    (public/pdfs/<slug>.pdf) are checked only when given."""
    slug: str
    pdf: Path            # the source PDF a rebuild would convert
    data: Path           # DATA_ROOT/<slug>, the whole conversion
    record: Path         # DATA_ROOT/.provenance/<slug>.json
    site: Path | None = None
    pdf_copy: Path | None = None


@dataclass(frozen=True)
class Verdict:
    slug: str
    status: str          # current, unverifiable, stale or missing (web/scripts/freshness.ts)
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"slug": self.slug, "status": self.status, "reasons": list(self.reasons)}


def _short(name: str) -> str:
    return name.removeprefix("src/reflow/")


def _names(names: list[str], most: int = 4) -> str:
    shown = ", ".join(_short(n) for n in names[:most])
    return shown + (f" and {len(names) - most} more" if len(names) > most else "")


def describe_git(git: dict | None) -> str:
    """"1525bc9a2f10" or "1525bc9a2f10 with 2 uncommitted files under src/reflow"; "" outside git."""
    if not git:
        return ""
    count = len(git.get("uncommitted", []))
    tail = f" with {count} uncommitted file{'s' if count != 1 else ''} under src/reflow" \
        if count else ""
    return f"{git['commit'][:12]}{tail}"


def _source_reason(was: dict, now: dict) -> str:
    a, b = was.get("files", {}), now["files"]
    parts = []
    for label, names in (("changed", sorted(k for k in a.keys() & b.keys() if a[k] != b[k])),
                         ("added", sorted(b.keys() - a.keys())),
                         ("removed", sorted(a.keys() - b.keys()))):
        if names:
            parts.append(f"{len(names)} {label} ({_names(names)})")
    built, here = describe_git(was.get("git")), describe_git(now.get("git"))
    where = f"; built from {built}, this checkout is {here}" if built and here else ""
    return f"the converter source differs from the build's: {'; '.join(parts) or 'files differ'}{where}"


def _diff(was: dict, now: dict) -> str:
    keys = sorted(k for k in was.keys() | now.keys() if was.get(k) != now.get(k))
    shown = [f"{k} {was.get(k, 'absent')} -> {now.get(k, 'absent')}" for k in keys[:4]]
    return ", ".join(shown) + (f" and {len(keys) - 4} more" if len(keys) > 4 else "")


def _well_formed(record: Any) -> bool:
    try:
        return (record["schema"] == SCHEMA and isinstance(record["output"]["v"], str)
                and isinstance(record["source"]["digest"], str)
                and isinstance(record["pdf"]["sha256"], str)
                and isinstance(record["options"], dict)
                and isinstance(record["dependencies"], dict))
    except (KeyError, TypeError):
        return False


def check(doc: Published, now: dict[str, Any] | None) -> Verdict:
    """Whether `doc` is what a rebuild with the inputs `now` (`inputs`, or None when the source
    PDF is missing) would publish:
      missing       nothing converted, or not published
      unverifiable  no record that describes this conversion (an old output, or one replaced
                    without a record), inputs a record cannot cover, or inputs that changed while
                    it was built, or no source PDF to compare
      stale         the record shows other inputs than now: source, PDF, options, dependencies;
                    or the published copy or PDF copy is not this conversion
      current       none of these
    Every reason found is given; the status is the most severe one."""
    def verdict(status: str, *reasons: str) -> Verdict:
        return Verdict(doc.slug, status, reasons)

    version = output_version(doc.data)
    if not (doc.data / MANIFEST_JSON).is_file():
        return verdict("missing", f"not converted here (no {doc.data.name}/manifest.json in the "
                                  "data folder)")
    if doc.site is not None and not (doc.site / MANIFEST_JSON).is_file():
        return verdict("missing", "converted but not published (no public copy)")
    record = read_json(doc.record)
    if record is None:
        return verdict("unverifiable", "no provenance record: converted before docs:build "
                       "recorded provenance, or by an older docs:build or a plain reflow convert")
    if not _well_formed(record):
        return verdict("unverifiable", f"the provenance record is not a schema {SCHEMA} record")
    if version is None:
        return verdict("unverifiable", "the conversion's manifest.json has no content digest")
    if record["output"]["v"] != version:
        return verdict("unverifiable", "the provenance record describes another build of this "
                       "document: the conversion was replaced without a new record")

    stale: list[str] = []
    unsure: list[str] = []
    if doc.site is not None and output_version(doc.site) != version:
        stale.append("the published copy is not this conversion (a publish that did not finish)")
    if doc.pdf_copy is not None and _sha256_or_none(doc.pdf_copy) != record["pdf"]["sha256"]:
        stale.append(f"public/pdfs/{doc.pdf_copy.name} is not the PDF that was converted")
    if now is None:
        unsure.append(f"the source PDF is not here to compare ({doc.pdf})")
    else:
        if record["pdf"]["sha256"] != now["pdf"]["sha256"]:
            stale.append("the source PDF changed since the build")
        if record["source"]["digest"] != now["source"]["digest"]:
            stale.append(_source_reason(record["source"], now["source"]))
        if record["options"] != now["options"]:
            stale.append(f"the conversion options differ: {_diff(record['options'], now['options'])}")
        if record["dependencies"] != now["dependencies"]:
            stale.append("the dependency versions differ: "
                         f"{_diff(record['dependencies'], now['dependencies'])}")
    if moved := record.get("changed_during_build"):
        unsure.append(f"its inputs changed while it was built ({', '.join(moved)})")
    unsure.extend(f"built with inputs a record cannot cover: {what}"
                  for what in record.get("uncovered", []))
    if stale:
        return verdict("stale", *stale, *unsure)
    if unsure:
        return verdict("unverifiable", *unsure)
    built = describe_git(record["source"].get("git"))
    cached = "; it read the PDF through the stage cache" if record.get("stage_cache") else ""
    return verdict("current", "built from this checkout's converter source, the same PDF, "
                   "options and dependency versions" + (f" (at {built})" if built else "") + cached)


def _document_meta(path: Path) -> dict[str, Any]:
    """The "meta" of a document.json, read from its start: the whole file can be over 100 MB, and
    "meta" comes before the pages. {} when it cannot be read."""
    decoder, text = json.JSONDecoder(), ""
    try:
        with path.open(encoding="utf-8") as f:
            while chunk := f.read(1 << 20):
                text += chunk
                at = text.find('"meta": ')
                if at < 0:
                    continue
                try:
                    meta = decoder.raw_decode(text, at + len('"meta": '))[0]
                except ValueError:
                    continue
                return meta if isinstance(meta, dict) else {}
    except OSError:
        pass
    return {}


def model_run(data: Path, key: str = "direct") -> dict[str, Any] | None:
    """What the page-by-page model run `key` (a steps.STEPS entry with `run`) of the conversion in
    `data` read and cost: "direct" (--engine direct, analyze.direct) or "polish" (--polish,
    analyze.polish). Its model, provider and prompt, the pages it read (from the cache too) and left unread, and the tokens, dollars
    and seconds per page and in all; a polish run adds the edits applied and rejected. None when
    the conversion has no such run. A cached page keeps the numbers of the request that made it,
    so this is what the run cost to make."""
    meta = _document_meta(data / DOCUMENT_JSON).get(key)
    if not isinstance(meta, dict):
        return None
    pages = meta.get("pages") if isinstance(meta.get("pages"), dict) else {}
    out = {
        "model": meta.get("model"), "provider": meta.get("provider"), "prompt": meta.get("prompt"),
        "pages": len(pages), "cached": meta.get("cached", 0),
        "unread": sorted(int(n) for n in (meta.get("unread") or {})),
        "tokensIn": meta.get("tokens_in", 0), "tokensOut": meta.get("tokens_out", 0),
        "dollars": meta.get("usd", 0.0), "seconds": meta.get("seconds", 0.0),
        "spent": meta.get("spent", 0.0), "stopped": bool(meta.get("stopped")),
        "perPage": [{"page": int(n), "tokensIn": p.get("in", 0), "tokensOut": p.get("out", 0),
                     "dollars": p.get("usd", 0.0), "seconds": p.get("s", 0.0),
                     "cached": bool(p.get("cached"))}
                    for n, p in sorted(pages.items(), key=lambda kv: int(kv[0]))],
    }
    if key == "polish":
        out.update(applied=meta.get("applied", 0), rejected=meta.get("rejected", 0))
    return out


def stage_timings(timings: Any) -> dict[str, dict[str, float]] | None:
    """`timings` ({"seconds": {stage: s}, "gpuWait": {stage: s}}, from the converter's `timings`
    progress line) cleaned to numbers only, or None when it holds no stage."""
    if not isinstance(timings, dict):
        return None

    def numbers(value: Any) -> dict[str, float]:
        return {k: round(float(v), 2) for k, v in value.items()
                if isinstance(k, str) and isinstance(v, int | float) and not isinstance(v, bool)
                } if isinstance(value, dict) else {}
    seconds = numbers(timings.get("seconds"))
    return {"seconds": seconds, "gpuWait": numbers(timings.get("gpuWait"))} if seconds else None


def build_info(record: dict[str, Any], data: Path, *, built: datetime | None = None,
               timings: Any = None) -> dict[str, Any]:
    """The public facts of the build `record` describes, whose conversion is in `data`: the
    engine, the pages OCR ran on, each AI step with its provider, resolved model, the answers it
    gave and the questions it was sent against the cap, the alt text, the reviewer's decisions
    applied, the converter commit ("+edits" when its tree was dirty) and the build time, and the
    tools that made the source PDF (its /Producer and /Creator), so a reviewer sees which tool's
    quirks to expect. With `timings` (see `stage_timings`) it also holds the seconds each stage of
    the build took, under "timings"; a build whose timings were not seen has none."""
    opts = record["options"]
    model = read_json(data / MODEL_JSON) or {}
    meta = _document_meta(data / DOCUMENT_JSON)
    ocr = model.get("ocr") if isinstance(model.get("ocr"), dict) else {}
    steps = []
    for step in STEPS:
        if step.info is not None and step.on(opts):
            stats = meta.get(step.meta) if step.meta else None
            steps.append(step.info(opts, stats if isinstance(stats, dict) else {}))
    alt = None
    if opts.get("alt_text", "none") != "none":
        stats = model.get("alt_text") if isinstance(model.get("alt_text"), dict) else {}
        alt = {"provider": opts["alt_text"],
               "model": opts.get("alt_text_model") or ALT_TEXT_MODELS[opts["alt_text"]],
               "described": stats.get("described", 0), "cached": stats.get("cached", 0)}
    ledger = read_json(data / DECISIONS_LEDGER)
    applied = ledger.get("applied") if isinstance(ledger, dict) else None
    git = record["source"].get("git")
    info = {
        "schema": 1,
        "engine": opts["engine"],
        "ocr": {"mode": opts.get("ocr", "auto"), "runner": ocr.get("runner"),
                "pages": sorted(ocr.get("done") or [])},
        "ai": steps,
        "altText": alt,
        "decisionsApplied": len(applied) if isinstance(applied, list) else 0,
        "converter": ({"commit": git["commit"][:12], "edits": bool(git.get("uncommitted"))}
                      if git else None),
        "built": (built or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pdf": {"producer": meta.get("producer"), "creator": meta.get("creator")},
    }
    for step in STEPS:
        if step.run and (made := model_run(data, step.key)) is not None:
            info[step.key] = made
    if (stages := stage_timings(timings)) is not None:
        info["timings"] = stages
    return info


# ---- python -m reflow.provenance ------------------------------------------------------------

USAGE = """usage: python -m reflow.provenance check|snapshot|record SPEC

SPEC is a JSON file: {"src": <the reflow package a rebuild here runs>, "options": {<reflow.run.Options
keywords>}, "docs": [{"slug", "pdf", "data", "record", "site"?, "pdfCopy"?, "options"?: {<the
document's own keywords, over the spec's>}}], "before"?: {...},
"stageCache"?: true when the build read the PDFs through the stage cache,
"timings"?: {slug: {"seconds": {stage: s}, "gpuWait": {stage: s}}} from the converter's timings lines}
  check     prints [{"slug", "status", "reasons"}], one verdict per document
  snapshot  prints {slug: inputs} of the documents whose PDF is here: the start of a build
  record    writes each document's record from SPEC's "before" (a snapshot) and the inputs now,
            tied to its conversion's manifest "v"; prints {slug: {"written", "changed", "info"}
            or {"error"}}, where "info" is its build_info, for docs:build to publish
"""


def _published(spec: dict) -> list[Published]:
    def path(value: str | None) -> Path | None:
        return Path(value) if value else None
    return [Published(d["slug"], Path(d["pdf"]), Path(d["data"]), Path(d["record"]),
                      path(d.get("site")), path(d.get("pdfCopy"))) for d in spec["docs"]]


def _dispatch(command: str, spec: dict) -> Any:
    from reflow.run import Options, with_device
    expected = spec.get("src")
    if expected and Path(expected).resolve() != PACKAGE_DIR:
        raise SystemExit(f"provenance: this Python imports reflow from {PACKAGE_DIR}, not "
                         f"{expected}; put that checkout's src first on PYTHONPATH")
    shared = spec.get("options", {})
    options = {d["slug"]: Options(**{**shared, **d.get("options", {})}) for d in spec["docs"]}
    docs = _published(spec)
    env = environment()

    def now(doc: Published) -> dict[str, Any] | None:
        return inputs(doc.pdf, with_device(options[doc.slug]), env) if doc.pdf.is_file() else None

    if command == "check":
        return [check(doc, now(doc)).to_json() for doc in docs]
    if command == "snapshot":
        return {doc.slug: found for doc in docs if (found := now(doc)) is not None}
    if command == "record":
        out: dict[str, Any] = {}
        for doc in docs:
            before = spec.get("before", {}).get(doc.slug)
            version = output_version(doc.data)
            if before is None or version is None:
                out[doc.slug] = {"error": "no snapshot from the start of the build"
                                 if before is None else "no converted manifest with a digest"}
                continue
            cache = ({"working": _sha256_or_none(doc.data / WORKING_PDF)}
                     if spec.get("stageCache") else None)
            made = make_record(before, now(doc), version, stage_cache=cache)
            for step in STEPS:   # what a model run cost, per page and in all
                if step.run and (ran := model_run(doc.data, step.key)) is not None:
                    made[step.key] = ran
            write_record(doc.record, made)
            out[doc.slug] = {"written": True, "changed": made.get("changed_during_build", []),
                             "info": build_info(made, doc.data,
                                                timings=(spec.get("timings") or {}).get(doc.slug))}
        return out
    raise SystemExit(USAGE)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in ("check", "snapshot", "record"):
        print(USAGE, file=sys.stderr)
        return 2
    spec = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    print(json.dumps(_dispatch(args[0], spec)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
