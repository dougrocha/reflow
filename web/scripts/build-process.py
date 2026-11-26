"""Linux file-lock and pidfd operations used by the Node/Bun build coordinator."""
import fcntl
import json
import os
import select
import signal
import sys
import time


def identity(pid):
    try:
        fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return None
        return {"pid": pid, "ppid": int(fields[1]), "start": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def pin(ref):
    """Validate after opening: all subsequent signals address this handle, never a PID."""
    try:
        fd = os.pidfd_open(ref["pid"])
    except ProcessLookupError:
        return None
    current = identity(ref["pid"])
    if current is None or current["start"] != ref["start"]:
        os.close(fd)
        return None
    return fd


def scan_tree(root):
    table = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            ref = identity(int(name))
            if ref:
                table[ref["pid"]] = ref
    if table.get(root["pid"], {}).get("start") != root["start"]:
        return []
    current = identity(root["pid"])
    if current is None or current["start"] != root["start"]:
        return []
    found = [root]
    seen = {root["pid"]}
    for parent in found:
        current = identity(parent["pid"])
        if current is None or current["start"] != parent["start"]:
            continue
        for ref in table.values():
            if ref["ppid"] == parent["pid"] and ref["pid"] not in seen:
                seen.add(ref["pid"])
                found.append(ref)
    return found


def send(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass


def stop_tree(root, grace):
    root_fd = pin(root)
    if root_fd is None:
        return
    handles = {root["pid"]: root_fd}
    try:
        # Freeze parents before scanning descendants, so they cannot fork during discovery.
        send(root_fd, signal.SIGSTOP)
        while True:
            added = False
            for ref in scan_tree(root):
                if ref["pid"] in handles:
                    continue
                fd = pin(ref)
                if fd is not None:
                    handles[ref["pid"]] = fd
                    send(fd, signal.SIGSTOP)
                    added = True
            if not added:
                break
        # The worker contract is that TERM handlers stop work, rather than spawn new work.
        for fd in reversed(list(handles.values())):
            send(fd, signal.SIGTERM)
            send(fd, signal.SIGCONT)
        poll = select.poll()
        for fd in handles.values():
            poll.register(fd, select.POLLIN)
        pending = set(handles.values())
        deadline = time.monotonic() + grace
        while pending and time.monotonic() < deadline:
            for fd, _ in poll.poll(max(1, int((deadline - time.monotonic()) * 1000))):
                pending.discard(fd)
                poll.unregister(fd)
        for fd in pending:
            send(fd, signal.SIGKILL)
        # Wait for the identities to exit, not merely for delivery of the signal.
        deadline = time.monotonic() + 2
        while pending and time.monotonic() < deadline:
            for fd, _ in poll.poll(100):
                pending.discard(fd)
                poll.unregister(fd)
        if pending:
            raise RuntimeError("build processes did not exit after SIGKILL")
    finally:
        # Whatever stopped this early (an error, a pidfd that could not be polled), nothing it
        # froze stays frozen: every pinned process gets SIGCONT, through its own handle only.
        for fd in handles.values():
            try:
                send(fd, signal.SIGCONT)
            except OSError:
                pass
            os.close(fd)


def main():
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("document builds require Linux pidfds and Python 3.9+")
    if sys.argv[1] == "lock":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        with open(sys.argv[2], "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            print("locked", flush=True)
            sys.stdin.buffer.read()  # parent death closes the pipe and releases the kernel lock
    elif sys.argv[1] == "worker":
        command = json.loads(sys.argv[2])
        if sys.stdin.buffer.read(1) == b"g":
            os.execvp(command[0], command)
    elif sys.argv[1] == "stop":
        # Between SIGSTOP and SIGCONT an interruption would leave the tree frozen (and holding
        # coordination.lock if it was in a transaction). Ctrl-C reaches every process of the
        # terminal's group, a second one included: finish the stop instead. Its length is bounded
        # by the grace period. The caller also starts this helper in a session of its own.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        stop_tree(json.loads(sys.argv[2]), float(sys.argv[3]))
    else:
        raise ValueError("unknown build process operation")


if __name__ == "__main__":
    main()
