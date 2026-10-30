"""Asks Claude where a human would slip up while typing a given text, and where
they'd stop to think. The answer is anchored to line numbers and substrings
(LLMs are unreliable at character offsets) and then resolved to positions."""

import json
import sys
from dataclasses import dataclass

AUTO_PAIRED = set("()[]{}\"'`")

SYSTEM = """\
You simulate one specific human typing out a text in real time, and decide \
exactly where they make mistakes and where they hesitate. The person is typing \
the text as if writing it themselves; every mistake they make gets noticed and \
fixed with backspace, so you only decide *what* goes wrong, *where*, and *how \
soon* they notice.

Make mistakes realistic for this content and this person:
- Motor slips: neighbouring key on a QWERTY keyboard, swapped letters ("teh"), \
doubled or dropped letters, Shift held too long ("THe"). Most common in long or \
fast-to-type words and awkward letter combinations; rare on the first letter of a word.
- Spelling slips on genuinely hard words ("recieve", "definately", "occured").
- Thinkos: the wrong but plausible word ("their" for "there", "than"/"then"), \
starting a different word than intended, a word typed from earlier/later in the sentence.
- In code, mostly make the mistakes a programmer makes rather than plain typos \
(roughly two thirds "code"/"syntax", the rest "motor"):
  - syntax: `=` vs `==` (or `:=`, `<>`/`!=` from another language), `->` vs `=>` \
or `.`, a missing or extra `;`, `:` or `,` noticed a few characters later, the \
wrong keyword or one from another language (`elif`/`else if`, `fun`/`function`, \
`let` without `rec`, `&&`/`and`), wrong operator (`+` for `^` or `++`, `<` for `<=`), \
`self.`/`this.` forgotten or added.
  - code: misspelled or mistyped identifiers (`lenght`, `reponse`), wrong casing \
(`userid` for `userId`), snake_case vs camelCase, the wrong but plausible \
method or field name typed first (`.length` for `.size`, `w2` for `w1`), wrong \
module prefix, arguments in the wrong order.
  - Brackets and quotes ()[]{}"'` are auto-paired by the editor, so a mistake's \
`original` and `typed` must not contain any of them. Never alter leading indentation.
Spread mistakes unevenly the way real typing goes: clusters when going fast \
through easy prose, fewer in careful passages.

noticed_after is how many more characters the person types after the mistake \
before noticing it: 0 for an immediate "oops", 1-4 typically, up to the end of the \
word or line for spelling/thinkos. Mistakes never span a line break, and the \
person always notices before pressing Enter, so a mistake on the last character \
of a line, or a dropped character (or a missing `;`) with noticed_after 0, is invisible - don't \
waste mistakes on those.

Pauses: places where this person would stop for a moment before continuing \
(planning the next sentence, recalling a function signature, deciding a variable \
name, before a tricky expression). Ordinary punctuation and line-break pauses are \
already handled - only add pauses that come from thinking about the content."""

COMPOSE = """

This person is composing the text, not copying it: they are writing it for \
the first time and it takes them a long while. Pauses can be long: 10-60 \
seconds to plan a new paragraph or an argument, a few seconds hunting for the \
right word.

They also revise their draft. Each revision is a first wording that they type \
and later change into the final text:
- kind "reword": a different word or phrase first ("good" before "compelling", \
"shows" before "suggests", a clumsier phrasing of the clause).
- kind "add": something left out of the first draft and added on rereading - \
an adjective, a qualifier ("often", "largely"), a short clause, a citation. \
`typed` is `original` without that part.
- kind "cut": something in the first draft that gets cut - a filler word \
("really", "very", "just"), a redundant phrase. `typed` is `original` with the \
extra part.
`original` is the exact final text at that point (never empty; include a \
neighbouring word when adding or cutting) and `typed` is the draft. With fix \
"backspace" they notice while still typing, delete back and retype (\
noticed_after up to the end of the clause); with fix "go_back" they finish \
more of the sentence (noticed_after: how many more characters, at most 80, \
never past the end of the line), then go back, change it in place and carry \
on. Revisions are about meaning and style, never typos."""

REVISION = {
    "type": "object",
    "properties": {
        "line": {"type": "integer", "description": "1-based line number"},
        "original": {"type": "string", "description": "exact substring of that line in its final form"},
        "occurrence": {"type": "integer", "description": "which occurrence of `original` in the line, 1-based"},
        "typed": {"type": "string", "description": "the first draft of that part"},
        "fix": {"type": "string", "enum": ["backspace", "go_back"]},
        "noticed_after": {"type": "integer"},
        "kind": {"type": "string", "enum": ["reword", "add", "cut"]},
    },
    "required": ["line", "original", "occurrence", "typed", "fix", "noticed_after", "kind"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "mistakes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer", "description": "1-based line number"},
                    "original": {"type": "string", "description":
                        "exact substring of that line that gets mistyped (may be empty for an extra insertion)"},
                    "occurrence": {"type": "integer", "description":
                        "which occurrence of `original` in the line, 1-based"},
                    "typed": {"type": "string", "description": "what the person actually types instead"},
                    "noticed_after": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["motor", "spelling", "thinko", "code", "syntax"]},
                },
                "required": ["line", "original", "occurrence", "typed", "noticed_after", "kind"],
                "additionalProperties": False,
            },
        },
        "pauses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "before": {"type": "string", "description":
                        "exact substring of that line the pause comes right before"},
                    "occurrence": {"type": "integer"},
                    "seconds": {"type": "number"},
                },
                "required": ["line", "before", "occurrence", "seconds"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mistakes", "pauses"],
    "additionalProperties": False,
}


@dataclass
class Mistake:
    pos: int            # index in the text where the mistake starts
    original: str
    typed: str
    noticed_after: int
    kind: str
    go_back: bool = False  # fixed later with the arrow keys rather than backspace


@dataclass
class Plan:
    mistakes: list[Mistake]    # including revisions (kinds reword, add and cut)
    pauses: dict[int, float]   # text index -> seconds to pause before typing it
    dropped: int               # suggestions that couldn't be placed


def _find(line: str, needle: str, occurrence: int) -> int:
    """Index of the n-th occurrence of needle in line, falling back to the first."""
    idx = -1
    for _ in range(max(occurrence, 1)):
        nxt = line.find(needle, idx + 1)
        if nxt < 0:
            break
        idx = nxt
    return idx if idx >= 0 else line.find(needle)


def resolve(text: str, raw: dict, code: bool) -> Plan:
    """Turn the model's line/substring anchors into positions, dropping anything
    that doesn't match the text or would be unsafe to replay."""
    lines = text.split("\n")
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)

    candidates, dropped = [], 0
    for m in raw.get("mistakes", []) + raw.get("revisions", []):
        n, original, typed = m["line"] - 1, m["original"], m["typed"]
        revision = "fix" in m
        ok = (0 <= n < len(lines) and original != typed and "\n" not in original + typed
              and "\t" not in original + typed)
        if ok and code and AUTO_PAIRED & set(original + typed):
            ok = False  # editors auto-close these; backspacing them is unpredictable
        idx = _find(lines[n], original, m["occurrence"]) if ok else -1
        indent = len(lines[n]) - len(lines[n].lstrip()) if ok else 0
        if idx < 0 or idx < indent or (not original and (idx == 0 or revision)):
            dropped += 1
            continue
        candidates.append(Mistake(starts[n] + idx, original, typed,
                                  max(0, min(m["noticed_after"], 80 if revision else 15)), m["kind"],
                                  revision and m["fix"] == "go_back"))

    mistakes, end = [], -1
    for m in sorted(candidates, key=lambda m: m.pos):
        if m.pos <= end:
            dropped += 1  # overlaps the previous mistake
            continue
        mistakes.append(m)
        end = m.pos + max(len(m.original), 1) - 1

    pauses = {}
    for p in raw.get("pauses", []):
        n = p["line"] - 1
        if not (0 <= n < len(lines) and p["before"]):
            continue
        idx = _find(lines[n], p["before"], p["occurrence"])
        if idx >= 0 and p["seconds"] > 0:
            pauses[starts[n] + idx] = float(p["seconds"])
    return Plan(mistakes, pauses, dropped)


def ask(text: str, code: bool, n_mistakes: int, persona: str, model: str,
        n_revisions: int = 0) -> dict:
    import anthropic

    numbered = "\n".join(f"{i + 1:>4}| {line}" for i, line in enumerate(text.split("\n")))
    kind = "source code" if code else "prose"
    mix = ""
    if code:
        motor = max(1, n_mistakes // 4)
        mix = (f" - at least {n_mistakes - motor} of them \"code\" or \"syntax\" mistakes "
               f"(a good share of each), and at most {motor} plain \"motor\" typos")
    system, schema = SYSTEM, SCHEMA
    if n_revisions:
        mix += f", about {n_revisions} revisions (spread through the whole text)"
        system += COMPOSE
        schema = {**SCHEMA, "properties": {**SCHEMA["properties"],
                                           "revisions": {"type": "array", "items": REVISION}},
                  "required": [*SCHEMA["required"], "revisions"]}
    prompt = (
        f"The person: {persona}\n\n"
        f"They are typing the following {kind} ({len(text)} characters). Choose about "
        f"{n_mistakes} mistakes{mix} and whatever thinking pauses fit. Line numbers are shown "
        f"before the '|' and are not part of the text.\n\n<text>\n{numbered}\n</text>"
    )
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason != "end_turn":
        raise RuntimeError(f"model stopped early ({message.stop_reason})")
    return json.loads(next(b.text for b in message.content if b.type == "text"))


def plan(text: str, code: bool, n_mistakes: int, persona: str, model: str,
         warn=lambda msg: print(f"warning: {msg}", file=sys.stderr),
         n_revisions: int = 0) -> Plan | None:
    """Get a mistake plan from Claude, or None (with a warning) if that fails."""
    try:
        import anthropic
    except ImportError:
        warn("the anthropic package isn't installed; using random typos")
        return None
    try:
        return resolve(text, ask(text, code, n_mistakes, persona, model, n_revisions), code)
    except (anthropic.AuthenticationError, TypeError) as e:
        # A client with no credentials at all raises TypeError at request time.
        if isinstance(e, TypeError) and "authentication" not in str(e):
            raise
        warn("no valid Anthropic credentials (set ANTHROPIC_API_KEY); using random typos")
    except (anthropic.AnthropicError, RuntimeError, ValueError, KeyError) as e:
        warn(f"LLM planning failed ({e}); using random typos")
    return None
