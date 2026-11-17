"""Builders for a synthetic OpenCode database (the fixture `opencode_home` returns one).

The schema is the subset of the real `opencode.db` that wfwatch reads, kept in the tests so that a
change in the adapter's expectations is visible here. Times are relative to model.now(); nothing
touches the real ~/.local/share/opencode.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from wfwatch import model

SCHEMA = """
CREATE TABLE project (
    id text PRIMARY KEY,
    worktree text NOT NULL,
    vcs text,
    name text
);
CREATE TABLE session_v2 (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    parent_id text,
    slug text NOT NULL DEFAULT '',
    directory text NOT NULL DEFAULT '',
    title text,
    agent text,
    model text,
    cost real NOT NULL DEFAULT 0,
    tokens_input integer NOT NULL DEFAULT 0,
    tokens_output integer NOT NULL DEFAULT 0,
    tokens_reasoning integer NOT NULL DEFAULT 0,
    tokens_cache_read integer NOT NULL DEFAULT 0,
    time_created integer NOT NULL,
    time_updated integer NOT NULL,
    time_busy integer,
    time_idle integer,
    idle_outcome text,
    time_archived integer,
    time_suspended integer
);
CREATE TABLE session_message (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    type text NOT NULL,
    seq integer NOT NULL,
    time_created integer NOT NULL,
    time_updated integer NOT NULL,
    data text NOT NULL
);
CREATE TABLE job (
    notification_id text PRIMARY KEY,
    id text NOT NULL,
    session_id text NOT NULL,
    child_session_id text,
    kind text NOT NULL,
    title text,
    status text NOT NULL,
    output text,
    error text,
    recovery text NOT NULL,
    time_created integer NOT NULL,
    time_updated integer NOT NULL
);
CREATE INDEX session_v2_parent_idx ON session_v2 (parent_id);
CREATE INDEX session_message_session_seq_idx ON session_message (session_id, seq);
CREATE INDEX session_message_time_created_idx ON session_message (time_created);
CREATE INDEX job_session_idx ON job (session_id);
"""


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class OpenCodeHome:
    """A fake OpenCode data directory with one database."""

    def __init__(self, root: Path):
        self.root = root
        self.db = root / "opencode.db"
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._seq: dict[str, int] = {}
        self._msg: dict[str, str] = {}

    # ---- time ----

    @staticmethod
    def at(**ago) -> datetime:
        return model.now() - timedelta(**ago)

    # ---- projects and sessions ----

    def project(self, project_id: str = "p1", worktree: str = "/home/user/dev/demo") -> str:
        self.conn.execute(
            "insert or replace into project (id, worktree, vcs) values (?, ?, 'git')", (project_id, worktree)
        )
        self.conn.commit()
        return project_id

    def session(
        self,
        sid: str,
        *,
        parent: str = "",
        title: str = "",
        directory: str = "/home/user/dev/demo",
        model_id: str = "opencode/zen-1",
        agent: str = "build",
        created: datetime | None = None,
        updated: datetime | None = None,
        busy: datetime | None = None,
        idle: datetime | None = None,
        idle_outcome: str | None = None,
        archived: datetime | None = None,
        project_id: str = "p1",
        tokens: tuple[int, int, int, int] = (0, 0, 0, 0),
        cost: float = 0.0,
    ) -> str:
        created = created or self.at(minutes=5)
        updated = updated or self.at(seconds=5)
        if busy is None and idle is None:
            busy = updated
        self.project(project_id)
        self.conn.execute(
            "insert or replace into session_v2 (id, project_id, parent_id, slug, directory, title, agent, model,"
            " cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, time_created, time_updated,"
            " time_busy, time_idle, idle_outcome, time_archived, time_suspended)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                sid,
                project_id,
                parent or None,
                title.lower().replace(" ", "-"),
                directory,
                title,
                agent,
                json.dumps({"id": model_id, "providerID": "opencode"}),
                cost,
                *tokens,
                ms(created),
                ms(updated),
                ms(busy) if busy else None,
                ms(idle) if idle else None,
                idle_outcome,
                ms(archived) if archived else None,
            ),
        )
        self.conn.commit()
        return sid

    def touch(self, sid: str, *, busy: datetime | None = None, idle: datetime | None = None, outcome=None, at=None):
        sets, args = ["time_updated=?"], [ms(at or model.now())]
        if busy is not None:
            sets.append("time_busy=?")
            args.append(ms(busy))
        if idle is not None:
            sets.append("time_idle=?")
            args.append(ms(idle))
        if outcome is not None:
            sets.append("idle_outcome=?")
            args.append(outcome)
        self.conn.execute(f"update session_v2 set {', '.join(sets)} where id=?", (*args, sid))
        self.conn.commit()

    # ---- messages ----

    def message(
        self,
        sid: str,
        *,
        kind: str = "assistant",
        parts: list[dict] | None = None,
        text: str = "",
        created: datetime | None = None,
        updated: datetime | None = None,
        metadata: dict | None = None,
        agent: str = "build",
        model_id: str = "opencode/zen-1",
        tokens: dict | None = None,
        description: str = "",
        outcome: str | None = None,
    ) -> str:
        created = created or self.at(seconds=10)
        msg_id = self._next_id(sid)
        seq = self._seq[sid] = self._seq.get(sid, 0) + 1
        data: dict = {"time": {"created": ms(created)}}
        if kind == "assistant":
            data["agent"] = agent
            data["model"] = {"id": model_id, "providerID": "opencode"}
            data["content"] = parts or ([{"type": "text", "text": text}] if text else [])
            if tokens is not None:
                data["tokens"] = tokens
        elif kind == "user":
            data["text"] = text
        elif kind in ("system", "synthetic"):
            data["text"] = text
            data["description"] = description
        if outcome is not None:
            data["outcome"] = outcome
        if metadata is not None:
            data["metadata"] = metadata
        self.conn.execute(
            "insert into session_message (id, session_id, type, seq, time_created, time_updated, data)"
            " values (?,?,?,?,?,?,?)",
            (msg_id, sid, kind, seq, ms(created), ms(updated or created), json.dumps(data)),
        )
        self.conn.commit()
        return msg_id

    def update_message(
        self,
        sid: str,
        msg_id: str,
        *,
        parts: list[dict] | None = None,
        text: str = "",
        tokens: dict | None = None,
        updated: datetime | None = None,
    ):
        row = self.conn.execute("select data from session_message where id=?", (msg_id,)).fetchone()
        data = json.loads(row[0])
        if parts is not None:
            data["content"] = parts
        elif text:
            data["content"] = [{"type": "text", "text": text}]
        if tokens is not None:
            data["tokens"] = tokens
        self.conn.execute(
            "update session_message set data=?, time_updated=? where id=?",
            (json.dumps(data), ms(updated or model.now()), msg_id),
        )
        self.conn.commit()

    def idle(self, sid: str, *, outcome: str = "succeeded", at: datetime | None = None) -> str:
        at = at or self.at(seconds=1)
        return self.message(sid, kind="idle", outcome=outcome, created=at, updated=at)

    def _next_id(self, sid: str) -> str:
        n = len(self._msg)
        self._msg[f"{sid}:{n}"] = "x"
        return f"msg_{n:04d}_{sid[-5:]}"

    # ---- parts ----

    @staticmethod
    def text(value: str) -> dict:
        return {"type": "text", "text": value}

    @staticmethod
    def reasoning(value: str) -> dict:
        return {"type": "reasoning", "text": value}

    def tool(
        self,
        name: str,
        call_id: str = "call_1",
        *,
        status: str = "completed",
        input: dict | None = None,
        output: str = "",
        exit: int | None = None,
        kind: str = "shell",
    ) -> dict:
        state: dict = {"status": status, "input": input or {}}
        if output:
            state["content"] = [{"type": "text", "text": output}]
        if exit is not None:
            state["metadata"] = {"exit": exit}
        return {
            "type": "tool",
            "id": call_id,
            "name": name,
            "executed": False,
            "state": state,
            "time": {"created": ms(self.at(seconds=8)), "ran": ms(self.at(seconds=7))},
        }

    # ---- jobs ----

    def job(
        self,
        job_id: str,
        sid: str,
        *,
        kind: str = "shell",
        status: str = "running",
        title: str = "",
        child: str = "",
        command: str = "",
        output: str = "",
        error: str = "",
        created: datetime | None = None,
        updated: datetime | None = None,
    ) -> None:
        created = created or self.at(seconds=30)
        updated = updated or created
        recovery = json.dumps(
            {"kind": "shell", "sessionID": sid, "shellID": job_id, "command": command}
            if kind == "shell"
            else {
                "kind": "subagent",
                "parentSessionID": sid,
                "childSessionID": child,
                "agent": "explore",
                "description": title,
            }
        )
        self.conn.execute(
            "insert or replace into job (notification_id, id, session_id, child_session_id, kind, title, status,"
            " output, error, recovery, time_created, time_updated) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"notif_{job_id}",
                job_id,
                sid,
                child or None,
                kind,
                title,
                status,
                output or None,
                error or None,
                recovery,
                ms(created),
                ms(updated),
            ),
        )
        self.conn.commit()

    def drop_job(self, job_id: str) -> None:
        self.conn.execute("delete from job where id=?", (job_id,))
        self.conn.commit()

    # ---- outputs ----

    def shell_output(self, shell_id: str, text: str, project: str = "deadbeef") -> Path:
        p = self.root / "shell" / project / f"{shell_id}.out"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    # ---- lifecycle ----

    def close(self) -> None:
        self.conn.close()

    @property
    def path(self) -> Path:
        return self.db
