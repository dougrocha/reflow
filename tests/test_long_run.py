"""scripts/long-run on real child processes: a failing pytest run, a failing other command, kills."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/long-run"


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def long_run(repo, *args):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=repo, capture_output=True,
                          text=True, timeout=120)


def latest(repo, name):
    run = (repo / ".worktree/runs" / f"{name}-latest").resolve()
    status = dict(line.split(": ", 1) for line in (run / "status").read_text().splitlines())
    return run, status, (run / "summary").read_text()


def test_failing_pytest_summary_names_only_the_failing_tests(repo):
    (repo / "test_fake.py").write_text(
        "def test_ok():\n    pass\n\n"
        "def test_broken():\n    print('noise\\n' * 500)\n    assert 1 == 2\n\n"
        "def test_raises():\n    raise ValueError('bad input')\n")
    done = long_run(repo, "fake", "--", sys.executable, "-m", "pytest", "-q", "test_fake.py")
    assert done.returncode == 1
    run, status, summary = latest(repo, "fake")
    assert status["state"] == "failed" and status["exit"] == "1"
    assert status["commit"] and "wall" in status
    assert summary.splitlines() == [
        "2 failed of 3 tests",
        "FAILURE test_fake.py::test_broken: assert 1 == 2",
        "FAILURE test_fake.py::test_raises: ValueError: bad input",
    ]
    assert "noise" in (run / "log").read_text()
    assert summary in done.stdout


def test_failing_command_summary_keeps_error_lines_and_the_tail(repo):
    script = ("for i in $(seq 500); do echo progress $i; done; echo 'ERROR: disk full' >&2; "
              "for i in $(seq 100); do echo step $i; done; exit 3")
    done = long_run(repo, "build", "--", "bash", "-c", script)
    assert done.returncode == 3
    _, status, summary = latest(repo, "build")
    assert status["state"] == "failed" and status["exit"] == "3"
    lines = summary.splitlines()
    assert lines[0] == "501: ERROR: disk full"
    assert lines[-1] == "601: step 100"
    assert len(lines) <= 20 and not any("progress" in line for line in lines)


def test_reruns_get_their_own_folder(repo):
    long_run(repo, "twice", "--", "true")
    long_run(repo, "twice", "--", "false")
    runs = sorted(p for p in (repo / ".worktree/runs").iterdir() if not p.is_symlink())
    assert len(runs) == 2
    assert latest(repo, "twice")[1]["state"] == "failed"


def start(repo, name, cmd):
    proc = subprocess.Popen([sys.executable, SCRIPT, name, "--", *cmd], cwd=repo,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not (repo / ".worktree/runs" / f"{name}-latest" / "status").exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    time.sleep(0.3)
    return proc


# A worker in the background: bash exits on the signal, and the sleep must not outlive the run.
WORKER = ["bash", "-c", "sleep 60 & echo $! > worker.pid; wait"]


def gone(repo) -> bool:
    pid = int((repo / "worker.pid").read_text())
    for _ in range(50):
        if not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]:
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    return False


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_the_whole_job_and_status_says_killed(repo, sig):
    proc = start(repo, "slow", WORKER)
    proc.send_signal(sig)
    assert proc.wait(timeout=30) == 128 + sig
    assert gone(repo)
    _, status, summary = latest(repo, "slow")
    assert status["state"] == "killed" and status["note"] == f"long-run got {sig.name}"
    assert summary.startswith(f"killed (long-run got {sig.name})")


def test_sigkilled_runner_is_marked_killed_by_the_next_call(repo):
    proc = start(repo, "doomed", WORKER)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    status = repo / ".worktree/runs/doomed-latest/status"
    assert "state: running" in status.read_text()
    shown = long_run(repo, "status", "doomed")
    assert "killed" in shown.stdout
    _, status, summary = latest(repo, "doomed")
    assert status["state"] == "killed" and "runner died" in status["note"]
    assert summary.startswith("killed")
    assert gone(repo)
