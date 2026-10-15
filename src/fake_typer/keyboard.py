"""Output backends and the real-time player.

`VirtualKeyboard` creates a kernel-level keyboard through /dev/uinput, so the
keystrokes are indistinguishable from a USB keyboard and reach any focused
application (Wayland, X11, or a TTY). `TerminalKeyboard` renders the same event
stream to stdout for previewing."""

import select
import sys
import threading
import time
from collections.abc import Callable

from .layout import KEYS, SPECIAL_KEYS
from .timing import KeyEvent

DEVICE_NAME = "fake-typer virtual keyboard"
ABORT_KEY = "KEY_ESC"
PAUSE_KEY = "KEY_F9"


class VirtualKeyboard:
    def __init__(self):
        from evdev import UInput, ecodes

        self.ecodes = ecodes
        names = {k.name for k in [*KEYS.values(), *SPECIAL_KEYS.values()]} | {
            "KEY_BACKSPACE", "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}
        try:
            self.ui = UInput({ecodes.EV_KEY: [ecodes.ecodes[n] for n in names]},
                             name=DEVICE_NAME)
        except PermissionError:
            sys.exit("Cannot open /dev/uinput. Add yourself to the 'input' group "
                     "(or add a udev rule giving it write access) and log in again.")
        self.held: set[str] = set()

    def send(self, key: str, down: bool) -> None:
        self.ui.write(self.ecodes.EV_KEY, self.ecodes.ecodes[key], int(down))
        self.ui.syn()
        (self.held.add if down else self.held.discard)(key)

    def close(self) -> None:
        for key in list(self.held):
            self.send(key, False)
        self.ui.close()


class TerminalKeyboard:
    def __init__(self):
        self.shift: set[str] = set()
        self.chars = {(k.name, k.shift): c for c, k in KEYS.items()}
        self.held: set[str] = set()

    def send(self, key: str, down: bool) -> None:
        (self.held.add if down else self.held.discard)(key)
        if key.endswith("SHIFT"):
            (self.shift.add if down else self.shift.discard)(key)
            return
        if not down:
            return
        if key == "KEY_BACKSPACE":
            sys.stdout.write("\b \b")
        else:
            sys.stdout.write(self.chars.get((key, bool(self.shift)), ""))
        sys.stdout.flush()

    def close(self) -> None:
        sys.stdout.write("\n")


class HotkeyWatcher(threading.Thread):
    """Watches the physical keyboards for the abort and pause keys."""

    def __init__(self):
        super().__init__(daemon=True)
        self.abort = threading.Event()
        self.paused = threading.Event()
        self.devices = []
        try:
            from evdev import InputDevice, ecodes, list_devices
            for path in list_devices():
                dev = InputDevice(path)
                keys = dev.capabilities().get(ecodes.EV_KEY, [])
                if dev.name != DEVICE_NAME and ecodes.KEY_ESC in keys:
                    self.devices.append(dev)
                else:
                    dev.close()
            self.codes = {ecodes.ecodes[ABORT_KEY]: self.abort.set,
                          ecodes.ecodes[PAUSE_KEY]: self._toggle_pause}
            self.EV_KEY = ecodes.EV_KEY
        except (ImportError, OSError):
            pass

    @property
    def available(self) -> bool:
        return bool(self.devices)

    def wait_for_modifiers_released(self, timeout: float = 5.0) -> None:
        """Block until the user lets go of Super/Alt/Ctrl/Shift, e.g. after the
        keybind that started us, so they don't combine with our keystrokes."""
        from evdev import ecodes
        mods = {ecodes.ecodes[f"KEY_{side}{m}"] for side in ("LEFT", "RIGHT")
                for m in ("META", "ALT", "CTRL", "SHIFT")}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not any(mods & set(dev.active_keys()) for dev in self.devices):
                    return
            except OSError:
                return
            time.sleep(0.03)

    def _toggle_pause(self) -> None:
        (self.paused.clear if self.paused.is_set() else self.paused.set)()

    def run(self) -> None:
        fds = {dev.fd: dev for dev in self.devices}
        while not self.abort.is_set() and fds:
            ready, _, _ = select.select(list(fds), [], [], 0.2)
            for fd in ready:
                try:
                    for ev in fds[fd].read():
                        if ev.type == self.EV_KEY and ev.value == 1 and ev.code in self.codes:
                            self.codes[ev.code]()
                except OSError:  # device unplugged
                    del fds[fd]


Progress = Callable[[float, float, bool], None]  # (fraction done, seconds left, paused)


def play(events: list[KeyEvent], kb, watcher: HotkeyWatcher | None,
         on_progress: Progress | None = None) -> bool:
    """Send events in real time. Returns False if aborted."""
    total = events[-1].t if events else 0.0

    def report(t: float, paused: bool = False) -> None:
        if on_progress:
            on_progress(min(t / total, 1.0) if total else 1.0, max(total - t, 0.0), paused)

    start = last_report = time.monotonic()
    report(0)
    try:
        for ev in events:
            while True:
                if watcher and watcher.abort.is_set():
                    return False
                if watcher and watcher.paused.is_set():
                    report(ev.t, paused=True)
                    start += _wait_paused(kb, watcher)
                    report(ev.t)
                    continue
                now = time.monotonic()
                if now - last_report >= 0.25:
                    report(min(now - start, ev.t))
                    last_report = now
                remaining = start + ev.t - now
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.05))
            kb.send(ev.key, ev.down)
        report(total)
        return True
    finally:
        kb.close()


def _wait_paused(kb, watcher: HotkeyWatcher) -> float:
    """Release held keys, wait for resume, then restore Shift. Returns time spent."""
    began = time.monotonic()
    held = set(kb.held)
    for key in held:
        kb.send(key, False)
    while watcher.paused.is_set() and not watcher.abort.is_set():
        time.sleep(0.05)
    if watcher.abort.is_set():
        return 0.0
    time.sleep(0.3)  # give the user a moment to let go of their own keys
    for key in held:
        if key.endswith("SHIFT"):  # re-pressing a letter would type it twice
            kb.send(key, True)
    return time.monotonic() - began
