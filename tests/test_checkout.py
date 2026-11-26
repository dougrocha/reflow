"""checkout: the main checkout, from the main checkout, from a worktree and outside git."""

from __future__ import annotations

import subprocess

import checkout


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


def test_a_worktree_finds_the_main_checkout(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "commit", "-q", "--allow-empty", "-m", "x")
    worktree = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    assert checkout.main_checkout(worktree).resolve() == main.resolve()
    assert checkout.main_checkout(main).resolve() == main.resolve()


def test_outside_git_the_checkout_is_the_folder_itself(tmp_path):
    assert checkout.main_checkout(tmp_path) == tmp_path


def test_a_sample_is_read_from_the_main_checkout_when_the_worktree_lacks_it(tmp_path):
    main = tmp_path / "main"
    (main / "samples").mkdir(parents=True)
    (main / "samples/a.pdf").write_bytes(b"")
    git(main, "init", "-q")
    git(main, "commit", "-q", "--allow-empty", "-m", "x")
    worktree = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    assert checkout.sample(worktree, "samples/a.pdf").resolve() == (main / "samples/a.pdf").resolve()
    (worktree / "samples").mkdir()
    (worktree / "samples/a.pdf").write_bytes(b"")
    assert checkout.sample(worktree, "samples/a.pdf") == worktree / "samples/a.pdf"
