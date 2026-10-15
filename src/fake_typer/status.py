"""Shared state between a running typer, the control commands (toggle/pause/stop)
and the status bar: a pid file plus a small JSON status file in $XDG_RUNTIME_DIR."""

import json
import os
import signal
import threading
import time
from pathlib import Path

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/fake-typer-{os.getuid()}") / "fake-typer"
STATUS_FILE = RUNTIME / "status.json"
PID_FILE = RUNTIME / "pid"


def running_pid() -> int | None:
    """Pid of the typer that is currently running, if any."""
    try:
        pid = int(PID_FILE.read_text())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if b"fake-typer" in cmdline or b"fake_typer" in cmdline else None


def signal_running(sig: int) -> bool:
    pid = running_pid()
    if pid is None:
        return False
    os.kill(pid, sig)
    return True


def toggle_pause() -> bool:
    return signal_running(signal.SIGUSR1)


def stop() -> bool:
    return signal_running(signal.SIGTERM)


class Status:
    """Writes the status file the bar widget reads. Each write is atomic, so a
    reader never sees half a file."""

    HEARTBEAT = 2.0  # seconds; the bar treats a status older than a few beats as dead

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.fields: dict = {}
        self.lock = threading.RLock()  # a signal can interrupt an update on the same thread
        self.closed = threading.Event()
        if enabled:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(os.getpid()))
            threading.Thread(target=self._beat, daemon=True).start()

    def _beat(self) -> None:
        while not self.closed.wait(self.HEARTBEAT):
            self.update()

    def update(self, **fields) -> None:
        if not self.enabled:
            return
        with self.lock:
            self.fields.update(fields, updated=time.time())
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.fields))
            os.replace(tmp, STATUS_FILE)

    def close(self) -> None:
        self.closed.set()
        if self.enabled and running_pid() == os.getpid():
            PID_FILE.unlink(missing_ok=True)
