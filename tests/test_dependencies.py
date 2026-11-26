"""The base install (no extras) declares every third-party package the converter imports."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "reflow"

# Import name -> distribution name, where they differ.
DISTRIBUTION = {"PIL": "pillow", "pypdfium2": "pypdfium2"}

# Packages pdfplumber requires, imported directly where the converter reads pdfminer's objects.
VIA_PDFPLUMBER = {"pdfminer"}

# pypdfium2's `to_numpy()` (extract/text.py) imports numpy; no module in src/reflow does.
NEEDED_WITHOUT_AN_IMPORT = {"numpy"}


def _declared() -> set[str]:
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    return {re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower().replace("_", "-") for d in dependencies}


def _top_level_imports(path: Path) -> set[str]:
    """Names imported at module level, outside `if`/`try` and function bodies."""
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_base_dependencies_cover_the_converter_imports():
    declared = _declared()
    needed = set(NEEDED_WITHOUT_AN_IMPORT)
    for path in SRC.rglob("*.py"):
        for name in _top_level_imports(path):
            if name in sys.stdlib_module_names or name == "reflow" or name in VIA_PDFPLUMBER:
                continue
            needed.add(DISTRIBUTION.get(name, name).lower().replace("_", "-"))
    assert needed - declared == set()
