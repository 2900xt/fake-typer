"""Turns target text into a stream of keystroke actions that includes realistic
mistakes and their corrections. The stream always leaves exactly the target text
on screen once replayed."""

import random
from dataclasses import dataclass, field
from typing import Literal

from .layout import KEYS, NEIGHBOURS


@dataclass
class Action:
    kind: Literal["char", "backspace", "notice", "pause", "key"]
    char: str = ""          # for "key": a name from layout.SPECIAL_KEYS
    # "typo" for wrong keystrokes, "lag" for correct ones typed before noticing,
    # "fix" for backspaces and retyping. Purely informational.
    tag: str = ""
    duration: float = 0.0   # for "pause"


@dataclass
class EditorQuirks:
    """Counter-measures for code editors that change what was typed. Each works
    the same in a plain text field, where it is simply a no-op."""
    # Esc before Enter, so Enter can't accept an autocomplete suggestion.
    dismiss_completion: bool = False
    # Before Enter, delete whatever sits after the cursor on the line: only
    # closers the editor auto-inserted can be there.
    clear_auto_pairs: bool = False
    # Don't type leading indentation; at the end of each line, replace whatever
    # indentation the editor ended up with (auto-indent, re-indent on typing
    # `else` or `}`...) with the text's own.
    fix_indentation: bool = False


OPENERS = set("([{\"'`")  # characters editors auto-close


@dataclass
class TypoModel:
    rate: float = 0.03            # baseline chance of a mistake per character
    code: bool = False            # restrict mistakes to identifiers/words in code
    # Relative weights of each kind of mistake.
    weights: dict[str, float] = field(default_factory=lambda: {
        "substitute": 0.45,   # hit a neighbouring key instead
        "insert": 0.15,       # fat-finger: neighbouring key *and* the right one
        "omit": 0.12,         # skipped a character
        "transpose": 0.15,    # swapped two characters ("teh")
        "double": 0.05,       # pressed a key twice
        "shift": 0.08,        # held/released Shift at the wrong moment
    })


def _is_wordy(ch: str) -> bool:
    return ch.isalnum() or ch in "_'"


class Planner:
    """Places mistakes at random according to `model`, plus any scripted ones:
    `scripted` maps a text index to (wrong text typed there, how many target
    characters it stands in for, characters typed before noticing), and
    `pauses` maps a text index to seconds of hesitation before it."""

    def __init__(self, text: str, model: TypoModel, rng: random.Random,
                 scripted: dict[int, tuple[str, int, int]] | None = None,
                 pauses: dict[int, float] | None = None,
                 quirks: EditorQuirks | None = None):
        self.text = text
        self.quirks = quirks or EditorQuirks()
        self.model = model
        self.rng = rng
        self.scripted = dict(scripted or {})
        self.pauses = dict(pauses or {})

    # How likely a mistake is at position i, given what surrounds it.
    def _error_chance(self, i: int, cooldown: int) -> float:
        text, ch = self.text, self.text[i]
        if ch in "\n\t" or ch not in KEYS:
            return 0.0
        prev = text[i - 1] if i else "\n"
        if self._in_indent(i):
            return 0.0  # leave indentation alone
        if self.model.code and not (_is_wordy(ch) or ch == " "):
            return 0.0  # editors auto-pair brackets/quotes; don't backspace around them
        p = self.model.rate
        if not _is_wordy(prev):
            p *= 0.5  # first letter of a word is rarely wrong
        if ch.isdigit() or (not ch.isalpha() and ch != " "):
            p *= 1.4  # top row and symbols are harder to hit
        if KEYS[ch].shift:
            p *= 1.2
        if cooldown:
            p *= 0.25  # people slow down and are careful right after fixing something
        return min(p, 0.5)

    def _mistake(self, i: int) -> tuple[list[str], int] | None:
        """Wrong characters to type at position i, and the index typing resumes from."""
        text, ch, rng = self.text, self.text[i], self.rng
        nxt = text[i + 1] if i + 1 < len(text) else ""
        options = dict(self.model.weights)
        if not NEIGHBOURS.get(ch):
            options.pop("substitute"), options.pop("insert")
        if not (nxt and nxt != ch and _is_wordy(ch) and _is_wordy(nxt)):
            options.pop("transpose")
        if not ch.isalpha():
            options.pop("double"), options.pop("shift")
        if ch == " ":
            options = {"omit": 1.0}
        if not options:
            return None
        kind = rng.choices(list(options), weights=list(options.values()))[0]

        if kind == "substitute":
            return [rng.choice(NEIGHBOURS[ch])], i + 1
        if kind == "insert":
            wrong = rng.choice(NEIGHBOURS[ch])
            return ([wrong, ch] if rng.random() < 0.5 else [ch, wrong]), i + 1
        if kind == "omit":
            return [], i + 1
        if kind == "transpose":
            return [nxt, ch], i + 2
        if kind == "double":
            return [ch, ch], i + 1
        # Shift slip: either held too long ("THe") or missed entirely ("the" for "The").
        if ch.isupper() and nxt.isalpha() and nxt.islower():
            return [ch, nxt.upper()], i + 2
        return [ch.swapcase()], i + 1

    def _lag(self) -> int:
        """How many more characters get typed before the mistake is noticed."""
        r = self.rng.random()
        if r < 0.35:
            return 0
        if r < 0.80:
            return self.rng.randint(1, 3)
        return self.rng.randint(4, 8)

    def _in_indent(self, i: int) -> bool:
        start = self.text.rfind("\n", 0, i) + 1
        return self.text[start:i + 1].strip(" \t") == ""

    def _line_end(self, i: int, typed: list[Action]) -> list[Action]:
        """Keys that make the line ending at text[i] come out right in an editor.
        `typed` is everything typed on the line so far, typos included."""
        text, q = self.text, self.quirks
        line = text[text.rfind("\n", 0, i) + 1:i]
        content = line.lstrip(" \t")
        indent = line[:len(line) - len(content)]
        out = []
        if not content:
            if q.fix_indentation:
                # A blank line may still hold the editor's auto-indent: select it
                # and overwrite it with a throwaway space.
                out += [Action("key", "select_line_start", "editor"),
                        Action("char", " ", "editor"), Action("backspace", tag="editor")]
            return out
        if q.clear_auto_pairs and any(a.kind == "char" and a.char in OPENERS for a in typed):
            # Select to the end of the line and overwrite it with a throwaway space.
            out += [Action("key", "select_line_end", "editor"),
                    Action("char", " ", "editor"), Action("backspace", tag="editor")]
        if q.fix_indentation:
            # Home goes to the first non-blank character (or the line start), and
            # Shift+Home from there selects any indentation before it.
            out += [Action("key", "line_start", "editor"), Action("key", "select_line_start", "editor")]
            if not indent or indent[0] == "\t":
                # Clear the selection: replacing it with nothing, or with a Tab
                # (which would indent the selected lines instead).
                out += [Action("char", " ", "editor"), Action("backspace", tag="editor")]
            out += [Action("char", c, "editor") for c in indent]
            out.append(Action("key", "line_end", "editor"))
        return out

    def _newline(self, i: int, typed: list[Action]) -> list[Action]:
        """Enter at text[i], plus whatever keeps the editor from interfering."""
        out = self._line_end(i, typed)
        if self.quirks.dismiss_completion and i and _is_wordy(self.text[i - 1]):
            out.append(Action("key", "dismiss", "editor"))  # an autocomplete popup may be open
        out.append(Action("char", "\n"))
        return out

    def plan(self) -> list[Action]:
        text, rng = self.text, self.rng
        actions: list[Action] = []
        i, cooldown, line_from = 0, 0, 0  # line_from: where this line's actions begin
        while i < len(text):
            if self.quirks.fix_indentation and self._in_indent(i) and text[i] in " \t":
                i += 1  # typed at the end of the line instead
                continue
            if i in self.pauses:
                actions.append(Action("pause", duration=self.pauses.pop(i)))
            mistake, lag = None, None
            if i in self.scripted:
                typed, length, lag = self.scripted.pop(i)
                mistake = list(typed), i + length
            elif rng.random() < self._error_chance(i, cooldown):
                mistake = self._mistake(i)
            if mistake is None:
                if text[i] == "\n":
                    actions.extend(self._newline(i, actions[line_from:]))
                    line_from = len(actions)
                else:
                    actions.append(Action("char", text[i]))
                i += 1
                cooldown = max(0, cooldown - 1)
                continue

            wrong, j = mistake
            screen = list(text[:i]) + wrong
            batch = [Action("char", c, "typo") for c in wrong]
            # Keep going on autopilot for a bit, but never past a line break or
            # (in code) into punctuation that an editor might auto-pair.
            for _ in range(self._lag() if lag is None else lag):
                if j >= len(text) or text[j] == "\n" or (self.model.code and not (_is_wordy(text[j]) or text[j] == " ")):
                    break
                screen.append(text[j])
                batch.append(Action("char", text[j], "lag"))
                j += 1

            # Find where the screen first departs from the target.
            d = i
            while d < len(screen) and d < len(text) and screen[d] == text[d]:
                d += 1
            if d == len(screen):
                # The "mistake" happened to produce correct text (e.g. omitted then lag
                # of zero). Nothing to fix; carry on from wherever the screen is.
                actions.extend(Action("char", a.char) for a in batch)
                i = d
                continue

            actions.extend(batch)
            actions.append(Action("notice"))
            actions.extend(Action("backspace", tag="fix") for _ in range(len(screen) - d))
            i, cooldown = d, rng.randint(3, 8)
        if text and text[-1] != "\n":
            actions.extend(self._line_end(len(text), actions[line_from:]))
        return actions


def replay(actions: list[Action]) -> str:
    """What ends up in a plain text field (no auto-indent, auto-closing or
    autocomplete) if `actions` are typed into it."""
    buf, cur, anchor = "", 0, None

    def selection() -> tuple[int, int]:
        return (min(anchor, cur), max(anchor, cur)) if anchor is not None else (cur, cur)

    for a in actions:
        start = buf.rfind("\n", 0, cur) + 1
        end = buf.find("\n", cur) % (len(buf) + 1)  # -1 (no newline) -> len(buf)
        if a.kind == "char" or (a.kind == "backspace" and anchor not in (None, cur)):
            lo, hi = selection()
            new = a.char if a.kind == "char" else ""
            buf, cur, anchor = buf[:lo] + new + buf[hi:], lo + len(new), None
        elif a.kind == "backspace":
            if cur:
                buf, cur = buf[:cur - 1] + buf[cur:], cur - 1
            anchor = None
        elif a.kind == "key" and a.char != "dismiss":
            select = a.char.startswith("select")
            anchor = (cur if anchor is None else anchor) if select else None
            cur = start if a.char.endswith("start") else end
    return buf
