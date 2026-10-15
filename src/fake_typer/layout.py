"""US QWERTY geometry: which physical key makes each character, where it sits,
and which finger presses it. Used both to emit keystrokes and to model typos
(neighbouring keys) and rhythm (hand/finger alternation)."""

from dataclasses import dataclass
from math import hypot

_BASE_ROWS = ["`1234567890-=", "qwertyuiop[]\\", "asdfghjkl;'", "zxcvbnm,./"]
_SHIFT_ROWS = ["~!@#$%^&*()_+", "QWERTYUIOP{}|", 'ASDFGHJKL:"', "ZXCVBNM<>?"]
# Horizontal stagger of each row, in key widths.
_ROW_OFFSET = [0.0, 1.5, 1.75, 2.25]

_PUNCT_KEYS = {
    "`": "KEY_GRAVE", "-": "KEY_MINUS", "=": "KEY_EQUAL", "[": "KEY_LEFTBRACE",
    "]": "KEY_RIGHTBRACE", "\\": "KEY_BACKSLASH", ";": "KEY_SEMICOLON",
    "'": "KEY_APOSTROPHE", ",": "KEY_COMMA", ".": "KEY_DOT", "/": "KEY_SLASH",
}

# Touch-typing finger assignment: 0-3 left pinky..index, 4 thumb, 5-8 right index..pinky.
_FINGERS = {
    0: "`1qaz", 1: "2wsx", 2: "3edc", 3: "45rtfgvb",
    5: "67yuhjnm", 6: "8ik,", 7: "9ol.", 8: "0-=p[]\\;'/",
}
_FINGER_OF = {ch: f for f, chars in _FINGERS.items() for ch in chars}


@dataclass(frozen=True)
class Key:
    name: str            # evdev-style key name, e.g. "KEY_A"
    shift: bool          # whether Shift must be held
    finger: int          # 0-8, see _FINGERS (4 = thumb)
    pos: tuple[float, float] | None  # (x, row) on the board; None for space/enter/etc.

    @property
    def hand(self) -> str:
        return "thumb" if self.finger == 4 else ("left" if self.finger < 4 else "right")


def _key_name(base: str) -> str:
    if base.isalpha():
        return f"KEY_{base.upper()}"
    if base.isdigit():
        return f"KEY_{base}"
    return _PUNCT_KEYS[base]


KEYS: dict[str, Key] = {}
for row, (base_row, shift_row) in enumerate(zip(_BASE_ROWS, _SHIFT_ROWS)):
    for col, (base, shifted) in enumerate(zip(base_row, shift_row)):
        pos = (col + _ROW_OFFSET[row], float(row))
        name = _key_name(base)
        finger = _FINGER_OF[base]
        KEYS[base] = Key(name, False, finger, pos)
        KEYS[shifted] = Key(name, True, finger, pos)

KEYS[" "] = Key("KEY_SPACE", False, 4, None)
KEYS["\n"] = Key("KEY_ENTER", False, 8, None)
KEYS["\t"] = Key("KEY_TAB", False, 0, None)

BACKSPACE = Key("KEY_BACKSPACE", False, 8, None)
SPECIAL_KEYS = {
    "dismiss": Key("KEY_ESC", False, 0, None),
    "line_start": Key("KEY_HOME", False, 8, None),
    "select_line_start": Key("KEY_HOME", True, 8, None),  # Shift+Home
    "line_end": Key("KEY_END", False, 8, None),
    "select_line_end": Key("KEY_END", True, 8, None),     # Shift+End
}

# Common typographic characters that have an obvious keyboard equivalent.
NORMALIZE = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"',
    "„": '"', "–": "-", "—": "-", "−": "-", " ": " ",
    "…": "...", "´": "'", "\r": None,
})


def can_type(ch: str) -> bool:
    return ch in KEYS


def neighbours(ch: str) -> list[str]:
    """Characters on keys physically adjacent to `ch`, in the same shift state."""
    key = KEYS.get(ch)
    if key is None or key.pos is None:
        return []
    x, y = key.pos
    out = []
    for other, k in KEYS.items():
        if k.pos is None or k.shift != key.shift or k.name == key.name:
            continue
        if hypot(k.pos[0] - x, k.pos[1] - y) <= 1.3:
            out.append(other)
    return out


NEIGHBOURS: dict[str, list[str]] = {ch: neighbours(ch) for ch in KEYS}
