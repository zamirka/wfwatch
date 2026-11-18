"""OpenCode sessions in the shared tracker: polling, nesting and background jobs.

The reader is a SQLite database, not an append-only file: a message is updated in place while the
agent streams, so the watch keeps a watermark and re-reads a small recent tail. Everything it
derives is idempotent (see `Projection`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import model as m
from .projection import Projection
from .store import JobRow, MessageRow, SessionRow, Store, StoreError, database_path

PREFIX = "opencode:"
SILENCE_SEC = m.QUIET_MAX[m.BUSY]  # a job without a row or output for this long is stale
MAX_SESSIONS = 500
MAX_DEPTH = 8
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class OpenCodeWatch(m.SessionWatch):
    """One session of OpenCode, with its tools, subagents and background commands."""

    TAIL_MESSAGES = 300  # the recent history on the first read
    RECENT_MESSAGES = 8  # re-read on every poll: the newest message streams in place
    ROW_OVERLAP_MS = 3_000  # re-read rows changed a moment before the watermark

    def __init__(self, row: SessionRow, db_path: Path):
        self.row = row
        self.db_path = Path(db_path)
        self.children: list[OpenCodeWatch] = []  # the sessions grouped under this root, one scan old
        transcript = self.db_path.parent / "opencode-sessions" / f"{row.id}.jsonl"  # a key, never read
        info = self._info(row)
        super().__init__(PREFIX + row.id, transcript, info)
        self._projection = Projection(self.main, info.name)
        self._since: int | None = None
        self._observed: dict[str, datetime] = {}
        self.error = ""
        self._refresh(row)

    # ---- identity and state ----

    @property
    def session_id_db(self) -> str:
        return self.row.id

    @staticmethod
    def _info(row: SessionRow) -> m.SessionInfo:
        status, since = _state(row, waiting=False)
        return m.SessionInfo(
            PREFIX + row.id,
            name=row.title or row.slug or row.id[-8:],
            alive=True,
            status=status,
            cwd=row.directory or row.worktree,
            since=since,
            entrypoint="OpenCode",
            started_at=row.created,
        )

    def update(self, row: SessionRow) -> None:
        self.row = row
        self.info.name = row.title or row.slug or row.id[-8:]
        self.info.cwd = row.directory or row.worktree
        self.info.started_at = row.created
        self.main.label = self.info.name

    def _refresh(self, row: SessionRow | None = None) -> None:
        row = row or self.row
        status, since = _state(row, waiting=self._projection.waiting)
        if status == m.WAITING and self._projection.waiting_since is not None:
            since = self._projection.waiting_since
        self.info.status, self.info.since = status, since
        self.info.alive = True
        self.main.model = self.main.model or row.model
        self.main.context_tokens = row.tokens_input + row.tokens_cache_read
        self.main.output_by_msg["opencode"] = row.tokens_output
        if self.main.last_ts is None:
            self.main.last_ts = row.updated or row.created
        if self.main.started is None:
            self.main.started = row.created
        # An open turn is not proof of work: a session (or a child) silent for an hour is stuck, and
        # a stuck child must not keep its parent in the overview for ever.
        busy = status in m.ACTIVE_STATUSES and self.quiet_sec() < m.QUIET_MAX[status]
        self.main.status = m.RUNNING if busy or self.running_bg_tasks() or self.running_subagents() else m.DONE

    # ---- polling ----

    def poll(self, info=None, *, store: Store | None = None) -> None:
        # A StoreError is raised, not swallowed: the caller's guard records it on this watch, where
        # the error is shown, instead of the card silently keeping its old state.
        own = store is None
        store = store or Store(self.db_path)
        try:
            fresh = store.sessions_by_ids([self.row.id])
            if fresh:
                self.update(fresh[0])
            rows = self._messages(store)
            self._projection.apply(rows)
            if rows:
                last = max(int(r.updated.timestamp() * 1000) if r.updated else 0 for r in rows)
                self._since = max(self._since or 0, last - self.ROW_OVERLAP_MS)
            jobs = store.jobs([self.row.id])
            self._projection.apply_jobs(jobs, store.shell_outputs())
            self._observe(jobs)
            self._refresh()
            self.poll_bg()  # a file being written is the freshest evidence a task still runs
            self._expire()
            self.error = ""
        finally:
            if own:
                store.close()

    def _messages(self, store: Store) -> list[MessageRow]:
        if self._since is None:
            return store.messages(self.row.id, tail=self.TAIL_MESSAGES)
        return store.messages(self.row.id, since_ms=self._since, tail=self.RECENT_MESSAGES)

    def _observe(self, jobs: Iterable[JobRow]) -> None:
        at = m.now()
        for job in jobs:
            if job.status == "running":
                self._observed[job.id] = at

    def _expire(self) -> None:
        at = m.now()
        for task in self.bg_map().values():
            if task.status != m.RUNNING:
                continue
            # The job row, the moment we saw that row and the last write to the output file are all
            # evidence; the latest of them decides, so a file still growing keeps its task alive.
            last = max(
                (t for t in (task.output_mtime, task.started, self._observed.get(task.task_id)) if t is not None),
                default=None,
            )
            if last and (at - last).total_seconds() >= SILENCE_SEC:
                task.status = m.STALE
                task.ended = last + timedelta(seconds=SILENCE_SEC)
                task.log_path = task.log_line = ""
                task.log_mtime = None
                task.output_path = ""
                self._observed.pop(task.task_id, None)

    # ---- the shared model's hooks ----

    @property
    def plan_steps(self) -> list[m.PlanStep]:
        """The checklist the session recorded with `todowrite` (empty when it recorded none)."""
        return self._projection.plan_steps

    def bg_map(self) -> dict[str, m.BgTask]:
        return self._projection.tasks

    @property
    def bg_tasks(self) -> list[m.BgTask]:
        """The commands of this session and of the sessions grouped under it: a child has no card
        of its own, so the command it runs belongs to the tree the user sees."""
        return [t for w in (self, *self.children) for t in w.bg_map().values()]

    def running_bg_tasks(self) -> list[m.BgTask]:
        return [t for t in self.bg_tasks if t.status == m.RUNNING]

    def recent_bg_tasks(self, at: datetime | None = None, within: float = m.IDLE_KEEP_SEC) -> list[m.BgTask]:
        at = at or m.now()
        return [
            t
            for t in self.bg_tasks
            if t.status == m.RUNNING or (t.ended is not None and (at - t.ended).total_seconds() < within)
        ]

    def output_roots(self) -> tuple[Path, ...]:
        return (self.db_path.parent / "shell",)

    def output_suffix(self) -> str:
        return ".out"

    def close(self) -> None:
        """Nothing is kept open between polls; a watch can simply be dropped."""


def _state(row: SessionRow, waiting: bool) -> tuple[str, datetime | None]:
    busy = row.busy is not None and (row.idle is None or row.busy > row.idle)
    if busy:
        return (m.WAITING if waiting else m.BUSY), row.busy
    return m.IDLE, row.idle or row.updated


# ---- discovery ----


def scan(
    cache: dict[str, OpenCodeWatch], within_sec: float, procs: m.ProcTable | None = None
) -> dict[str, OpenCodeWatch]:
    """Read recent OpenCode sessions and attach child sessions to the root they belong to."""
    path = database_path()
    since = m.now() - timedelta(seconds=within_sec)
    try:
        with Store(path) as store:
            rows = _tree(store, since)
            live = {r.id for r in rows}
            for sid in [sid for sid in cache if sid.removeprefix(PREFIX) not in live]:
                cache.pop(sid, None)
            for row in rows:
                sid = PREFIX + row.id
                watch = cache.get(sid)
                if watch is None:
                    watch = cache[sid] = OpenCodeWatch(row, path)
                else:
                    watch.update(row)
                m.Tracker.guard(watch, lambda watch=watch: watch.poll(store=store))
    except StoreError:
        cache.clear()
        return {}
    roots = {root: children for root, children in _group(cache).items() if _shown(root, children)}
    refresh_processes({w.session_id: w for root, children in roots.items() for w in (root, *children)}, procs)
    for root, children in roots.items():
        root.procs = sorted([*root.procs, *(p for child in children for p in child.procs)], key=lambda p: -p.cpu)
    return {w.session_id: w for w in roots}


def _shown(root: OpenCodeWatch, children: list[OpenCodeWatch]) -> bool:
    """Whether a session and its children are worth a card: they work, wait, run something in the
    background or went idle a moment ago."""
    at = m.now()
    return any(w.shown(at) for w in (root, *children))


def _tree(store: Store, since: datetime) -> list[SessionRow]:
    """Recent sessions, their recent children and their ancestors: a running child is grouped
    under a root that may itself have gone quiet long ago."""
    by_id: dict[str, SessionRow] = {r.id: r for r in store.recent_sessions(since)}
    for _ in range(MAX_DEPTH):
        parents = [r.parent_id for r in by_id.values() if r.parent_id and r.parent_id not in by_id]
        if not parents:
            break
        by_id.update({r.id: r for r in store.sessions_by_ids(parents) if not r.archived})
    for _ in range(MAX_DEPTH):
        young = [r.id for r in by_id.values() if not r.archived]
        children = [r for r in store.children_of(young) if r.id not in by_id and not r.archived and _recent(r, since)]
        if not children:
            break
        by_id.update({r.id: r for r in children})
        if len(by_id) >= MAX_SESSIONS:
            break
    rows = list(by_id.values())
    return _cap(rows) if len(rows) > MAX_SESSIONS else rows


def _cap(rows: list[SessionRow]) -> list[SessionRow]:
    """Keep the newest trees whole, oldest trees go first: half a tree shows a child without the
    root it is grouped under, and a slice by time would cut the newest session off."""
    kept: list[SessionRow] = []
    for tree in sorted(_trees(rows).values(), key=_tree_time, reverse=True):
        if kept and len(kept) + len(tree) > MAX_SESSIONS:
            break
        kept += tree
    return kept


def _trees(rows: list[SessionRow]) -> dict[str, list[SessionRow]]:
    by_id = {r.id: r for r in rows}
    trees: dict[str, list[SessionRow]] = {}
    for row in rows:
        root, seen = row, set()
        while root.parent_id and root.parent_id in by_id and root.id not in seen:
            seen.add(root.id)
            root = by_id[root.parent_id]
        trees.setdefault(root.id, []).append(row)
    return trees


def _tree_time(tree: list[SessionRow]) -> datetime:
    return max((r.updated or r.created or _EPOCH for r in tree), default=_EPOCH)


def _recent(row: SessionRow, since: datetime) -> bool:
    if row.updated is not None and row.updated >= since:
        return True
    return row.busy is not None and (row.idle is None or row.busy > row.idle)


def _group(cache: dict[str, OpenCodeWatch]) -> dict[OpenCodeWatch, list[OpenCodeWatch]]:
    roots: dict[OpenCodeWatch, list[OpenCodeWatch]] = {}
    for watch in cache.values():
        root, seen = watch, set()
        while root.row.parent_id:
            parent = cache.get(PREFIX + root.row.parent_id)
            if parent is None or parent is root or root.session_id in seen:
                break
            seen.add(root.session_id)
            root = parent
        roots.setdefault(root, []).append(watch)
    for root, watches in roots.items():
        children = [w for w in watches if w is not root]
        for child in children:
            report = _report_for(child, cache, root)
            if report is not None:
                state, text = report
                child.main.result = m.first_line(text) or ""
                child.main.status = m.FAILED if state in ("failed", "error") else m.DONE
        root.children = children
        root.subagents = {child.transcript: child.main for child in children}
        root.events = sorted((e for w in watches for e in w._projection.events), key=lambda e: e.ts)[-300:]
        root.bg_agents = [
            child.main
            for child in children
            if child.row.id in root._projection.running_children or child.main.status == m.RUNNING
        ]
        if root.bg_agents and root.main.status == m.DONE:
            root.main.status = m.RUNNING
    return roots


def _report_for(watch: OpenCodeWatch, cache: dict[str, OpenCodeWatch], root: OpenCodeWatch):
    """The report about a child is written to its own parent's messages, which may not be the root."""
    parent = cache.get(PREFIX + watch.row.parent_id) if watch.row.parent_id else None
    seen: set[str] = set()
    while parent is not None and parent.row.id not in seen:
        got = parent._projection.reports.get(watch.row.id)
        if got is not None:
            return got
        seen.add(parent.row.id)
        parent = cache.get(PREFIX + parent.row.parent_id) if parent.row.parent_id else None
    return root._projection.reports.get(watch.row.id)


def refresh_processes(cache: dict[str, OpenCodeWatch], procs: m.ProcTable | None) -> None:
    """Find the child process behind each running shell job by the .out file it holds open."""
    if procs is None:
        return
    me = {os.getpid()} | {p.pid for p in procs.descendants(os.getpid())}
    servers = [p.pid for p in procs.procs.values() if p.name.lower().startswith("opencode")]
    candidates = {p.pid for pid in servers for p in procs.descendants(pid)} - me
    files = m.open_files(candidates) if candidates else {}
    owners: dict[str, set[int]] = {}
    for pid, fds in files.items():
        for _, name in fds:
            owners.setdefault(os.path.realpath(name), set()).add(pid)
    for watch in cache.values():
        watch.procs = []
        # Only this session's own commands: the running ones of its children belong to them, and
        # `scan()` gathers them for the root once.
        for task in watch.bg_map().values():
            if task.status != m.RUNNING or not task.output_path:
                continue
            pids = owners.get(os.path.realpath(task.output_path), set())
            leaf = _busiest_leaf(procs, pids, me)
            if leaf is not None:
                watch.procs.append(leaf)
        watch.procs.sort(key=lambda p: -p.cpu)


def _busiest_leaf(procs: m.ProcTable, pids: set[int], me: set[int]) -> m.Proc | None:
    leaves: list[m.Proc] = []
    for pid in pids:
        proc = procs.get(pid)
        if proc is None:
            continue
        live = [p for p in procs.descendants(pid) if p.pid not in me and "<defunct>" not in p.args]
        if not live:
            leaves.append(proc)
            continue
        parents = {p.ppid for p in live}
        leaves += [p for p in live if p.pid not in parents]
    return max(leaves, key=lambda p: (p.cpu, p.elapsed)) if leaves else None
