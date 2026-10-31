# Claude Code and Codex parity audit

Audited 2026-11-01 against every change through `48ac073` (Claude releases 0.1.0–0.2.2), plus the initial uncommitted Codex adapter. The port uses the existing session UI, background-task model, rendering, keyboard handling and terminal safeguards.

| Historical capability | History | Codex implementation and evidence |
|---|---|---|
| Sessions, tools, tokens, model | Initial release | Incremental rollout adapter; `test_tracker_reads_codex_tools_tokens_and_completed_turn` |
| Child and nested agents, shared log | Initial release | Root ancestry from top-level metadata or `source.subagent.thread_spawn`; child/grandchild, inherited metadata, completed parent and old parent retention tests |
| Silent background agent visibility | `5d4f0f1` | Verified open rollout writer keeps old sessions and their children alive beyond the silence cutoff; `test_writer_keeps_long_silent_agent_and_old_parent_grouped` |
| Background commands and shell-only work | Initial release, `0118762` | Direct and code-mode result envelopes, yielded cells, output lines; completion of the agent turn does not finish its commands |
| Task stop and completion | `5f14925` | `write_stdin` exit results (including Ctrl+C), native `CommandExecution` completion and code-cell wait/termination; out-of-order completion regression |
| Current child process and CPU | Initial release | Verified writer, thread environment, command and launch matching; nested process aggregation; tool session identifiers never become PIDs |
| Redirected stdout/stderr logs | `affac05` | Read from OS-reported writable descriptors, including the busiest leaf; real-process CLI e2e verifies CPU and redirected output |
| Non-regular output files | `affac05` | Shared `last_output_line` retains nonblocking regular-file validation; existing FIFO tests remain in the full suite |
| Process identity and sleep drift | `3d86b29` | Writer descriptors are rediscovered each scan, not cached as PID-only evidence; observed command identities use shared start-tick/creation-time validation; PID reuse regression |
| MCP server labels | `affac05` | Shared server-name normalization with Codex arguments retained; `test_mcp_action_shows_server_and_tool` |
| Actions across large screenshot records | `f95d349` | Incremental replay retains actions across a 1.5 MB image result; malformed, partial and replaced transcripts are covered |
| Resume and restart recovery | Initial release, later lifecycle fixes | Relevant history is replayed at startup, including commands older than the previous tail window; repeated scans do not double-count; truncation forgets old commands |
| Overview, cards, language, theme, keys, terminal cleanup | Initial release | Shared views and CLI retained; existing rendering, keyboard and PTY e2e tests plus Codex CLI subprocess e2e |

## Data-source boundaries

- Claude workflow phase diagrams depend on the Claude Workflow plugin's journals and phase scripts, not on generic assistant messages. Their discovery and rendering are retained. Codex rollouts have no equivalent phase journal; Codex agents and commands appear in the shared session views.
- Web-only ChatGPT conversations do not produce these local rollout files and are outside a local process monitor's data source.
- Approval decisions are not reliably persisted as a live waiting-state record in rollouts. Busy/idle comes from turn events; wfwatch does not claim exact approval-state detection.
- When OS evidence is unavailable, uncompleted turns and commands become stale after an hour of silence. A stale task has no invented exit code. A verified live writer keeps long silent work visible.
- macOS SIP hides environments of system binaries. A fallback requires a unique command and launch-time match below the verified writer. Ambiguous candidates remain unassigned; this behavior has its own regression test.
- A batch with multiple commands and no unambiguous command-to-result association retains independent task IDs and output; its initial label can be the wrapper name. Native command completion supplies the command and final output.

## Verification and release gates

Tests use synthetic records and isolated Claude/Codex homes, never committed personal rollouts. New missing behavior was implemented through failing regressions and then passing tests. The real-process e2e uses an isolated writer process, a shell child and redirected output, with OS `ps`/`lsof` inspection and a real CLI subprocess.

Before publishing, run the complete unit/e2e suite, explicit-encoding CI mode, Ruff, package build/metadata checks, clean-wheel CLI checks and tests from the source distribution. GitHub CI additionally covers Linux/macOS, Python 3.10–3.14, Rich 13 and unusual locale/terminal settings. Publish a versioned commit/tag only after local gates pass; verify GitHub CI and install the published GitHub revision outside this checkout.
