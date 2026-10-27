#!/usr/bin/env python3
"""Synthetic Claude Code data: to try wfwatch without workflows of your own, and for the screenshots.

Writes a fake CLAUDE_CONFIG_DIR where every project, session and agent is made up:

- workflow "api-hardening": running, three phases, six agents (the third phase starts later);
- workflow "dependency-audit": completed 19 minutes ago;
- session "cache-refactor": working, two subagents;
- session "docs-rewrite": waiting for your answer;
- session "nightly-bench": idle, but a background command (Bash with run_in_background) runs;
- session "release-prep": idle, it launched "api-hardening".

Usage:

    python scripts/demo_data.py                      # into a new temporary directory, prints its path
    python scripts/demo_data.py /tmp/wf-demo --live  # and keep it going (Ctrl+C to stop)
    CLAUDE_CONFIG_DIR=/tmp/wf-demo wfwatch           # in another terminal

With --live, agents keep calling tools and finish, the third phase starts and the workflow
completes, the background command prints its progress and ends: about two minutes of activity.
The script starts a tiny process per session (wfwatch checks that a session's process is alive,
and finds the processes started by its Bash tool) and stops them when it exits.

Without --live the sessions are tied to the process that ran the script (its parent, usually your
shell; --pid to choose another one): they look alive while it runs.

Only the standard library is needed. Your own ~/.claude is never read or written: the target
directory must be empty or made by this script before (it is then rebuilt).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MARKER = ".wfwatch-demo"  # a directory with this file was made by this script and may be rebuilt
HOME = "/home/user/dev"  # where the made-up projects live
STORY_END = 110.0  # seconds after the start when the last scripted event happens

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-4-5", "claude-haiku-4-5"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def uid(*parts: str, n: int = 16) -> str:
    """A stable made-up id."""
    return hashlib.sha1("/".join(parts).encode()).hexdigest()[:n]


def slug(cwd: str) -> str:
    """The projects/<slug> directory name Claude Code derives from a working directory."""
    return "".join(c if c.isalnum() else "-" for c in cwd)


# ---- tool calls ----

Call = tuple[str, dict]


def read(path: str) -> Call:
    return "Read", {"file_path": path}


def edit(path: str) -> Call:
    return "Edit", {"file_path": path, "old_string": "…", "new_string": "…"}


def write(path: str) -> Call:
    return "Write", {"file_path": path, "content": "…"}


def bash(description: str, command: str) -> Call:
    return "Bash", {"command": command, "description": description}


def grep(pattern: str) -> Call:
    return "Grep", {"pattern": pattern}


def glob(pattern: str) -> Call:
    return "Glob", {"pattern": pattern}


def search(query: str) -> Call:
    return "WebSearch", {"query": query}


# ---- the story ----


@dataclass
class AgentSpec:
    label: str
    phase: int  # index into the workflow's phases
    model: str
    # (start, end, period): when it starts and returns its result, in seconds relative to the moment the
    # data is made (negative – in the past; end None – still working), and the seconds between its tool calls
    when: tuple[float, float | None, float]
    calls: list[Call]  # cycled
    result: str = ""


@dataclass
class WorkflowSpec:
    run_id: str
    task_id: str
    name: str
    description: str
    phases: list[str]
    agents: list[AgentSpec]
    launched: float
    finished: float | None  # when the completion notification comes


@dataclass
class SessionSpec:
    sid: str
    name: str
    cwd: str
    entrypoint: str  # cli | claude-vscode
    model: str
    status: str  # busy | waiting | idle, at the start
    since: float  # when the status was set


def story(lang: str) -> tuple[list[WorkflowSpec], list[SessionSpec]]:
    """Everything that happens, in English or Russian (names of sessions and workflows stay)."""

    def L(en: str, ru: str) -> str:  # noqa: N802 – reads like a table
        return ru if lang == "ru" else en

    api, web = f"{HOME}/shop-api", f"{HOME}/web-frontend"
    api_tests = bash(L("Run API tests", "Тесты API"), "pytest tests/api -q")

    hardening = WorkflowSpec(
        run_id="wf_1a2b3c4d-5e6",
        task_id="w7k3m9q2x",
        name="api-hardening",
        description=L("Harden the public API before the 2.0 release", "Укрепить публичный API перед релизом 2.0"),
        phases=[L("Survey", "Анализ"), L("Fix", "Исправления"), L("Review", "Ревью")],
        launched=-9 * 60,
        finished=96,
        agents=[
            AgentSpec(
                label=L("Map endpoints", "Карта эндпоинтов"),
                phase=0,
                model=SONNET,
                when=(-530, -330, 14),
                calls=[glob("src/api/**/*.py"), read(f"{api}/src/api/routes.py"), grep("@router.get")],
                result=L("34 public endpoints, 6 without an auth check", "34 эндпоинта, у 6 нет проверки авторизации"),
            ),
            AgentSpec(
                label=L("Find test gaps", "Пробелы в тестах"),
                phase=0,
                model=SONNET,
                when=(-528, -280, 17),
                calls=[bash(L("Coverage report", "Отчёт о покрытии"), "pytest --cov=src -q"), grep("def test_")],
                result=L("11 handlers have no negative tests", "У 11 обработчиков нет негативных тестов"),
            ),
            AgentSpec(
                label=L("Auth handlers", "Авторизация"),
                phase=1,
                model=OPUS,
                when=(-270, 41, 9),
                calls=[
                    read(f"{api}/src/auth/handlers.py"),
                    grep("require_user"),
                    edit(f"{api}/src/auth/handlers.py"),
                    bash(L("Run auth tests", "Тесты авторизации"), "pytest tests/test_auth.py -q"),
                    edit(f"{api}/tests/test_auth.py"),
                ],
                result=L("Auth checks on 6 endpoints, 14 new tests", "Проверка авторизации на 6 эндпоинтах, 14 тестов"),
            ),
            AgentSpec(
                label=L("Rate limits", "Лимиты запросов"),
                phase=1,
                model=SONNET,
                when=(-268, 14, 11),
                calls=[
                    read(f"{api}/src/middleware/limits.py"),
                    edit(f"{api}/src/middleware/limits.py"),
                    grep("RATE_LIMIT"),
                    bash(L("Short load test", "Короткий нагрузочный тест"), "make load-test"),
                ],
                result=L("Per-key limits on 12 endpoints", "Лимиты по ключу на 12 эндпоинтах"),
            ),
            AgentSpec(
                label=L("Input validation", "Проверка ввода"),
                phase=1,
                model=OPUS,
                when=(-265, -70, 12),
                calls=[read(f"{api}/src/api/orders.py"), edit(f"{api}/src/api/orders.py"), api_tests],
                result=L("Validation on 9 handlers, all tests pass", "Проверка ввода в 9 обработчиках, тесты зелёные"),
            ),
            AgentSpec(
                label=L("Error responses", "Ответы об ошибках"),
                phase=1,
                model=HAIKU,
                when=(-262, 30, 7),
                calls=[
                    grep("raise HTTPException"),
                    read(f"{api}/src/api/errors.py"),
                    edit(f"{api}/src/api/errors.py"),
                    api_tests,
                ],
                result=L("One error format across 34 endpoints", "Единый формат ошибок на 34 эндпоинтах"),
            ),
            AgentSpec(
                label=L("Security review", "Ревью безопасности"),
                phase=2,
                model=OPUS,
                when=(43, 90, 6),
                calls=[
                    read(f"{api}/src/auth/handlers.py"),
                    grep("token"),
                    search("OWASP API security top 10"),
                    read(f"{api}/src/middleware/limits.py"),
                ],
                result=L("No blockers; two notes for the 2.1 backlog", "Блокеров нет, два замечания в бэклог 2.1"),
            ),
            AgentSpec(
                label=L("API review", "Ревью API"),
                phase=2,
                model=SONNET,
                when=(44, 80, 7),
                calls=[
                    read(f"{api}/docs/api.md"),
                    bash(L("Diff the public API schema", "Сравнить схему API"), "make api-diff"),
                    read(f"{api}/src/api/errors.py"),
                ],
                result=L("Schema changes are backward compatible", "Изменения схемы обратно совместимы"),
            ),
        ],
    )

    audit = WorkflowSpec(
        run_id="wf_9d02b6e4-c55",
        task_id="w2p8r4t6z",
        name="dependency-audit",
        description=L("Check dependencies for known vulnerabilities", "Проверить зависимости на известные уязвимости"),
        phases=[L("Scan", "Сканирование"), L("Report", "Отчёт")],
        launched=-41 * 60,
        finished=-19 * 60,
        agents=[
            AgentSpec(
                label=L("npm packages", "Пакеты npm"),
                phase=0,
                model=HAIKU,
                when=(-2440, -1820, 25),
                calls=[bash(L("Audit npm packages", "Аудит пакетов npm"), "npm audit --json")],
                result=L("2 moderate issues, fixes available", "2 уязвимости средней важности, есть исправления"),
            ),
            AgentSpec(
                label=L("Python packages", "Пакеты Python"),
                phase=0,
                model=HAIKU,
                when=(-2438, -1700, 25),
                calls=[bash(L("Audit Python packages", "Аудит пакетов Python"), "pip-audit -f json")],
                result=L("No known vulnerabilities", "Известных уязвимостей нет"),
            ),
            AgentSpec(
                label=L("Docker images", "Образы Docker"),
                phase=0,
                model=SONNET,
                when=(-2436, -1560, 30),
                calls=[bash(L("Scan the base image", "Сканировать базовый образ"), "trivy image node:22-slim")],
                result=L("Base image is up to date", "Базовый образ актуален"),
            ),
            AgentSpec(
                label=L("Summary", "Сводка"),
                phase=1,
                model=OPUS,
                when=(-1550, -1150, 20),
                calls=[read(f"{web}/package.json"), write(f"{web}/docs/security/audit.md")],
                result=L("Report written to docs/security/audit.md", "Отчёт записан в docs/security/audit.md"),
            ),
        ],
    )

    sessions = [
        SessionSpec("5c1e9a70-0d4b-4f2e-9b1a-6f3e2d8c7a10", "release-prep", api, "cli", OPUS, "idle", -525),
        SessionSpec("8b7f2c14-6e3a-4d9b-a2c5-1e4f7d0b9c32", "cache-refactor", api, "claude-vscode", OPUS, "busy", -190),
        SessionSpec(
            "2e6d4b81-9f7c-4a35-8d2e-5b0c1a7f6e94", "docs-rewrite", f"{HOME}/docs-site", "cli", SONNET, "waiting", -140
        ),
        SessionSpec(
            "d40a7e5b-3c2f-4e81-b6d9-7a1f0e3c5b28", "nightly-bench", f"{HOME}/perf-lab", "cli", OPUS, "idle", -300
        ),
        SessionSpec("71f3c0a9-8e2d-4b6c-9a4f-3d5e1b7c0f63", "deps-bump", web, "cli", SONNET, "idle", -1110),
    ]
    return [hardening, audit], sessions


# ---- writing ----

Row = Callable[[datetime], dict]  # a record, made when it is written (it carries the time)


def notification(task_id: str, status: str, summary: str, output: str = "") -> str:
    """The <task-notification> Claude Code puts into a session when a background task ends."""
    out = f"<output-file>{output}</output-file>\n" if output else ""
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n{out}<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n</task-notification>"
    )


def queued(content: str) -> Row:
    return lambda ts: {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(ts), "content": content}


def prompt(cwd: str, sid: str = "", **fields) -> Row:
    """A user record: a prompt, or (with fields) a tool result."""
    return lambda ts: {
        "type": "user",
        "timestamp": iso(ts),
        "cwd": cwd,
        **({"sessionId": sid} if sid else {"isSidechain": True}),
        "message": {"role": "user", "content": "…"},
        **fields,
    }


def tool_result(tool_id: str, text: str) -> dict:
    return {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


@dataclass
class Op:
    at: float  # seconds relative to the start
    seq: int
    fn: Callable[[datetime], None]  # gets the timestamp to write


class Demo:
    """Builds the directory and plays the story: ``advance(t)`` writes everything up to t seconds."""

    def __init__(self, home: Path, lang: str = "en", pids: dict[str, int] | None = None, start: datetime | None = None):
        self.home = home
        self.lang = lang
        self.start = start or datetime.now(timezone.utc)
        self.workflows, self.sessions = story(lang)
        self.pids = pids or {}  # session name -> pid (default: the parent of this script)
        self.ops: list[Op] = []
        self.done = 0
        self.on_bg_end: Callable[[], None] | None = None  # --live: stop the process of the background command
        self._msg = 0
        self._plan()

    def L(self, en: str, ru: str) -> str:  # noqa: N802
        return ru if self.lang == "ru" else en

    # ---- paths ----

    def session(self, name: str) -> SessionSpec:
        return next(s for s in self.sessions if s.name == name)

    def transcript(self, s: SessionSpec) -> Path:
        return self.home / "projects" / slug(s.cwd) / f"{s.sid}.jsonl"

    def output_file(self, s: SessionSpec, task_id: str) -> Path:
        return self.home / "tmp" / slug(s.cwd) / s.sid / "tasks" / f"{task_id}.output"

    # ---- primitives ----

    def add(self, t: float, fn: Callable[[datetime], None]) -> None:
        self.ops.append(Op(t, len(self.ops), fn))

    def put(self, t: float, path: Path, *rows: Row | str) -> None:
        """At t, append rows (records, or plain lines of text) to a file."""
        self.add(t, lambda ts: self.append(path, ts, *(r if isinstance(r, str) else r(ts) for r in rows)))

    @staticmethod
    def append(path: Path, ts: datetime, *rows: dict | str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")
        os.utime(path, (ts.timestamp(), ts.timestamp()))  # as if written then

    def reply(self, model: str, content: dict, ctx: int, cwd: str) -> Row:
        """An assistant message: a tool call or a text."""

        def row(ts: datetime) -> dict:
            self._msg += 1
            usage = {
                "input_tokens": 6,
                "cache_read_input_tokens": ctx,
                "cache_creation_input_tokens": 900,
                "output_tokens": 120 + (self._msg * 37) % 480,
            }
            msg = {"id": f"msg_{uid('m', str(self._msg), n=20)}", "role": "assistant", "model": model}
            return {
                "type": "assistant",
                "timestamp": iso(ts),
                "cwd": cwd,
                "message": {**msg, "content": [content], "usage": usage},
            }

        return row

    @staticmethod
    def tool_use(call: Call, tool_id: str) -> dict:
        return {"type": "tool_use", "id": tool_id, "name": call[0], "input": call[1]}

    @staticmethod
    def text(s: str) -> dict:
        return {"type": "text", "text": s}

    def registry(self, s: SessionSpec, status: str, ts: datetime) -> None:
        """sessions/<pid>.json, rewritten whole as Claude Code does."""
        pid = self.pids.get(s.name) or os.getppid()
        shared = list(self.pids.values()).count(pid) > 1 or not self.pids
        path = self.home / "sessions" / (f"{pid}-{s.name}.json" if shared else f"{pid}.json")
        rec = {
            "pid": pid,
            "sessionId": s.sid,
            "name": s.name,
            "status": status,
            "cwd": s.cwd,
            "entrypoint": s.entrypoint,
            "kind": "interactive",
            "statusUpdatedAt": ms(ts),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # what a session does
    def act(self, s: SessionSpec, t: float, call: Call, ctx: int = 60_000) -> None:
        tool_id = f"toolu_{uid(s.sid, str(t), call[0], n=20)}"
        self.put(t, self.transcript(s), self.reply(s.model, self.tool_use(call, tool_id), ctx, s.cwd))

    def say(self, s: SessionSpec, t: float, msg: str, ctx: int = 60_000) -> None:
        self.put(t, self.transcript(s), self.reply(s.model, self.text(msg), ctx, s.cwd))

    def status(self, s: SessionSpec, t: float, status: str) -> None:
        self.add(t, lambda ts: self.registry(s, status, ts))

    # ---- the plan ----

    def _plan(self) -> None:
        for s in self.sessions:
            self.put(min(s.since, -600) - 60, self.transcript(s), prompt(s.cwd, s.sid))
            self.status(s, s.since, s.status)
        owners = {"api-hardening": "release-prep", "dependency-audit": "deps-bump"}
        for w in self.workflows:
            self._plan_workflow(self.session(owners[w.name]), w)
        self._plan_release_prep()
        self._plan_cache_refactor()
        self._plan_docs_rewrite()
        self._plan_nightly_bench()
        self._plan_deps_bump()
        self.ops.sort(key=lambda o: (o.at, o.seq))

    def _plan_workflow(self, s: SessionSpec, w: WorkflowSpec) -> None:
        session_dir = self.transcript(s).with_suffix("")
        run = session_dir / "subagents" / "workflows" / w.run_id
        script = session_dir / "workflows" / "scripts" / f"{w.name}-{w.run_id}.js"
        phases = "".join(f"    {{ title: {json.dumps(p, ensure_ascii=False)} }},\n" for p in w.phases)
        js = (
            f"export const meta = {{\n  name: {json.dumps(w.name)},\n"
            f"  description: {json.dumps(w.description, ensure_ascii=False)},\n  phases: [\n{phases}  ],\n}}\n\n"
            "// (the rest of the workflow script is not needed for the demo)\n"
        )
        tool_id = f"toolu_{uid(w.run_id, n=20)}"
        launched = {
            "status": "async_launched",
            "taskId": w.task_id,
            "taskType": "local_workflow",
            "runId": w.run_id,
            "workflowName": w.name,
            "summary": w.description,
            "scriptPath": str(script),
            "transcriptDir": str(run),
        }
        self.add(
            w.launched,
            lambda ts: (script.parent.mkdir(parents=True, exist_ok=True), script.write_text(js, encoding="utf-8")),
        )
        self.put(w.launched, run / "journal.jsonl", lambda ts: {"type": "launched", "runId": w.run_id})
        self.put(
            w.launched,
            self.transcript(s),
            self.reply(s.model, self.tool_use(("Workflow", {"script": str(script)}), tool_id), 52_000, s.cwd),
            prompt(s.cwd, s.sid, toolUseResult=launched, **tool_result(tool_id, f"Workflow {w.name} launched.")),
        )
        for i, a in enumerate(w.agents):
            self._plan_agent(run, w, a, f"a{uid(w.run_id, a.label)}", f"k{i}", s.cwd)
        if w.finished is not None:
            note = notification(w.task_id, "completed", f'Workflow "{w.name}" completed')
            self.put(w.finished, self.transcript(s), queued(note))

    def _plan_agent(self, run: Path, w: WorkflowSpec, a: AgentSpec, agent_id: str, key: str, cwd: str) -> None:
        path = run / f"agent-{agent_id}.jsonl"
        start, end, period = a.when
        record = {"type": "started", "key": key, "agentId": agent_id, "label": a.label, "phase": w.phases[a.phase]}
        self.put(start, run / "journal.jsonl", lambda ts: record)
        self.put(start, path, prompt(cwd))
        n, t = 0, start + 3
        while t < (end if end is not None else STORY_END + 30) - 1:
            call = self.tool_use(a.calls[n % len(a.calls)], f"toolu_{uid(agent_id, str(n), n=20)}")
            self.put(t, path, self.reply(a.model, call, 14_000 + 2_600 * n, cwd))
            n += 1
            t += period * (0.75 + 0.5 * ((n * 7) % 5) / 4)  # a little irregular, but reproducible
        if end is not None:
            self.put(end, path, self.reply(a.model, self.text(a.result), 14_000 + 2_600 * n, cwd))
            result = {"type": "result", "key": key, "agentId": agent_id, "result": a.result}
            self.put(end, run / "journal.jsonl", lambda ts: result)

    def _plan_subagent(self, s: SessionSpec, desc: str, when: tuple[float, float, float], calls: list[Call]) -> None:
        """An ordinary subagent of a session (the Agent tool): its own transcript and a .meta.json."""
        agent_id = f"a{uid(s.sid, desc)}"
        path = self.transcript(s).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        meta = path.with_name(f"agent-{agent_id}.meta.json")
        start, end, period = when
        self.act(s, start - 1, ("Agent", {"description": desc, "prompt": "…", "subagent_type": "general-purpose"}))
        self.add(
            start,
            lambda ts: (
                path.parent.mkdir(parents=True, exist_ok=True),
                meta.write_text(
                    json.dumps({"agentType": "general-purpose", "description": desc}, ensure_ascii=False),
                    encoding="utf-8",
                ),
            ),
        )
        self.put(start, path, prompt(s.cwd))
        n, t = 0, start + 2
        while t < end:
            call = self.tool_use(calls[n % len(calls)], f"toolu_{uid(agent_id, str(n), n=20)}")
            self.put(t, path, self.reply(SONNET, call, 12_000 + 2_100 * n, s.cwd))
            n, t = n + 1, t + period
        self.put(end, path, self.reply(SONNET, self.text(self.L("Done.", "Готово.")), 12_000 + 2_100 * n, s.cwd))

    def _plan_bg_task(
        self,
        s: SessionSpec,
        task_id: str,
        desc: str,
        command: str,
        when: tuple[float, float],
        lines: list[str],
        final: str = "",
    ) -> None:
        """A Bash command started with run_in_background: its output file and the notification at the end."""
        out = self.output_file(s, task_id)
        start, end = when
        tool_id = f"toolu_{uid(task_id, n=20)}"
        call = ("Bash", {"command": command, "description": desc, "run_in_background": True})
        result = (
            f"Command running in background with ID: {task_id}. Output is being written to: {out}. "
            "You will be notified when it completes."
        )
        self.put(start, out)  # the file appears empty
        self.put(
            start,
            self.transcript(s),
            self.reply(s.model, self.tool_use(call, tool_id), 33_000, s.cwd),
            prompt(s.cwd, s.sid, toolUseResult={"backgroundTaskId": task_id}, **tool_result(tool_id, result)),
        )
        step = (end - start - 2) / len(lines)
        for i, ln in enumerate(lines):
            self.put(start + 1 + i * step, out, ln)
        if final:
            self.put(end, out, final)
        note = notification(task_id, "completed", f'Background command "{desc}" completed (exit code 0)', str(out))
        self.put(end, self.transcript(s), queued(note))
        if end > 0:
            self.add(end, lambda ts: self.on_bg_end() if self.on_bg_end else None)

    # ---- the sessions ----

    def _plan_release_prep(self) -> None:
        s = self.session("release-prep")
        self.act(s, -560, read(f"{s.cwd}/docs/release-checklist.md"), 31_000)
        self.act(
            s, -548, bash(self.L("Show open release blockers", "Открытые блокеры релиза"), "gh issue list -l blocker")
        )
        self.say(
            s,
            -527,
            self.L(
                "api-hardening is running: three phases, I will report when it is done.",
                "api-hardening запущен: три этапа, сообщу, когда закончится.",
            ),
        )
        self.status(s, 97, "busy")  # the notification woke it up
        self.act(s, 99, read(f"{s.cwd}/CHANGELOG.md"), 74_000)
        self.act(s, 102, edit(f"{s.cwd}/CHANGELOG.md"), 76_000)
        self.say(
            s,
            106,
            self.L(
                "api-hardening is done: 4 fixes, the review found no blockers.",
                "api-hardening готов: 4 исправления, ревью блокеров не нашло.",
            ),
            77_000,
        )
        self.status(s, 107, "idle")

    def _plan_cache_refactor(self) -> None:
        s = self.session("cache-refactor")
        c = s.cwd
        tests = bash(self.L("Run cache tests", "Тесты кеша"), "pytest tests/cache -q")
        for k, call in enumerate(
            [read(f"{c}/src/cache/store.py"), grep("get_cached"), edit(f"{c}/src/cache/store.py"), tests]
        ):
            self.act(s, -180 + 25 * k, call, 40_000 + 3_000 * k)
        self._plan_subagent(
            s,
            self.L("Find call sites", "Найти места вызова"),
            (-70, 64, 3.2),
            [grep("cache.get"), read(f"{c}/src/orders/service.py"), grep("@cached"), read(f"{c}/src/catalog/views.py")],
        )
        self._plan_subagent(
            s,
            self.L("Draft migration notes", "Заметки о миграции"),
            (-62, 88, 4.4),
            [
                read(f"{c}/CHANGELOG.md"),
                read(f"{c}/src/cache/store.py"),
                write(f"{c}/docs/migration-3.0.md"),
                edit(f"{c}/docs/migration-3.0.md"),
            ],
        )
        all_tests = bash(self.L("Run all tests", "Все тесты"), "pytest -q")
        for k, call in enumerate([edit(f"{c}/src/orders/service.py"), edit(f"{c}/src/catalog/views.py"), all_tests]):
            self.act(s, 92 + 5 * k, call, 70_000 + 2_000 * k)

    def _plan_docs_rewrite(self) -> None:
        s = self.session("docs-rewrite")
        guide = f"{s.cwd}/guide/getting-started.md"
        for k, call in enumerate([read(guide), grep("deprecated"), edit(guide)]):
            self.act(s, -420 + 60 * k, call, 30_000 + 4_000 * k)
        self.say(
            s,
            -141,
            self.L(
                "The guide grew to 4,000 words. Split it into two pages?",
                "Руководство выросло до 4000 слов. Разбить его на две страницы?",
            ),
            44_000,
        )

    def _plan_nightly_bench(self) -> None:
        s = self.session("nightly-bench")
        self.act(s, -900, read(f"{s.cwd}/bench/README.md"), 22_000)
        built = "Successfully built shop_api-2.0.0.tar.gz and shop_api-2.0.0-py3-none-any.whl"
        self._plan_bg_task(
            s,
            "b3n8c4h2k",
            self.L("Build release wheels", "Собрать wheel-пакеты"),
            "python -m build",
            (-840, -545),
            ["running sdist", "running bdist_wheel", built],
        )
        names = "json_parse json_dump http_roundtrip orm_select orm_bulk_insert cache_miss auth_token csv_export"
        lines = [
            f"[{i}/42] bench_{names.split()[i % 8]}  {(i * 37) % 480 / 10 + 0.4:.2f} ms ±{0.3 + i % 7 * 0.3:.1f}%"
            for i in range(1, 43)
        ]
        self._plan_bg_task(
            s,
            "b7q2w5e9r",
            self.L("Run benchmark suite", "Прогнать бенчмарки"),
            "python bench/run.py --all",
            (-330, 70),
            lines,
            final=self.L(
                "42 benchmarks, 3 slower than last night by more than 5%",
                "42 бенчмарка, 3 медленнее прошлой ночи больше чем на 5%",
            ),
        )
        self.say(
            s,
            -305,
            self.L(
                "The benchmarks run in the background; I will compare them with last night's run.",
                "Бенчмарки идут в фоне; сравню их с прошлой ночью, когда закончатся.",
            ),
            35_000,
        )
        self.status(s, 72, "busy")
        self.act(s, 74, read(f"{s.cwd}/results/latest.json"), 41_000)
        self.say(
            s,
            79,
            self.L(
                "3 regressions over 5%: http_roundtrip, search_query, csv_export.",
                "3 регрессии больше 5%: http_roundtrip, search_query, csv_export.",
            ),
            43_000,
        )
        self.status(s, 80, "idle")

    def _plan_deps_bump(self) -> None:
        s = self.session("deps-bump")
        self.act(s, -2470, read(f"{s.cwd}/package.json"), 25_000)
        self.say(
            s,
            -1120,
            self.L(
                "dependency-audit is done: 2 moderate npm issues, fixes are available.",
                "dependency-audit готов: 2 уязвимости в npm, исправления есть.",
            ),
            39_000,
        )

    # ---- playing ----

    def advance(self, t: float) -> int:
        """Write everything scheduled up to t seconds after the start; returns how many steps were written."""
        n = 0
        while self.done < len(self.ops) and self.ops[self.done].at <= t:
            op = self.ops[self.done]
            op.fn(self.start + timedelta(seconds=op.at))
            self.done += 1
            n += 1
        return n

    def build(self) -> None:
        """The directory as it is at the start: everything up to now."""
        (self.home / "projects").mkdir(parents=True, exist_ok=True)
        (self.home / "sessions").mkdir(parents=True, exist_ok=True)
        (self.home / MARKER).write_text("made by scripts/demo_data.py; safe to delete\n", encoding="utf-8")
        self.advance(0.0)


# ---- processes for --live ----

# A stand-in for a session process: it lives while the demo script does. Given a command, it starts
# it the way the Bash tool does: in a shell that sources ~/.claude/shell-snapshots/... (wfwatch
# recognises the shells of a session by that).
HOLDER = """
import os, subprocess, sys, time
parent = os.getppid()
shell = subprocess.Popen(["/bin/sh", "-c", sys.argv[1]]) if len(sys.argv) > 1 else None
while os.getppid() == parent:
    time.sleep(0.5)
"""
# The background command: a bit of CPU work until the stop file appears or its session is gone.
BUSY = """
import os, sys, time
session, stop = int(sys.argv[1]), sys.argv[2]
while not os.path.exists(stop):
    t = time.time() + 0.3
    while time.time() < t:
        pass
    time.sleep(0.5)
    try:
        os.kill(session, 0)
    except OSError:
        break
"""


class Processes:
    """A process per session; the one of the background command also runs its shell and a busy child."""

    def __init__(self, home: Path, names: list[str], bg: str):
        self.stop_file = home / "tmp" / "stop-background"
        snapshot = home / "shell-snapshots" / "snapshot-sh-demo.sh"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("# shell state of the demo session\n", encoding="utf-8")
        command = f". '{snapshot}' && '{sys.executable}' -c '{BUSY}' \"$PPID\" '{self.stop_file}'; exit $?"
        self.procs = {
            name: subprocess.Popen(
                [sys.executable, "-c", HOLDER, *([command] if name == bg else [])], stdin=subprocess.DEVNULL
            )
            for name in names
        }

    def pids(self) -> dict[str, int]:
        return {name: p.pid for name, p in self.procs.items()}

    def stop_background(self) -> None:
        """The background command is over: its process exits (the session process stays)."""
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.touch()

    def close(self) -> None:
        self.stop_background()
        for p in self.procs.values():
            with contextlib.suppress(OSError):
                p.terminate()
        for p in self.procs.values():
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=2)


# ---- command line ----


def prepare(home: Path) -> None:
    """An empty directory, or one made by this script before (it is cleared); anything else is refused."""
    if home.exists():
        if not home.is_dir():
            raise SystemExit(f"demo_data: {home} is not a directory")
        entries = list(home.iterdir())
        if entries and not (home / MARKER).exists():
            raise SystemExit(f"demo_data: {home} is not empty and was not made by this script – refusing to touch it")
        for p in entries:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
    home.mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write a synthetic CLAUDE_CONFIG_DIR for trying wfwatch.")
    ap.add_argument(
        "dir",
        nargs="?",
        type=Path,
        help="where to write (empty, or made by this script); default: a new temporary directory",
    )
    ap.add_argument("--live", action="store_true", help="keep going: agents work and finish (about two minutes)")
    ap.add_argument("--duration", type=float, default=0, help="--live: stop after this many seconds (default: Ctrl+C)")
    ap.add_argument("--lang", choices=("en", "ru"), default="en", help="language of the made-up content")
    ap.add_argument("--pid", type=int, help="without --live: the process the sessions belong to (default: the parent)")
    args = ap.parse_args(argv)

    home = (args.dir or Path(tempfile.mkdtemp(prefix="wfwatch-demo-"))).expanduser().absolute()
    if home == Path.home() / ".claude" or (home / "settings.json").exists():
        raise SystemExit(f"demo_data: {home} looks like a real Claude Code directory – refusing to touch it")
    prepare(home)
    if any(c.isspace() for c in str(home)):
        print("demo_data: the path contains spaces – background command output will not be found", file=sys.stderr)

    names = [s.name for s in story(args.lang)[1]]
    if not args.live:
        pid = args.pid or os.getppid()
        Demo(home, args.lang, dict.fromkeys(names, pid)).build()
        print(home)
        return 0

    procs = Processes(home, names, "nightly-bench")

    def on_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGHUP, on_signal)
    try:
        demo = Demo(home, args.lang, procs.pids())
        demo.on_bg_end = procs.stop_background
        demo.build()
        print(home, flush=True)
        print(
            f"demo_data: live – in another terminal: CLAUDE_CONFIG_DIR={home} wfwatch   (Ctrl+C here to stop)",
            file=sys.stderr,
            flush=True,
        )
        t0 = time.monotonic()
        told = False
        while True:
            t = time.monotonic() - t0
            demo.advance(t)
            if not told and t > STORY_END:
                print("demo_data: the story is over; the sessions stay alive until Ctrl+C", file=sys.stderr, flush=True)
                told = True
            if args.duration and t >= args.duration:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        procs.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
