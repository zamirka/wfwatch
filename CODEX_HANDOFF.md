# wfwatch maintenance notes: Claude Code, Codex and OpenCode

Version 0.5.0 adds local OpenCode sessions to the shared monitor. The adapter lives in
`wfwatch/opencode/` (`store.py` reads the SQLite database, `projection.py` turns rows into the shared
model, `watch.py` polls, nests and discovers) and is integrated at `Tracker.scan()` in
`wfwatch/model.py`. `WFWATCH_OPENCODE_DB` overrides the database, which also isolates every test.

The Codex adapter (0.3.x) is in `wfwatch/codex.py`; its historical feature audit, migrated
regressions and data-source boundaries are documented in
[docs/codex-parity-audit.md](docs/codex-parity-audit.md). The OpenCode sources, boundaries and test
map are in [docs/opencode-audit.md](docs/opencode-audit.md). Regression tests are in
`tests/test_codex.py` and `tests/test_opencode*.py`, with fixtures in `tests/opencode_home.py`; all
fixtures use synthetic homes.

Lifecycle records are replayed at startup, then read incrementally. Display history is bounded.
OS writer descriptors preserve long silent work; command/session identifiers are not PIDs.
macOS SIP can hide process environments, so unique command/launch matching is also supported.
Native command completion resolves code-mode batches and out-of-order result notifications.

OpenCode is a database, not an append-only file: a message row is rewritten while the agent streams.
The watch keeps a watermark, re-reads a small recent tail and re-applies a row only when its stored
JSON changed, so a second update within the same millisecond is still seen. `job` rows exist only
while a job runs, and a synthetic message completes it. The connection is opened `mode=ro`; output
files are read only under `<data dir>/shell` with the `.out` suffix.

Release checks: complete pytest suite (including encoding-warning mode), Ruff, wheel/sdist build,
Twine metadata checks, clean-wheel CLI and source-distribution tests, then GitHub CI. Update the
user installation from the published GitHub tag, with a final check outside the working tree.
