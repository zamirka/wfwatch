# Changelog

All notable changes to wfwatch are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A background agent no longer vanishes from its session while it is silent. Subagents were judged
  by their transcript alone: one that wrote nothing for 90 seconds (a long tool call, a long model
  reply) counted as done, and if wfwatch was started at that moment it was not listed at all. A
  background agent now runs from its launch to the notification of its end, as a background command
  does. An agent waiting on a background command of its own still runs: Claude Code moves a
  subagent's Bash call to the background after 180 seconds and reports "completed" for it. An agent
  resumed after a notification runs again. It is marked done when the notification comes, not 90
  seconds later, and its time counts from the launch, however long its transcript is.
- A session with a running background agent stays in the overview, however long it has been silent.
- A live session no longer looks closed after the host slept. On WSL2 `ps` then says a process
  started hours later than it did, which looked like a reused process id. On Linux the process is
  now recognized by its start in clock ticks, the same in `/proc` and in the session record, and
  the times are compared only when these two differ or are not there.

## [0.2.0] - 2026-10-28

### Added

- A background command that sends its output to a file of its own
  (`blender -b scene.blend -a > render.log 2>&1`) shows the last line of that file: Claude Code's
  output file stays empty then. The file is the one the running process writes its stdout or
  stderr to, as the system tells (`lsof` on macOS, `/proc` on Linux), not a path from the
  transcript. Whichever of the two files was written last gives the line.

### Changed

- An MCP tool call shows its server: `blender · execute_blender_code`, `playwright · browser_click`
  instead of the bare tool name.

### Fixed

- An output file that is not a regular file (a named pipe) is not read: opening it could block.
- The recent actions of a session no longer vanish behind a screenshot. Of a long transcript only
  its last 512 KB are read at first; a tool that returns an image (blender-mcp `look`, a browser
  screenshot) writes a line of a megabyte, which filled that window alone. Lines over 64 KB no
  longer count toward it, and it reaches 8 MB back at most.

## [0.1.1] - 2026-10-28

### Fixed

- A background command or a workflow stopped with the TaskStop tool no longer shows as running
  forever. Claude Code sends no notification for such a task; the successful result of the
  TaskStop call is now taken as its end ("killed").

## [0.1.0] - 2026-10-27

First public release. wfwatch reads files that Claude Code writes for its own use; their format is
not documented and may change with any Claude Code update. This release was checked against
Claude Code 2.1.311.

### Added

- Overview of everything that is going on: workflow runs (running, or finished in the last
  30 minutes) and Claude Code sessions without a workflow (working, waiting for you, running a
  background command, or idle for less than 15 minutes), numbered for quick switching, with a
  shared log.
- Workflow diagram: a phase timeline, the current phase with its agents fanned out as cards
  (model, status, time, rhythm of tool calls, calls, context, last action), neighbouring phases,
  a log and a status line. The layout adapts to the terminal, from 80x24 to wide screens.
- Cards view (`v` or `--cards`): agents of each phase as cards, scrolled with PgUp/PgDn and
  Home/End; `a` expands every phase.
- Session view: recent actions, subagents fanned out like workflow agents, the session log and
  background commands.
- Background commands (Bash with `run_in_background`): a session that is quiet while one runs
  is shown as "background task", not as idle, with the command, how long it runs, the process
  it is running now (CPU %) and the last line of its output.
- Agents replaced after a workflow resume are dimmed and left out of the counts.
- `--list` of recent runs; `--once` prints one frame to stdout; a run id, its beginning or a part
  of the workflow name opens that run.
- Keys: `1`–`9` (two digits in a row for 10 and above), Tab/Shift+Tab and ←/→, ↑/↓, `o`/`0`/Esc,
  `f`, `v`, `a`, `q`. Letter keys also work in the Russian keyboard layout.
- English and Russian interface: `--lang`, `WFWATCH_LANG` or the locale (`LC_ALL`, `LC_MESSAGES`,
  `LANG`).
- Dark and light themes: `--theme auto|dark|light` (auto follows `COLORFGBG`); `NO_COLOR` is
  respected.
- Text from transcripts (names, labels, tool arguments, messages, output lines) is shown as plain
  text: control characters are dropped and line breaks become spaces, so it can neither drive the
  terminal nor break the layout. Task notifications are taken only from Claude Code's own records,
  and a background command's output is read only under `/tmp/claude-<uid>/` or `CLAUDE_CONFIG_DIR`.
- Resilience: unexpected or malformed lines in transcripts are skipped; a read or render failure
  shows in the status line instead of closing the program (`--debug` re-raises it); the terminal
  is restored after `q`, Ctrl+C, SIGTERM and SIGHUP.
- `--version` and `python -m wfwatch`.

[Unreleased]: https://github.com/zamirka/wfwatch/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/zamirka/wfwatch/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/zamirka/wfwatch/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/zamirka/wfwatch/releases/tag/v0.1.0
