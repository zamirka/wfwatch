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

A checklist a session records itself (`update_plan` in Codex, the experimental `todowrite` in
OpenCode) is the `plan_steps` list of `m.PlanStep`; the shared renderer shows progress and the list,
and no adapter infers steps from commands or messages.

Release checks: complete pytest suite (including encoding-warning mode), Ruff, wheel/sdist build,
Twine metadata checks, clean-wheel CLI and source-distribution tests, then GitHub CI. Update the
user installation from the published GitHub tag, with a final check outside the working tree.

Explicit Codex plans are also supported by `wfwatch/plans.py` and `wfwatch plan`. See
[approved plans](docs/codex-approved-plans.md) and `skills/wfwatch-plan/SKILL.md`. They override
native plan state per full thread UUID; the monitor never writes. Tests live in
`tests/test_explicit_plans.py`. Approval metadata is agent-reported, not a runtime permission gate.

Version 0.6.0 ships `wfwatch codex setup/status/remove`. Setup previews by default; `--apply`
backs up originals and changes only owned content. `--require-approval` is opt-in. Package data
`wfwatch/data/codex-plan.md` must match the source skill. Global user config is never auto-installed
by the package. See `tests/test_codex_setup.py` and `scripts/check_codex_install.py`.

Codex CLI-launched children are linked by exact JSON start identities from `codex exec --json`
and `opencode run --format json`, including native CommandExecution records and delayed shell
output. A direct `> file` destination can supply OpenCode's first start record only when its
timestamp falls within the recorded execution interval. Reads are bounded. Tracker overlays these
children in the existing subagent cards/graph and suppresses duplicate overview rows; provider
native child lists remain unchanged. Conflicting owners and cycles remain separate. No ancestry
is inferred from project directories. Orca `agent launch --agent codex` receipts are resolved through its local read-only
`agent_session_records.adopted_native_id` mapping. Missing databases or changed schemas leave
those chats separate. Launch wrappers, resumed sessions and
commands without verifiable start identities remain separate. Tests are in `tests/test_codex.py`.

## CLI grouping publication checkpoint

Session: `01a1fabf-6417-71b2-8ff9-3fe08356478a`.
The grouping fix and regression tests are ready for publication on main without a new release.
Validation record: [docs/subagent-grouping-validation.md](docs/subagent-grouping-validation.md)
(0.6.0 source update). The tests exercise actual Tracker scan/poll transitions and synthetic
provider stores; Orca tests never read the user's database. Local wheel installation was verified
outside the repository. Unrelated `marketplace/` work is excluded.
Next step: commit this scoped change, push main and verify the CI matrix for that exact commit.
Known limitations and rollback are in the validation record.
