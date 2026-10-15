import random

import pytest

from fake_typer.layout import KEYS, NEIGHBOURS
from fake_typer.planner import Planner, TypoModel, replay
from fake_typer.timing import Rhythm, Timer, to_events

SAMPLES = [
    "The quick brown fox jumps over the lazy dog.",
    "Hello, World! It's 2026 and THE year of \"typing\".\n\nNew paragraph here.",
    "def f(x):\n    return {'a': [x, x * 2]}  # comment\n",
    "a",
    "  leading spaces\n\tand tabs",
]


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("code", [False, True])
def test_plan_always_produces_target(text, code):
    for seed in range(200):
        model = TypoModel(rate=0.3, code=code)
        actions = Planner(text, model, random.Random(seed)).plan()
        assert replay(actions) == text


def test_mistakes_happen_and_get_fixed():
    text = "the quick brown fox jumps over the lazy dog " * 20
    actions = Planner(text, TypoModel(rate=0.05), random.Random(1)).plan()
    assert any(a.tag == "typo" for a in actions)
    assert any(a.kind == "backspace" for a in actions)


def test_neighbours_are_adjacent():
    assert set(NEIGHBOURS["g"]) >= {"f", "h", "t", "b"}
    assert "p" not in NEIGHBOURS["a"]
    assert all(c.isupper() for c in NEIGHBOURS["G"])


def simulate(events):
    """Replay raw key events the way an OS would turn them into characters.
    Like the kernel, a press of a key that is already down is ignored."""
    chars = {(k.name, k.shift): c for c, k in KEYS.items()}
    down, out = set(), []
    for e in events:
        if not e.down:
            down.discard(e.key)
            continue
        if e.key in down:
            continue
        down.add(e.key)
        shift = {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"} & down
        if not e.key.endswith("SHIFT"):
            if e.key == "KEY_BACKSPACE":
                out and out.pop()
            elif (e.key, bool(shift)) in chars:
                out.append(chars[(e.key, bool(shift))])
    return "".join(out)


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("wpm", [40, 120, 400])
def test_key_events_type_the_text(text, wpm):
    for seed in range(100):
        rng = random.Random(seed)
        actions = Planner(text, TypoModel(rate=0.2), rng).plan()
        events = to_events(Timer(Rhythm(wpm=wpm), rng).schedule(actions), rng)
        assert simulate(events) == text
        assert all(a.t <= b.t for a, b in zip(events, events[1:]))


def test_speed_is_roughly_right():
    text = "the quick brown fox jumps over the lazy dog " * 50
    rng = random.Random(0)
    actions = Planner(text, TypoModel(rate=0), rng).plan()
    strokes = Timer(Rhythm(wpm=60, think_rate=0), rng).schedule(actions)
    wpm = len(text) / 5 / (strokes[-1].t / 60)
    assert 40 < wpm < 70


from fake_typer import llm

CODE = "def total(items):\n    length = len(items)\n    return sum(items) / length\n"


def test_llm_plan_resolves_and_replays():
    raw = {
        "mistakes": [
            {"line": 2, "original": "length", "occurrence": 1, "typed": "lenght",
             "noticed_after": 3, "kind": "code"},
            {"line": 3, "original": "length", "occurrence": 1, "typed": "len",
             "noticed_after": 0, "kind": "thinko"},
            {"line": 2, "original": "(items)", "occurrence": 1, "typed": "(item)",
             "noticed_after": 0, "kind": "code"},          # brackets: dropped in code mode
            {"line": 9, "original": "x", "occurrence": 1, "typed": "y",
             "noticed_after": 0, "kind": "motor"},          # no such line
            {"line": 2, "original": "    ", "occurrence": 1, "typed": "  ",
             "noticed_after": 0, "kind": "motor"},          # indentation: dropped
        ],
        "pauses": [{"line": 3, "before": "sum", "occurrence": 1, "seconds": 1.5}],
    }
    plan = llm.resolve(CODE, raw, code=True)
    assert [(m.original, m.typed) for m in plan.mistakes] == [("length", "lenght"), ("length", "len")]
    assert plan.dropped == 3
    assert CODE[plan.mistakes[1].pos:].startswith("length\n")
    assert list(plan.pauses.values()) == [1.5] and CODE[next(iter(plan.pauses)):].startswith("sum")

    scripted = {m.pos: (m.typed, len(m.original), m.noticed_after) for m in plan.mistakes}
    for seed in range(20):
        actions = Planner(CODE, TypoModel(rate=0, code=True), random.Random(seed),
                          scripted, plan.pauses).plan()
        assert replay(actions) == CODE
        typed = "".join(a.char for a in actions if a.kind == "char")
        assert "lenght" in typed
        assert any(a.kind == "pause" and a.duration == 1.5 for a in actions)


def test_llm_overlapping_mistakes_keep_first():
    text = "the weather is nice"
    raw = {"mistakes": [
        {"line": 1, "original": "weather", "occurrence": 1, "typed": "whether", "noticed_after": 2, "kind": "thinko"},
        {"line": 1, "original": "ather", "occurrence": 1, "typed": "ahter", "noticed_after": 0, "kind": "motor"},
    ], "pauses": []}
    plan = llm.resolve(text, raw, code=False)
    assert [m.typed for m in plan.mistakes] == ["whether"] and plan.dropped == 1


from fake_typer.planner import EditorQuirks

PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"'}


class FakeEditor:
    """A text field. With `ide=True` it behaves like a code editor: auto-indents
    on Enter, auto-closes brackets (typing a closer steps over an auto-inserted
    one), re-indents a line when `else` is typed (wrongly, like the user's IDE),
    accepts the autocomplete suggestion on Enter while its popup is open, and has
    "smart" Home (first non-blank character, then the line start)."""

    def __init__(self, ide=True):
        self.ide = ide
        self.buf, self.cur, self.anchor, self.popup = "", 0, None, False
        self.chars = {(k.name, k.shift): c for c, k in KEYS.items()}

    def _line(self):
        start = self.buf.rfind("\n", 0, self.cur) + 1
        end = self.buf.find("\n", self.cur) % (len(self.buf) + 1)
        return start, end

    def _sel(self):
        a = self.anchor if self.anchor is not None else self.cur
        return min(a, self.cur), max(a, self.cur)

    def _put(self, s, keep_after=""):
        lo, hi = self._sel()
        self.buf = self.buf[:lo] + s + keep_after + self.buf[hi:]
        self.cur, self.anchor = lo + len(s), None

    def _move(self, to, select):
        self.anchor = (self.cur if self.anchor is None else self.anchor) if select else None
        self.cur = to

    def press(self, key, shift):
        start, end = self._line()
        if key == "KEY_ESC":
            self.popup = False
        elif key == "KEY_HOME":
            line = self.buf[start:end]
            first = start + len(line) - len(line.lstrip())
            smart = first if self.ide and self.cur != first else start
            self._move(smart, shift)
        elif key == "KEY_END":
            self._move(end, shift)
        elif key == "KEY_BACKSPACE":
            lo, hi = self._sel()
            if hi > lo:
                self._put("")
            elif self.cur:
                self.buf, self.cur = self.buf[:self.cur - 1] + self.buf[self.cur:], self.cur - 1
            self.popup = False
        elif key == "KEY_ENTER":
            if self.ide and self.popup:
                self._put("_channel")  # accepts the suggestion instead of a newline
                self.popup = False
                return
            line = self.buf[start:self.cur]
            indent = line[:len(line) - len(line.lstrip())] if self.ide else ""
            if self.ide and line.rstrip().endswith(("(", "{", "->", "=")):
                indent += "  "
            self._put("\n" + indent)
        else:
            ch = self.chars[(key, shift)]
            nxt = self.buf[self.cur:self.cur + 1]
            if self.ide and ch in PAIRS.values() and nxt == ch and self.anchor is None:
                self.cur += 1  # type over the auto-inserted closer
            elif self.ide and ch in PAIRS and self.anchor is None:
                self._put(ch, keep_after=PAIRS[ch])
            else:
                self._put(ch)
            self.popup = self.ide and (ch.isalnum() or ch == "_")
            start, _ = self._line()
            if self.ide and self.buf[start:self.cur].strip() == "else":
                # Re-indent to two less than the previous line, like the user's IDE.
                prev = self.buf[:start - 1].rsplit("\n", 1)[-1]
                want = max(0, len(prev) - len(prev.lstrip()) - 2)
                line = self.buf[start:self.cur]
                self.buf = self.buf[:start] + " " * want + line.lstrip() + self.buf[self.cur:]
                self.cur = start + want + len(line.lstrip())

    def run(self, events):
        down = set()
        for e in events:
            if not e.down:
                down.discard(e.key)
            elif e.key not in down:
                down.add(e.key)
                if not e.key.endswith("SHIFT"):
                    self.press(e.key, bool({"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"} & down))
        return self.buf


OCAML = """let draw_rect g p1 w h =
  set_graphics_state g;
  let (x, y) = ocaml_coords g p1 in
  Graphics.draw_rect x (y - h) w h

let f x =
  if x > 0 then
    (x + 1)
  else
[x]
"""

WIDGET = """{
  repaint = (fun (g: Gctx.gctx) ->
      let (_, y1) = w1.size () in
      w1.repaint g;
      w2.repaint (Gctx.translate g (0, y1)));
  handle = (fun (g: Gctx.gctx) (e: Gctx.event) ->
      if event_within g e (w1.size ())
      then w1.handle g e
      else
        let g2 = Gctx.translate g (0, snd (w1.size ())) in
        if event_within g2 e (w2.size ())
        then w2.handle g2 e
        else ());
  size = (fun () ->
      let (x1, y1) = w1.size () in
      let (x2, y2) = w2.size () in
      (max x1 x2, y1 + y2));
}"""

ON = EditorQuirks(dismiss_completion=True, clear_auto_pairs=True, fix_indentation=True)


def type_into(text, quirks, seed, ide=True, rate=0.1):
    rng = random.Random(seed)
    actions = Planner(text, TypoModel(rate=rate, code=True), rng, quirks=quirks).plan()
    assert replay(actions) == text
    events = to_events(Timer(Rhythm(wpm=200), rng).schedule(actions), rng)
    return FakeEditor(ide).run(events)


@pytest.mark.parametrize("text", [OCAML, WIDGET])
def test_editor_quirks_survive_a_code_editor(text):
    for seed in range(40):
        assert type_into(text, ON, seed) == text


@pytest.mark.parametrize("text", [OCAML, WIDGET])
def test_editor_quirks_are_harmless_in_a_plain_text_field(text):
    for seed in range(40):
        assert type_into(text, ON, seed, ide=False) == text


def test_without_quirks_the_editor_mangles_code():
    out = type_into(WIDGET, EditorQuirks(), 0, rate=0)
    assert out != WIDGET and out.rstrip().endswith("}")  # leftover auto-closers, as reported
    out = type_into(OCAML, EditorQuirks(), 0, rate=0)
    assert "in_channel" in out
