# CLI child-session grouping validation

Version: 0.6.0 source update, without a new release tag.
Session: `01a1fabf-6417-71b2-8ff9-3fe08356478a`.

## Behavior and evidence

Codex-launched Codex/OpenCode sessions are grouped by exact start-event IDs. Orca launch
receipts use the local read-only session-to-native-ID mapping. A shared directory alone
never establishes ancestry. Conflicting owners, cycles and unavailable Orca mappings leave
sessions separate. Redirected OpenCode output is bounded and its start timestamp must match
the execution interval; command batches cannot attribute shared stdout to a particular launch.

`tests/test_codex.py` covers direct launches, delayed output, redirected output, stale/future
output timestamps, non-launch commands, ambiguous ownership, cycles, native children alongside
CLI children, nested launches, repeated scan/poll, child completion, missing parents, transcript
replacement, missing/corrupt Orca databases and schema drift. Fixtures isolate the Orca database
as well as Codex, OpenCode and explicit plans.

## Verification

- Codex regression suite: 52 passed, including from a clean installed wheel outside the checkout.
- Clean-wheel CLI acceptance: setup preview, approval modes, updates, independent session plans,
  rendering, restart and removal passed (`scripts/check_codex_install.py`).
- Wheel and sdist built; strict Twine metadata checks passed.
- Ruff lint/format and `git diff --check` passed.
- Full suite with default-encoding warnings treated as errors: 695 passed, 1 skipped.
- GitHub CI is checked after push for the exact commit.

## Limits and rollback

Unsupported launch wrappers, resumed sessions and missing start evidence remain separate.
A redirected launch can be linked after its command-completion record becomes available.
Orca database schema changes degrade to separate rows. This is a local read-only integration;
no provider logs or databases are modified.

Rollback is a normal revert of the grouping commit. The existing `v0.6.0` tag remains unchanged.
No new release is created by this update.
