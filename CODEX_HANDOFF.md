# wfwatch Codex maintenance notes

Version 0.3.0 adds local Codex CLI/app sessions to the shared monitor. The implementation is in
`wfwatch/codex.py`, integrated at `Tracker.scan()` in `wfwatch/model.py`.

The historical feature audit, migrated regressions and data-source boundaries are documented in
[docs/codex-parity-audit.md](docs/codex-parity-audit.md). Regression tests are in
`tests/test_codex.py`, including real CLI/process/output tests. All fixtures use synthetic homes.

Lifecycle records are replayed at startup, then read incrementally. Display history is bounded.
OS writer descriptors preserve long silent work; command/session identifiers are not PIDs.
macOS SIP can hide process environments, so unique command/launch matching is also supported.
Native command completion resolves code-mode batches and out-of-order result notifications.

Release checks: complete pytest suite (including encoding-warning mode), Ruff, wheel/sdist build,
Twine metadata checks, clean-wheel CLI and source-distribution tests, then GitHub CI. Update the
user installation from the published GitHub tag, with a final check outside the working tree.
