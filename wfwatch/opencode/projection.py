"""OpenCode rows to the shared model: an agent's actions, usage and background tasks.

A message is stored as one row with a list of parts; while the agent streams, that row is updated
in place. The projection therefore applies rows idempotently: a call is counted once by its id,
and the last action follows the newest message. Applying the same rows again changes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone

from .. import model as m
from .store import JobRow, MessageRow

WAITING_TOOLS = frozenset({"question", "plan", "permission"})
_STATUS = {"completed": "completed", "error": "failed", "cancelled": "killed"}
_SHELL_TEXT = re.compile(r"<shell\b[^>]*>(.*?)</shell>", re.DOTALL)
_SUBAGENT_TEXT = re.compile(r"<subagent\b[^>]*>(.*?)</subagent>", re.DOTALL)
_SEEN_MAX = 5000  # message stamps kept; older rows are never re-read

# OpenCode names its tools in lower case; the shared views know the Claude names.
TOOL_NAMES = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "shell": "Bash",
    "bash": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Agent",
    "subagent": "Agent",
    "skill": "Skill",
}


def describe_tool(name: str, inp: dict) -> m.Action:
    """One OpenCode tool call in the words the cards and the log already use."""
    name = m._s(name)
    inp = inp if isinstance(inp, dict) else {}
    if name in TOOL_NAMES:
        renamed = TOOL_NAMES[name]
        return m.describe_tool(renamed, _claude_input(renamed, inp))
    if name == "question":
        questions = inp.get("questions")
        first = questions[0] if isinstance(questions, list) and questions else {}
        text = first.get("question") or first.get("header") if isinstance(first, dict) else ""
        return m.Action("Question", m.first_line(text, 80))
    if name == "execute":
        return m.Action("Code", m.first_line(inp.get("code"), 80))
    if name == "list":
        return m.Action("List", m.short_path(inp.get("path")))
    if name == "todowrite":
        return m.Action("Todo", m.first_line(inp.get("todos"), 80))
    if name.startswith("mcp__"):
        return m.describe_tool(name, inp)
    return m.Action(name or "?", m.first_line(inp.get("description"), 80))


def _claude_input(name: str, inp: dict) -> dict:
    """The argument names `model.describe_tool` reads, filled from where OpenCode keeps them."""
    out = dict(inp)
    if "file_path" not in out:
        out["file_path"] = inp.get("path") or inp.get("filePath") or inp.get("file")
    if name == "Skill" and "skill" not in out:
        out["skill"] = inp.get("id") or inp.get("name")
    return out


def output_line(text: str) -> str:
    """The last non-empty line of a command's output, without ANSI control sequences."""
    lines = [m.clean(line).strip() for line in m.without_ansi(text or "").splitlines()]
    return next((line for line in reversed(lines) if line), "")[:500]


def _within(text: str, pattern: re.Pattern) -> str:
    match = pattern.search(text or "")
    return match[1] if match else ""


class Projection:
    """Everything derived from the rows of one OpenCode session. The same rows twice change nothing."""

    def __init__(self, agent: m.Agent, label: str):
        self.agent = agent
        self.label = label
        self.tasks: dict[str, m.BgTask] = {}
        self.events: list[m.Event] = []
        self.reports: dict[str, tuple[str, str]] = {}  # child session id -> (state, result)
        self.running_children: set[str] = set()
        self._seen: dict[str, tuple[int, int, int]] = {}
        self._calls: set[str] = set()
        self._tools_running: dict[str, str] = {}
        self._tool_started: dict[str, datetime] = {}
        self._last_key = (0, 0)

    @property
    def waiting(self) -> bool:
        return any(name in WAITING_TOOLS for name in self._tools_running.values())

    @property
    def waiting_since(self) -> datetime | None:
        """When the question the agent waits on was asked."""
        times = [self._tool_started[i] for i, name in self._tools_running.items() if name in WAITING_TOOLS]
        return min(times) if times else None

    def apply(self, rows: Iterable[MessageRow]) -> None:
        for row in rows:
            try:
                self._apply(row)
            except m.RECORD_ERRORS:  # one damaged row must not hide the session
                continue
        self.events.sort(key=lambda e: e.ts)
        if len(self.events) > 300:
            del self.events[: len(self.events) - 300]
        if len(self._seen) > _SEEN_MAX:
            for msg_id in list(self._seen)[: len(self._seen) - _SEEN_MAX]:
                del self._seen[msg_id]

    # ---- one message ----

    def _apply(self, row: MessageRow) -> None:
        stamp = (int(row.updated.timestamp() * 1000) if row.updated else 0, row.seq, row.digest)
        if self._seen.get(row.id) == stamp:
            return
        self._seen[row.id] = stamp
        key = (int(row.created.timestamp() * 1000) if row.created else 0, row.seq)
        if row.created or row.updated:
            self._touch(row.created, row.updated)
        if row.type == "assistant":
            self._assistant(row, key)
        elif row.type == "idle":
            self._idle(row)
        elif row.type == "synthetic":
            self._synthetic(row)

    def _touch(self, created: datetime | None, updated: datetime | None = None) -> None:
        """A message row is rewritten while the agent streams: the row's `updated` is activity now,
        even when the message was created hours ago. `created` only says when it began."""
        last = updated or created
        if last is not None and (self.agent.last_ts is None or last > self.agent.last_ts):
            self.agent.last_ts = last
        if created is not None and (self.agent.started is None or created < self.agent.started):
            self.agent.started = created

    def _assistant(self, row: MessageRow, key: tuple[int, int]) -> None:
        data = row.data
        model = data.get("model")
        if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"]:
            self.agent.model = m._s(model["id"])
        content = data.get("content")
        if not isinstance(content, list):
            return
        last: m.Action | None = None
        for part in content:
            action = self._part(row, part, key)
            if action is not None:
                last = action
        if last is not None and key >= self._last_key:
            self._last_key = key
            self.agent.last_action = last

    def _part(self, row: MessageRow, part, key: tuple[int, int]) -> m.Action | None:
        if not isinstance(part, dict):
            return None
        kind = part.get("type")
        if kind == "text":
            text = part.get("text")
            if isinstance(text, str) and (line := m.first_line(text)):
                return m.Action("", m.first_line(line, 120))
            return None
        if kind != "tool":
            return None
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        status = state.get("status")
        if status == "streaming":  # the arguments are not complete yet
            return None
        call_id = m._s(part.get("id"))
        if not call_id:
            return None
        inp = state.get("input")
        inp = inp if isinstance(inp, dict) else {}
        action = describe_tool(part.get("name"), inp)
        name = m._s(part.get("name"))
        if status == "running":
            self._tools_running[call_id] = name
            if (started := _part_time(part) or row.created) is not None:
                self._tool_started[call_id] = started
        else:
            self._tools_running.pop(call_id, None)
            self._tool_started.pop(call_id, None)
        if call_id not in self._calls:
            self._calls.add(call_id)
            self.agent.tool_calls += 1
            ts = _part_time(part) or row.created
            if ts:
                self.agent.tool_ts.append(ts)
                self.agent.last_ts = max(self.agent.last_ts or ts, ts)
            self.events.append(m.Event(ts or row.created or m.now(), self.label, "tool", action=action))
        return action

    def _idle(self, row: MessageRow) -> None:
        outcome = m._s(row.data.get("outcome")) or "succeeded"
        if outcome in ("failed", "interrupted"):
            kind = "failed" if outcome == "failed" else "stale"
            self.events.append(m.Event(row.created or m.now(), self.label, kind, detail=outcome))

    def _synthetic(self, row: MessageRow) -> None:
        metadata = row.data.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        source = m._s(metadata.get("source"))
        text = row.data.get("text") if isinstance(row.data.get("text"), str) else ""
        if source == "shell":
            self._shell_done(row, metadata, text)
        elif source == "subagent":
            child = m._s(metadata.get("childID"))
            if child:
                result = m.clean(_within(text, _SUBAGENT_TEXT) or text)
                self.reports[child] = (m._s(metadata.get("state")) or "completed", result)

    def _shell_done(self, row: MessageRow, metadata: dict, text: str) -> None:
        shell_id = m._s(metadata.get("shellID") or metadata.get("jobID"))
        if not shell_id:
            return
        state = m._s(metadata.get("state"))
        exit_code = metadata.get("exit")
        exit_code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
        status = _STATUS.get(state, "completed")
        if exit_code not in (None, 0):
            status = "failed"
        task = self.tasks.get(shell_id)
        if task is None:
            task = self.tasks[shell_id] = m.BgTask(shell_id, m._s(row.data.get("description")), row.created or m.now())
        body = _within(text, _SHELL_TEXT) or text
        task.status = status
        task.ended = row.created or m.now()
        task.exit_code = exit_code
        line = output_line(body)
        if line:
            task.last_line = line
            task.output_size = len(body.encode("utf-8"))
            task.output_mtime = row.created

    # ---- background jobs ----

    def apply_jobs(self, jobs: Iterable[JobRow], outputs: dict[str, object]) -> None:
        running: set[str] = set()
        for job in jobs:
            if job.kind == "subagent":
                if job.status == "running" and job.child_session_id:
                    running.add(job.child_session_id)
                continue
            if job.kind != "shell" or not job.id:
                continue
            task = self.tasks.get(job.id)
            if task is not None and task.status != m.RUNNING:
                continue  # a completed task is not resurrected by a row that outlived it
            if task is None:
                description = m.first_line(job.title or job.command, 120)
                task = self.tasks[job.id] = m.BgTask(job.id, description, job.created or m.now())
            output = outputs.get(job.id)
            if output:
                task.output_path = str(output)
            if job.status in _STATUS:
                task.status = _STATUS.get(job.status, "completed")
                task.ended = job.updated or m.now()
            if job.output:
                line = output_line(job.output)
                if line:
                    task.last_line = line
                    task.output_mtime = job.updated
        self.running_children = running
        for task_id, path in outputs.items():
            task = self.tasks.get(task_id)
            if task is not None:
                # The job row can be gone while its command still writes: the file is the evidence.
                task.output_path = str(path)


def _part_time(part: dict) -> datetime | None:
    time = part.get("time")
    if not isinstance(time, dict):
        return None
    value = time.get("created")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
