# Contributing to wfwatch

Bug reports, ideas and pull requests are welcome. wfwatch is a small tool, so a short issue
first is the easiest way to agree on a bigger change.

## Setting up

You need Python 3.10 or newer on macOS or Linux (on Windows, work inside WSL).

```bash
git clone https://github.com/zamirka/wfwatch
cd wfwatch
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/wfwatch --version
```

`.venv/bin/wfwatch` (or `.venv/bin/python -m wfwatch`) runs your working copy.

To see every screen without workflows of your own, use the synthetic data of
`scripts/demo_data.py` (made-up projects and sessions; your `~/.claude` is not touched):

```bash
python3 scripts/demo_data.py /tmp/wf-demo --live    # keeps the demo going until Ctrl+C
CLAUDE_CONFIG_DIR=/tmp/wf-demo .venv/bin/wfwatch     # in another terminal
```

`python3 scripts/demo_data.py --help` lists the options.

## Checks

CI runs the same commands on macOS and Linux with Python 3.10–3.14:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

`.venv/bin/ruff format .` fixes the formatting. ruff is pinned in the `dev` extra, because
another ruff version may report different things. The configuration targets Python 3.10, so do
not use anything newer than 3.10 has (for example `datetime.UTC`).

## Tests

- Tests never read the real `~/.claude`. `tests/conftest.py` has builders (`Home`) for a
  synthetic `CLAUDE_CONFIG_DIR`: session registry records, transcripts, workflow journals and
  scripts, background task output.
- Times are relative to `model.now()`; the `frozen_now` fixture stops the clock. No fixed dates,
  no `sleep`.
- The interface is English in tests unless a test switches the language itself. Terminal size
  and color settings are pinned by `conftest.py`, so the result does not depend on the terminal
  or the locale the tests run in.
- Behavior a user can see needs a test. Rendering tests draw whole screens at fixed sizes
  (80x24, 120x40, 200x60) and check them.

## Code style

- Code, comments and docstrings are in English.
- The data layer (`model.py`) does not produce interface text: it keeps codes and values, and
  rendering turns them into words.
- Every string the interface shows comes from the catalog in `wfwatch/i18n.py`, with both `en`
  and `ru`. Phrases with a number go through `plural()` (English has two forms, Russian three).
  Store whole phrases with named placeholders, do not glue pieces together.
  `tests/test_i18n.py` checks that the catalog is complete and that every key is used.
- Russian text uses the en dash «–», never the em dash.
- Widths are counted in terminal cells (`rich.cells.cell_len`), not in characters: wide
  characters such as CJK and emoji take two cells.

## When a Claude Code update breaks wfwatch

The files wfwatch reads are internal to Claude Code and not documented, so an update can change
them. If something stops showing up or looks wrong after an update, open an issue with the
output of `wfwatch --version` and `claude --version` and an anonymized sample of a line that
wfwatch does not understand (see Privacy below). A fix should come with a test that builds such
a line with the `conftest.py` builders.

## Privacy

Claude Code transcripts contain your prompts, code, file paths, command output and sometimes
secrets. Before you attach anything to an issue or a pull request:

- do not attach whole transcripts, `~/.claude` files or the output of `--debug` as they are;
- cut a sample down to the fields that matter and replace project names, paths, prompts and
  output with placeholders;
- take screenshots on synthetic data only (`scripts/demo_data.py`), or make sure no project,
  session, file or command names are visible;
- never commit real data to the tests.

## Pull requests

- One topic per pull request; say what changes for the user.
- Add a line to `CHANGELOG.md` under "Unreleased".
- Run the checks above before pushing.
