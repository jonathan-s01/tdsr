# TDSR project overview (working notes for patching)

Fork version 0.7.13.

Orientation map for TDSR, a console screen reader by Tyler Spivey (GPLv3).
Updated 2026-09-23 to cover the scrollback, ignored-symbols and voice-selection patches.

## Repo layout

```
tdsr/
  bin/tdsr                    dev launcher; adds repo root to sys.path, calls tdsr.__main__.main()
  pyproject.toml              flit build; console script tdsr = tdsr.__main__:main
  pylintrc                    only disables bad-indentation (the project uses TABS)
  readme.md                   user docs: install, keys, config, scrollback, ignored symbols, voices, plugins
  COPYING.txt                 GPL v3
  uv.lock
  tdsr/
    __init__.py               docstring + __version__
    __main__.py               raw mode on stdin, restores termios on exit
    tdsr.py                   the entire screen reader (~1100 lines after patching)
    tdsr.cfg.dist             defaults copied to ~/.tdsr.cfg on first run
    backend/
      __init__.py             empty
      mac.py                  macOS speech server (AVSpeechSynthesizer via pyobjc)
      speechdispatcher.py     Linux speech server (speechd.client SSIPClient)
```

## How it works

TDSR is a PTY man-in-the-middle. It forks a pty, execs your shell into the child, feeds
everything the child prints into a pyte terminal emulator so it has a shadow copy of the
screen to review, and passes the raw bytes through to your real terminal untouched.
Speech goes out over a small line protocol to a speech-server subprocess.

Flow: `bin/tdsr` -> `tdsr/__main__.py:main()` (saves termios, `tty.setraw(0)`) ->
`tdsr/tdsr.py:main(term_params)`, which does argparse, picks a backend, starts `Synth`,
seeds and reads `~/.tdsr.cfg`, `os.forkpty()`, builds `MyScreen` + `pyte.Stream`, installs
the default `KeyHandler` and plugin binds, sets up SIGWINCH, then loops on `select()` over
stdin, the pty fd, and a signal pipe, with the timeout driven by the next scheduled callback.

## Where to change things

### Keybindings
Three structures at the BOTTOM of `tdsr.py`:
- `COMMANDS` - command name -> zero-arg callable. This is the registry; adding an entry
  here plus one in `DEFAULT_KEYS` is all a new command needs.
- `DEFAULT_KEYS` - command name -> key spec string (`'alt+shift+u'`), same notation the
  config accepts.
- `FIXED_KEYS` - raw byte sequences that are not remappable (arrows, backspace, delete),
  because TDSR must observe the real keys programs use.

`parse_key(spec)` turns a spec into bytes. `build_keymap()` merges `DEFAULT_KEYS` with the
config `[keys]` section and is called from `main()`; the module-level `keymap` starts empty
and is populated there, so anything importing this module must call `build_keymap()` first.

Case in a spec is ignored and `shift` must be explicit, so `alt+g` and `ALT+G` are the same
key. A spec of `none`/`off` leaves the command unbound. Unparseable specs fall back to the
default; duplicate bindings are logged and the first one wins.

Double-press binds are the sequence doubled (`alt+k alt+k`), resolved in
`KeyHandler.process` within `REPEAT_KEY_TIMEOUT` (0.5 s). Handlers can return
`KeyHandler.PASSTHROUGH` (also send the bytes to the child) or `KeyHandler.REMOVE`.

### Modal handlers (a stack)
`state.key_handlers`; only the top one receives input.
`KeyHandler` (default), `ConfigHandler` (alt+c), `CopyHandler` (alt+v),
`BufferHandler` (collects typed text until Enter, then calls `on_accept`).

### Review cursor and reading
Position is `state.revx` / `state.revy`. **`revy` can now be negative, meaning scrollback.**
- Lines: `sayline(y)`, `prevline`, `nextline`, `topOfScreen`, `topOfHistory`,
  `bottomOfScreen`, `bottomOfBuffer`
  (alt g / alt h; alt t and alt b were avoided because readline binds them)
- Words: `sayword(spell=False)`, `prevword`, `nextword`, `move_prevchar`, `move_nextchar`, `get_char`
- Chars: `saychar(y, x, phonetically=False)`, `prevchar`, `nextchar`, `startOfLine`, `endOfLine`, `skip_to_previous_char`

### Screen accessors (added by the scrollback patch)
Anything reading screen text must go through these rather than touching
`screen.buffer[y][x].data`, so it works in scrollback as well as on screen:
- `line_text(y)` - full text of a review line
- `cell(y, x)` - one character; pads short history lines with spaces
- `history_line(y)` - scrollback text only; y is negative, -1 is most recent
- `min_y()` - topmost review line, `-len(state.history)`

### Speech output
- `say(text, force_process_symbols=False)` - strips, applies `filter_ignored`, then symbol
  substitution, sends `s<text>`. Returns early if nothing is left to speak.
- `say_character(ch)` - symbol name if the codepoint is in `[symbols]`, else `l<ch>`.
  **Deliberately not filtered**, so explicit character review always tells the truth.
- `silence()` - sends `x`.
- `sb()` - flushes `speech_buffer` (accumulated program output) through
  `replace_duplicate_characters_with_count()` then `say()`.
- `filter_ignored(text)` - replaces runs of ignored characters with one space, collapses gaps.

Speech server protocol (stdin, one command per line, utf-8):
```
s<text>    speak
l<char>    speak as a character
x          stop now
r<int>     rate
v<int>     volume 0-100
V<value>   voice: index, name, or identifier (mac.py only; speechdispatcher.py has no handler)
```
`Synth.send()` restarts the subprocess once on BrokenPipeError. `Synth.start()` re-applies
remembered rate/volume/voice.

### MyScreen (pyte.Screen subclass) - where output becomes speech
Overrides: `index` (scrollback capture), `set_margins`, `draw`/`draw2`, `tab`, `linefeed`,
`backspace`, `report_device_status`, `reset_mode`/`set_mode` (alt screen 1049),
`erase_in_display`, `select_graphic_rendition`, `cursor_up`, `scroll_down`, `scroll_up`,
`cursor_position`, `cursor_to_line`.
`draw2` suppresses key echo by comparing the drawn char to `lastkey`.
To change what gets spoken as output arrives, edit `draw2` / `linefeed` / `sb`.

### Config
`CONFIG_FILE`, chosen in `main()` by `choose_config_file()` (see section 17); normally
`~/.tdsr.cfg`, seeded from `tdsr/tdsr.cfg.dist`. Sections `[speech]`, `[symbols]`,
`[commands]`, `[plugins]`. Defaults live in `State.__init__`. Every setter writes the value
then calls `state.save_config()`, which rewrites the whole file.

`[speech]` keys: `process_symbols`, `key_echo`, `cursor_tracking`, `line_pause`,
`repeated_symbols`, `repeated_symbols_values`, `prompt`, `rate`, `volume`, `cursor_delay`,
plus the patched-in `ignore_symbols`, `ignore_symbols_ranges`, `scrollback_lines`,
`cursor_settle_max`, `voice` (and legacy `voice_idx`, still read).

To add a setting: default in `State.__init__`, read it where used, add a `ConfigHandler`
key and `set_*` method, document it in readme.md.

### Plugins
`handle_plugin(name)` imports `plugins.<name>`, scrapes screen lines backwards until the
prompt/command regex matches, calls `mod.parse_output(lines)` and speaks the results.
Bound from `[plugins]` as `module = letter` -> alt+letter. `[commands]` holds the regex of
the command that produced the output.

### Scheduling
`schedule(timeout, func, set_tempsilence=False)` -> `state.delayed_functions`;
`run_scheduled()` runs due entries; `time_until_next_delayed()` feeds the select timeout.
Arrow keys no longer use this directly; see Cursor read debouncing below.

### Clipboard
`handle_clipboard` (alt+r twice: select, then copy), `copy_text`, `copy_to_clip`
(`pbcopy` / `wl-copy` / `xclip -selection clip`).

## Patched features

### 1. Scrollback
`state.history` is a `collections.deque` of plain strings, `maxlen` from
`scrollback_lines` (default 1000, 0 disables). `MyScreen.index()` captures the line at the
top margin before pyte discards it, but only when the scroll region starts at row 0 and the
alternate screen is not active, so vim and less don't pollute history.

Review is seamless: `prevline` walks past row 0 into negative `revy`, says "history" the
first time it crosses, and "top" only at the true oldest line. `nextline` says "screen"
coming back. `alt shift u` twice (`topOfHistory`) jumps to the oldest line. Cursor tracking
pulls you back to the live screen whenever new output arrives.

While you are sitting in history and new lines scroll in, `index()` decrements `revy` so
the review cursor stays anchored to the same text rather than drifting.

### 2. Ignored symbols
`State.build_ignore_re()` compiles `ignore_symbols_ranges` (comma-separated hex codepoints
and low-high ranges) into a character-class regex stored on `state.ignore_re`.
`filter_ignored()` is applied inside `say()`, so it covers line, word and output reading but
not `say_character`. Defaults: box drawing 2500-257F, block elements 2580-259F, geometric
shapes 25A0-25FF, powerline E0B0-E0BF. Arrows are left alone on purpose.
Global on/off is `ignore_symbols`, toggled with `i` in the config menu.

### 3. Cursor read debouncing
Arrow keys no longer use `schedule(CURSOR_TIMEOUT, ...)`. They call
`schedule_cursor_read(func)`, which stores a single pending read in
`state.pending_read` with two deadlines:
- `state.pending_deadline` - when to fire. Starts at the cap, because until the program
  emits something there is nothing new to read.
- `state.pending_limit` - press time + `CURSOR_SETTLE_MAX`, never exceeded.

`defer_cursor_read()` is called from the main loop right after `stream.feed()`; it pushes
the deadline to now + `CURSOR_TIMEOUT`, capped at the limit. So the read fires once output
has been quiet for `cursor_delay`, or at the cap, whichever comes first.
`cancel_cursor_read()` is called from `process_input` so a new keypress discards an
outstanding read instead of producing double speech.

`run_scheduled()` services the pending read before the ordinary delayed functions, and
`time_until_next_delayed()` now returns `min()` of all deadlines including the pending
read. It previously returned `delayed_functions[0]`, which is not necessarily the soonest
because the list is appended to and never sorted; that made `select()` oversleep and added
latency on top of the stale-read problem.

Tradeoff to remember: when a key produces no output at all (left arrow at column 0), the
read now waits the full `cursor_settle_max` instead of 20 ms. The screen is unchanged in
that case so the text spoken is still correct, just later.

### 4. Voice selection (macOS)
`tdsr --list-voices` prints index, name, language and identifier. It runs before the pty
fork and restores cooked mode first so the output is readable.
`mac.py:find_voice()` resolves a `V<value>` payload as an index, exact identifier, exact
name, or partial name. `Synth.set_voice` sends the raw string, and the choice is saved as
`voice` in config (identifier recommended, since indexes shift). Legacy `voice_idx` is still
read at startup and replaced with `voice` the next time a voice is set.

## Conventions and gotchas
- Indentation is TABS: one for class-body defs, two for method bodies, three for nested.
  Module-level function bodies use one tab. Match exactly.
- Everything is global module state: `state`, `screen`, `synth`, `speech_buffer`, `lastkey`.
- Coordinates are `(y, x)`, but `copy_text` takes `start_y, start_x, end_y, end_x`.
- `cell(y, x)` can return `''` for the second half of a wide character.
- `say()` strips, so whitespace-only strings speak nothing.

### 5. Find, prompt jumping, repeat, where am I (v0.2.0)
- `find` / `find2` prompt through `BufferHandler`; `find_next` / `find_previous` walk
  matches over history and screen together via `line_text()`, case insensitively, wrapping
  once. `find_in_line()` does the per-line work in both directions. Search text persists in
  `state.find_text`.
- `prev_prompt` / `next_prompt` scan for `get_prompt_re()`, which reads the `prompt` config
  value, treats the shipped `.*` as unset, and falls back to `DEFAULT_PROMPT_RE`. The
  compiled pattern is cached on `state.prompt_re` and must be invalidated when config is
  re-read.
- `move_to_line()` centralises the history/screen boundary announcement used by prompt
  jumping.
- `say()` and `say_character()` record `state.last_utterance`; `repeat_last` and
  `spell_last` replay it. Both restore the value afterwards so repeat stays repeatable.
- `where_am_i` reports position, total, and column, phrased differently in history.
- `all_lines()` returns every reviewable line as `(y, text)`; currently used by tests and
  intended for say-all.

### 6. Say all, highlighting, indentation, punctuation (v0.3.0)
**Speech server protocol is now bidirectional.** `Synth` opens the server with
`stdout=PIPE`; `Synth.fileno()` and `Synth.read_events()` expose it, and the main loop
selects on it. The server writes one word per line: `done` when an utterance finishes,
`cancelled` when stopped. `mac.py` implements this with an `AVSpeechSynthesizer` delegate;
`speechdispatcher.py` tries `set_notification_on` and degrades quietly if unsupported.

- `SayAll` holds a cursor and speaks one line per `done` event, skipping blanks.
  `say_all_watchdog()` advances anyway after `SAY_ALL_FALLBACK` seconds so a server that
  never reports completion can't stall it. Any keypress calls `stop_say_all()` from
  `process_input`.
- `say()` gained a `ui` flag. TDSR's own announcements go through `say_ui()` and are never
  given punctuation names, otherwise "indent 4, return 1" reads as "indent 4 comma return
  1". `say_with_prefix()` composes a literal prefix with processed screen content, and
  `process_for_speech()` exposes the processing without speaking.
- Punctuation levels (`none`/`some`/`most`/`all`) via `State.build_punctuation_re()`,
  cached on `state.punctuation_re`. Only characters that have a name in `[symbols]` are
  used. `process_symbols` still takes precedence when explicitly enabled, preserving the
  original behaviour. Note that substitution collapses double spaces, but spelling
  (`force_process_symbols`) deliberately does not, since its spacing is the point.
- `indent_prefix()` measures leading whitespace in columns (tabs to the next multiple of
  8) and divides by `indent_unit`.
- `logical_line_bounds()` / `row_is_full()` detect wrapped lines by checking whether the
  row above fills the final column. History lines are never joined, since they are already
  stored as complete lines.
- `highlighted_regions()` reads `.reverse` off pyte's character cells. Screen only;
  scrollback stores text without attributes.
- `MyScreen.bell()` announces the terminal bell. `check_command_done()` announces when
  output has been quiet for `COMMAND_DONE_TIMEOUT`, suppressed during say all.
- `state.pass_next` makes `process_input` forward one keypress untouched.
- `time_until_next_delayed()` now also bounds the say-all watchdog and command-done timer,
  so neither can oversleep.

### 7. Audit pass (v0.3.1)
- **`copy_text` corrupted multi-line selections.** It ordered the two columns
  independently of the two lines, so selecting column 50 of one line to column 10 of a
  later line produced a completely different region. Upstream bug, present since the
  original. It now orders the `(y, x)` pairs together and treats the selection as an
  ordered pair rather than a rectangle.
- **`copy_to_clip` leaked a zombie per copy** and silently did nothing on an unknown
  platform. It now reaps (`wait(timeout=2)`, then `kill()`), polls rather than waits for
  `wl-copy` which stays resident by design, and raises when there is no clipboard command
  so the caller can say "Failed".
- **Say all stalled on unspeakable lines.** A line of box drawing filters to nothing, so
  `say()` never sent anything and no completion event arrived; say all sat waiting for the
  6 second watchdog on every table border. `say()` now returns whether it actually spoke,
  and `SayAll` skips straight on when it didn't.
- Copy line follows wrapped lines (`logical_line_bounds`), completing the logical-lines
  feature, and copy mode gained `a` for scrollback plus screen.
- `clamp_review()` added and called from the navigation entry points. Scrollback can
  shrink underneath the review cursor, which previously let `where_am_i` report
  "history line -3 of 1".

### 8. Selection semantics (v0.3.2)
Selection is a **stream**, not a rectangle or a set of lines: `handle_clipboard` anchors
`(copy_y, copy_x)` and `copy_text` takes everything between that point and the review
cursor, as an ordered pair.

The usability problem was that `prevline`/`nextline` change `revy` but deliberately leave
`revx` alone, and `sayline` reads the whole line, so the column is invisible. Users
anchored selections partway into a line with no way to know.

- `handle_clipboard` now announces the anchor via `describe_position()`, and reports the
  size of the result via `describe_copied()`. `copy_text` returns the text it copied.
- `selection_mode` config: `stream` (original) or `lines`. Line mode forces the columns to
  the full width and orders by row only, so direction stops mattering. Toggled with `m` in
  the config menu.
- `cancel_selection` (alt shift r) drops an anchor without copying.

Documentation drift fixed in the same pass: the readme claimed alt shift u twice jumped to
the oldest scrollback line, which stopped being true when v0.2.0 moved to the command
registry (alt g replaced it). Also added the previously undocumented `alt x`,
`alt shift m`, `alt >`, config menu `e` and `c`, and the `key_echo`, `cursor_tracking` and
`line_pause` settings. `doccheck.py` in the working notes cross-checks `DEFAULT_KEYS`,
`ConfigHandler.keymap`, `CopyHandler.keymap` and the config defaults against the readme;
worth rerunning after any change that adds a key or a setting.

### 9. Field feedback fixes (v0.4.0)
All five from real use on macOS.

- **`last_content_y()`**: the screen is always a full rectangle, so a prompt part way down
  a 30 row window left 26 blank rows that `nextline` happily walked through. Navigation now
  stops at the last row with text, but never above `screen.cursor.y`, so a full screen
  editor with the cursor on a blank line below the text still works. Config
  `stop_at_last_line`.
- **Horizontal arrows announce a line change.** `arrow_left`/`arrow_right` capture
  `screen.cursor.y` at keypress and `say_cursor_char()` compares it when the read fires; a
  changed row reads the whole line instead of a character. This only became visible after
  the v0.1.0 debounce fix: previously the read happened before the redraw, so TDSR spoke
  the stale position and appeared not to move at all.
- **`strip_prompt_prefix()`** drops a `user@host` prefix when reading a prompt line, since
  it is identical on every history entry. Only applies when the line matches the prompt
  regex AND something follows the prompt, so an empty prompt still reads in full. Pattern
  is `prompt_strip_pattern`; cached on `state.prompt_strip_re`, invalidate on config read.
- Selection end announces its position, matching the start.
- **Shift+arrow selection**, off by default (`shift_arrow_selection`). The handlers return
  `PASSTHROUGH` when disabled so the sequences reach the program untouched. When enabled
  they anchor at the review cursor if no selection is running, then extend. Note this takes
  shift+arrows away from nano's own marking.
- `MenuHandler` is a reusable browsable list (up/down/escape); `browse_commands` builds its
  items from the live bindings so remapping is reflected. `ConfigHandler.CONFIG_HELP` must
  be updated whenever a config menu key is added; `doccheck.py` will catch a missing readme
  entry but not a missing help entry.

### 10. AI assistant (v0.5.0)
New module `tdsr/ai.py`, deliberately isolated; `tdsr.py` only calls into it. No new
dependencies, `urllib.request` from the stdlib.

Why it works over ssh: TDSR is already a PTY intermediary, so it has the screen
(`line_text` over history plus screen) and it can write to the child (`write_all(child_fd,
...)`). Those are the only two capabilities the assistant uses, so it is indifferent to
how many hops away the actual shell is. Nothing is installed remotely.

Architecture:
- `AIClient` runs the request on a daemon thread and writes one byte to `ai_pipe` when
  done. The main loop selects on that pipe alongside stdin, the child, the signal pipe and
  the synth. The screen reader never blocks on the network.
- `state.ai_answer` keeps the reply as lines so `review_ai_answer` can browse it through
  `MenuHandler`. Speech is ephemeral; a blind user needs the reply to persist.

Safety, in order of how much they actually matter:
1. **`ConfirmHandler` is the only path to the child.** `accept()` is the single call to
   `write_all(child_fd, ...)` in the whole AI path, and it is reached only by a keypress.
   It writes the command *without* a newline, so the user still presses enter. There is
   deliberately no setting to bypass this, and the tests assert that rejection, escape,
   spelling, repeating and unknown keys all type nothing.
2. **Prompt injection is real and unsolved.** Terminal contents are attacker-controlled if
   anything can write to the terminal. The system prompt forbids following instructions
   found in the contents and the contents are fenced in the request, which helps but is
   not a boundary. The confirmation step is the boundary.
3. `redact()` strips common secret shapes before sending. Pattern matching only; it cannot
   recognise a secret that reads as prose. Documented as a safety net, not a guarantee.
4. `DANGEROUS` refuses a short list outright (recursive rm, mkfs, dd to a device,
   shutdown/reboot, fork bomb, Proxmox destroy). A backstop, not security.

Deliberately not built: automatic file rewriting. The cat-edit-write-back round trip the
user described works, but writing a whole file through a PTY heredoc risks a partial write
to a remote file, which is a bad failure to recover from over ssh. A confirmed command
already covers the use case with less machinery and a clearer failure mode.

Note `parse_key` only uppercases letters, so `alt+shift+/` collides with `alt+/`; bind the
shifted character directly (`alt+?`). The duplicate-binding test catches this.

### 11. AI context control (v0.5.1)
- Default context dropped from 200 lines to `DEFAULT_CONTEXT_LINES = 60`. 200 was chosen
  carelessly; a screenful is the sensible unit and keeps requests small.
- `context_mode`: `screen` (default), `scrollback`, `selection`. Selection mode reuses the
  existing `copy_y`/`revy` anchor, so marking a region with alt r then asking sends only
  that region. This is the real privacy control: it bounds what leaves the machine to what
  the user explicitly chose.
- `apply_budget()` enforces line and character ceilings, trimming from the front because
  the end of the buffer is where errors are. A single line longer than the whole char
  budget is truncated rather than dropped, otherwise a wide terminal with one long line
  sent nothing at all (caught by test).
- `ai_context()` returns `(text, description, error)`. The description is spoken on every
  request ("thinking, sending 34 lines"), so what left the machine is never a mystery.
  `describe_ai_context` reports it without sending; unbound by default since not everyone
  wants another key.
- Trimmed context is prefixed with a note to the model, so it does not confidently answer
  about lines it never received.

System prompt rewritten and substantially expanded. It now states what the snapshot is and
that it may be truncated, that the model cannot run or read anything and has no memory,
that output is spoken aloud (no markdown, lead with the answer, don't describe layout),
that it should prefer reversible and widely available commands and say so plus suggest a
backup when a command is destructive, and that terminal contents are data which must never
be treated as instructions. Tests assert each of these clauses is present, so a future edit
that drops one fails loudly.

### 12. Context reporting and profiles (v0.6.0)
**Size reporting.** `estimate_tokens()` divides by `CHARS_PER_TOKEN = 4.0`, an admitted
approximation used only for the pre-send figure; everything derived from it is spoken with
"roughly". Real counts come from the API `usage` block and accumulate on the client as
`total_input`, `total_output`, `request_count`. `describe_ai_context` (alt shift d) gives
lines, characters, estimated tokens and the active profile without sending; shift u in the
config menu reports real usage.

One subtlety: a response with no `usage` block must record `None`, not `(0, 0)`. Recording
zeros would silently understate the session total rather than admitting the count is
unknown. Caught by test; the check is `'input_tokens' in usage` rather than a truthiness
test on the parsed tuple.

**Profiles.** Named instruction sets in `~/.tdsr-ai-profiles`, chosen with alt shift f and
sent with every request. `parse_profiles()` reads an INI-like format but is hand written
rather than using configparser, because profile bodies are free text that may contain `=`,
`:`, `#` and `[...]` without being parsed as structure. Empty sections are dropped; text
before the first header is ignored so users can leave notes.

`build_system_prompt()` appends the profile under a heading that explicitly marks it as the
user's own standing instructions which may be trusted, in contrast to the terminal snapshot
which is marked untrusted. That asymmetry is the reason profiles live in a file the user
controls rather than being scraped from anywhere.

The chosen profile is persisted as `ai.profile` and restored by `apply_saved_profile()`
at startup, matching case insensitively and logging rather than failing when the named
profile has since been deleted.

`ProfileHandler` offers "no profile" as the first option rather than requiring a separate
clear command, and `r` reads the full body so a profile can be checked before use.
`CreateProfilesHandler` writes `EXAMPLE_PROFILES` only when the file does not already
exist.

### 13. Audit fixes (v0.6.1)
Six defects, all found by probing v0.6.0 rather than by use. Worth noting that every one
of them is an *interaction* bug between features built in separate versions, which is the
category that unit tests written alongside a feature tend to miss.

- `handle_ai_result` did not call `stop_say_all`, so a reply arriving during a continuous
  read produced two overlapping voices.
- `propose_command` pushed a `ConfirmHandler` unconditionally, stacking it on top of an
  open `MenuHandler`. The user could accept a command they had forgotten about. It now
  checks `len(state.key_handlers) > 1` (index 0 is always the default handler) and holds
  the command for `pending_command` (alt shift k) instead.
- A model reply is usually one paragraph, so `review_ai_answer` presented it as a single
  unreadable item. `split_for_review()` splits on sentence boundaries at 200 characters
  when there are no line breaks to use.
- Profiles were sent uncapped with every request, making an oversized one a recurring
  cost. `cap_profile()` trims to `MAX_PROFILE_CHARS`, keeping the *start* (unlike terminal
  context, which keeps the end) because a profile's opening sentences carry the intent.
- Profiles were not run through `redact()`. They are user authored, but a profile file
  copied between machines can still carry a key.
- No way to abandon a slow request. `AIClient.abandon()` marks the in-flight request so
  its result is dropped when it lands. The first implementation only handled the busy
  case, so a reply that arrived between the question and the cancel was still delivered;
  it now discards an unclaimed result too. Race caught by test.

### 14. Column awareness (v0.7.0)
Columnar output (`ls`, tables) is padded with runs of spaces. Line reading ran the columns
together; word navigation gave no signal that a jump had crossed one.

- `split_columns()` splits on runs of `column_gap` spaces (default 3). Three is chosen
  because prose never contains three consecutive spaces, so ordinary text is never broken
  up. A configured value of 1 is clamped to 2, since 1 would split on every word.
- `say_columns()` processes each segment through `process_for_speech()` **first**, then
  joins with the marker and emits via `say_ui`. The order is the whole trick: joining
  first would let punctuation processing turn the marker comma into the spoken word
  "comma" whenever `punctuation_level` includes commas. Tested explicitly.
- It also stays **one utterance per line**, which matters because `SayAll` advances on one
  completion event per line. Emitting a separate utterance per column would desynchronise
  say all.
- `gap_between()` measures the longest space run between two columns; `crossed_column()`
  compares it to the threshold. `announce_column_jump()` is called by `nextword`/`prevword`
  after the move, comparing start and end positions, so it is direction independent.

Cursor semantics confirmed while implementing (worth recording, it is not obvious):
`sayword()` saves and restores `revx`/`revy`, so alt k never moves the cursor.
`nextword`/`prevword` both leave the cursor on the **first character of the word just
read**, in both directions.

### 15. Stability pass
- `state.last_drawn` was read in `cursor_position` but never initialised, so an escape
  sequence arriving before any character was drawn raised `AttributeError`. Initialised in
  `State.__init__`, and the guard now uses `getattr` and rejects `''` (which `ord()` can't take).
- `write_all()` replaces bare `os.write()` on the tty and the pty. `os.write` may write only
  part of the buffer, which silently truncated terminal output; it also retries on `EINTR`.
- Backends raised `IndexError` on an empty protocol line, killing the speech server and
  forcing a respawn. Both now ignore empty lines, and numeric parsing is guarded.
- `handle_plugin` used `mod` after a failed import (`UnboundLocalError`); it now returns a
  handler that reports the failure. Bad `prompt` / `commands` regexes are caught too.
- `State.get_bool()` / `State.get_number()` replace direct `getboolean` / `int` / `float`
  reads, so a hand-edited `~/.tdsr.cfg` can't stop TDSR from starting. `build_symbols_re`
  skips non-numeric and out-of-range keys; a `configparser` error is logged, not fatal.
- `BufferHandler` decodes with `errors='replace'`, supports backspace and escape-to-cancel,
  and tolerates a missing `on_accept`.
- `Synth` survives a speech server that won't start (`pipe` stays `None`), and `close()`
  no longer hangs, using `wait(timeout=2)` then `kill()`.
- `MAX_SPEECH_BUFFER` caps `speech_buffer` so output with no newline can't grow unbounded.
- Dead module globals (`newbytes`, `in_escape`, `escsec`) removed.

### 16. Field fixes (v0.7.1)
Three from real use on macOS.

- **Voice selection did nothing with VoiceOver on.** `mac.py` set
  `prefersAssistiveTechnologySettings` to True on every utterance, and with VoiceOver
  running that makes VoiceOver's voice override the utterance's. It is now
  `voice is None`, so a chosen voice wins and the unset case still follows VoiceOver.
  Consequence: once a voice is chosen, TDSR's own rate and volume apply (VoiceOver's
  rate no longer does). An empty `V` payload clears the voice back to `None`.
- **Voice menu.** Config `V` opens `VoiceMenuHandler` when `mac_backend` is true (bundled
  macOS backend, no `-s`). The list comes from `mac.py --list-voices-tsv` (a `lang` line,
  then `identifier\tname\tlanguage`) via `list_system_voices()` / `parse_voice_list()`,
  sorted with the system language first. Moving previews by sending `V<identifier>` before
  speaking the name, so each voice announces itself. Enter saves, escape restores the
  original, letters jump by name. Item 0 is "System voice" (identifier `''`), which removes
  the `voice` key. Without a list it falls back to the old typed `BufferHandler`.
- **Nested launch guard.** `handle_child` sets `TDSR_ACTIVE=1` (`NESTED_ENV`) in the child.
  `main()` returns early, silently, when it is set, so `tdsr` in `.zshrc` no longer recurses.
  Silent on purpose: the nested call happens on every window open. Never recommend
  `exec tdsr` in a startup file; the guard's early exit would then close the shell.
- **Prompt stripping reads only the command.** `DEFAULT_PROMPT_STRIP_RE` now reaches the
  prompt character, anchored on a leading `user@host` so ordinary text is not touched.
  `\x25` is used for `%` because `ConfigParser` interpolation rejects a literal percent;
  `get_prompt_strip_re` now also catches `configparser.Error` (the readme's old example
  pattern with a literal `%` would have raised on arrow up). `LEGACY_PROMPT_STRIP_RES`
  upgrades the old default, which first run had written into `~/.tdsr.cfg`.
  `strip_prompt_prefix` skips the alternate screen, so nano/vim/less lines are never stripped.

### 17. Settings location and AI defaults (v0.7.2)
- **Per-checkout settings.** Running a development copy with `uv run` shared `~/.tdsr.cfg`
  with the installed copy, and since every setter rewrites the whole file each copy kept
  overwriting the other. `choose_config_file()` picks, in order: `--config`, `TDSR_CONFIG`,
  `<checkout>/tdsr.cfg` when `source_checkout_root()` finds `pyproject.toml` beside the
  package, else `~/.tdsr.cfg`. `seed_config_file()` creates a missing project file as a copy
  of `~/.tdsr.cfg` (home is read, never written), otherwise from the dist defaults. `/tdsr.cfg`
  is git ignored because it can hold `api_key`. The greeting says "using project settings".
  `CONFIG_FILE` is a module global assigned in `main()`; `save_config` reads it.
- **AI model.** `claude-sonnet-4-5` is deprecated, retiring 2026-11-30. Default is now
  `claude-sonnet-5-5`. First run had written the old model into users' config files, so
  `upgrade_model_settings()` swaps exactly the old default (not a user's own choice) and
  saves. Sonnet 5.5 thinks by default and `max_tokens` includes thinking, so the default cap
  went from 1024 to 4096; the old 1024 is raised along with the model swap. A reply with no
  text and `stop_reason == max_tokens` now says to raise the cap. Do not send
  `thinking: disabled` or `temperature`: Sonnet 5.5 returns 400 for both.
- Optional `effort` (`low`/`medium`/`high`) is sent as `output_config.effort`; empty sends
  nothing. HTTP errors other than 401/429 now speak the API's own message, truncated.

### 18. Other providers and the key file (v0.7.3)
- **Providers.** `ai.provider` is `auto` (default), `anthropic`, `openai` or `azure`.
  `detect_provider()` guesses from the URL (azure host, `api-version=`, `/openai/deployments/`
  -> azure; anthropic host or `/messages` -> anthropic; `/chat/completions`, `openai`, or a
  trailing `/v1` -> openai; else anthropic). `complete_endpoint()` appends
  `/chat/completions` or `/messages` to a base URL so users can paste what they were given.
  `_request` dispatches to `_request_anthropic` / `_request_openai`; `_post` does the HTTP.
  OpenAI format sends the system prompt as the first message, `reasoning_effort` for
  `effort`, and reads `choices[0].message.content` (string or parts list), `prompt_tokens`
  / `completion_tokens`, `finish_reason == length`.
- **Token parameter.** OpenAI's current models reject `max_tokens`; many compatible servers
  don't know `max_completion_tokens`. Start with the new name; on a 400 whose message names
  it, swap and retry once, remembering the choice in `token_param`. Any other 400 is
  re-raised with its body replayed through `io.BytesIO` so the spoken error keeps the message.
- **Auth.** Defaults: anthropic `x-api-key`, azure `api-key`, openai `Authorization: Bearer`.
  `auth_header` overrides the name; `authorization` (any case) always gets `Bearer`.
- **TLS.** `ca_bundle` builds an `ssl` context from a PEM file. Python on macOS does not use
  the keychain, so corporate TLS inspection fails with CERTIFICATE_VERIFY_FAILED; that case
  now gets a spoken message pointing at `ca_bundle`.
- **Key file.** `ai_api_key()` order: env var, key file, `ai.api_key`. The key file is
  `api_key_file` if set, else `default_key_file()`: `~/.tdsr.key` beside `~/.tdsr.cfg`, or
  `tdsr.key` beside a folder copy's `tdsr.cfg`. `read_key_file()` takes the first line that is
  not blank or `#`, and logs if the file is group/world readable. `/tdsr.key` and `*.key`
  are git ignored. When enabled but keyless, `ask_ai` says which file it looked in.
- Bundles must never include `tdsr.key` or `tdsr.cfg`.

### 19. Internal gateways (v0.7.4)
- `ca_bundle = keychain` (`KEYCHAIN`): `keychain_certificates()` runs `security find-certificate
  -a -p` on the search list and on `MAC_ROOTS`, and `_ssl_context()` loads the PEM via
  `cadata` on top of Python's defaults. Python never reads the keychain, so an organisation's
  own CA (trusted by Safari) is otherwise unknown. Empty result falls back to defaults.
- `verify_tls = False` gives an unverified context and logs a warning. Documented as last
  resort; IT samples often use `curl -k` / `verify=False` out of habit.
- `timeout` (default `REQUEST_TIMEOUT`, 60) is per client. `_ssl_context()` is cached.
- `describe_error()` is module level, shared by `_worker` and `AIClient.test()`. Adds 403 as
  auth failure, FastAPI `detail`, string `error`, timeouts, missing ca file, bad JSON, and
  mentions VPN on network failure.
- `configure_ai(client)` in tdsr.py replaces the inline block in `main()`.
- `tdsr --test-ai` (`test_ai()`): restores cooked mode, reads the chosen settings file, sends
  one synchronous request, prints settings file, provider, address, model, key status and
  the result as plain lines. Exits before the nested guard so it works inside TDSR.

### 20. Spoken key names in help (v0.7.5)
`key_spec_for` only replaced `+`, `,` and `.`, so the command browser said "ask ai, alt /" and
the synthesiser dropped the slash, leaving just "alt". `speak_key_spec()` now names symbols
from a fixed `KEY_NAMES` table (not `[symbols]`, so wording is stable), capitalises lone
letters so they are read as letter names, and says "twice" for double presses.

### 21. Key lookup, capitals, deleted pitch (v0.7.6)
- A relative `api_key_file` was resolved against the current directory, so `tdsr.key` only
  worked when started from the project folder. `ai_key_file()` now joins it to the settings
  file's folder. `ai_key_candidates()` adds `~/.tdsr.key` as a fallback; `--test-ai` lists both
  and says where the key was found.
- New protocol line `P<multiplier>` sets pitch for the next `s`/`l` utterance only. mac.py:
  `setPitchMultiplier_` (ignored under VoiceOver when `prefersAssistiveTechnologySettings` is
  true, i.e. no chosen voice). speechdispatcher.py: maps to -100..100 via `set_pitch`, resets to 0.
- `say_character(ch, deleted=False)`: `capitals` (`cap`/`pitch`/`both`/`off`, default cap),
  `cap_pitch` 1.4, `lower_pitch_deleted` True, `deleted_pitch` 0.7, both clamped 0.5..2.0.
  Deleted pitch wins over cap pitch; "cap" is still said. Backspace, delete, and BufferHandler
  backspace pass `deleted=True`. Config menu: `a` cycles capitals, `D` toggles deleted pitch.

### 22. Settings survive versions (v0.7.7)
- Cause of the reported revert: the dev config lived in `<checkout>/tdsr.cfg`, so a fresh
  unpack had none and was re-seeded from `~/.tdsr.cfg`. Separately, `save_config` wrote the
  whole in-memory config, clobbering hand edits made while running.
- Folder copies now use `DEV_CONFIG` (`~/.tdsr-dev.cfg`) and `DEV_KEY_FILE`. `seed_config_file`
  copies an old `<checkout>/tdsr.cfg` byte for byte if present. `ai_key_candidates()` tries
  api_key_file, default key file, `<checkout>/tdsr.key`, `~/.tdsr.key`.
- `State.mark_loaded()` snapshots raw values after `load_config_file()`. `save_config()` re-reads
  disk with `interpolation=None`, applies only values differing from the snapshot (and
  removals), writes, re-snapshots. Defaults never written unless changed.
- Unparseable file: `backup_unreadable_config()` copies it to `<path>.unreadable-<stamp>` before
  any write; `state.config_problem` makes the greeting say so. Also triggered if the file
  turns unreadable before a save.

### 23. Field fixes (v0.7.8)
- `BufferHandler` echoes typed characters via `say_character` (honours `key_echo`), ignores
  escape sequences and non-printables, and says "blank" on backspace when empty.
- Double reading: zsh sometimes redraws the just-typed character. The first draw matches
  `lastkey` and is echoed; the redraw went to `speech_buffer` and was spoken again (as plain
  speech, which macOS reads as "capital X"). `draw2` records `state.echoed`; one identical
  draw within `ECHO_REPEAT_WINDOW` (0.4 s) is dropped. `sb()` routes single-character output
  through `say_character`, so capitals are always "cap".
- `ConfirmHandler`: `y` sends the command plus `\r` when `ai.run_on_accept` (default True),
  `e` types without running. Still the only path to the child; still a required keypress.
  `DANGEROUS` refusal unchanged.

### 24. Text review in prompts (v0.7.9)
- `TextReview` mixin: `self.text`, `self.pos` (cursor on a character, `len` = end) and
  `REVIEW_KEYS` mapping arrows, home/end, ctrl a/e, alt m , . j k l u i o, alt shift m, alt >
  to `review_*`. Mirrors screen review semantics (moving lands on and reads a char or word).
- `BufferHandler(TextReview, KeyHandler)`: insert at cursor, backspace before, delete key at.
  `buffer` is a property over `text` for old callers. Bracketed paste (`\x1b[200~`..`\x1b[201~`)
  is collected across reads; an unbracketed chunk with a newline or over 4 chars is also a
  paste. Line breaks become spaces; never submits.
- `ConfirmHandler(TextReview, ...)`: review keys handled in `handle_unknown_key`, read only.
- `ai_usage_report` bound to alt shift s.

### 25. Chat history (v0.7.10)
- `AIClient.history`: in-memory list of `(question, context, reply)`, appended in `_worker`
  only on `ok` and only if not abandoned. Saved whether or not `history_on` is set. Never
  written to disk; gone when TDSR exits. `clear_history()` returns the count.
- `history_on` (default False, not saved to config). Toggled by `toggle_ai_history`
  (alt shift l) and by Tab in the ask prompt (`BufferHandler(on_tab=...)`).
  `clear_ai_history` is bound to `none`.
- `dedupe_snapshots()` removes from each earlier snapshot any line (stripped, whole-line match)
  present in a later snapshot or the current one. `plan_history(turns, context, max_chars)`
  builds `(user_text, reply)` pairs and drops whole exchanges oldest-first to fit
  `ai.history_chars` (default `DEFAULT_HISTORY_CHARS` 40000). Returns
  `(pairs, included, dropped, chars)`.
- `ask(question, context, history=None)`; history is `(pairs, dropped)` and travels via
  `self._pending` so `_worker(q, c)` keeps its signature (tests wrap it). `_messages()` builds
  user/assistant turns for both wire formats, prefixing a left-out note when trimmed.
- `build_system_prompt(history)`: swaps `NO_MEMORY` and appends `HISTORY_NOTE`.
- Spoken sizes are words (`ai.words`, `history_words`). Toggle says the change in words;
  Thinking and alt shift d include earlier exchanges and total words when on.

### 26. Arrows in nano, copy file (v0.7.11)
- `say_cursor_char`: in a full screen program (`in_full_screen_program()`, i.e.
  `screen.saved_buffer` set) left/right never read the whole line. At or past the end of the
  text (`x >= len(line_text(y).rstrip())`) it says "new line"; otherwise the character,
  even after a row change. Shell behaviour (row change reads the line) is unchanged.
- **Copy file** (`copy_file`, alt shift v). `copy_file2` writes ctrl e, ctrl u, then
  `copy_file_command(path)` plus `\r` to the child, sets `tempsilence` and
  `clip_expect_until`, and schedules `copy_file_timeout`. The command is
  `[ -f F ] && [ -r F ] && base64 < F | tr -d '\n' | { printf OSC52; cat; printf ST; } || printf OSC52 ! ST`,
  with escapes written as `\033` text so the echoed command never contains a real ESC.
  `shell_word()` quotes and keeps a leading `~/`.
- `Osc52Filter.feed(bytes)` runs on raw child output before the utf-8 decoder. Accepted
  sequences are removed from both pyte input and the bytes written to the real terminal.
  Events: `copied`, `unreadable` (`!` payload), `interrupted` (any non-base64 byte other
  than BEL or ESC \, so a ctrl c can't swallow the rest of the session), `too_large`
  (`MAX_CLIP_BASE64`). Partial starts and a trailing ESC are held in `pending` until the
  next read. `osc52_accepted()` follows `osc52_clipboard`: `expected` (default, only
  inside `clip_expect_until`), `always`, `off`.
- In nano, `nano_file_name()` parses the title bar (`NANO_TITLE_RE`) into
  `state.clip_nano_path`; the next `copy_file` at the prompt pre-fills it.
  `BufferHandler` gained ctrl u to clear.
- `copy_to_clip` accepts bytes. `test_tdsr.py` and `doccheck.py` now find the repo from
  their own location instead of a hard-coded path.

### 27. Spaces always spoken (v0.7.12)
- `say_character` names whitespace through `WHITESPACE_NAMES` (space, tab, no-break space)
  when `[symbols]` has no entry. Previously a settings file without `32 = space` sent
  `l ` and mac.py spoke nothing, so arrowing onto a space between words in nano was silent.

### 28. Copy file name detection (v0.7.13)
- Field report: in nano, alt shift v said to leave the program, i.e. `nano_file_name()`
  returned None, so nothing was remembered. Title matching now uses `search` on rows 0
  and 1 with flags stripped separately (`NANO_FLAGS_RE`), so single spacing or a row above
  the title no longer defeats it.
- Fallback `launched_file_name()`: scans the saved normal screen (`saved_buffer`, via
  `saved_line()`) from `saved_cursor.y` up 3 rows for `EDITOR_COMMAND_RE` (nano, vim,
  less, etc., optional sudo and path), shlex-splits the args and takes the first that is
  not an option. Covers nano with no title bar and other programs.
- When both fail, logs rows 0 and 1 at info level and says "Couldn't find the file name".

### Pre-existing issues, deliberately left alone
- `b'\x1bO'` (bottomOfScreen) is a prefix of `b'\x1bOA'` etc. (application-mode arrows).
  Lookups are whole-chunk so this is fine in practice, but a read that splits the sequence
  would mis-fire. Changing it means restructuring `KeyHandler` around a prefix trie.
- `erase_in_display(3)` (clear scrollback) is ignored rather than clearing `state.history`.

Fixed in passing: `State.__init__` set `self.symbols_Re` while `main()` assigned
`state.symbols_re`, leaving the init attribute dead. Now consistently `symbols_re`.
Also fixed: `time_until_next_delayed()` used `delayed_functions[0]` on an unsorted list;
it now uses `min()`.

## Testing
pyte, wcwidth, speechd and pyobjc are not installed in the container, and TDSR needs a real
pty plus a speech server, so it can't run end to end there. What works offline:
- `python -m py_compile tdsr/tdsr.py`
- `test_tdsr.py`, which stubs `pyte` and `wcwidth` with a fake screen and a fake synth
  that records what would be spoken. Covers filtering, history accessors, boundary
  announcements, `index()` capture rules, and the Synth voice protocol.
- `find_voice` can be tested by extracting the function source and exec'ing it against fake
  voice objects, since `Foundation` can't be imported.

Real testing: `bin/tdsr --debug`, then read `tdsr.log` in the working directory.
Useful trick: `bin/tdsr -s "<program>"` where the program just reads the line protocol on
stdin lets you see what TDSR would speak without any TTS.

## Patch checklist
1. Edit `tdsr/tdsr.py` (tabs).
2. New key -> add to `keymap`, document in readme.md Keys.
3. New setting -> default in `State.__init__`, `ConfigHandler` entry, document in readme.md.
4. Reading screen text -> use `cell()` / `line_text()`, never `screen.buffer` directly.
5. Protocol change -> update BOTH `backend/mac.py` and `backend/speechdispatcher.py`.
6. `python -m py_compile tdsr/tdsr.py` and run `test_tdsr.py`.
