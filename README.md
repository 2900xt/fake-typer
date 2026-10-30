# fake-typer

Types a block of text (prose, an essay, code) into **whatever window has focus**,
the way a person would: uneven rhythm, pauses to think, typos, and backspacing to fix them.

It creates a real virtual keyboard with Linux `uinput`, so it works in every app on
Wayland (Hyprland, GNOME, KDE), X11, and even a TTY.

## Keybind + status bar (Hyprland)

1. Install the command: `uv tool install --editable .` (puts `fake-typer` in `~/.local/bin`).
2. Bind it in `~/.config/hypr/custom/keybinds.lua`:

   ```lua
   hl.bind("SUPER + ALT + V", hl.dsp.exec_cmd(HOME .. "/.local/bin/fake-typer toggle"))
   ```

3. Copy some text, click where it should go, and press **Super+Alt+V**. It waits
   until you let go of the keys, then types into the focused window. Press the
   keybind again to pause or resume, or tap V twice quickly (holding Super+Alt) to
   stop. Esc also stops. While you hold Super, Alt or Ctrl it holds off typing, so
   its keys never combine with yours into a shortcut.

Settings for keybind runs go in `~/.config/fake-typer/config.toml`. It holds the
same options as the command line, e.g. `wpm = 80`, `llm = true` or `persona = "..."`.
The API key goes in `~/.config/fake-typer/.env`.

**Progress:** while it runs, `$XDG_RUNTIME_DIR/fake-typer/status.json` holds the
state (`planning`/`typing`/`paused`/`done`/`stopped`/`error`), progress, time
left and a preview. It's rewritten at least every 2 s. `fake-typer status` prints it.
A Quickshell bar widget for end-4's illogical-impulse config (copy in `extras/quickshell-ii/`) is in
`~/.config/quickshell/ii/modules/ii/bar/FakeTyper.qml`. It shows a progress ring
and "Typing 42% · 1:23 left". It's display-only; control it with the keybind.

## Usage

```sh
uv run fake-typer essay.txt            # 5 s to focus the target window, then types
uv run fake-typer --clipboard -w 80    # type the clipboard at ~80 wpm
cat main.py | uv run fake-typer --strip-indent   # code into an auto-indenting editor
uv run fake-typer -n notes.txt         # dry run: preview it typing in this terminal
uv run fake-typer --llm main.py        # Claude decides where the mistakes go
uv run fake-typer --llm --persona "tired student, shaky speller" essay.txt
```

While typing: **Esc** stops, **F9** pauses/resumes.

Main options: `--wpm`, `--typo-rate` (0 for none), `--think-rate`, `--max-pause`,
`--mode auto|prose|code`, `--strip-indent`, `--delay`, `--seed`, `--llm`, `--persona`,
`--model`, `--compose`, `--compose-wpm`, `--revise-rate`. See `--help`.

### Long documents (`--compose`)

Prose of 250 words or more (an essay, say) is typed as if it were being written
for the first time, not copied. Turn this on or off with `--compose` / `--no-compose`.

- **Pace:** it types in bursts at `--wpm`, then stops to think: up to a minute
  before a new paragraph, a few seconds before a sentence, now and then a hunt
  for the right word. These pauses are stretched so the whole run averages
  `--compose-wpm` (default 20) including all the thinking and revising. A
  600-word essay takes about half an hour. A single pause is capped at
  `--max-pause` (90 s when composing).
- **Revising the draft:** about `--revise-rate` (default 1.5) times per 100 words,
  the first draft differs from the final text. Sometimes the typist notices while
  still typing, deletes back and rewrites. Otherwise they finish more of the
  sentence, reread it, go back with the arrow keys, fix it in place, and carry on
  from where they were. Without `--llm` this means leaving a word out and adding
  it back, or putting a filler word ("really", "just") in and cutting it. With
  `--llm`, Claude writes real revisions: a different word first (`shows` →
  `suggests`), a qualifier or clause added on rereading, a redundant phrase cut,
  and also suggests longer planning pauses where the argument turns.

Revisions only go back within the current paragraph, and at most 90 characters,
since the arrow keys are the only movement every app handles the same way.
Compose mode can't be combined with `--editor`.

### LLM-planned mistakes (`--llm`)

With `--llm`, Claude reads the whole text before typing starts and decides which
mistakes this person would make, where they'd make them, and how soon they'd notice.
It also picks where they'd stop to think. Besides key slips in awkward words, it
produces mistakes that come from understanding the content:

- prose: misspelling hard words (`recieve`), or writing `their` for `there`
- code: `lenght` for `length`, `userid` for `userId`, `=` for `==`, guessing
  `.length()` before `.size()`, a missing `:` that gets fixed at the end of the line

You get pauses before tricky expressions or new ideas. `--typo-rate` sets roughly
how many mistakes to ask for.

`--llm` needs `ANTHROPIC_API_KEY`. You can put it in a `.env` file instead of
exporting it:

```sh
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

The `.env` in the current directory is read first, then `~/.config/fake-typer/.env`,
which works from any directory. A variable already set in your shell takes
priority, and `.env` is git-ignored. It makes one API call per run (default model
`claude-opus-5-5`). If the call fails, it falls back to random typos. Each suggestion
is anchored to a line and a substring, and any that don't match the text are
discarded, so the output still always matches the input.

## How it works

- **`planner.py`** decides where mistakes and draft revisions happen. The chance goes up for symbols,
  digits, and shifted keys, and down for the first letter of a word and just after
  a correction. The mistake types are: hitting a neighbouring key, fat-fingering two
  keys, skipping a character, swapping two (`teh`), doubling a key, and Shift slips
  (`THe`). The typist often doesn't notice right away. They may type a few more
  characters, pause, backspace to the error, and retype. The final text always
  matches the input exactly (this is checked before typing starts).
- **`timing.py`** sets the rhythm. Gaps between keys are lognormal around your wpm,
  and the overall speed drifts in bursts and lulls. Common bigrams (`th`, `er`) are
  faster, keys on alternating hands are faster, and the same finger on different
  keys is slower. Shift adds time, and so do symbols. There are pauses after commas,
  sentences, and paragraphs, plus an occasional longer stop to think. Every key has
  a realistic hold time, keys overlap (rollover), and Shift goes down a moment before
  the letter.
- **`layout.py`** holds the US QWERTY geometry and finger map, which drive both of the above.
- **`llm.py`** asks Claude for mistakes and pauses (structured JSON output) and
  converts them to positions in the text, which the planner then replays.
- **`keyboard.py`** contains the uinput device, a terminal preview backend, and the Esc/F9 hotkeys.

## Notes

- You need write access to `/dev/uinput`, usually by being in the `input` group.
- The US keyboard layout is assumed. Smart quotes and dashes are converted to ASCII.
  Other characters with no key on the keyboard are skipped, with a warning.
- Code editors change typed code in several ways. In code mode (auto-detected),
  each line is finished off so it comes out exactly as in the input:
  - **Shift+End**, then a space and a backspace: deletes closing brackets the editor
    auto-inserted that are still after the cursor.
  - **Home**, then **Shift+Home**: selects whatever indentation the editor ended up
    with (auto-indent, or re-indenting on `else`/`}`), and the line's real
    indentation is typed over it. Leading indentation isn't typed at the start of a line.
  - **End**, then **Esc** if the line ends in a word: Esc closes any autocomplete
    popup, so Enter can't accept a suggestion (`in` → `in_channel`).

  All of this is harmless in a plain text box. Turn it off with `--no-editor`
  (`editor = false` in the config), e.g. for Vim-style editors, where Esc leaves
  insert mode. Soft-wrapped long lines can confuse Home/End.
