"""Where the main checkout is, for the tools that run in any git worktree of the repository.

The main checkout holds what worktrees share: the venv, the gitignored samples, the page-check
caches. The standard library only, so a script runs it before anything is installed. The web
scripts have the same helper in web/scripts/lib/checkout.ts."""

from __future__ import annotations

import subprocess
from pathlib import Path


def main_checkout(repo: Path) -> Path:
    """The main checkout of the repository that `repo` is a checkout of: `repo` itself outside git."""
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return repo
    return Path(common).parent


def sample(repo: Path, relative: str) -> Path:
    """A file of `repo`, else the main checkout's: some samples are gitignored and live only there."""
    here = repo / relative
    return here if here.exists() else main_checkout(repo) / relative
