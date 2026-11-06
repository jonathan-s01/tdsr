# Changes in this fork

Compared with the original TDSR by Tyler Spivey. Each item is documented in readme.md;
PROJECT_OVERVIEW.md has the technical notes.

## Reading and navigation
- Scrollback: review lines that have scrolled off the top of the screen.
- Say all (continuous reading), find text, jump between shell prompts.
- Repeat or spell the last thing spoken; where am I.
- Reading highlighted text in menus; wrapped lines read as one line.
- Optional indentation announcements, punctuation levels, column awareness.
- Ignored symbols: box drawing and similar decoration is not spoken.
- Shell prompt prefix (user@host) dropped when arrowing through history.
- Navigation stops at the last line with text instead of walking blank rows.
- Capitals said as "cap" and/or with higher pitch; deleted characters at lower pitch.
- In nano and other full screen programs, arrowing past the end of a line says
  "new line" instead of reading the next line.
- Spaces and tabs are always named when reviewing or arrowing, even if the
  settings file has no name for them.

## Cursor keys
- Arrow keys wait for the program to finish redrawing, so stale text is not read.

## Copying
- Selection announces where it starts and ends and how much was copied.
- Multi-line selections no longer copy the wrong region (original bug).
- Line selection mode, cancel selection, optional shift+arrow selection.
- Copy a whole file to the clipboard with alt shift v, through ssh and
  pct enter, without enabling anything on the remote system. Pressed inside
  nano, vim or less it remembers the file name for the next press.

## Settings and keys
- Remappable keys, a spoken list of commands (alt shift h).
- Voice menu on macOS; voices work with VoiceOver running.
- Settings are only saved when changed, and a damaged settings file is backed up.
- A nested TDSR started from a shell startup file no longer launches twice.

## AI assistant (optional, off by default)
- Ask questions about what is on the screen, with suggested commands that only
  run after you confirm. Works with Anthropic, OpenAI and Azure style services.
- Controls for exactly what is sent, instruction profiles, usage reporting,
  and in-memory chat history.

## Stability
- Many crash fixes for unusual input, damaged settings and speech server failures.
- Offline test suite: `python test_tdsr.py`.
