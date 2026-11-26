"""Identity races in the exact helper shipped to the web coordinator, without real signals."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location(
    "build_process", Path(__file__).parents[1] / "web/scripts/build-process.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_scan_rejects_root_replaced_during_table_read():
    table = {77: {"pid": 77, "ppid": 1, "start": 200},
             78: {"pid": 78, "ppid": 77, "start": 201}}
    with patch.object(helper.os, "listdir", return_value=["77", "78"]), patch.object(
        helper, "identity", side_effect=table.get
    ):
        assert helper.scan_tree({"pid": 77, "start": 100}) == []


def test_pin_rejects_pid_reused_before_identity_validation():
    with patch.object(helper.os, "pidfd_open", return_value=40), patch.object(
        helper, "identity", return_value={"pid": 77, "start": 200}
    ), patch.object(helper.os, "close") as close:
        assert helper.pin({"pid": 77, "start": 100}) is None
        close.assert_called_once_with(40)


def test_signals_use_the_pinned_handle_even_if_numeric_pid_is_reused():
    with patch.object(helper.os, "pidfd_open", return_value=40), patch.object(
        helper, "identity", return_value={"pid": 77, "start": 100}
    ), patch.object(helper.signal, "pidfd_send_signal") as send, patch.object(
        helper.os, "kill"
    ) as numeric_kill:
        fd = helper.pin({"pid": 77, "start": 100})
        helper.send(fd, helper.signal.SIGTERM)
        send.assert_called_once_with(40, helper.signal.SIGTERM)
        numeric_kill.assert_not_called()


def test_tree_with_replaced_root_never_signals_replacement_children():
    with patch.object(helper, "pin", return_value=40), patch.object(
        helper, "scan_tree", return_value=[]
    ), patch.object(helper.os, "close"), patch.object(helper, "send") as send, patch.object(
        helper.select, "poll"
    ) as poll:
        poll.return_value.poll.return_value = [(40, 1)]
        helper.stop_tree({"pid": 77, "start": 100}, 0.1)
        assert {call.args[0] for call in send.call_args_list} == {40}
        assert helper.signal.SIGKILL not in [call.args[1] for call in send.call_args_list]


def test_scan_rechecks_root_after_reading_the_entire_table():
    old = {"pid": 77, "ppid": 1, "start": 100}
    replacement_child = {"pid": 78, "ppid": 77, "start": 201}
    replacement = {"pid": 77, "ppid": 1, "start": 200}
    with patch.object(helper.os, "listdir", return_value=["77", "78"]), patch.object(
        helper, "identity", side_effect=[old, replacement_child, replacement]
    ):
        assert helper.scan_tree({"pid": 77, "start": 100}) == []


HELPER = str(Path(__file__).parents[1] / "web/scripts/build-process.py")


def test_worker_runs_its_command_only_after_the_go_byte(tmp_path):
    import json
    import subprocess
    import sys

    marker = tmp_path / "ran"
    command = json.dumps([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"])
    # EOF (the script died before saving the worker's identity) and any other byte: nothing runs.
    for stdin in (b"", b"x"):
        done = subprocess.run([sys.executable, HELPER, "worker", command], input=stdin, timeout=30)
        assert done.returncode == 0
        assert not marker.exists()
    subprocess.run([sys.executable, HELPER, "worker", command], input=b"g", timeout=30, check=True)
    assert marker.exists()


def test_stop_resumes_every_pinned_process_when_it_fails_midway():
    sent = []
    tree = [{"pid": 77, "ppid": 1, "start": 100}, {"pid": 78, "ppid": 77, "start": 101}]
    with patch.object(helper, "pin", side_effect=[40, 41]), patch.object(
        helper, "scan_tree", side_effect=[tree, RuntimeError("scan failed")]
    ), patch.object(helper, "send", side_effect=lambda fd, sig: sent.append((fd, sig))), patch.object(
        helper.os, "close"
    ):
        with pytest.raises(RuntimeError, match="scan failed"):
            helper.stop_tree({"pid": 77, "start": 100}, 1)
    assert (40, helper.signal.SIGSTOP) in sent and (41, helper.signal.SIGSTOP) in sent
    assert sent[-2:] == [(40, helper.signal.SIGCONT), (41, helper.signal.SIGCONT)]
    assert {fd for fd, _ in sent} == {40, 41}


def _state(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


# The real helper, held in its frozen window: the stop has frozen the root and ignores the
# interruptions, and its scan of the descendants waits for a line on stdin. Without the hold that
# window lasts a few milliseconds, which a test under load can miss.
HELD_STOP = """import importlib.util, sys
spec = importlib.util.spec_from_file_location("build_process", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
scan = helper.scan_tree
def held(root):
    helper.scan_tree = scan
    print("frozen", flush=True)
    sys.stdin.readline()
    return scan(root)
helper.scan_tree = held
sys.argv = sys.argv[1:]
helper.main()
"""


def test_an_interrupted_stop_never_leaves_the_tree_frozen():
    """Ctrl-C (and TERM, HUP) reaching the stop helper while it has the tree frozen: the real
    helper and a real process tree in a session of its own; only those processes are signalled."""
    import json
    import os
    import signal
    import subprocess
    import sys
    import time

    for interruption in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        tree = subprocess.Popen(["sh", "-c", "sleep 100 & sleep 100 & wait"], start_new_session=True)
        try:
            for _ in range(200):
                kids = [int(n) for n in os.listdir("/proc") if n.isdecimal() and helper.identity(int(n))
                        and helper.identity(int(n))["ppid"] == tree.pid]
                if len(kids) == 2:
                    break
                time.sleep(0.01)
            ref = {"pid": tree.pid, "start": helper.identity(tree.pid)["start"]}
            stopper = subprocess.Popen([sys.executable, "-c", HELD_STOP, HELPER, "stop",
                                        json.dumps(ref), "5"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert stopper.stdout.readline() == "frozen\n"
            deadline = time.monotonic() + 10
            while _state(tree.pid) != "T" and time.monotonic() < deadline:
                time.sleep(0.001)
            assert _state(tree.pid) == "T", "the helper never froze the tree, so nothing was tested"
            stopper.send_signal(interruption)
            stopper.send_signal(interruption)
            stopper.communicate("\n", timeout=30)
            assert stopper.returncode == 0
            time.sleep(0.2)
            left = [p for p in [tree.pid, *kids] if _state(p) not in (None, "Z")]
            assert left == [], {p: _state(p) for p in left}
        finally:
            try:
                os.killpg(tree.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            tree.wait()
