# OpenCode parity audit

Audited 2026-11-16 against OpenCode 2.3.0 and a live local database. The adapter reads one SQLite
file and feeds the existing session UI: no separate renderer, keys, theme or terminal handling.

| Capability | OpenCode source | Implementation and evidence |
|---|---|---|
| Sessions, model, tokens | `session_v2` (`title`, `slug`, `directory`, `model`, `tokens_*`), `project.worktree` | `SessionRow`; context = `tokens_input + tokens_cache_read`; `test_tracker_reads_tools_tokens_and_a_finished_turn` |
| Tool actions and the shared log | `session_message.data.content` parts of type `tool` (`name`, `state.status`, `state.input`) | Lower-case names mapped to the shared actions; a call is counted once by its id; `test_tool_parts_become_actions_and_usage`, `test_tool_names_and_arguments_are_readable` |
| Streaming output | the same row is updated in place | Watermark + a re-read tail; a row is re-applied when its stored JSON changed, so two updates in one millisecond are both seen; `test_streaming_message_updates_in_place`, `test_two_updates_in_one_millisecond_are_both_seen` |
| Waiting for the user | `question`, `plan`, `permission` tools with `state.status = running` | Session status `WAITING`; `test_tool_names_and_arguments_are_readable` |
| Turn end and failures | `session_message` rows of type `idle` with `outcome` | `failed`/`interrupted` become log events, including provider errors such as a free usage limit; `test_idle_failure_becomes_an_event` |
| Child sessions and subagents | `session_v2.parent_id`; `job.kind = subagent`; synthetic messages with `metadata.source = subagent` | Children and grandchildren grouped under the root, a report finishes the child; `test_child_sessions_are_grouped_under_their_root`, `test_a_subagent_report_finishes_the_child` |
| Background commands | `job` rows (`kind = shell`, `status`, `output`, `recovery`), `<data>/shell/*/<id>.out` | Command, last output line, exit code; `test_background_shell_job_and_its_output`, `test_synthetic_shell_completion_finishes_a_background_task` |
| Process and CPU | the `.out` file held open by a descendant of a running OpenCode process | File-descriptor match as for Codex; `test_shell_process_is_found_by_the_output_file_it_holds_open` |
| Archives, silence, removal | `time_archived`, `time_busy`/`time_idle`, `time_updated` | Archived sessions are skipped; a vanished database removes its cards without an error; `test_archived_and_stale_sessions_disappear`, `test_missing_or_broken_database_never_stops_the_tracker` |
| Overview, cards, CLI, languages | shared views | `test_cli_overview_shows_an_opencode_session_end_to_end`, `test_claude_and_opencode_sessions_live_side_by_side` |

## Data-source boundaries

- The database belongs to OpenCode. It is opened as `file:...?mode=ro`, so a running app is never
  blocked or changed; the schema is internal and may change between versions. A database without
  the `project`, `session_v2`, `session_message` and `job` tables is reported as a schema error and
  the adapter stays silent rather than guessing.
- OpenCode keeps no per-session process record and no workflow phase journal. A session counts as
  working while `time_busy` is newer than `time_idle`, and workflow diagrams stay Claude-specific.
- `job` rows exist only while a job runs; on completion OpenCode deletes the row and writes a
  synthetic message. The adapter therefore completes a task from either source and never
  resurrects a finished task from a late row.
- Output files are read only under `<data dir>/shell` with the `.out` suffix, with symlinks
  resolved: the path comes from the database, that is from data, and must not lead anywhere else.
- Message types other than `assistant`, `idle` and `synthetic` (for example `user`, `system`,
  `compaction`) carry no activity to show and are ignored. `session_part` is the legacy V1 table and
  is empty in current databases.
- A job without a row or output for an hour becomes stale, with no invented exit code. On Windows
  the process lookup is skipped; the task itself still appears.

## Verification and release gates

Tests build a synthetic database through `tests/opencode_home.py` and never read
`~/.local/share/opencode`: `WFWATCH_OPENCODE_DB` isolates unit tests and subprocesses. The CLI tests
run the real `python -m wfwatch --once` against that database. The full suite, Ruff, build and
metadata checks, a clean-wheel CLI run and the source-distribution tests are the same gates as for
the Claude and Codex adapters.
