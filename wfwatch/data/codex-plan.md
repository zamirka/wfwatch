---
name: wfwatch-plan
description: Record and maintain an authorized multi-step Codex task as a session plan in wfwatch. Use when executing or resuming a task after the permissions required by the user's instructions have been obtained.
---

# Session plans in wfwatch

Follow the user's authorization policy first. If their instructions require approval before tools,
propose the plan in chat and wait before reading files, loading skills or recording a plan. This
skill does not itself require an extra approval round for a task the user already authorized.

Use concise action titles, usually 3–7 words, such as "Inspect navigation", "Wrap stage titles",
and "Verify and install". Discuss scope, constraints and acceptance criteria in chat; a short title
must not conceal extra work. Record the same titles the user saw.

After obtaining the required authorization:

1. Run `wfwatch plan show`. Identity defaults to `CODEX_THREAD_ID`. If it is missing, use
   `--session FULL-UUID` only with a verified ID of the current thread. Never choose the latest
   log, a shared project directory or a short suffix as identity. If identity cannot be established,
   report that plan tracking cannot be connected and ask how to proceed.
2. Resume an existing plan only if it belongs to this task. For a new or changed scope, obtain the
   authorization required by the user's policy before replacing the old plan.
3. Record the plan with `wfwatch plan set --approval 'user request or explicit approval reference'
   --step 'Inspect navigation' --step 'Implement the fix' --step 'Verify results'`. Quote shell
   arguments safely or pass an argument array. `--approval` records the authorization reference;
   it does not authenticate consent. Never invent one. Use `--replace` only for a newly authorized
   replacement. Do not overwrite another task merely because it shares the session.
4. Before starting a step run `wfwatch plan step 1 in_progress --revision N`; after verifying the
   result run `wfwatch plan step 1 completed --revision N`. N is the revision from the latest
   successful command. Each write returns the next revision. Update sequentially; only one step
   can be active. If the revision changed, reread and reconcile, never blindly retry.
5. On a block or interruption, do not mark the step complete. Report the block in chat; return an
   active step to pending when paused. On resume read the saved plan. A finished turn does not
   mean the work is finished. On tracking errors report them and restore tracking or agree on an
   alternative before continuing.

Do not ask again for each step of an already approved plan. Ask about added scope when required by
user policy. Respect stop requests. Do not edit Codex transcripts. An explicit wfwatch plan takes
precedence over native update_plan, so avoid maintaining two independent plans for the same task.

Files: `~/.local/share/wfwatch/plans/<UUID>.json`, overridable by `WFWATCH_PLAN_DIR`.
The monitor reads updates automatically; restart an already running monitor after upgrading the
program. View a session, use Up/Down to select stages, `p` for the full plan and Escape to return.

Instructions are not a runtime tool lock. Do not promise that the agent always obeys them.
