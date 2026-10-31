"""Read-only Codex CLI/app rollout adapter. No hooks, API access or credentials required.

Rollouts are an internal format. Busy/idle is inferred from turn events, with a one-hour
silence cutoff for crashed/interrupted writers that did not record a completion event.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import model as m

CODEX_DIR = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def process_threads(pids: list[int]) -> dict[int, str]:
    """Read only the thread identity from candidate command environments; never display them."""
    if not pids:
        return {}
    out = {}
    if sys.platform.startswith("linux"):
        for pid in pids:
            try:
                fields = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            for field in fields:
                if field.startswith(b"CODEX_THREAD_ID="):
                    out[pid] = field.split(b"=", 1)[1].decode("utf-8", "replace")
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "eww", "-p", ",".join(map(str, pids)), "-o", "pid=,command="],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        text = result.stdout.decode("utf-8", "replace")
        records = list(re.finditer(r"(?m)^\s*(\d+)\s+", text))
        for i, record in enumerate(records):
            end = records[i + 1].start() if i + 1 < len(records) else len(text)
            match = re.search(r"(?:^|\s)CODEX_THREAD_ID=([^\s]+)", text[record.end() : end])
            if match:
                out[int(record[1])] = match[1]
    return out


def command_matches(command: str, process: m.Proc) -> bool:
    try:
        expected, actual = shlex.split(command), shlex.split(process.args)
    except ValueError:
        return False
    if not expected or not actual:
        return False
    if "-c" in actual or "-lc" in actual:
        return actual[-1] == command or any(process.args.partition(flag)[2] == command for flag in (" -c ", " -lc "))
    if os.path.basename(expected[0]) == os.path.basename(actual[0]) and expected[1:] == actual[1:]:
        return True
    # POSIX shells can replace themselves with the final simple command. For
    # `cd dir; sleep 60`, ps then contains `sleep 60`, not the original shell.
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    start = max((i + 1 for i, token in enumerate(tokens) if token in (";", "&&", "||", "|")), default=0)
    tail = tokens[start:]
    stop = next((i for i, token in enumerate(tail) if token in (">", ">>", "<", "2>")), len(tail))
    tail = tail[:stop]
    return bool(tail) and os.path.basename(tail[0]) == os.path.basename(actual[0]) and tail[1:] == actual[1:]


class CodexWatch(m.SessionWatch):
    def __init__(self, path: Path):
        # A fork can contain its parent's session_meta later in the transcript.
        # Only the first record identifies the owner of this rollout.
        with path.open("rb") as f:
            record = m._loads(f.readline(2 << 20)) or {}
            header = f.read(self.TAIL_BYTES)
        meta = record.get("payload")
        if record.get("type") != "session_meta" or not isinstance(meta, dict) or not m._s(meta.get("id")):
            raise ValueError("missing Codex session metadata")
        sid = "codex:" + m._s(meta["id"])
        source = meta.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        spawn = spawn if isinstance(spawn, dict) else {}
        label = m._s(
            meta.get("agent_path")
            or meta.get("agent_nickname")
            or spawn.get("agent_path")
            or spawn.get("agent_nickname")
        )
        cwd = m._s(meta.get("cwd"))
        info = m.SessionInfo(
            sid,
            name="Codex · " + (label or sid[-8:]),
            cwd=cwd,
            entrypoint="codex · " + m._s(meta.get("originator")),
            alive=True,
            status=m.IDLE,
        )
        super().__init__(sid, path, info)
        # Lifecycles can start hours before the activity window. Replay relevant
        # records once, then read only appended bytes; keep display history bounded.
        self.main.tail = m.Tail(path, needles=(b'"turn_context"', b'"event_msg"', b'"response_item"'))
        parent = m._s(meta.get("parent_thread_id") or spawn.get("parent_thread_id"))
        self.parent_id = "codex:" + parent if parent else ""
        self._own_events: list[m.Event] = []
        self.children: list[CodexWatch] = []
        self._calls: dict[str, tuple[str, dict, datetime | None]] = {}
        self._tasks: dict[str, m.BgTask] = {}
        self._task_pids: dict[str, dict[int, tuple[int | None, float]]] = {}
        self._commands: dict[str, str] = {}
        self._completed_commands: dict[str, tuple[dict, object]] = {}
        self.writer_alive = False
        self.main.label = label or info.name
        # A long turn can push turn_context out of the activity tail. Seed its model
        # from a bounded header; later context records still override it normally.
        for line in header.splitlines():
            if b'"turn_context"' in line:
                entry = m._loads(line)
                context = entry.get("payload") if entry and entry.get("type") == "turn_context" else None
                if isinstance(context, dict):
                    self.main.model = m._s(context.get("model")) or self.main.model

    def poll(self, info=None) -> None:
        entries = self.main.tail.read()
        if self.main.tail.reset:
            self.main.tail.reset = False
            self.main.reset_stats()
            self._own_events.clear()
            self.events.clear()
            self.info.status = m.IDLE
            self._calls.clear()
            self._tasks.clear()
            self._task_pids.clear()
            self._commands.clear()
            self._completed_commands.clear()
        for entry in entries:
            try:
                self._feed(entry)
            except m.RECORD_ERRORS:
                continue
        at = m.now()
        if not self.writer_alive:
            for task in self._running_own():
                last = task.output_mtime or task.started
                if last and (at - last).total_seconds() >= m.QUIET_MAX[m.BUSY]:
                    task.status, task.ended = "stale", at
        # File activity is not proof that an old turn is still working.
        self.info.alive = self.writer_alive or self.quiet_sec(at) < m.QUIET_MAX[m.BUSY]
        self.main.status = (
            m.RUNNING if (self.info.status == m.BUSY and self.info.alive) or self._running_own() else m.DONE
        )
        for child in self.children:
            child.poll()
        self.bg_agents = [child.main for child in self.children if child.main.status == m.RUNNING]
        self.merge_events()

    def _running_own(self) -> list[m.BgTask]:
        return [t for t in self._tasks.values() if t.status == m.RUNNING]

    def shown(self, at=None) -> bool:
        return (self.writer_alive and self.status in m.ACTIVE_STATUSES) or super().shown(at)

    @property
    def bg_tasks(self) -> list[m.BgTask]:
        return list(self._tasks.values()) + [t for child in self.children for t in child._tasks.values()]

    def running_bg_tasks(self) -> list[m.BgTask]:
        return [t for t in self.bg_tasks if t.status == m.RUNNING]

    def recent_bg_tasks(self, at=None, within=m.IDLE_KEEP_SEC) -> list[m.BgTask]:
        at = at or m.now()
        return [
            t for t in self.bg_tasks if t.status == m.RUNNING or (t.ended and (at - t.ended).total_seconds() < within)
        ]

    def poll_bg(self) -> None:
        # Output is embedded in Codex records, not in Claude's task output directory.
        pass

    def _command_result(self, payload, ts) -> None:
        name, args, started = self._calls.pop(m._s(payload.get("call_id")), ("", {}, None))
        if name not in ("exec_command", "write_stdin", "exec", "wait"):
            return
        output = payload.get("output")
        blocks = output if isinstance(output, list) else [{"text": output}]
        for block in blocks:
            text = block.get("text") if isinstance(block, dict) else None
            if not isinstance(text, str):
                continue
            data = m._loads(text.encode("utf-8"))
            if not isinstance(data, dict):
                data = {}
                if match := re.search(r"Script running with cell ID ([\w-]+)", text):
                    data["session_id"] = "cell:" + match[1]
                elif name == "wait" and args.get("cell_id") and re.search(r"Script (completed|terminated)", text):
                    data["session_id"] = "cell:" + m._s(args["cell_id"])
                    data["exit_code"] = 0 if "Script completed" in text else 130
                if match := re.search(r"Process running with session ID (\d+)", text):
                    data["session_id"] = match[1]
                if match := re.search(r"Process exited with code (-?\d+)", text):
                    data["exit_code"] = int(match[1])
                if data:
                    data["output"] = text.partition("Output:\n")[2]
            sid = str(data.get("session_id") or args.get("session_id") or "")
            if not sid or not ("session_id" in data or "exit_code" in data):
                continue
            task = self._tasks.get(sid)
            if task is None:
                if "session_id" not in data:
                    continue
                task = self._tasks[sid] = m.BgTask(sid, m.first_line(args.get("cmd") or name, 120), started or ts)
                self._commands[sid] = m._s(args.get("cmd"))
            self._update_task(task, data.get("output"), data.get("exit_code"), ts)
            if finished := self._completed_commands.pop(sid, None):
                self._command_completed(*finished)

    @staticmethod
    def _update_task(task, output, exit_code, ts) -> None:
        if isinstance(output, str):
            lines = [m.clean(s).strip() for s in m._ANSI.sub("", output).splitlines() if m.clean(s).strip()]
            if lines:
                task.last_line = lines[-1][:500]
            task.output_size += len(output.encode("utf-8"))
            task.output_mtime = ts
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            task.exit_code = exit_code
            task.status = "completed" if exit_code == 0 else "failed"
            task.ended = ts
            task.log_path, task.log_line, task.log_mtime = "", "", None

    def _command_completed(self, item, ts) -> None:
        if not isinstance(item, dict) or item.get("type") != "CommandExecution":
            return
        sid = str(item.get("process_id") or "")
        task = self._tasks.get(sid)
        if task is not None:
            task.description = m.first_line(item.get("command"), 120) or task.description
            self._update_task(task, item.get("aggregated_output"), item.get("exit_code"), ts)
        elif sid:
            self._completed_commands[sid] = (item, ts)

    def merge_events(self) -> None:
        events = self._own_events + [e for child in self.children for e in child._own_events]
        self.events = sorted(events, key=lambda e: e.ts)[-300:]

    def _feed(self, entry: dict) -> None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        kind, typ = entry.get("type"), payload.get("type")
        ts = m.parse_ts(entry.get("timestamp"))
        if kind not in ("turn_context", "event_msg", "response_item"):
            return
        # Token/service records must not keep an otherwise silent session alive.
        activity = (
            kind == "turn_context"
            or (
                kind == "event_msg"
                and typ
                in (
                    "task_started",
                    "turn_started",
                    "task_complete",
                    "turn_complete",
                    "turn_aborted",
                    "agent_message",
                    "user_message",
                )
            )
            or (
                kind == "response_item"
                and typ
                in ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output", "message")
            )
        )
        if ts and activity:
            self.main.started = self.main.started or ts
            self.main.last_ts = ts
        if kind == "turn_context":
            self.main.model = m._s(payload.get("model")) or self.main.model
            self.info.cwd = m._s(payload.get("cwd")) or self.info.cwd
        elif kind == "event_msg":
            if typ == "item_completed":
                self._command_completed(payload.get("item"), ts)
            if typ in ("task_started", "turn_started", "user_message"):
                self.info.status, self.info.since = m.BUSY, ts
            elif typ in ("task_complete", "turn_complete", "turn_aborted"):
                self.info.status, self.info.since = m.IDLE, ts
                text = m.first_line(payload.get("last_agent_message"), 120)
                if text:
                    self.main.last_action = m.Action("", text)
            elif typ == "agent_message":
                self.main.last_action = m.Action("", m.first_line(payload.get("message"), 120))
            elif typ == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    last, total = info.get("last_token_usage"), info.get("total_token_usage")
                    if isinstance(last, dict):
                        self.main.context_tokens = m._int(last.get("input_tokens"))
                    if isinstance(total, dict):
                        self.main.output_by_msg["codex"] = m._int(total.get("output_tokens"))
        elif kind == "response_item":
            if typ in ("function_call", "custom_tool_call"):
                self.info.status, self.info.since = m.BUSY, ts
                name = m._s(payload.get("name")) or "?"
                raw = payload.get("arguments") if typ == "function_call" else payload.get("input")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except (ValueError, RecursionError):
                    args = None
                short_name = name.rsplit(".", 1)[-1]
                if short_name in ("exec_command", "write_stdin", "exec", "wait"):
                    command_args = args if isinstance(args, dict) else {}
                    if short_name == "exec" and isinstance(raw, str):
                        literals = re.findall(r"""\bcmd\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""", raw)
                        if len(literals) == 1:
                            with contextlib.suppress(ValueError, SyntaxError):
                                command_args = {"cmd": ast.literal_eval(literals[0])}
                    self._calls[m._s(payload.get("call_id"))] = (short_name, command_args, ts)
                if isinstance(args, dict):
                    text = next(
                        (
                            args[k]
                            for k in ("description", "cmd", "command", "file_path", "path", "query", "title", "target")
                            if args.get(k)
                        ),
                        "",
                    )
                else:
                    text = raw if typ == "custom_tool_call" else ""
                action = m.Action(name, m.first_line(text, 120))
                if name.startswith("mcp__"):
                    action = m.Action(m.describe_tool(name, args).tool, action.arg)
                self.main.tool_calls += 1
                self.main.last_action = action
                if ts:
                    self.main.tool_ts.append(ts)
                self._own_events.append(m.Event(ts or m.now(), self.main.label, "tool", action=action))
                self._own_events = self._own_events[-300:]
            elif typ in ("function_call_output", "custom_tool_call_output"):
                self._command_result(payload, ts)
            elif typ == "message" and payload.get("role") == "assistant":
                content = payload.get("content")
                if isinstance(content, list):
                    text = " ".join(m._raw(c.get("text")) for c in content if isinstance(c, dict))
                    if text:
                        self.main.last_action = m.Action("", m.first_line(text, 120))


def scan(cache: dict[Path, CodexWatch], within_sec: float, procs: m.ProcTable | None = None) -> dict[str, CodexWatch]:
    """Incrementally read recent rollouts and attach child threads to their parent session."""
    cutoff = m.now().timestamp() - within_sec
    files = []
    candidates = [p.pid for p in procs.procs.values() if p.name in ("codex", "codex.exe")] if procs else []
    opened = m.open_files(candidates) if candidates else {}
    writers = {os.path.realpath(path): pid for pid, fds in opened.items() for _, path in fds}
    all_paths = []
    try:
        all_paths = list((CODEX_DIR / "sessions").glob("*/*/*/rollout-*.jsonl"))
        for path in all_paths:
            if m._mtime(path) >= cutoff or os.path.realpath(path) in writers:
                files.append(path)
    except OSError:
        pass
    for path in files:
        try:
            if path not in cache:
                cache[path] = CodexWatch(path)
            watch = cache[path]
            watch.children.clear()
            watch.info.pid = writers.get(os.path.realpath(path), 0)
            watch.writer_alive = bool(watch.info.pid)
            m.Tracker.guard(cache[path], cache[path].poll)
            if watch.parent_id:
                parent = watch.parent_id.removeprefix("codex:")
                parent_path = next((p for p in all_paths if p.name.endswith(f"-{parent}.jsonl")), None)
                if parent_path and parent_path not in files:
                    files.append(parent_path)
        except (OSError, *m.RECORD_ERRORS):
            continue
    for path in set(cache) - set(files):
        del cache[path]
    refresh_processes(cache, procs)
    watches = {w.session_id: w for w in cache.values()}
    for w in watches.values():
        w.subagents.clear()
        w.children.clear()
    roots = {}
    for sid, w in watches.items():
        root, seen = w, set()
        while root.parent_id in watches and root.parent_id not in seen:
            seen.add(root.session_id)
            root = watches[root.parent_id]
        if root is w or root.session_id in seen:
            roots[sid] = w
        else:
            root.subagents[w.transcript] = w.main
            root.children.append(w)
    # A parent may finish its own turn while a delegated thread continues.
    for w in roots.values():
        w.merge_events()
        w.procs = sorted([*w.procs, *(p for child in w.children for p in child.procs)], key=lambda p: -p.cpu)
        w.bg_agents = [a for a in w.subagents.values() if a.status == m.RUNNING]
        if w.bg_agents:
            w.info.alive = True
    return roots


def refresh_processes(cache: dict[Path, CodexWatch], procs: m.ProcTable | None) -> None:
    if procs is None:
        return
    candidates = {p.pid for w in cache.values() if w.info.pid for p in procs.descendants(w.info.pid)}
    candidates -= {os.getpid(), *(p.pid for p in procs.descendants(os.getpid()))}
    threads = process_threads(sorted(candidates))
    mapped: dict[tuple[Path, str], tuple[m.Proc, list[m.Proc]]] = {}
    claims: dict[int, list[tuple[Path, str]]] = {}
    for path, w in cache.items():
        descendants = {p.pid for p in procs.descendants(w.info.pid)} if w.info.pid else set()
        for task in w._running_own():
            for pid in descendants & candidates:
                p = procs.get(pid)
                if not p or not command_matches(w._commands.get(task.task_id) or task.description, p):
                    continue
                if pid in threads:
                    eligible = threads[pid] == w.session_id.removeprefix("codex:")
                else:
                    # SIP hides environments of macOS system binaries. A matching
                    # launch under the verified writer is usable only when unique.
                    eligible = bool(task.started and abs(procs.taken - p.elapsed - task.started.timestamp()) < 60)
                if eligible:
                    claims.setdefault(pid, []).append((path, task.task_id))
    for path, w in cache.items():
        w.procs = []
        for task in w._running_own():
            matches = [procs.procs[pid] for pid, owners in claims.items() if owners == [(path, task.task_id)]]
            nested = {p.pid for match in matches for p in procs.descendants(match.pid)}
            matches = [p for p in matches if p.pid not in nested]
            if len(matches) == 1:
                shell = matches[0]
                live = [p for p in procs.descendants(shell.pid) if "<defunct>" not in p.args]
                parents = {p.ppid for p in live}
                leaves = [p for p in live if p.pid not in parents] or [shell]
                leaf = max(leaves, key=lambda p: p.cpu)
                w.procs.append(leaf)
                w._task_pids[task.task_id] = {
                    p.pid: (m.proc_start_ticks(p.pid), procs.taken - p.elapsed) for p in (shell, leaf)
                }
                mapped[path, task.task_id] = (leaf, [shell, *live])
            elif task.task_id in w._task_pids and not any(
                m.pid_alive(pid, datetime.fromtimestamp(started, timezone.utc), procs, ticks)
                for pid, (ticks, started) in w._task_pids[task.task_id].items()
            ):
                task.status, task.ended = "stale", m.now()
                task.log_path, task.log_line, task.log_mtime = "", "", None
        w.procs.sort(key=lambda p: -p.cpu)
    files = m.open_files({p.pid for _, group in mapped.values() for p in group}) if mapped else {}
    for (path, sid), (leaf, _) in mapped.items():
        task = cache[path]._tasks[sid]
        output = next((name for fd, name in files.get(leaf.pid, ()) if fd in (1, 2)), "")
        task.log_path, task.log_line, task.log_mtime = output, "", None
        if output and (got := m.last_output_line(Path(output))):
            task.log_line, _, task.log_mtime = got
