# Security policy

## Supported versions

Fixes go into the latest release only.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub:
the **Security** tab of the repository, then **Report a vulnerability**
(<https://github.com/zamirka/wfwatch/security/advisories/new>). Describe the problem and how to
reproduce it; a synthetic example is enough, please do not send real transcripts.

## What wfwatch does

- It only reads files: under `CLAUDE_CONFIG_DIR` (`~/.claude` by default) and the output files
  of background commands that Claude Code keeps under `/tmp/claude-<uid>/`.
- It runs `ps` once per scan to see which sessions are alive and what their background commands
  are running.
- It never writes files, never opens network connections and sends nothing anywhere.
- It shows what is in your transcripts: prompts, commands, file names, lines of output. Keep that
  in mind when you share your screen or record it.

Examples of what is in scope: text from a transcript that reaches the terminal as control
sequences, a file that makes wfwatch read outside the directories above, a crafted record that
makes it hang or use unbounded memory.
