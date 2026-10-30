import argparse
import os
import random
import re
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import llm, status
from .keyboard import ABORT_KEY, PAUSE_KEY, HotkeyWatcher, TerminalKeyboard, VirtualKeyboard, play
from .layout import NORMALIZE, can_type
from .planner import EditorQuirks, Planner, TypoModel, random_revisions, replay
from .timing import Rhythm, Timer, to_events


CONFIG_DIR = Path("~/.config/fake-typer").expanduser()
COMPOSE_WORDS = 250  # prose at least this long is typed as if being written
CONFIG_FILE = CONFIG_DIR / "config.toml"
ENV_FILES = [".env", CONFIG_DIR / ".env"]


class Failure(Exception):
    """A problem to report to the user (in the terminal, bar and a notification)."""


def load_env_files() -> None:
    """Read KEY=value lines from the first env files found. Variables already set
    in the environment win."""
    for path in ENV_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def looks_like_code(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    symbols = sum(text.count(c) for c in "{}();=<>[]#$_")
    indented = sum(l[:1] in " \t" for l in lines) / len(lines)
    return symbols / max(len(text), 1) > 0.04 or indented > 0.3


def read_input(args) -> str:
    if args.clipboard:
        result = subprocess.run(["wl-paste", "--no-newline", "--type", "text"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise Failure("The clipboard has no text in it")
        return result.stdout
    if args.file in (None, "-"):
        if sys.stdin.isatty():
            print("Paste text, then press Ctrl-D:", file=sys.stderr)
        return sys.stdin.read()
    with open(args.file, encoding="utf-8") as f:
        return f.read()


def prepare(text: str, strip_indent: bool) -> str:
    text = text.translate(NORMALIZE)
    if strip_indent:
        text = re.sub(r"(?m)^[ \t]+", "", text)
    dropped = sorted({c for c in text if not can_type(c)})
    if dropped:
        print(f"warning: skipping characters with no key on a US keyboard: {''.join(dropped)!r}",
              file=sys.stderr)
        text = "".join(c for c in text if can_type(c))
    return text


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fake-typer",
        description="Type text into the focused window like a human would, "
                    "rhythm, typos, corrections and all.",
        epilog="commands: 'fake-typer toggle' types the clipboard, or pauses/resumes a run "
               "in progress, stopping it if run twice within half a second (meant for a "
               "keybind); 'fake-typer pause', 'fake-typer stop', "
               f"'fake-typer status'. Defaults can be set in {CONFIG_FILE}.")
    ap.add_argument("file", nargs="?", help="text file to type (default: stdin)")
    ap.add_argument("-c", "--clipboard", action="store_true", help="type the clipboard contents")
    ap.add_argument("-w", "--wpm", type=float, default=65, help="average typing speed (default 65)")
    ap.add_argument("-t", "--typo-rate", type=float, default=0.03,
                    help="chance of a mistake per character, 0 to disable (default 0.03)")
    ap.add_argument("--mode", choices=["auto", "prose", "code"], default="auto",
                    help="code mode avoids mistakes around brackets/quotes and pauses at line ends")
    ap.add_argument("--strip-indent", action="store_true",
                    help="don't type leading whitespace (for editors that auto-indent)")
    ap.add_argument("--editor", action=argparse.BooleanOptionalAction, default=None,
                    help="work around code-editor features: Esc before Enter so it can't accept "
                         "an autocomplete suggestion, delete auto-inserted closing brackets at the "
                         "end of each line, and set each line's indentation after the editor has "
                         "auto-indented it (default: on in code mode)")
    ap.add_argument("--think-rate", type=float, default=0.02,
                    help="chance per word of a longer thinking pause (default 0.02)")
    ap.add_argument("--max-pause", type=float,
                    help="longest single pause in seconds (default 6, or 90 when composing)")
    ap.add_argument("--compose", action=argparse.BooleanOptionalAction, default=None,
                    help="type as if writing the text for the first time: long stops to plan "
                         "sentences and paragraphs, and revisions to the draft (default: on for "
                         f"prose of {COMPOSE_WORDS}+ words)")
    ap.add_argument("--compose-wpm", type=float, default=20,
                    help="overall pace when composing, counting all the thinking and revising; "
                         "--wpm still sets the speed of each burst (default 20)")
    ap.add_argument("--revise-rate", type=float, default=1.5,
                    help="revisions to the draft per 100 words when composing (default 1.5)")
    ap.add_argument("-d", "--delay", type=float,
                    help="seconds to wait before typing, to focus the target window "
                         "(default 5, or 0.3 with --keybind)")
    ap.add_argument("--keybind", action="store_true",
                    help="started from a keybind: no countdown, wait for the keybind's modifier "
                         "keys to be released, report problems as notifications")
    ap.add_argument("--llm", action=argparse.BooleanOptionalAction, default=False,
                    help="have Claude decide where the mistakes and thinking pauses go "
                         "(needs ANTHROPIC_API_KEY)")
    ap.add_argument("--persona", default="a fluent, fairly fast typist who knows the material",
                    help="who is typing, for --llm (e.g. 'tired student, shaky speller')")
    ap.add_argument("--model", default="claude-opus-5-5", help="Claude model for --llm")
    ap.add_argument("--seed", type=int, help="random seed for a reproducible run")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="type into this terminal instead of the virtual keyboard")
    return ap


def load_config(ap: argparse.ArgumentParser) -> None:
    """Use values from the config file as defaults for the command-line options."""
    try:
        data = tomllib.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        return
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"warning: ignoring {CONFIG_FILE}: {e}", file=sys.stderr)
        return
    known = {a.dest for a in ap._actions}
    defaults = {}
    for key, value in data.items():
        key = key.replace("-", "_")
        if key in known:
            defaults[key] = value
        else:
            print(f"warning: unknown setting '{key}' in {CONFIG_FILE}", file=sys.stderr)
    ap.set_defaults(**defaults)


def main() -> None:
    argv = sys.argv[1:]
    command = argv[0] if argv and argv[0] in ("toggle", "pause", "stop", "status") else None
    if command:
        argv = argv[1:]
    if command == "status":
        print(status.STATUS_FILE.read_text() if status.STATUS_FILE.exists() else "{}")
        return
    if command == "stop":
        sys.exit(0 if status.stop() else "not typing")
    if command == "pause":
        sys.exit(0 if status.toggle_pause() else "not typing")
    if command == "toggle":
        if status.toggle_pause():
            return
        argv = ["--clipboard", "--keybind", *argv]

    ap = build_parser()
    load_config(ap)
    args = ap.parse_args(argv)
    load_env_files()
    run(args)


def pace(actions, rhythm: Rhythm, rng: random.Random, target: float | None):
    """Time the actions; with a target duration (seconds), stretch or shrink the
    thinking pauses until the whole run takes about that long."""
    seed = rng.random()
    for _ in range(5):
        timer = Timer(rhythm, random.Random(seed))  # same seed: only the pauses change
        strokes = timer.schedule(actions)
        if not (target and strokes and timer.thinking) or abs(strokes[-1].t - target) < 0.03 * target:
            break
        # Pauses that hit max_pause don't stretch, so this converges over a few rounds.
        busy = strokes[-1].t - timer.thinking  # typing, fixing and rereading
        rhythm.pause_scale *= max(target - busy, 0.1 * timer.thinking) / timer.thinking
    return strokes


def run(args) -> None:
    def notify(title: str, body: str = "") -> None:
        if args.keybind:
            subprocess.run(["notify-send", "-a", "Fake typer", title, body], check=False)

    def warn(msg: str) -> None:
        print(f"warning: {msg}", file=sys.stderr)
        notify("Fake typer", msg)

    if not args.dry_run and status.running_pid():
        sys.exit("already typing; run 'fake-typer stop' to cancel it")

    st = status.Status(enabled=not args.dry_run)
    watcher = None if args.dry_run else HotkeyWatcher()
    typing = False

    def on_stop(*_):
        if watcher:
            watcher.abort.set()
        if not typing:
            raise KeyboardInterrupt  # still planning: bail out right away

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGUSR1, lambda *_: watcher and watcher.keybind())

    try:
        st.update(state="planning", progress=0.0, remaining=0.0, message="Reading text")
        text = read_input(args)
        code = args.mode == "code" or (args.mode == "auto" and looks_like_code(text))
        text = prepare(text, args.strip_indent)
        # The terminal preview isn't an editor, so it gets plain typing.
        editor = (code and not args.dry_run) if args.editor is None else args.editor
        if editor:
            text = re.sub(r"[ \t]+$", "", text, flags=re.M)  # trailing blanks can't be kept
        if not text:
            raise Failure("Nothing to type")
        words = len(text.split())
        compose = (not code and words >= COMPOSE_WORDS) if args.compose is None else args.compose
        if compose and editor:
            raise Failure("--compose goes back with the arrow keys, which needs --no-editor")
        max_pause = args.max_pause if args.max_pause is not None else (90.0 if compose else 6.0)
        n_revisions = round(words / 100 * args.revise_rate) if compose else 0
        st.update(preview=text[:80], chars=len(text), mode="code" if code else "prose")

        # Create the device early so the compositor has picked it up by the time we type.
        kb = TerminalKeyboard() if args.dry_run else VirtualKeyboard()
        kb_ready = time.monotonic() + 0.5

        rng = random.Random(args.seed)
        model, scripted, pauses = TypoModel(rate=args.typo_rate, code=code), {}, {}
        revisions = random_revisions(text, args.revise_rate, rng) if compose else {}
        if args.llm and (args.typo_rate > 0 or n_revisions):
            print(f"Asking {args.model} where this typist would slip up...", file=sys.stderr)
            st.update(message="Asking Claude where to make mistakes")
            n = round(len(text) * args.typo_rate)
            plan = llm.plan(text, code, n, args.persona, args.model, warn=warn, n_revisions=n_revisions)
            if plan:
                model.rate = 0  # Claude's mistakes and revisions replace the random ones
                scripted = {m.pos: (m.typed, len(m.original), m.noticed_after)
                            for m in plan.mistakes if not m.go_back}
                revisions = {m.pos: (m.typed, len(m.original), m.noticed_after)
                             for m in plan.mistakes if m.go_back}
                pauses = plan.pauses
                kinds = {}
                for m in plan.mistakes:
                    kinds[m.kind] = kinds.get(m.kind, 0) + 1
                print(f"Claude planned {len(plan.mistakes)} mistakes "
                      f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}) and "
                      f"{len(pauses)} pauses" + (f"; {plan.dropped} didn't fit the text" if plan.dropped else ""),
                      file=sys.stderr)
        quirks = EditorQuirks(dismiss_completion=editor, clear_auto_pairs=editor,
                              fix_indentation=editor and not args.strip_indent)
        actions = Planner(text, model, rng, scripted, pauses, quirks, revisions).plan()
        assert replay(actions) == text, "planner produced the wrong text"
        rhythm = Rhythm(wpm=args.wpm, code=code, max_pause=max_pause, think_rate=args.think_rate,
                        compose=compose)
        strokes = pace(actions, rhythm, rng, words / args.compose_wpm * 60 if compose else None)
        events = to_events(strokes, rng)

        typos = sum(1 for a in actions if a.kind == "notice")
        revised = sum(1 for a in actions if a.kind == "pause" and a.tag == "revise")
        duration = events[-1].t if events else 0
        print(f"{len(text)} characters, {'composing' if compose else ('code' if code else 'prose')} "
              f"mode, {typos} typos" + (f", {revised} later revisions" if compose else "") +
              f", about {duration / 60:.1f} min", file=sys.stderr)
        if not editor and not args.strip_indent and re.search(r"(?m)^[ \t]", text):
            print("hint: if the target editor auto-indents, use --editor or --strip-indent",
                  file=sys.stderr)
        st.update(typos=typos, duration=duration, message="")

        if watcher:
            if watcher.available:
                watcher.start()
                print(f"Press {ABORT_KEY[4:]} to stop, {PAUSE_KEY[4:]} to pause/resume.", file=sys.stderr)
            else:
                warn("can't read physical keyboards, so Esc/F9 won't work; "
                     "use 'fake-typer stop' or Ctrl-C")
        delay = args.delay if args.delay is not None else (0.3 if args.keybind else 5)
        if args.keybind and watcher:
            watcher.wait_for_modifiers_released()
            time.sleep(max(delay, kb_ready - time.monotonic()))
        elif not args.dry_run:
            for left in range(int(delay), 0, -1):
                print(f"\rFocus the target window... {left} ", end="", file=sys.stderr, flush=True)
                time.sleep(1)
            time.sleep(max(delay - int(delay), kb_ready - time.monotonic(), 0))
            print("\rTyping.                          ", file=sys.stderr)

        typing = True
        finished = play(events, kb, watcher, lambda frac, left, paused: st.update(
            state="paused" if paused else "typing", progress=frac, remaining=left))
        st.update(state="done" if finished else "stopped")
        if not finished:
            sys.exit("stopped")
    except KeyboardInterrupt:
        st.update(state="stopped")
        sys.exit("stopped")
    except Failure as e:
        st.update(state="error", message=str(e))
        notify("Fake typer", str(e))
        sys.exit(str(e))
    except Exception as e:
        st.update(state="error", message=str(e))
        notify("Fake typer crashed", str(e))
        raise
    finally:
        st.close()
