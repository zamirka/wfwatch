"""Shared fixtures: a synthetic CLAUDE_CONFIG_DIR and builders for the files Claude Code writes.

Times are always relative to model.now(), never a fixed date. The interface is in English unless a
test switches the language itself, and the terminal is the same 80x25 one without color overrides:
the locale, the terminal and the ~/.claude of the machine running the tests do not matter."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wfwatch import i18n, model

SCRIPT = """export const meta = {
  name: 'demo-flow',
  description: "Демо: два этапа",
  phases: [
    { title: 'Аналитика', detail: 'сценарии [A]' },
    { title: `Ревью` },
    { title: 'Доработка' },
  ],
}
const x = await agent('...', { label: 'не этап', phase: 'Аналитика' })
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Home:
    """Builders for a fake ~/.claude (the fixture `claude_home` returns one)."""

    SCRIPT = SCRIPT

    def __init__(self, root: Path):
        self.root = root

    # ---- time ----

    iso = staticmethod(iso)

    @staticmethod
    def at(**ago) -> datetime:
        return model.now() - timedelta(**ago)

    @staticmethod
    def ago(**kw) -> str:
        return iso(model.now() - timedelta(**kw))

    @staticmethod
    def ms_ago(**kw) -> int:
        return int((model.now() - timedelta(**kw)).timestamp() * 1000)

    # ---- records ----

    @staticmethod
    def jl(path: Path, *rows) -> Path:
        """Append JSONL rows (dicts are serialized, str/bytes written as they are)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            for r in rows:
                if isinstance(r, bytes):
                    b = r
                elif isinstance(r, str):
                    b = r.encode("utf-8")
                else:
                    b = json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                f.write(b if b.endswith(b"\n") else b + b"\n")
        return path

    @staticmethod
    def assistant(
        ts: str, msg_id: str, content: list, out: int = 10, ctx: int = 1000, model_id: str = "claude-opus-5-5"
    ) -> dict:
        return {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "model": model_id,
                "content": content,
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": ctx,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": out,
                },
            },
        }

    @staticmethod
    def tool(name: str, tool_id: str = "toolu_1", **inp) -> dict:
        return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}

    @staticmethod
    def text(s: str) -> dict:
        return {"type": "text", "text": s}

    @staticmethod
    def launch(run_id: str, task_id: str, ts: str, name: str = "", cwd: str = "") -> dict:
        r = {
            "type": "user",
            "timestamp": ts,
            "toolUseResult": {
                "status": "async_launched",
                "taskId": task_id,
                "taskType": "local_workflow",
                "runId": run_id,
            },
        }
        if name:
            r["toolUseResult"]["workflowName"] = name
        if cwd:
            r["cwd"] = cwd
        return r

    @staticmethod
    def agent_launch(agent_id: str, ts: str, description: str = "", tool_id: str = "toolu_A") -> dict:
        """The result of an Agent call that went to the background."""
        return {
            "type": "user",
            "timestamp": ts,
            "toolUseResult": {
                "isAsync": True,
                "status": "async_launched",
                "agentId": agent_id,
                "description": description,
                "prompt": "Fix it",
                "resolvedModel": "claude-opus-5-5",
                "outputFile": f"/tmp/claude-501/-p/s/tasks/{agent_id}.output",
                "canReadOutputFile": True,
                "canContinueAgent": True,
            },
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": "Async agent launched successfully."}
                ]
            },
        }

    @staticmethod
    def notify(task_id: str, status: str, ts: str, summary: str = "", output: str = "") -> dict:
        body = f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        if output:
            body += f"<output-file>{output}</output-file>\n"
        body += f"<status>{status}</status>\n"
        if summary:
            body += f"<summary>{summary}</summary>\n"
        return {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": ts,
            "content": body + "</task-notification>",
        }

    # ---- files ----

    def session(
        self,
        sid: str,
        status: str = "busy",
        name: str | None = None,
        pid: int | None = None,
        cwd: str = "/home/user/dev/demo_app",
        since_ms: int | None = None,
        entrypoint: str = "cli",
        started_ms: int | None = None,
        fname: str | None = None,
    ) -> Path:
        """A registry record; the process is this test process (alive) unless pid is given."""
        rec = {
            "pid": pid or os.getpid(),
            "sessionId": sid,
            "name": name if name is not None else f"name-{sid}",
            "status": status,
            "cwd": cwd,
            "entrypoint": entrypoint,
            "statusUpdatedAt": since_ms if since_ms is not None else self.ms_ago(seconds=30),
        }
        if started_ms is not None:
            rec["startedAt"] = started_ms
        p = self.root / "sessions" / (fname or f"{sid}.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return p

    def transcript(self, sid: str, *rows, project: str = "-p") -> Path:
        return self.jl(self.root / "projects" / project / f"{sid}.jsonl", *rows)

    def run(
        self,
        session: str,
        run_id: str,
        launched: str | None = None,
        finished: str | None = None,
        project: str = "-p",
        cwd: str | None = None,
        label: str | None = None,
        phase: str = "Этап",
        script: str | None = None,
    ) -> Path:
        """A workflow run with one agent (finished with a result if `finished` is set)."""
        launched = launched or self.ago(minutes=10)
        sess = self.root / "projects" / project / session
        run = sess / "subagents" / "workflows" / run_id
        self.jl(sess.with_suffix(".jsonl"), self.launch(run_id, f"t-{run_id}", launched, name=f"flow-{run_id}"))
        self.jl(
            run / "journal.jsonl",
            {"type": "launched"},
            {"type": "started", "key": "k", "agentId": "a1", "label": label or f"агент {run_id}", "phase": phase},
        )
        self.jl(run / "agent-a1.jsonl", {"type": "user", "timestamp": launched, "cwd": cwd or f"/x/{project}"})
        if script is not None:
            (sess / "workflows" / "scripts").mkdir(parents=True, exist_ok=True)
            (sess / "workflows" / "scripts" / f"demo-flow-{run_id}.js").write_text(script, encoding="utf-8")
        if finished:
            self.jl(run / "journal.jsonl", {"type": "result", "key": "k", "agentId": "a1", "result": "ok"})
            self.jl(sess.with_suffix(".jsonl"), self.notify(f"t-{run_id}", "completed", finished))
        return run


# Variables that change what Rich and argparse print (size, colors, "is this a terminal").
TERMINAL_ENV = (
    "FORCE_COLOR",
    "NO_COLOR",
    "PYTHON_COLORS",
    "CLICOLOR_FORCE",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
    "COLORFGBG",
    "JUPYTER_COLUMNS",
    "JUPYTER_LINES",
)


@pytest.fixture(scope="session")
def no_claude_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("no-claude-config")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, no_claude_dir: Path):
    """The same terminal for every test (80x25, the size Rich assumes off a terminal; no color
    overrides) and an empty CLAUDE_CONFIG_DIR, also for subprocesses: a test that needs data
    builds it with `claude_home`, it never sees the real ~/.claude."""
    for name in TERMINAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "25")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(no_claude_dir))
    monkeypatch.setattr(model, "CLAUDE_DIR", no_claude_dir)
    monkeypatch.setattr(model, "CLAUDE_TMP", no_claude_dir / "tmp")


@pytest.fixture(autouse=True)
def ui_lang(monkeypatch: pytest.MonkeyPatch):
    """English for every test (also in subprocesses); a test may switch with i18n.use_lang, it is restored."""
    monkeypatch.setenv("WFWATCH_LANG", "en")
    saved = i18n.current()
    i18n.use_lang("en")
    yield
    i18n.use_lang(saved)


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Home:
    monkeypatch.setattr(model, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(model, "CLAUDE_TMP", tmp_path / "tmp")
    # the real process list is not part of any test (tests that need one provide it)
    monkeypatch.setattr(model.ProcTable, "snapshot", classmethod(lambda cls: None))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "projects").mkdir()
    return Home(tmp_path)


@pytest.fixture
def demo_script() -> str:
    return SCRIPT


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Stop the clock (at the real current time, so file mtimes stay consistent)."""
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(model, "now", lambda: t0)
    return t0


@pytest.fixture
def demo_run(claude_home: Home):
    """A running demo workflow: three phases, two agents in the first (one done, one working)."""
    h = claude_home
    h.session("sess-1", name="demo-session", fname=f"{os.getpid()}.json")
    sess = h.root / "projects" / "-proj" / "sess-1"
    run = sess / "subagents" / "workflows" / "wf_abc-123"
    (sess / "workflows" / "scripts").mkdir(parents=True)
    (sess / "workflows" / "scripts" / "demo-flow-wf_abc-123.js").write_text(SCRIPT, encoding="utf-8")
    h.jl(sess.with_suffix(".jsonl"), h.launch("wf_abc-123", "t1", h.ago(minutes=10)))
    h.jl(
        run / "journal.jsonl",
        {"type": "launched"},
        {"type": "started", "key": "k1", "agentId": "a1", "label": "Аналитик", "phase": "Аналитика"},
        {"type": "started", "key": "k2", "agentId": "a2", "label": "Архитектор", "phase": "Аналитика"},
    )
    h.jl(
        run / "agent-a1.jsonl",
        {"type": "user", "timestamp": h.ago(minutes=9, seconds=59)},
        h.assistant(h.ago(minutes=9, seconds=55), "m1", [h.tool("Read", file_path="/x/app/main.py")]),
        h.assistant(h.ago(minutes=8), "m2", [h.text("Готово")], out=50),
    )
    h.jl(
        run / "agent-a2.jsonl",
        {"type": "user", "timestamp": h.ago(minutes=9, seconds=58)},
        h.assistant(h.ago(minutes=9, seconds=51), "m3", [h.tool("Bash", command="pytest -q", description="Run tests")]),
        h.assistant(h.ago(minutes=9, seconds=51), "m3", [h.tool("Grep", pattern="TODO")]),
    )
    h.jl(
        run / "journal.jsonl",
        {"type": "result", "key": "k1", "agentId": "a1", "result": {"summary": "Сценарии готовы\nподробности"}},
    )
    return h, sess, run
