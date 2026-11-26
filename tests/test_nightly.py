"""scripts/nightly on a scratch repository: one complete report per commit, whatever the triggers."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A main checkout with this branch's nightly and long-run, cloned from a bare origin."""
    origin, main = tmp_path / "origin.git", tmp_path / "main"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    sh("git", "clone", "-q", str(origin), str(main), cwd=tmp_path)
    (main / "scripts").mkdir()
    for name in ("nightly", "long-run"):
        shutil.copy(SCRIPTS / name, main / "scripts" / name)
    setup = main / "scripts" / "worktree-setup"
    setup.write_text("#!/bin/sh\necho set up\n")
    setup.chmod(0o755)
    sh("git", "add", ".", cwd=main)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=main)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=main)
    return main


def env_for(tmp_path, jobs):
    return {**os.environ, "REFLOW_NIGHTLY_DIR": str(tmp_path / "nightly"),
            "REFLOW_NIGHTLY_JOBS": json.dumps(jobs)}


def nightly(repo, env, *args, **kw):
    return subprocess.Popen([sys.executable, repo / "scripts" / "nightly", *args], cwd=repo,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, **kw)


def fields(path):
    return dict(line.split(": ", 1) for line in path.read_text().splitlines())


def head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def job(code):
    return [".", [sys.executable, "-c", code]]


def test_a_run_writes_a_complete_report_and_a_second_trigger_runs_nothing(repo, tmp_path):
    env = env_for(tmp_path, {"ok": job("print('fine')"), "bad": job("raise SystemExit(3)")})
    first = nightly(repo, env)
    assert first.wait(60) == 0, first.stdout.read()   # complete, though a job failed
    report = tmp_path / "nightly" / head(repo)[:12]
    status = fields(report / "status")
    assert status["state"] == "complete" and status["result"] == "failed"
    assert status["commit"] == head(repo) and status["jobs"] == "ok bad"
    assert status["job ok"].startswith("passed exit 0")
    assert status["job bad"].startswith("failed exit 3")
    summary = (report / "summary").read_text()
    assert "== ok: passed" in summary and "== bad: failed" in summary
    assert (tmp_path / "nightly" / "latest").resolve() == report
    before = (report / "ok" / "status").read_text()

    second = nightly(repo, env)
    assert second.wait(60) == 0
    assert "already has" in second.stdout.read()
    assert (report / "ok" / "status").read_text() == before


def test_two_triggers_at_once_make_one_run(repo, tmp_path):
    env = env_for(tmp_path, {"slow": job("import time; time.sleep(3)")})
    first = nightly(repo, env)
    report = tmp_path / "nightly" / head(repo)[:12]
    deadline = time.monotonic() + 30
    while not (report / "slow" / "status").exists():
        assert time.monotonic() < deadline
        time.sleep(0.1)
    second = nightly(repo, env)
    assert second.wait(30) == 0
    assert "another run is in progress" in second.stdout.read()
    assert first.wait(60) == 0
    assert fields(report / "status")["state"] == "complete"
    assert (report / ".attempts" / "slow").read_text() == "1\n"


def test_a_run_killed_halfway_converges_on_the_next_trigger(repo, tmp_path):
    marker = tmp_path / "second-time"
    code = (f"import pathlib, time; p = pathlib.Path({str(marker)!r}); "
            "p.exists() or (p.touch(), time.sleep(60))")
    env = env_for(tmp_path, {"first": job("print(1)"), "flaky": job(code), "last": job("print(3)")})
    run = nightly(repo, env)
    report = tmp_path / "nightly" / head(repo)[:12]
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.1)
    os.killpg(run.pid, signal.SIGKILL)   # the runner, long-run and the job, with no cleanup
    run.wait(10)
    assert fields(report / "status")["state"] == "running"
    assert fields(report / "flaky" / "status")["state"] == "running"
    first_status = (report / "first" / "status").read_text()

    again = nightly(repo, env)
    assert again.wait(60) == 0, again.stdout.read()
    status = fields(report / "status")
    assert status["state"] == "complete" and status["result"] == "passed"
    assert (report / "first" / "status").read_text() == first_status   # kept, not run again
    assert (report / ".attempts" / "flaky").read_text() == "2\n"
    assert fields(report / "last" / "status")["state"] == "passed"


def test_a_job_killed_every_time_stops_after_its_attempts(repo, tmp_path):
    env = env_for(tmp_path, {"dies": job("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")})
    report = tmp_path / "nightly" / head(repo)[:12]
    nightly(repo, env).wait(60)
    assert fields(report / "status")["state"] == "running"
    assert not (tmp_path / "nightly" / "latest").exists()   # only a complete report is latest
    nightly(repo, env).wait(60)
    status = fields(report / "status")
    assert status["state"] == "complete" and status["job dies"].startswith("killed")
    third = nightly(repo, env)
    assert third.wait(60) == 0 and "already has" in third.stdout.read()


def test_a_job_that_outlives_its_killed_runner_keeps_the_lock(repo, tmp_path):
    marker = tmp_path / "started"
    code = (f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(3)")
    env = env_for(tmp_path, {"slow": job(code)})
    run = nightly(repo, env)
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.1)
    os.kill(run.pid, signal.SIGKILL)   # only the runner; long-run and the job go on
    run.wait(10)
    second = nightly(repo, env)
    assert second.wait(30) == 0
    assert "another run is in progress" in second.stdout.read()
    report = tmp_path / "nightly" / head(repo)[:12]
    deadline = time.monotonic() + 30
    while fields(report / "slow" / "status")["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    # long-run writes the job's result a moment before it exits and frees the lock, so a trigger
    # in that moment still finds the run in progress.
    deadline = time.monotonic() + 30
    while True:
        third = nightly(repo, env)
        assert third.wait(30) == 0, third.stdout.read()
        if fields(report / "status")["state"] == "complete":
            break
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert (report / ".attempts" / "slow").read_text() == "1\n"   # its result was kept


def test_the_jobs_run_the_commands_scripts_affected_names(monkeypatch):
    """nightly is one detached file and cannot import CHECKS; its table must not drift from it."""
    import importlib.machinery
    import importlib.util

    def load(name):
        loader = importlib.machinery.SourceFileLoader(name, str(SCRIPTS / name))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        monkeypatch.setitem(sys.modules, name, module)   # dataclasses looks its module up here
        loader.exec_module(module)
        return module

    jobs, checks = load("nightly").JOBS, load("affected").CHECKS
    for job, check in (("unit", "unit"), ("pages", "pages"), ("eval-label", "eval-label"),
                       ("eval-structure", "eval-structure"), ("eval-scan", "eval-scan")):
        wanted = checks[check].command.replace(" --quick", "").split()   # the full suite, not quick
        assert jobs[job][1] == wanted, job
    assert jobs["robustness"][1] == [*checks["robustness"].command.split(), "--robustness-out",
                                     "{dir}"]
