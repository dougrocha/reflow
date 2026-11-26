"""The import layers of the reflow package: a stage imports only the stages before it.

Each unit is a subpackage (reflow.extract) or a top-level module (reflow.run). LAYERS lists them
in order, and a module may import its own unit and the units before it, never one after. The
imports are the ones the stage cache follows (reflow.stagecache): static, inside functions,
relative and literal import_module calls, but not the body of an `if TYPE_CHECKING:`, which never
runs. A new unit must be placed here on purpose."""

from pathlib import Path

from reflow import stagecache

LAYERS = (
    # The base: the document model and the helpers every stage shares. They import no stage.
    "reflow", "model", "geometry", "text_quality", "leaders", "patterns", "pdfobjects", "names", "cachedir",
    "pool", "budget", "providers", "device", "pageimages", "identity", "tessdata", "fontcensus",
    "progress_lines",
    # The stages that read the PDF, and the cache that keys them.
    "ingest", "stagecache", "extract", "checks",
    # One conversion's options and workers, which every pass reads, and the pass that builds it.
    "run", "steps", "prepare",
    # The passes that build the tree and the order they run in (the model stage's key holds no
    # later unit), then the renderer and the published site.
    "analyze", "pipeline", "viewer", "render", "output",
    # The saved model and its provenance, and the entry points.
    "provenance", "saved", "convert", "rerender", "cli",
)


def _modules(root: Path) -> dict[str, Path]:
    return {".".join(p.relative_to(root).with_suffix("").parts).removesuffix(".__init__"): p
            for p in (root / "reflow").rglob("*.py") if "__pycache__" not in p.parts}


def _unit(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "reflow"


def _resolve(name: str, modules: dict[str, Path]) -> str:
    """The module an import name lands in: itself, or the module that holds the name."""
    while name not in modules:
        name = name.rpartition(".")[0]
    return name


def backward_imports(root: Path = stagecache.PACKAGE_ROOT) -> list[str]:
    """Each import of a module in a unit later than the importer's, as "a imports b"."""
    modules = _modules(root)
    rank = {unit: i for i, unit in enumerate(LAYERS)}
    found = []
    for module, path in sorted(modules.items()):
        must, may = stagecache.module_imports(path, module)
        for target in sorted({_resolve(t, modules) for t in must | may}):
            if rank[_unit(target)] > rank[_unit(module)]:
                found.append(f"{module} imports {target}")
    return found


def test_every_unit_has_a_layer():
    assert {_unit(m) for m in _modules(stagecache.PACKAGE_ROOT)} == set(LAYERS)


def test_no_stage_imports_a_later_stage():
    assert backward_imports() == []


def test_an_import_inside_a_function_is_caught(tmp_path):
    """The kind that hid import cycles."""
    for name, text in {"__init__.py": "", "extract/__init__.py": "", "analyze/__init__.py": "",
                       "analyze/headings.py": "",
                       "extract/tables.py": "def f():\n    from reflow.analyze import headings\n"
                       }.items():
        path = tmp_path / "reflow" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert backward_imports(tmp_path) == ["reflow.extract.tables imports reflow.analyze",
                                          "reflow.extract.tables imports reflow.analyze.headings"]


# Cycles between modules of one unit. The layers rule out a cycle between units, not one inside a
# unit, like the ones through analyze.enrich that broke an earlier layering. None is known.
KNOWN_CYCLES: list[set[str]] = []


def import_cycles(root: Path = stagecache.PACKAGE_ROOT) -> list[set[str]]:
    """The groups of modules that import each other, directly or through others (Tarjan)."""
    modules = _modules(root)
    graph = {}
    for module, path in modules.items():
        must, may = stagecache.module_imports(path, module)
        graph[module] = {_resolve(t, modules) for t in must | may} - {module}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    cycles = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = set()
            while (w := stack.pop()) != v:
                group.add(w)
            if group:
                cycles.append(group | {v})

    for v in sorted(graph):
        if v not in index:
            visit(v)
    return cycles


def test_no_import_cycle_but_the_known_ones():
    assert sorted(map(sorted, import_cycles())) == sorted(map(sorted, KNOWN_CYCLES))
