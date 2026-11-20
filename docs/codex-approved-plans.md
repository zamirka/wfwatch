# Optional Codex plans

Installing or upgrading wfwatch never changes Codex settings. Native `update_plan` records are
still read automatically. A textual plan in chat is not a structured checklist. If the tool is
unavailable, explicitly connect the companion skill below. No server or network access is needed
by wfwatch; running Codex itself still uses its configured model provider.

## Connect

```sh
wfwatch codex setup                         # preview the exact changes, write nothing
wfwatch codex setup --apply                 # install the skill and managed instructions
wfwatch codex status
```

The standard mode follows your existing authorization policy; it does not add approval questions
just for monitoring. To require explicit agreement on a proposed plan before **any** tools,
including reads and diagnostics, choose the stricter option:

```sh
wfwatch codex setup --require-approval
wfwatch codex setup --require-approval --apply
```

Use `setup --standard --apply` to return to standard mode. Omitting the mode flag on an update
preserves the existing choice. `--home PATH` or `CODEX_HOME` selects a different Codex home.
The default is `~/.codex`. Start a new Codex session after setup and ask it to use wfwatch-plan for
the task. Existing sessions may need the instructions supplied again; old text plans are not
converted automatically. In strict mode, first approve the proposed actions in chat.

The installer ships inside the wheel and source distribution. It places the
[companion skill](../skills/wfwatch-plan/SKILL.md) in `skills/wfwatch-plan/SKILL.md` and adds a
marked block to `AGENTS.md`. It previews changes, backs up changed originals under
`.wfwatch-backups/<timestamp>/`, and tracks ownership in `.wfwatch-integration.json`.
Unrelated instructions and skills are preserved. A conflicting manual installation, changed
managed content or `AGENTS.override.md` is reported instead of silently overwritten. Back up and
reconcile those files first; status cannot prove which instructions an already running agent loaded.

After a package upgrade, run `wfwatch codex setup` again to preview the bundled skill update,
then `wfwatch codex setup --apply`. Repeated setup with the same version and options is a no-op.

```sh
wfwatch codex remove                        # preview
wfwatch codex remove --apply                # remove only managed content
```

Removal preserves plan files, backups, other skills and personal instructions. If managed content
has been manually edited, removal refuses and explains the conflict. The backups contain personal
instructions; treat them as private files.

## How agents record a plan

```sh
wfwatch plan show
wfwatch plan set --approval 'User request or explicit approval reference' \
  --step 'Inspect navigation' --step 'Implement the fix' --step 'Verify results'
wfwatch plan step 1 in_progress --revision 1
wfwatch plan step 1 completed --revision 2
```

Identity defaults to `CODEX_THREAD_ID`; alternatively pass `--session FULL-UUID` on every command.
Missing or invalid identity is an error. Never guess the current session from a directory or newest
log. `show` returns JSON or `null`. Each write returns a new revision; stale updates are rejected.
Replacing a plan requires `set --replace` with authorization for the replacement. Only one step can
be active. A step must start before completion; paused steps may return to pending. Completed steps
cannot reopen without replacing the plan. The agent reports status; wfwatch does not infer success.

Plans are written atomically under `~/.local/share/wfwatch/plans/<UUID>.json`;
`WFWATCH_PLAN_DIR` overrides it. A session lock rejects overlapping writes. After a writer crash,
remove a stale `.lock` directory only after confirming that no writer remains active.
Plans survive restarts, isolate parallel sessions and override native plans for that session.
Removing the explicit file restores native plan display. Unreadable or malformed files are reported
in the no-plan state; if native steps exist they remain available. Use `plan show` for the specific
file error. A saved plan alone does not make a session active.

## Read the plan

In a session diagram, Up/Down select steps, Home/End select the first/last, and `f` resumes following
the active step. Long titles wrap in the left column and selected-step node. The list fits complete
entries around the selection; an oversized diagram falls back to scrollable cards.
Press `p` (Russian `з`) for the full plan, with line/page scrolling and Home/End. `p` or Escape returns.
If no plan has been received, `p` explains how to connect tracking. Cards also wrap long text.
Restart the monitor after upgrading the executable.

The skill recommends concise titles (usually 3–7 words), with scope and criteria discussed separately.
Approval references are agent-reported metadata, **not authenticated consent**. Skills and AGENTS.md
are behavioral instructions, **not a runtime tool lock**; higher-priority or project instructions and
model behavior can affect compliance. Test your own client and workflow before relying on them.

## Release checks

The test suite covers file preservation, conflicts, rollback, repeated setup, both policies,
session isolation, revisions and full-text navigation. `scripts/check_codex_install.py` exercises the
installed wheel in temporary directories, including a synthetic two-session lifecycle and removal.
CI runs unit tests across macOS/Linux/Windows and packaging checks on macOS/Linux.
These checks verify the integration, not universal agent obedience. Live Codex acceptance results,
when available, are reported separately with their limitations in the release notes.
