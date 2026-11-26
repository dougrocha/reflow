"""An opt-in cache of the slowest early stages of a conversion (page checks, `--stage-cache`).

Editing analysis or rendering code should not repeat the PDF reading. Five stages are cached on
disk (`STAGES`), each keyed by what it reads and a hash of only the code it runs:

  * Witness (`reflow.prepare.read_witness`, build `reflow.ingest.witness.read_page`): OCR of one rendered page that the text-trust
    preflight compares with its text layer. Keyed by the working copy before replacement OCR,
    the page, the render scale, the languages and the tessdata digest.

  * Ingest (`cached_ingest`, build `reflow.ingest.pages.read_source`): the working copy qpdf
    writes and the Document of facts and page info read from it. Keyed by the source PDF's bytes
    and name.
  * OCR (`CachedRunner` wraps the OCR runner): the text-layered copy it wrote and its word grades.
    Keyed by the bytes of the working copy it reads, the plan, the languages and the engine's
    data (traineddata, configs, TESSDATA_PREFIX: `Runner.environment`).
  * Extraction (`cached_extract`, build `extract_document`): every page's lines, drawings and
    ruled tables. Keyed by the working copy once OCR is done and the word grades beside it.
  * Page reads for the tag tree and links (`cached`, see reflow.extract.pagecontent). Keyed by
    the working copy once OCR is done.

So each stage is keyed by the artifact the stage before it made, by its bytes. The working copy
is the same bytes each time qpdf makes it (reflow.ingest.load.normalize), so a stage after ingest
still hits when the ingest entry is made again. OCR output differs run to run, so a reading is
never paired with a copy OCR'd by another run: an edit to the OCR rules, an OCR entry evicted on
its own, or a plan that OCRs a page another way makes extraction and the page reads miss.

The code hash is never a hand-kept list. `code_hash(entries)` hashes `closure(entries)`: every
reflow module importing the entries runs, found by walking imports (static ones, ones inside
functions, relative ones and literal import_module calls; not the body of an `if TYPE_CHECKING`)
and every parent package, whose `__init__` runs first and whose imports are followed too. The
package `__init__` files the stages reach import little for that reason (reflow.ingest's imports
nothing). An import the walk can't resolve, or a computed one, raises ClosureError: a key that
might miss code is never made. The non-Python files beside the closure are in the hash too
(`resources`), and so are the installed packages and OCR tools (`identity.tool_versions`). The build a
stage is given must be code in its closure (`_check_build`): a lambda written at the call site
would put code outside the key. tests/test_stagecache_keys.py pins each stage's closure to an
allow-list.

Only `Options.stage_cache` turns this on: the page-check harness sets it, and the CLI's
`--stage-cache` (`docs:build --stage-cache`). The tag tree and link reads are not cached whole;
what is cached is the pdfplumber parse of the pages, which lives in a module that imports no
analysis code (tests/test_layers.py holds every extract module to that).
"""

from __future__ import annotations

import ast
import hashlib
import os
import pickle
import shutil
import inspect
from contextlib import nullcontext
from functools import lru_cache, partial
from pathlib import Path

from reflow import cachedir, identity
from reflow.names import WORKING_PDF

PACKAGE_ROOT = Path(__file__).resolve().parent.parent   # the directory holding reflow/
# Each cached stage and the modules it runs. A stage's key holds the closure of its entries
# (`closure`) and the artifacts it reads, never the code that calls it: the build a stage is
# given must be code inside that closure (`_check_build`).
STAGES: dict[str, tuple[str, ...]] = {
    "ingest": ("reflow.ingest.pages",),        # read_source: the working copy and its Document
    "ocr": ("reflow.ingest.ocr",),             # the runner; it reads the working copy
    "extract": ("reflow.extract.parallel",),   # extract_document: every page's lines and tables
    "pages": ("reflow.extract.pagecontent",),  # the pdfplumber parse (reflow.extract.pagecontent)
    # the text witness (reflow.ingest.witness): one entry per page it reads
    "witness": ("reflow.ingest.witness",),
    # The finished model (reflow.saved): every pass, and the source pass that feeds them.
    "model": ("reflow.pipeline", "reflow.prepare"),
}
INGEST_ENTRIES, OCR_ENTRIES, EXTRACT_ENTRIES = STAGES["ingest"], STAGES["ocr"], STAGES["extract"]
_DYNAMIC_IMPORTS = {"import_module", "__import__"}
# importlib.resources.files(<package>): the files of a package the code reads (the viewer's
# stylesheets and script). The package is followed as an import, so its resources are in the key.
_RESOURCE_READS = {"files"}


class ClosureError(Exception):
    """A stage's code reaches an import the scanner cannot resolve to a file, or the stage was
    given a build outside its code, so its key could miss code it runs. Never caught: an
    unsound key would serve stale entries."""


def _module_file(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _type_checking(test: ast.expr) -> bool:
    """`TYPE_CHECKING` or `typing.TYPE_CHECKING`, the guard whose body never runs."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
        (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _walk(node: ast.AST) -> list[ast.AST]:
    """Every node under `node`, without the body of an `if TYPE_CHECKING:` (its else runs). A
    stack, not nested generators: those pass each node up through every level above it."""
    out: list[ast.AST] = []
    todo = [node]
    while todo:
        for child in ast.iter_child_nodes(todo.pop()):
            if isinstance(child, ast.If) and _type_checking(child.test):
                out.extend(child.orelse)
                todo.extend(child.orelse)
                continue
            out.append(child)
            todo.append(child)
    return out


def _absolute(module: str, is_package: bool, level: int, name: str | None) -> str:
    """The absolute name of `from <level dots><name> import ...` written in `module`."""
    package = module.split(".") if is_package else module.split(".")[:-1]
    if level > len(package):
        raise ClosureError(f"{module}: a relative import climbs out of the package")
    base = package[:len(package) - level + 1]
    return ".".join(base + ([name] if name else []))


@lru_cache(maxsize=4096)
def _parsed_imports(source: bytes, module: str, is_package: bool
                    ) -> tuple[frozenset[str], frozenset[str]]:
    """(required, optional) reflow modules `module` imports. A required one must resolve; an
    optional one (`from M import n`, where n may be a name in M or a submodule) is followed when
    it does. An import name the scanner can't read (a computed import_module or __import__) or a
    relative dynamic import raises ClosureError."""
    nodes = _walk(ast.parse(source))
    required: set[str] = set()
    optional: set[str] = set()
    dynamic = _DYNAMIC_IMPORTS | {a.asname for node in nodes
                                  if isinstance(node, ast.ImportFrom) and node.module == "importlib"
                                  for a in node.names
                                  if a.name in _DYNAMIC_IMPORTS and a.asname}
    reads = {a.asname or a.name for node in nodes
             if isinstance(node, ast.ImportFrom) and node.module == "importlib.resources"
             for a in node.names if a.name in _RESOURCE_READS}
    for node in nodes:
        if isinstance(node, ast.Import):
            required.update(a.name for a in node.names if a.name.split(".")[0] == "reflow")
        elif isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if node.level:
                source = _absolute(module, is_package, node.level, node.module)
            if source.split(".")[0] != "reflow":
                continue
            required.add(source)
            optional.update(f"{source}.{a.name}" for a in node.names if a.name != "*")
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else \
                func.attr if isinstance(func, ast.Attribute) else None
            if _resource_read(func, reads):
                arg = node.args[0] if node.args else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    raise ClosureError(f"{module}:{node.lineno}: a resource read the scanner "
                                       "cannot follow; name the package")
                if arg.value.split(".")[0] == "reflow":
                    required.add(arg.value)
                continue
            if called not in dynamic:
                continue
            arg = node.args[0] if node.args else None
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)) \
                    or arg.value.startswith("."):
                raise ClosureError(f"{module}:{node.lineno}: a dynamic import the scanner "
                                   "cannot read; import the module by name")
            if arg.value.split(".")[0] == "reflow":
                required.add(arg.value)
    return frozenset(required), frozenset(optional)


def _resource_read(func: ast.expr, reads: set[str]) -> bool:
    """`resources.files(...)`, `importlib.resources.files(...)`, or `files(...)` imported from
    importlib.resources."""
    if isinstance(func, ast.Name):
        return func.id in reads
    if isinstance(func, ast.Attribute) and func.attr in _RESOURCE_READS:
        owner = func.value
        return (isinstance(owner, ast.Name) and owner.id == "resources") or \
            (isinstance(owner, ast.Attribute) and owner.attr == "resources")
    return False


def module_imports(path: Path, module: str) -> tuple[frozenset[str], frozenset[str]]:
    """`_parsed_imports` of the file's bytes, parsed once per content."""
    return _parsed_imports(path.read_bytes(), module, path.name == "__init__.py")


def closure(entries: tuple[str, ...], root: Path = PACKAGE_ROOT) -> list[Path]:
    """The reflow files importing `entries` runs, sorted: their imports, transitively (static
    ones, ones inside functions, relative ones and literal import_module calls; not the body of
    an `if TYPE_CHECKING:`), and every parent package of each, whose own imports run first and
    are followed the same way. A reflow module that resolves to no file raises ClosureError."""
    seen: dict[Path, None] = {}
    todo = [(module, True) for module in entries]
    while todo:
        module, required = todo.pop()
        path = _module_file(root, module)
        if path is None:
            if required and not root.joinpath(*module.split(".")).is_dir():   # a namespace dir runs nothing
                raise ClosureError(f"{module} is imported but is no module under {root}")
            continue
        if path in seen:
            continue
        seen[path] = None
        parts = module.split(".")
        todo.extend((".".join(parts[:i]), True) for i in range(1, len(parts)))
        must, may = module_imports(path, module)
        todo.extend((m, True) for m in must)
        todo.extend((m, False) for m in may)
    return sorted(seen)


def resources(files: list[Path]) -> list[Path]:
    """The non-Python files beside `files`, sorted: those in each file's directory and in its
    subdirectories that are not packages (a package is reached by import, or not at all). Any of
    them may be read by the code beside it, so all of them are in the key."""
    found: set[Path] = set()
    todo = sorted({path.parent for path in files})
    while todo:
        directory = todo.pop()
        for item in directory.iterdir():
            if item.is_dir():
                if item.name != "__pycache__" and not (item / "__init__.py").exists():
                    todo.append(item)
            elif item.suffix != ".py":
                found.add(item)
    return sorted(found)


def code_hash(entries: tuple[str, ...], root: Path = PACKAGE_ROOT) -> str:
    """The sha256 of the stage's closure and the resources beside it, by name and bytes."""
    h = hashlib.sha256()
    files = closure(entries, root)
    for path in files + resources(files):
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _code_file(build) -> Path | None:
    """The file defining the function `build` runs, through partials, bound methods and
    functools.wraps; None when it has no Python code."""
    seen = 0
    while seen < 32:
        seen += 1
        if isinstance(build, partial):
            build = build.func
        elif inspect.ismethod(build):
            build = build.__func__
        elif getattr(build, "__wrapped__", None) is not None:
            build = build.__wrapped__
        else:
            break
    code = getattr(build, "__code__", None)
    return Path(code.co_filename).resolve() if code is not None else None


def _check_build(entries: tuple[str, ...], build) -> None:
    """Refuse a build whose code is in the reflow package but not in the stage's closure: an
    edit to it would not change the key. A build from outside the package (a test's double) is
    not reflow code and is allowed; one with no Python code is refused."""
    path = _code_file(build)
    if path is None:
        raise ClosureError(f"{build!r} has no Python code to put in the key")
    if path.is_relative_to(PACKAGE_ROOT / "reflow") and path not in closure(entries):
        raise ClosureError(f"{path.relative_to(PACKAGE_ROOT)} builds a stage of {entries} "
                           "but is not in its key; build with a function the stage's modules define")


def stage_key(entries: tuple[str, ...], *parts: object, root: Path = PACKAGE_ROOT) -> str:
    h = hashlib.sha256(code_hash(entries, root).encode())
    h.update(identity.tool_key().encode())
    h.update(repr(parts).encode())
    return h.hexdigest()[:20]


def _entry(root: Path, kind: str, pdf: Path, key: str) -> Path:
    return root / kind / identity.file_sha256(pdf)[:16] / key


_MISS = object()


def _try_read(entry: Path, read):
    """`read(entry)`, touched as most recently used (eviction keeps it longest), or _MISS if
    the entry is absent, incomplete or corrupt."""
    if not entry.exists():
        return _MISS
    try:
        value = read(entry)
    except Exception:  # noqa: BLE001 - any failure to read the entry is a miss
        return _MISS
    try:
        os.utime(entry)
    except OSError:
        pass
    return value


def _read(entry: Path, read):
    """`_try_read` holding the entry shared, so eviction cannot remove it mid-read. An
    unreadable entry is removed once no reader holds it."""
    if not entry.exists():
        return _MISS
    with cachedir.try_lock(entry, shared=True) or nullcontext():
        if (value := _try_read(entry, read)) is not _MISS:
            return value
    if (held := cachedir.try_lock(entry, wait=False)) is not None:
        with held:
            if entry.exists() and _try_read(entry, read) is _MISS:
                cachedir.remove(entry)
    return _MISS


def _cached(entry: Path, read, build, write, entry_now):
    """(value, True) read from `entry`, or (build(), False) with the value written by
    `write(tmp, value)` and renamed into place. The build runs holding the entry's lock and only
    if the entry is still missing once it holds it, so two processes or threads never build one
    key at once: the second waits, then reads what the first wrote.

    `entry_now()` is the entry the key gives now. The key hashes the code on disk when it is
    computed, but the build may import more of that code later (an import inside a function, a
    spawned worker), so an edit made during the build can leave a result of the new code under
    the old code's key. When the entry differs after the build, the result is returned and not
    written. This narrows that window and does not close it: an edit reverted before the check,
    or code the process imported before the edit, still gets through. It runs only after a build,
    never on a hit."""
    if (value := _read(entry, read)) is not _MISS:
        return value, True
    with cachedir.try_lock(entry) or nullcontext():
        if (value := _try_read(entry, read)) is not _MISS:
            return value, True
        cachedir.remove(entry)   # absent, or corrupt with no reader left
        value = build()
        if entry_now() == entry:
            _write(entry, write, value)
        return value, False


def _write(entry: Path, write, value) -> None:
    """Write an entry beside its place under a name of this writer's own, then rename it in."""
    tmp = cachedir.temp_path(entry)
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        write(tmp, value)
        tmp.rename(entry)   # a directory is not replaced: another writer's entry is kept
    except Exception:  # noqa: BLE001 - the cache is best effort
        cachedir.remove(tmp)


def keep(entry: Path, write) -> None:
    """Write an entry of a stage that is not built through `cached` (reflow.saved keeps a model
    the conversion built): `write(tmp, None)` beside it, renamed in; another writer's entry wins."""
    _write(entry, write, None)


def touch(entry: Path) -> None:
    """Mark an entry read, as `_try_read` does: eviction keeps the most recently used longest."""
    try:
        os.utime(entry)
    except OSError:
        pass


def drop(entry: Path) -> None:
    """Remove an entry its reader found unusable, once no other reader holds it."""
    if (held := cachedir.try_lock(entry, wait=False)) is not None:
        with held:
            cachedir.remove(entry)


def _readable_pdf(path: Path) -> bool:
    import pikepdf
    try:
        with pikepdf.open(path):
            return True
    except Exception:  # noqa: BLE001 - any failure to read means the entry is unusable
        return False


def _ingest_entry(root: Path, pdf: Path) -> Path:
    return _entry(root, "ingest", pdf, stage_key(INGEST_ENTRIES, pdf.name))   # the name is in meta


def _read_ingest(working: Path):
    def read(entry: Path):
        with (entry / "doc.pkl").open("rb") as f:
            value = pickle.load(f)
        if not _readable_pdf(entry / WORKING_PDF):
            raise ValueError("unreadable working copy")
        shutil.copyfile(entry / WORKING_PDF, working)
        return value
    return read


def _write_ingest(working: Path):
    def write(tmp: Path, value) -> None:
        tmp.mkdir()
        shutil.copyfile(working, tmp / WORKING_PDF)
        with (tmp / "doc.pkl").open("wb") as f:
            pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    return write


def cached_ingest(root: Path, pdf: Path, working: Path, build):
    """(Document, hit): the saved one with its working copy written to `working`, or `build()`'s,
    which writes `working` itself, saved for the next run. `build` is read_source with its
    arguments bound."""
    _check_build(INGEST_ENTRIES, build)
    return _cached(_ingest_entry(root, pdf), _read_ingest(working), build, _write_ingest(working),
                   lambda: _ingest_entry(root, pdf))


class CachedRunner:
    """An OCR runner that reuses an earlier run's output for the same copy read (the working
    copy's bytes), plan (each page's reason and mode), languages, OCR code and tool versions, and
    the engine's data (`inner.environment(langs)`: its models and configuration). A runner that
    can't say what its data is runs uncached. An entry holds both things a run leaves, the
    text-layered copy and the word evidence files (ingest.ocr_trust), and is a miss unless it
    holds both: grades are never paired with another run's PDF."""

    def __init__(self, inner, root: Path, pdf: Path) -> None:
        self.inner, self.root, self.pdf = inner, root, pdf
        self.name = inner.name
        self.grades = inner.grades

    def run(self, src: Path, dst: Path, plan, langs: tuple[str, ...], evidence: Path) -> None:
        data = self.inner.environment(tuple(langs))
        if data is None:
            self.inner.run(src, dst, plan, langs, evidence)
            return
        _check_build(OCR_ENTRIES, self.inner.run)
        def entry_now() -> Path:
            return _entry(self.root, "ocr", self.pdf,
                          stage_key(OCR_ENTRIES, self.inner.name, plan.key(), tuple(langs), data,
                                    identity.file_sha256(src)))

        def read(entry: Path) -> None:
            if not (entry / "evidence").is_dir() or not _readable_pdf(entry / WORKING_PDF):
                raise ValueError("incomplete entry")
            try:
                shutil.copyfile(entry / WORKING_PDF, dst)
                for f in (entry / "evidence").iterdir():
                    shutil.copyfile(f, evidence / f.name)
            except OSError:
                for f in evidence.iterdir():
                    f.unlink(missing_ok=True)
                raise

        def write(tmp: Path, _) -> None:
            (tmp / "evidence").mkdir(parents=True)
            shutil.copyfile(dst, tmp / WORKING_PDF)
            for f in evidence.iterdir():
                shutil.copyfile(f, tmp / "evidence" / f.name)

        _cached(entry_now(), read, lambda: self.inner.run(src, dst, plan, langs, evidence), write,
                entry_now)


def _pickled_entry(root: Path, kind: str, entries: tuple[str, ...], pdf: Path,
                   parts: tuple) -> Path:
    return _entry(root, kind, pdf, stage_key(entries, *parts))


def _extract_parts(working: Path) -> tuple[str, str]:
    """What extraction reads: the working copy, and the OCR word grades kept beside it
    (extract_pages reads them through ingest.ocr_trust.read_sidecar). A conversion never writes
    `working` after reflow.prepare returns, and its staging folder's name is its own
    (output.staged), so the hash remembered for the path is never another conversion's."""
    from reflow.ingest.ocr_trust import sidecar_path
    sidecar = sidecar_path(working)
    return identity.file_sha256(working), identity.file_sha256(sidecar) if sidecar.is_file() else "no grades"


def _unpickle(entry: Path):
    with entry.open("rb") as f:
        return pickle.load(f)


def _pickle(tmp: Path, value) -> None:
    with tmp.open("wb") as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)


def cached(root: Path, kind: str, entries: tuple[str, ...], pdf: Path, build, *parts):
    """(value, hit): the value saved under `kind` for this source PDF, the code `entries` run and
    `parts` (anything else the result depends on), or `build()`, saved; see `_cached`. A corrupt
    or truncated entry is a miss and is removed. `build` must run code in the closure of
    `entries` (`_check_build`)."""
    _check_build(entries, build)
    return _cached(_pickled_entry(root, kind, entries, pdf, parts), _unpickle, build, _pickle,
                   lambda: _pickled_entry(root, kind, entries, pdf, parts))


def cached_extract(root: Path, pdf: Path, working: Path, build):
    """(pages and tables, hit) for this source PDF's working copy and the extraction code.
    `build` is extract_document with its arguments bound."""
    return cached(root, "extract", EXTRACT_ENTRIES, pdf, build, *_extract_parts(working))

