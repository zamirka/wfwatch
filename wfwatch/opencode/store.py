"""Read-only access to the local OpenCode database.

OpenCode keeps its sessions, messages and background jobs in one SQLite database (WAL mode).
Everything here is a read: the connection is opened with ``mode=ro``, so it can never change a
file the running app depends on. The schema belongs to OpenCode and may change; a database that
does not have what this module needs is reported as `StoreError` and skipped by the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DB_ENV = "WFWATCH_OPENCODE_DB"
_Listing = tuple[tuple[str, int], ...]  # (project directory name, its mtime) for each shell directory
_SHELL_OUTPUTS: dict[Path, tuple[_Listing, dict[str, Path]]] = {}
_REQUIRED = ("project", "session_v2", "session_message", "job")
# The columns this reader names. A database that lost one of them is reported as a schema error
# instead of failing in the middle of a query, where the whole tracker would stop with it.
_REQUIRED_COLUMNS = {
    "project": ("id", "worktree"),
    "session_v2": (
        "id",
        "project_id",
        "parent_id",
        "title",
        "slug",
        "directory",
        "model",
        "agent",
        "cost",
        "tokens_input",
        "tokens_output",
        "tokens_reasoning",
        "tokens_cache_read",
        "time_created",
        "time_updated",
        "time_busy",
        "time_idle",
        "idle_outcome",
        "time_archived",
    ),
    "session_message": ("id", "session_id", "type", "seq", "time_created", "time_updated", "data"),
    "job": (
        "id",
        "session_id",
        "child_session_id",
        "kind",
        "title",
        "status",
        "output",
        "error",
        "recovery",
        "time_created",
        "time_updated",
    ),
}
_SESSION_COLUMNS = """
    s.id, s.parent_id, s.title, s.slug, s.directory, s.model, s.agent,
    s.cost, s.tokens_input, s.tokens_output, s.tokens_reasoning, s.tokens_cache_read,
    s.time_created, s.time_updated, s.time_busy, s.time_idle, s.idle_outcome,
    s.time_archived,
    p.worktree
"""
_MESSAGE_COLUMNS = "m.id, m.session_id, m.type, m.seq, m.time_created, m.time_updated, m.data"
_JOB_COLUMNS = (
    "j.id, j.session_id, j.child_session_id, j.kind, j.title, j.status,"
    " j.output, j.error, j.recovery, j.time_created, j.time_updated"
)


class StoreError(RuntimeError):
    """The database is missing, unreadable or has a schema this reader does not know."""


def database_path() -> Path:
    """The OpenCode database: WFWATCH_OPENCODE_DB, else XDG_DATA_HOME/opencode/opencode.db."""
    override = os.environ.get(DB_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    data = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(data).expanduser() if data else Path.home() / ".local" / "share"
    return root / "opencode" / "opencode.db"


def _dt(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _json(value) -> dict:
    if not isinstance(value, str) or not value:
        return {}
    try:
        data = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _model(value) -> str:
    data = _json(value)
    model = data.get("id")
    return model if isinstance(model, str) else ""


def _text(value) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class SessionRow:
    id: str
    parent_id: str
    title: str
    slug: str
    directory: str
    model: str
    agent: str
    cost: float
    tokens_input: int
    tokens_output: int
    tokens_reasoning: int
    tokens_cache_read: int
    created: datetime | None
    updated: datetime | None
    busy: datetime | None
    idle: datetime | None
    idle_outcome: str
    archived: datetime | None
    worktree: str


@dataclass(frozen=True)
class MessageRow:
    id: str
    session_id: str
    type: str
    seq: int
    created: datetime | None
    updated: datetime | None
    data: dict
    digest: int  # of the stored JSON: two updates can share a timestamp, not their content


@dataclass(frozen=True)
class JobRow:
    id: str
    session_id: str
    child_session_id: str
    kind: str
    title: str
    status: str
    output: str
    error: str
    recovery: dict
    created: datetime | None
    updated: datetime | None

    @property
    def command(self) -> str:
        command = self.recovery.get("command")
        return command if isinstance(command, str) else ""


class Store:
    """One read-only connection to an OpenCode database."""

    MAX_IDS = 400  # SQLite has a limited number of parameters per statement

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else database_path()
        self.read_only = True
        if not self.path.is_file():
            raise StoreError(f"no OpenCode database at {self.path}")
        uri = f"file:{quote(str(self.path))}?mode=ro"
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(uri, uri=True, timeout=2.0)
            db.execute("pragma busy_timeout=500")
            tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        except sqlite3.Error as e:
            if db is not None:
                db.close()
            raise StoreError(f"cannot read {self.path}: {e}") from None
        self.db = db
        missing = [name for name in _REQUIRED if name not in tables]
        if missing:
            self.db.close()
            raise StoreError(f"{self.path} has no {', '.join(missing)} table")
        for table, columns in _REQUIRED_COLUMNS.items():
            try:
                have = {row[1] for row in self.db.execute(f"pragma table_info({table})")}
            except sqlite3.Error as e:
                self.db.close()
                raise StoreError(f"cannot read {self.path}: {e}") from None
            lost = [c for c in columns if c not in have]
            if lost:
                self.db.close()
                raise StoreError(f"{self.path}: {table} has no {', '.join(lost)} column")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- queries ----

    def recent_sessions(self, since: datetime) -> list[SessionRow]:
        """Sessions changed since `since`, newest last; archived ones are left out.

        A message row is rewritten while the agent streams; the session row usually follows it, but
        a session that has been streaming for hours must not fall out of the window for that reason.
        """
        at = int(since.timestamp() * 1000)
        rows = self._sessions(
            "s.time_archived is null and (s.time_updated >= ? or s.id in"
            " (select m.session_id from session_message m where m.time_updated >= ?))",
            (at, at),
        )
        return sorted(rows, key=lambda r: (r.created or since, r.id))

    def sessions_by_ids(self, ids: Iterable[str]) -> list[SessionRow]:
        out = []
        for group in self._groups(ids):
            marks = ",".join("?" * len(group))
            out += self._sessions(f"s.id in ({marks})", tuple(group))
        return out

    def children_of(self, ids: Iterable[str]) -> list[SessionRow]:
        out = []
        for group in self._groups(ids):
            marks = ",".join("?" * len(group))
            out += self._sessions(f"s.parent_id in ({marks})", tuple(group))
        return out

    def messages(self, session_id: str, since_ms: int | None = None, tail: int = 0) -> list[MessageRow]:
        """Messages of one session, oldest first.

        With `since_ms`, only rows changed since then; `tail` additionally re-reads the last rows,
        which a streaming message updates in place without always moving `time_updated` far."""
        found: dict[str, MessageRow] = {}
        if since_ms is None:
            rows = self._rows(
                f"select {_MESSAGE_COLUMNS} from session_message m where m.session_id=?"
                " order by m.time_created desc, m.seq desc limit ?",
                (session_id, tail or 200),
            )
            found.update((row[0], self._message(row)) for row in rows)
        else:
            rows = self._rows(
                f"select {_MESSAGE_COLUMNS} from session_message m where m.session_id=? and m.time_updated >= ?",
                (session_id, since_ms),
            )
            found.update((row[0], self._message(row)) for row in rows)
            if tail:
                rows = self._rows(
                    f"select {_MESSAGE_COLUMNS} from session_message m where m.session_id=?"
                    " order by m.time_created desc, m.seq desc limit ?",
                    (session_id, tail),
                )
                found.update((row[0], self._message(row)) for row in rows)
        return sorted(found.values(), key=lambda r: (r.created or datetime.min.replace(tzinfo=timezone.utc), r.seq))

    def jobs(self, session_ids: Iterable[str]) -> list[JobRow]:
        """Background jobs of these sessions and the ones whose child session is one of them."""
        out = []
        ids = [s for s in dict.fromkeys(session_ids) if s]
        for group in self._groups(ids):
            marks = ",".join("?" * len(group))
            rows = self._rows(
                f"select {_JOB_COLUMNS} from job j where j.session_id in ({marks}) or j.child_session_id in ({marks})",
                (*group, *group),
            )
            out += [self._job(row) for row in rows]
        return sorted(out, key=lambda j: (j.created or datetime.min.replace(tzinfo=timezone.utc), j.id))

    def shell_outputs(self) -> dict[str, Path]:
        """shell id -> the file OpenCode streams that command's output to.

        Every session asks on every poll, and the answer only changes when a file appears or goes
        away. A project directory's own mtime records exactly that, so the listing is redone only
        when some directory changed, instead of walking every file of every project each time."""
        root = self.path.parent / "shell"
        try:
            stamp = tuple(sorted((p.name, p.stat().st_mtime_ns) for p in root.iterdir() if p.is_dir()))
        except OSError:
            return {}
        cached = _SHELL_OUTPUTS.get(root)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        out: dict[str, Path] = {}
        try:
            for path in root.glob("*/*.out"):
                out[path.stem] = path
        except OSError:
            pass
        _SHELL_OUTPUTS[root] = (stamp, out)
        return out

    # ---- row mapping ----

    def _sessions(self, where: str, args: tuple) -> list[SessionRow]:
        rows = self._rows(
            f"select {_SESSION_COLUMNS} from session_v2 s left join project p on p.id = s.project_id where {where}",
            args,
        )
        return [self._session(row) for row in rows]

    def _rows(self, sql: str, args: tuple = ()) -> list:
        """Run a read; a database that changes under us is this reader's error, not a crash."""
        try:
            return self.db.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            raise StoreError(f"{self.path}: {e}") from None

    @staticmethod
    def _session(row) -> SessionRow:
        return SessionRow(
            id=_text(row[0]),
            parent_id=_text(row[1]),
            title=_text(row[2]),
            slug=_text(row[3]),
            directory=_text(row[4]),
            model=_model(row[5]),
            agent=_text(row[6]),
            cost=_float(row[7]),
            tokens_input=_int(row[8]),
            tokens_output=_int(row[9]),
            tokens_reasoning=_int(row[10]),
            tokens_cache_read=_int(row[11]),
            created=_dt(row[12]),
            updated=_dt(row[13]),
            busy=_dt(row[14]),
            idle=_dt(row[15]),
            idle_outcome=_text(row[16]),
            archived=_dt(row[17]),
            worktree=_text(row[18]),
        )

    @staticmethod
    def _message(row) -> MessageRow:
        raw = _text(row[6])
        return MessageRow(
            id=_text(row[0]),
            session_id=_text(row[1]),
            type=_text(row[2]),
            seq=_int(row[3]),
            created=_dt(row[4]),
            updated=_dt(row[5]),
            data=_json(raw),
            digest=int.from_bytes(hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=8).digest(), "big"),
        )

    @staticmethod
    def _job(row) -> JobRow:
        return JobRow(
            id=_text(row[0]),
            session_id=_text(row[1]),
            child_session_id=_text(row[2]),
            kind=_text(row[3]),
            title=_text(row[4]),
            status=_text(row[5]),
            output=_text(row[6]),
            error=_text(row[7]),
            recovery=_json(row[8]),
            created=_dt(row[9]),
            updated=_dt(row[10]),
        )

    def _groups(self, ids: Iterable[str]) -> list[list[str]]:
        values = list(dict.fromkeys(i for i in ids if i))
        return [values[i : i + self.MAX_IDS] for i in range(0, len(values), self.MAX_IDS)]
