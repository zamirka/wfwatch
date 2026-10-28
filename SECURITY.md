# Security policy

## Supported versions

Fixes go into the latest release only.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub:
the **Security** tab of the repository, then **Report a vulnerability**
(<https://github.com/zamirka/wfwatch/security/advisories/new>). Describe the problem and how to
reproduce it; a synthetic example is enough, please do not send real transcripts.

## What wfwatch does

- It only reads files: under `CLAUDE_CONFIG_DIR` (`~/.claude` by default), the output files
  of background commands that Claude Code keeps under `/tmp/claude-<uid>/`, and the end of the
  file a running background command writes to instead (`> render.log`). The path of that file
  comes from the system (`lsof` on macOS, `/proc/<pid>/fd` on Linux: the stdout or stderr of the
  process the command runs now), never from a transcript, and only a regular file is read.
- It runs `ps` once per scan to see which sessions are alive and what their background commands
  are running, and, while a background command runs, `lsof` on macOS for the processes of its
  session's Bash tool.
- It never writes files, never opens network connections and sends nothing anywhere.
- It shows what is in your transcripts: prompts, commands, file names, lines of output. Keep that
  in mind when you share your screen or record it.

Examples of what is in scope: text from a transcript that reaches the terminal as control
sequences, a file that makes wfwatch read anything but the files above, a crafted record that
makes it hang or use unbounded memory.
