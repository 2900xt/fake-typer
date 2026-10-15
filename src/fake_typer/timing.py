"""Assigns human-like timing to planned actions and expands them into a
timeline of raw key presses/releases (including Shift and key rollover)."""

import math
import random
from dataclasses import dataclass

from .layout import BACKSPACE, KEYS, SPECIAL_KEYS, Key
from .planner import Action

# The most frequent English bigrams are typed noticeably faster than average.
FAST_BIGRAMS = set(
    "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha "
    "as ou io le ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur".split()
)


@dataclass
class Rhythm:
    wpm: float = 65.0
    jitter: float = 0.30          # spread of individual intervals (lognormal sigma)
    code: bool = False
    max_pause: float = 6.0        # cap on any single thinking pause, seconds
    think_rate: float = 0.02      # chance per word of a longer "what next?" pause


@dataclass
class KeyEvent:
    t: float       # seconds from start
    key: str       # evdev key name
    down: bool


@dataclass
class Stroke:
    t: float
    key: Key
    dwell: float
    action: Action


class Timer:
    def __init__(self, rhythm: Rhythm, rng: random.Random):
        self.r = rhythm
        self.rng = rng
        self.base = 60.0 / (rhythm.wpm * 5)   # mean seconds per keystroke
        self.flow = 1.0                         # slowly drifting speed (bursts and lulls)

    def _ln(self, mean: float, sigma: float) -> float:
        """Lognormal sample with the given mean."""
        return mean * math.exp(self.rng.gauss(-sigma * sigma / 2, sigma))

    def _pause(self, mean: float, sigma: float = 0.5) -> float:
        return min(self._ln(mean, sigma), self.r.max_pause)

    def _drift(self) -> None:
        # AR(1) process around 1.0: typists speed up and slow down in stretches.
        self.flow = 1 + 0.97 * (self.flow - 1) + self.rng.gauss(0, 0.03)
        self.flow = min(max(self.flow, 0.7), 1.5)

    def gap(self, prev: Action | None, prev2: Action | None, cur: Action) -> float:
        """Delay between the previous keypress and this one."""
        self._drift()
        if cur.kind == "notice":
            return self._pause(0.45, 0.4)      # "wait, that's wrong"
        if cur.kind == "backspace":
            if prev and prev.kind == "notice":
                return 0.0
            return self._ln(self.base * 0.6, 0.15)  # backspace runs are quick and even
        if prev is None:
            return 0.0

        ch = cur.char
        p = prev.char if prev.kind == "char" else ""
        pp = prev2.char if prev2 and prev2.kind == "char" else ""
        mean = self.base * self.flow

        if prev.kind == "backspace":
            mean += self._pause(0.15, 0.4)     # reorient after correcting
        elif (p + ch).lower() in FAST_BIGRAMS:
            mean *= 0.7
        else:
            pk, ck = KEYS.get(p), KEYS.get(ch)
            if pk and ck:
                if pk.name == ck.name:
                    mean *= 0.9                # repeated key
                elif pk.finger == ck.finger:
                    mean *= 1.4                # same finger, different key: slow
                elif "thumb" in (pk.hand, ck.hand) or pk.hand != ck.hand:
                    mean *= 0.85               # alternating hands: fast
                else:
                    mean *= 1.05

        key = KEYS.get(ch)
        if key and key.shift:
            mean += self.base * 0.4
        if ch.isdigit() or (not ch.isalnum() and ch not in " \n\t.,'"):
            mean *= 1.5 if not self.r.code else 1.25

        extra = 0.0
        if p == "\n":
            extra += self._pause(1.6 if pp == "\n" else (0.9 if self.r.code else 0.6))
        elif p == " " and pp in ".!?":
            extra += self._pause(0.7)          # new sentence
        elif p == " " and pp in ",;:":
            extra += self._pause(0.25)
        if p in " \n" and ch not in " \n" and self.rng.random() < self.r.think_rate:
            extra += self._pause(2.0, 0.6)    # occasional stop to think
        if p == " " and ch != " ":
            mean *= 1.15                       # starting a new word

        return self._ln(mean, self.r.jitter) + extra

    def dwell(self) -> float:
        return min(self._ln(0.09, 0.25), 0.25)

    def schedule(self, actions: list[Action]) -> list[Stroke]:
        strokes, t = [], 0.0
        prev = prev2 = None
        for a in actions:
            if a.kind == "pause":
                t += min(a.duration, self.r.max_pause)
                continue
            if a.kind == "key":
                # Editing keys (Esc, Shift+Home) don't change the typing context.
                t += self._ln(self.base * 0.8, 0.2)
                strokes.append(Stroke(t, SPECIAL_KEYS[a.char], self.dwell(), a))
                continue
            t += self.gap(prev, prev2, a)
            if a.kind != "notice":
                key = BACKSPACE if a.kind == "backspace" else KEYS[a.char]
                strokes.append(Stroke(t, key, self.dwell(), a))
            prev2, prev = prev, a
        return strokes


def shift_key(key: Key) -> str:
    # Use the Shift on the opposite hand, as touch typists do.
    return "KEY_LEFTSHIFT" if key.hand == "right" else "KEY_RIGHTSHIFT"


def to_events(strokes: list[Stroke], rng: random.Random) -> list[KeyEvent]:
    """Expand strokes into press/release events. Keys may overlap (rollover), but
    Shift is always down for exactly the strokes that need it."""
    events: list[KeyEvent] = []
    held_shift: str | None = None
    last_up: dict[str, KeyEvent] = {}   # most recent release of each key
    for i, s in enumerate(strokes):
        prev_t = strokes[i - 1].t if i else -1.0
        next_s = strokes[i + 1] if i + 1 < len(strokes) else None
        next_t = next_s.t if next_s else math.inf
        want = shift_key(s.key) if s.key.shift else None

        if want and not held_shift:
            lead = rng.uniform(0.03, 0.08)
            t = max(prev_t + 0.01, s.t - lead)
            if want in last_up:
                t = max(t, last_up[want].t + 0.005)
            events.append(KeyEvent(min(t, s.t - 0.001), want, True))
            held_shift = want

        # A key that is still down can't be pressed again (the OS ignores it),
        # so lift any earlier press of this key first.
        if s.key.name in last_up and last_up[s.key.name].t >= s.t:
            last_up[s.key.name].t = s.t - 0.005
        up = s.t + s.dwell
        if next_s and next_s.key.name == s.key.name:
            up = min(up, s.t + (next_t - s.t) * 0.6)
        events.append(KeyEvent(s.t, s.key.name, True))
        last_up[s.key.name] = KeyEvent(up, s.key.name, False)
        events.append(last_up[s.key.name])

        next_wants = next_s and next_s.key.shift and shift_key(next_s.key) == held_shift
        if held_shift and not next_wants:
            release = min(s.t + rng.uniform(0.03, 0.09), (s.t + next_t) / 2)
            last_up[held_shift] = KeyEvent(release, held_shift, False)
            events.append(last_up[held_shift])
            held_shift = None

    # Stable sort keeps press-before-release order for events at the same instant.
    events.sort(key=lambda e: e.t)
    return events
