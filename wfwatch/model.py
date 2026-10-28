"""Read-only view of Claude Code workflow runs and sessions on disk.

Sources (under CLAUDE_CONFIG_DIR, ~/.claude by default):
  projects/<slug>/<session>/subagents/workflows/<run>/journal.jsonl     agent start/result records
  projects/<slug>/<session>/subagents/workflows/<run>/agent-<id>.jsonl  agent transcript, written live
  projects/<slug>/<session>/workflows/scripts/<name>-<run>.js           workflow script: meta (name, phases)
  projects/<slug>/<session>.jsonl                                       session transcript: workflow launches,
                                                                        background commands, task notifications
  sessions/<pid>.json                                                   registry of running sessions
plus one `ps` call per scan (session liveness, processes started by Bash tool calls) and
/tmp/claude-<uid>/<slug>/<session>/tasks/<task>.output (output of background commands).

Nothing is ever written. The file formats are not documented, so every record is parsed
defensively: a malformed line is skipped, never fatal.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CLAUDE_TMP = Path("/tmp")  # Claude Code keeps background task output in /tmp/claude-<uid>/...
DEBUG = False  # --debug: let unexpected errors propagate instead of recording them

RUNNING, DONE, FAILED, STALE, PENDING = "running", "done", "failed", "stale", "pending"
BUSY, WAITING, IDLE, BACKGROUND = "busy", "waiting", "idle", "background"  # SessionWatch.state

# What a malformed record can raise while being parsed; such a record is skipped.
RECORD_ERRORS = (AttributeError, KeyError, TypeError, ValueError, OverflowError, IndexError, RecursionError)


def describe_error(e: BaseException) -> str:
    return clean(f"{type(e).__name__}: {e}")[:300]


# ---- text that reaches the screen ----

# Characters that are never text: C0 controls (tab, newline and the like included), DEL, C1 controls
# (0x9b is CSI for some terminals) and the bidirectional overrides and isolates (they reorder what
# is shown). Transcripts carry text written by models, tools and web pages: an ESC from there must
# never reach the terminal, where it would run as a control sequence (set the window title, write
# the clipboard, clear the screen, leave the alternate screen).
CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_BREAKS = re.compile(r"[^\S ]+")  # whitespace other than the plain space: newlines, tabs, ...


def clean(s: str) -> str:
    """Text from the data made safe to show on one line: newlines, tabs and the like (with the spaces
    around them) become one space, so a value never breaks the layout; control characters are
    dropped. Plain spaces inside the text are kept (aligned output stays aligned)."""
    if _BREAKS.search(s):  # split and join, not a regex with backtracking: linear on any input
        parts = _BREAKS.split(s)
        inner = [p.strip(" ") for p in parts[1:-1]]
        s = " ".join([parts[0].rstrip(" "), *(p for p in inner if p), parts[-1].lstrip(" ")])
    return CONTROL_CHARS.sub("", s)


# ---- primitive parsing ----


def _loads(line: bytes) -> dict | None:
    """One JSONL record, or None if the line is not a JSON object."""
    try:
        d = json.loads(line.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def _raw(v) -> str:
    """A string field as it is (numbers as text, anything else empty): for values that are searched
    or used as paths, never shown."""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _s(v) -> str:
    """A string field to show or to match on: one clean line (see clean); numbers as text, anything else empty."""
    return clean(v) if isinstance(v, str) else _raw(v)


def _int(v) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and abs(v) < 1e18:
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return 0


def _ms(v) -> datetime | None:
    """Epoch milliseconds (as used by the session registry) to an aware datetime."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(s) -> datetime | None:
    """ISO timestamp to an aware datetime (naive times are UTC); anything else is None."""
    if not isinstance(s, str) or not s or len(s) > 64:
        return None
    s = s.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    s = _FRACTION.sub(lambda m: "." + m[1][:6].ljust(6, "0"), s, count=1)  # Python 3.10 wants 3 or 6 digits
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now() -> datetime:
    """The clock used everywhere (tests may monkeypatch it)."""
    return datetime.now(timezone.utc)


class Tail:
    """Reads the complete lines appended to a file since the previous call.

    A missing or unreadable file reads as empty. When the file is truncated or replaced
    (a new inode), reading starts over from the beginning and ``reset`` is set, so the caller
    can drop what it derived from the old content (and clear the flag)."""

    CHUNK = 1 << 20  # read big files piecewise, never whole

    def __init__(
        self, path: Path, needles: tuple[bytes, ...] = (), last_bytes: int = 0, skip_prefixes: tuple[bytes, ...] = ()
    ):
        self.path = path
        self.needles = needles  # if set, only lines containing one of them are returned
        self.skip_prefixes = skip_prefixes  # lines starting with these are never returned
        self.last_bytes = last_bytes  # > 0: start with only the tail of a long file
        self.offset = 0
        self.reset = False
        self._ino: int | None = None
        self._buf = b""
        self._skip_partial = False  # the first line after a seek into the middle is cut
        self._dropping = False  # inside a long skipped line: discard up to the next newline

    def lines(self) -> Iterator[bytes]:
        """Raw new lines (filtered by needles and prefixes), read in chunks."""
        try:
            st = os.stat(self.path)
        except (OSError, ValueError):
            return
        if self._ino is not None and (st.st_ino != self._ino or st.st_size < self.offset):
            self.offset, self._buf, self._skip_partial, self._dropping, self.reset = 0, b"", False, False, True
        self._ino = st.st_ino
        size = st.st_size
        if size <= self.offset:
            return
        if self.offset == 0 and self.last_bytes and size > self.last_bytes:
            self.offset, self._skip_partial = size - self.last_bytes, True
        try:
            f = open(self.path, "rb")  # noqa: SIM115 – the generator keeps it open between chunks
        except (OSError, ValueError):
            return
        with f:
            try:
                f.seek(self.offset)
            except OSError:
                return
            while self.offset < size:
                try:
                    chunk = f.read(min(self.CHUNK, size - self.offset))
                except OSError:
                    return
                if not chunk:
                    break
                self.offset += len(chunk)
                if self._dropping:
                    k = chunk.find(b"\n")
                    if k < 0:
                        continue
                    chunk, self._dropping = chunk[k + 1 :], False
                data = self._buf + chunk if self._buf else chunk
                end = data.rfind(b"\n")  # data[:end] holds complete lines
                start = 0
                if end >= 0 and self._skip_partial:
                    start, self._skip_partial = data.find(b"\n") + 1, False
                self._buf = data[end + 1 :] if end >= 0 else data
                if self.skip_prefixes and len(self._buf) > 65536 and self._buf.startswith(self.skip_prefixes):
                    self._buf, self._dropping = b"", True
                if end > start:
                    yield from self._split(data, start, end)

    def _split(self, data: bytes, start: int, end: int) -> Iterator[bytes]:
        """Lines of data[start:end]. With needles, only the matching lines are cut out (a long
        transcript is mostly lines nobody needs; splitting it all would churn memory)."""
        if not self.needles:
            for ln in data[start:end].split(b"\n"):
                if ln.strip() and not (self.skip_prefixes and ln.startswith(self.skip_prefixes)):
                    yield ln
            return
        bounds = set()
        for n in self.needles:
            i = data.find(n, start, end)
            while i >= 0:
                ls = data.rfind(b"\n", start, i) + 1 or start
                le = data.find(b"\n", i, end + 1)
                bounds.add((ls, le))
                i = data.find(n, le, end)
        for ls, le in sorted(bounds):
            if not (self.skip_prefixes and data.startswith(self.skip_prefixes, ls)):
                yield data[ls:le]

    def read(self) -> list[dict]:
        """New JSON object records; other lines are skipped."""
        return [d for ln in self.lines() if (d := _loads(ln)) is not None]


def short_model(model: str | None) -> str:
    if not isinstance(model, str) or not model:
        return ""
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?!\d)", model)  # claude-opus-4-20250514 -> opus 4
    if not m:
        return model
    return f"{m[1]} {m[2]}.{m[3]}" if m[3] else f"{m[1]} {m[2]}"


def short_path(p) -> str:
    if not isinstance(p, str) or not p:
        return ""
    parts = Path(p).parts
    return clean("/".join(parts[-2:]) if len(parts) > 1 else p)


def result_text(r) -> str:
    """An agent result: plain text or a structured answer (when the workflow sets a schema)."""
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        for k in ("summary", "verdict", "result", "conclusion"):
            if isinstance(r.get(k), str):
                return r[k]
        for v in r.values():
            if isinstance(v, str) and v.strip():
                return v
    if r is None:
        return ""
    try:
        return json.dumps(r, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return str(r)


def first_line(s, limit: int = 200) -> str:
    if not isinstance(s, str):
        s = result_text(s)
    for ln in (s or "").splitlines():
        ln = clean(ln).strip().strip("*#").strip()
        if ln:
            return ln[:limit]
    return ""


@dataclass(frozen=True)
class Action:
    """What an agent did: a tool call (the tool and its main argument) or a message it wrote.

    It stays data; the views put it into words (and translate the few tools shown as a phrase)."""

    tool: str = ""  # tool name; "" for a message
    arg: str = ""  # file, command, pattern, query...; for a message – its first line


def describe_tool(name, inp) -> Action:
    """A tool call reduced to what cards and the log show."""
    name = _s(name) or "?"
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return Action(name, short_path(inp.get("file_path") or inp.get("notebook_path")))
    if name == "Bash":
        return Action(name, first_line(inp.get("description") or inp.get("command"), 80))
    if name in ("Grep", "Glob"):
        return Action(name, _s(inp.get("pattern")))
    if name == "WebSearch":
        return Action(name, _s(inp.get("query")))
    if name == "WebFetch":
        return Action(name, _s(inp.get("url")))
    if name in ("Agent", "Task"):
        return Action("Agent", _s(inp.get("description")))
    if name == "Skill":
        return Action(name, _s(inp.get("skill")))
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return Action(f"{mcp_server(server)} · {tool}" if server and tool else name.split("__")[-1])
    return Action(name)


def mcp_server(name: str) -> str:
    """The server part of an MCP tool name, without the prefixes Claude Code adds: plugin_<plugin>_<server>
    (where the server is usually named after its plugin) and claude_ai_ for the connectors of claude.ai."""
    for prefix in ("plugin_", "claude_ai_"):
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix) :]
            break
    half = len(name) // 2
    return name[:half] if len(name) % 2 and name[half] == "_" and name[:half] == name[half + 1 :] else name


@dataclass
class Event:
    """Something that happened, kept as data; the views put it into words."""

    ts: datetime
    who: str  # agent label, "" for events of the run itself
    kind: str  # start | done | failed | tool | run
    code: str = ""  # run: launched | relaunched | finished
    detail: str = ""  # done: first line of the result; run finished: the status from the notification
    action: Action | None = None  # tool: the call


@dataclass
class Agent:
    agent_id: str
    label: str
    phase: str
    tail: Tail
    status: str = RUNNING  # effective status: journal status, RUNNING turns STALE when the run is over
    started: datetime | None = None
    last_ts: datetime | None = None
    model: str = ""
    tool_calls: int = 0
    last_action: Action | None = None
    context_tokens: int = 0
    output_by_msg: dict[str, int] = field(default_factory=dict)
    tool_ts: list[datetime] = field(default_factory=list)
    result: str = ""
    journal_status: str = RUNNING  # what the journal says: RUNNING (no result yet), DONE or FAILED
    superseded: bool = False  # replaced by a later attempt (resume, retry): not counted anywhere
    seq: int = 0  # order of the "started" record in the journal

    @property
    def output_tokens(self) -> int:
        return sum(self.output_by_msg.values())

    def elapsed(self, at: datetime) -> float:
        if not self.started:
            return 0.0
        end = at if self.status == RUNNING else (self.last_ts or at)
        return max(0.0, (end - self.started).total_seconds())

    def idle(self, at: datetime) -> float:
        return (at - self.last_ts).total_seconds() if self.last_ts else 0.0

    def reset_stats(self) -> None:
        """The transcript was replaced: forget everything derived from it."""
        self.started = self.last_ts = None
        self.model, self.last_action = "", None
        self.tool_calls = self.context_tokens = 0
        self.output_by_msg.clear()
        self.tool_ts.clear()


@dataclass
class Phase:
    title: str  # "" for agents without a phase
    agents: list[Agent]

    @property
    def current(self) -> list[Agent]:
        """Agents that count: those replaced by a later attempt are left out."""
        return [a for a in self.agents if not a.superseded]

    @property
    def status(self) -> str:
        st = {a.status for a in (self.current or self.agents)}
        if not st:
            return PENDING
        for s in (RUNNING, FAILED, STALE):  # an error must not look like an interruption
            if s in st:
                return s
        return DONE


def current_phase(phases: list[Phase]) -> int:
    """Index of the phase to follow: the first running one, else the last one that has agents."""
    for i, p in enumerate(phases):
        if p.status == RUNNING:
            return i
    started = [i for i, p in enumerate(phases) if p.agents]
    return started[-1] if started else 0


def feed_agent(a: Agent, entries: list[dict], on_cwd=None) -> list[tuple[datetime, Action]]:
    """Update an agent from new transcript records; return its tool calls (time, call)."""
    calls: list[tuple[datetime, Action]] = []
    for e in entries:
        try:
            _feed_one(a, e, on_cwd, calls)
        except RECORD_ERRORS:
            continue
    return calls


def _feed_one(a: Agent, e: dict, on_cwd, calls: list) -> None:
    ts = parse_ts(e.get("timestamp"))
    if ts:
        a.started = a.started or ts
        if a.last_ts is None or ts > a.last_ts:
            a.last_ts = ts
    cwd = _s(e.get("cwd"))
    if on_cwd and cwd:
        on_cwd(cwd)
    if e.get("type") != "assistant":
        return
    msg = e.get("message")
    if not isinstance(msg, dict):
        return
    if isinstance(msg.get("model"), str) and msg["model"]:
        a.model = _s(msg["model"])
    u = msg.get("usage")
    if isinstance(u, dict) and u:
        a.context_tokens = (
            _int(u.get("input_tokens"))
            + _int(u.get("cache_read_input_tokens"))
            + _int(u.get("cache_creation_input_tokens"))
        )
        mid = msg.get("id")
        a.output_by_msg[mid if isinstance(mid, str) and mid else str(len(a.output_by_msg))] = _int(
            u.get("output_tokens")
        )
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_use":
            a.tool_calls += 1
            if ts:
                a.tool_ts.append(ts)
            a.last_action = describe_tool(c.get("name"), c.get("input"))
            calls.append((ts or now(), a.last_action))
        elif c.get("type") == "text" and first_line(c.get("text")):
            a.last_action = Action("", first_line(c.get("text"), 120))


# ---- workflow script meta ----


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


class _Js:
    """A tiny reader for the object literal of `export const meta = {...}`.

    Understands strings (with escapes), template literals, arrays, objects and comments;
    any other value (identifiers, calls, arithmetic) is skipped as an opaque expression."""

    _IDENT = re.compile(r"[A-Za-z_$][\w$]*|\d[\w.]*")

    def __init__(self, s: str, i: int):
        self.s, self.i = s, i

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def ws(self) -> None:
        s = self.s
        while self.i < len(s):
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                j = s.find("\n", self.i)
                self.i = len(s) if j < 0 else j + 1
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                self.i = len(s) if j < 0 else j + 2
            else:
                break

    def value(self):
        self.ws()
        c = self.peek()
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c and c in "'\"`":
            return self.string()
        self.skip_expr()
        return None

    def obj(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self.ws()
            c = self.peek()
            if not c:
                raise ValueError("unterminated object")
            if c == "}":
                self.i += 1
                return out
            if c == ",":
                self.i += 1
                continue
            start = self.i
            key = self.key()
            self.ws()
            if key is not None and self.peek() == ":":
                self.i += 1
                out[key] = self.value()
            else:  # shorthand property, method, spread, computed key
                self.skip_expr()
            if self.i == start:
                raise ValueError(f"unexpected {c!r}")

    def arr(self) -> list:
        self.i += 1
        out: list = []
        while True:
            self.ws()
            c = self.peek()
            if not c:
                raise ValueError("unterminated array")
            if c == "]":
                self.i += 1
                return out
            if c == ",":
                self.i += 1
                continue
            start = self.i
            out.append(self.value())
            if self.i == start:
                raise ValueError(f"unexpected {c!r}")

    def key(self) -> str | None:
        c = self.peek()
        if c in ("'", '"'):
            return self.string()
        if self.s.startswith("...", self.i) or c == "[":
            return None
        m = self._IDENT.match(self.s, self.i)
        if not m:
            return None
        self.i = m.end()
        return m[0]

    def string(self) -> str:
        q, s = self.s[self.i], self.s
        self.i += 1
        out: list[str] = []
        while self.i < len(s):
            c = s[self.i]
            if c == q:
                self.i += 1
                return "".join(out)
            if c == "\\" and self.i + 1 < len(s):
                self.i += 1
                out.append(self._escape())
                continue
            if q == "`" and s.startswith("${", self.i):
                start = self.i
                self.i += 2
                self._skip_until("}")
                self.i += 1
                out.append(s[start : self.i])
                continue
            out.append(c)
            self.i += 1
        raise ValueError("unterminated string")

    def _escape(self) -> str:
        s, c = self.s, self.s[self.i]
        self.i += 1
        if c in _JS_ESCAPES:
            return _JS_ESCAPES[c]
        if c == "\n":  # line continuation
            return ""
        if c == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", s[self.i : self.i + 2]):
            self.i += 2
            return chr(int(s[self.i - 2 : self.i], 16))
        if c == "u":
            if s.startswith("{", self.i):
                j = s.find("}", self.i)
                if j > 0 and re.fullmatch(r"[0-9a-fA-F]{1,6}", s[self.i + 1 : j]):
                    code, self.i = int(s[self.i + 1 : j], 16), j + 1
                    return chr(code) if code <= 0x10FFFF else ""
            elif re.fullmatch(r"[0-9a-fA-F]{4}", s[self.i : self.i + 4]):
                self.i += 4
                return chr(int(s[self.i - 4 : self.i], 16))
        return c

    def skip_expr(self) -> None:
        """Skip an expression up to a ',' or a closing bracket at depth 0 (not consumed)."""
        self._skip_until(",", stop_on_close=True)

    def _skip_until(self, stop: str, stop_on_close: bool = False) -> None:
        depth, s = 0, self.s
        while self.i < len(s):
            self.ws()
            c = self.peek()
            if not c:
                break
            if c in "'\"`":
                self.string()
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    if stop_on_close or c == stop:
                        return
                else:
                    depth -= 1
            elif c == stop and depth == 0:
                return
            self.i += 1
        raise ValueError("unterminated expression")


_META = re.compile(r"\bexport\s+const\s+meta\b[^=]{0,200}?=")


def parse_meta(js: str) -> tuple[str, str, list[str]]:
    """name, description and phase titles from `export const meta = {...}` of a workflow script."""
    m = _META.search(js)
    if not m:
        return "", "", []
    i = js.find("{", m.end(), m.end() + 200)
    if i < 0:
        return "", "", []
    try:
        meta = _Js(js, i).value()
    except (ValueError, IndexError, RecursionError):
        return "", "", []
    if not isinstance(meta, dict):
        return "", "", []
    phases = []
    for p in meta.get("phases") if isinstance(meta.get("phases"), list) else []:
        if isinstance(p, dict) and isinstance(p.get("title"), str):
            phases.append(clean(p["title"]))
        elif isinstance(p, str):
            phases.append(clean(p))
    return _s(meta.get("name")), _s(meta.get("description")), phases


# ---- processes ----

PID_SLACK_SEC = 60  # a session process starts a little before it writes its registry record
SHELL_MARK = "shell-snapshots"  # Bash tool commands run in a shell that sources ~/.claude/shell-snapshots/...


def parse_etime(s: str) -> float:
    """ps elapsed time `[[dd-]hh:]mm:ss` in seconds."""
    days, _, rest = s.strip().rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    if not 1 <= len(parts) <= 3:
        raise ValueError(s)
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec + (int(days) * 86400 if days else 0)


@dataclass
class Proc:
    pid: int
    ppid: int
    cpu: float  # %CPU as reported by ps
    elapsed: float  # seconds since the process started
    args: str  # command line

    @property
    def name(self) -> str:
        first = self.args.split(None, 1)[0] if self.args.strip() else "?"
        return os.path.basename(first).lstrip("-") or first


class ProcTable:
    """One `ps` snapshot: who runs, since when, and the process tree."""

    def __init__(self, procs: dict[int, Proc], taken: float):
        self.procs = procs
        self.taken = taken  # epoch seconds of the snapshot
        self.kids: dict[int, list[Proc]] = {}
        for p in procs.values():
            if p.ppid != p.pid:
                self.kids.setdefault(p.ppid, []).append(p)

    @classmethod
    def snapshot(cls) -> ProcTable | None:
        """Current processes, or None when ps is unavailable (errors are ignored)."""
        if sys.platform == "win32":
            return None
        try:
            r = subprocess.run(
                ["ps", "-A", "-o", "pid=,ppid=,pcpu=,etime=,args="],
                capture_output=True,
                timeout=5,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if r.returncode != 0:
            return None
        return cls.parse(r.stdout.decode("utf-8", "replace"), now().timestamp())

    @classmethod
    def parse(cls, text: str, taken: float) -> ProcTable:
        procs: dict[int, Proc] = {}
        for line in text.splitlines():
            f = line.split(None, 4)
            if len(f) < 4:
                continue
            try:
                p = Proc(
                    int(f[0]),
                    int(f[1]),
                    float(f[2].replace(",", ".")),
                    parse_etime(f[3]),
                    clean(f[4]) if len(f) > 4 else "",
                )
            except ValueError:
                continue
            procs[p.pid] = p
        return cls(procs, taken)

    def get(self, pid: int) -> Proc | None:
        return self.procs.get(pid)

    def started(self, pid: int) -> float | None:
        p = self.procs.get(pid)
        return self.taken - p.elapsed if p else None

    def descendants(self, pid: int) -> list[Proc]:
        out, stack, seen = [], list(self.kids.get(pid, ())), {pid}
        while stack:
            p = stack.pop()
            if p.pid in seen:
                continue
            seen.add(p.pid)
            out.append(p)
            stack += self.kids.get(p.pid, ())
        return out

    def tool_processes(self, pid: int) -> list[Proc]:
        """For each Bash tool shell of a session, its busiest leaf process; busiest first."""
        return sorted((leaf for _, leaf in self.tool_shells(pid)), key=lambda p: -p.cpu)

    def tool_shells(self, pid: int) -> list[tuple[Proc, Proc]]:
        """Each Bash tool shell of a session that runs something, with its busiest leaf process.

        Other children of the session (MCP servers and the like) are not tool shells and are skipped."""
        if pid <= 0:
            return []
        me = {os.getpid()} | {p.pid for p in self.descendants(os.getpid())}  # wfwatch itself and its ps
        shells = [p for p in self.descendants(pid) if SHELL_MARK in p.args and p.pid not in me]
        nested = {d.pid for sh in shells for d in self.descendants(sh.pid)}
        out = []
        for sh in shells:
            if sh.pid in nested:  # a shell started from another tool shell: counted there
                continue
            live = [d for d in self.descendants(sh.pid) if d.pid not in me and "<defunct>" not in d.args]  # no zombies
            alive = {d.pid for d in live}
            leaves = [d for d in live if not any(k.pid in alive for k in self.kids.get(d.pid, ()))]
            if leaves:
                out.append((sh, max(leaves, key=lambda p: (p.cpu, p.elapsed))))
        return out


def open_files(pids: Iterable[int]) -> dict[int, list[tuple[int, str]]]:
    """(descriptor, path) of the regular files these processes have open for writing, as the
    system tells (/proc on Linux, lsof elsewhere): stdout and stderr show where a command sends its
    output with `> file`. The paths come from the system, not from a transcript. Processes that
    are gone are skipped."""
    pids = sorted({p for p in pids if p > 0})
    if not pids or sys.platform == "win32":
        return {}
    if os.path.isdir("/proc/self/fdinfo"):
        return _proc_open_files(pids)
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        r = subprocess.run(
            [lsof, "-w", "-a", "-p", ",".join(map(str, pids)), "-Fpfatn"],
            capture_output=True,
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return parse_lsof(r.stdout.decode("utf-8", "replace"))  # exit code 1 when some of the processes are gone


def parse_lsof(text: str) -> dict[int, list[tuple[int, str]]]:
    """`lsof -F pfatn` output: pid -> (descriptor, path) of regular files open for writing (access w or u)."""
    records: list[tuple[int, str, dict[str, str]]] = []
    pid = 0
    for line in text.splitlines():
        key, val = line[:1], line[1:]
        if key == "p":
            pid = int(val) if val.isdigit() else 0
        elif key == "f":
            records.append((pid, val, {}))
        elif key and records:
            records[-1][2][key] = val
    out: dict[int, list[tuple[int, str]]] = {}
    for pid, fd, r in records:
        name = r.get("n", "")
        if pid and fd.isdigit() and r.get("t") == "REG" and r.get("a") in ("w", "u") and name.startswith("/"):
            out.setdefault(pid, []).append((int(fd), name))
    return out


def _proc_open_files(pids: list[int]) -> dict[int, list[tuple[int, str]]]:
    out: dict[int, list[tuple[int, str]]] = {}
    for pid in pids:
        try:
            fds = sorted(int(fd) for fd in os.listdir(f"/proc/{pid}/fd") if fd.isdigit())
        except OSError:
            continue
        for fd in fds:
            link = f"/proc/{pid}/fd/{fd}"
            try:
                if not stat.S_ISREG(os.stat(link).st_mode):
                    continue
                with open(f"/proc/{pid}/fdinfo/{fd}", encoding="ascii", errors="replace") as f:
                    flags = next((int(ln.split()[1], 8) for ln in f if ln.startswith("flags:")), 0)
                if flags & (os.O_WRONLY | os.O_RDWR) == 0:  # open for reading only
                    continue
                path = os.readlink(link)
            except (OSError, ValueError, IndexError):
                continue
            if path.startswith("/"):
                out.setdefault(pid, []).append((fd, path))
    return out


def pid_alive(pid: int, started_at: datetime | None = None, procs: ProcTable | None = None) -> bool:
    """Is the session process still there? With a ps snapshot, a process that started after the
    session wrote its record is someone else on a reused pid."""
    if pid <= 0:
        return False
    if procs is not None:
        if procs.get(pid) is None:
            return False
        st = procs.started(pid)
        return not (started_at is not None and st is not None and st > started_at.timestamp() + PID_SLACK_SEC)
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _win_pid_alive(pid: int) -> bool:  # pragma: no cover – Windows only; os.kill(pid, 0) sends Ctrl+C there
    try:
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: exists
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001 – best effort
        return True


# ---- session registry ----

ACTIVE_STATUSES = (BUSY, WAITING)  # the session works or waits for the user
# the registry status can get stuck: a session listed as busy while its history has been silent for a day
QUIET_MAX = {BUSY: 3600, WAITING: 3 * 3600}
IDLE_KEEP_SEC = 15 * 60  # a session that just went idle is still shown: it is probably being worked with


@dataclass
class SessionInfo:
    """A record of the session registry (sessions/<pid>.json)."""

    session_id: str
    name: str = ""
    pid: int = 0
    alive: bool = False
    status: str = ""  # busy | waiting | idle
    cwd: str = ""
    since: datetime | None = None  # when the status last changed
    entrypoint: str = ""  # cli, claude-vscode, ...
    started_at: datetime | None = None  # when the session process started

    @classmethod
    def from_record(cls, d: dict, stem: str = "", procs: ProcTable | None = None) -> SessionInfo:
        pid = _int(d.get("pid")) or _int(stem)
        started = _ms(d.get("startedAt")) or _parse_lstart(d.get("procStart"))
        return cls(
            session_id=_s(d.get("sessionId")),
            name=_s(d.get("name")),
            pid=pid,
            alive=pid_alive(pid, started, procs),
            status=_s(d.get("status")),
            cwd=_s(d.get("cwd")),
            since=_ms(d.get("statusUpdatedAt")),
            entrypoint=_s(d.get("entrypoint")),
            started_at=started,
        )

    def display_name(self) -> str:
        return self.name or self.session_id[:8]

    def idle_recently(self, at: datetime) -> bool:
        return self.status == IDLE and self.since is not None and (at - self.since).total_seconds() < IDLE_KEEP_SEC

    def recently_active(self, at: datetime) -> bool:
        """Works, waits for the user, or went idle a moment ago."""
        return self.status in ACTIVE_STATUSES or self.idle_recently(at)


def _parse_lstart(v) -> datetime | None:
    """`procStart` is ps lstart in UTC, e.g. 'Wed Oct 14 19:23:06 2026'."""
    if not isinstance(v, str):
        return None
    try:
        return datetime.strptime(" ".join(v.split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_sessions(procs: ProcTable | None = None, cache: dict[Path, dict] | None = None) -> dict[str, SessionInfo]:
    """sessionId -> SessionInfo from the registry of running sessions.

    With ``cache``, a record that cannot be read right now (being rewritten) is replaced by the
    last good one for that file."""
    out: dict[str, SessionInfo] = {}
    try:
        files = sorted((CLAUDE_DIR / "sessions").glob("*.json"))
    except OSError:
        files = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, RecursionError):
            d = None
        if isinstance(d, dict) and isinstance(d.get("sessionId"), str) and d["sessionId"]:
            if cache is not None:
                cache[f] = d
        elif cache is not None and f in cache:
            d = cache[f]
        else:
            continue
        try:
            info = SessionInfo.from_record(d, f.stem, procs)
        except RECORD_ERRORS:
            continue
        if info.session_id and (info.session_id not in out or info.alive):
            out[info.session_id] = info
    if cache is not None:
        for f in set(cache) - set(files):
            del cache[f]
    return out


# ---- session transcript: launches, background commands, notifications ----

_LAUNCH = b'"local_workflow"'
_NOTIFY = b"<task-notification>"
_BG_LAUNCH = b'"run_in_background"'
_BASH = (b'"name":"Bash"', b'"name": "Bash"')  # a foreground command that times out moves to the background
_BG_ID = b'"backgroundTaskId"'
_STOP_IDS = (b'"task_id"', b'"shell_id"')  # a TaskStop (KillShell before it) call and its result
_STOP_TOOLS = ("TaskStop", "KillShell")
_API_LINES = (b'{"type":"api-request-',)  # request logs: huge, and they only repeat the history
_TS = re.compile(rb'"timestamp"\s*:\s*"([^"]{1,64})"')
_CWD = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.){1,1024})"')
_NOTICE = "<task-notification>"
_BLOCK = re.compile(r"<task-notification>(.*?)(?:</task-notification>|$)", re.DOTALL)
_TAG = {k: re.compile(f"<{k}>(.*?)</{k}>", re.DOTALL) for k in ("task-id", "status", "summary", "output-file")}
_OUTPUT_AT = re.compile(r"Output is being written to: (\S+?\.output)")
_EXIT_CODE = re.compile(r"\(exit code (-?\d+)\)")
_QUOTED = re.compile(r'"(.+?)"')


def _content(d: dict) -> list:
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _notice_text(d: dict) -> str:
    """The text of a record that is a task notification, else "".

    Claude Code delivers a notification as a queue operation and as a user turn made of it (marked
    with origin.kind "task-notification" by recent versions). The text of either starts with the
    tag. Nothing else counts: the assistant or a tool output quoting a notification (or a page the
    agent read) must not finish someone's task or point wfwatch at a file to read."""
    kind = d.get("type")
    if kind == "queue-operation":
        text = d.get("content")
    elif kind == "user" and "toolUseResult" not in d:
        origin = d.get("origin")
        if isinstance(origin, dict) and origin.get("kind") not in (None, "task-notification"):
            return ""
        msg = d.get("message")
        text = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(text, list):  # text blocks only: a tool_result is output, not a notification
            text = "".join(
                c["text"]
                for c in text
                if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
            )
    else:
        return ""
    text = text.lstrip() if isinstance(text, str) else ""
    return text if text.startswith(_NOTICE) else ""


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def output_allowed(p: str | Path) -> bool:
    """May this background command output file be read? Only where such files are kept: under
    CLAUDE_TMP/claude-<uid>/ (where Claude Code writes them) or under CLAUDE_DIR, with the
    .output suffix, symlinks resolved. The path comes from a transcript, that is from data:
    wfwatch must not be made to read any other file."""
    try:
        path = Path(p)
        if not path.is_absolute():
            return False
        real = path.resolve()
        roots = ((CLAUDE_TMP / f"claude-{_uid()}").resolve(), CLAUDE_DIR.resolve())
    except (OSError, ValueError, RuntimeError):
        return False
    return real.suffix == ".output" and any(real.is_relative_to(r) for r in roots)


def _unescape(raw: bytes) -> str:
    """Text cut from a raw JSON line: undo the JSON string escapes."""
    try:
        v = json.loads(b'"' + raw + b'"')
        return v if isinstance(v, str) else ""
    except (ValueError, RecursionError):
        return raw.decode("utf-8", "replace")


@dataclass
class Launch:
    ts: datetime
    task_id: str
    run_id: str
    name: str = ""
    summary: str = ""
    cwd: str = ""


@dataclass
class Notice:
    ts: datetime
    status: str  # completed | failed | killed
    summary: str = ""
    output_file: str = ""


@dataclass
class BgTask:
    """A Bash command the session started with run_in_background."""

    task_id: str
    description: str = ""
    started: datetime | None = None
    status: str = RUNNING  # running | completed | failed | killed; stale – lost when the session restarted
    ended: datetime | None = None
    summary: str = ""
    output_path: str = ""
    last_line: str = ""  # last non-empty line of the output, without ANSI sequences
    output_mtime: datetime | None = None
    output_size: int = 0
    exit_code: int | None = None
    # a file of its own the running command writes to (`> render.log 2>&1`), as the system tells
    log_path: str = ""
    log_line: str = ""
    log_mtime: datetime | None = None
    _output_key: tuple = field(default=(), repr=False, compare=False)
    _log_key: tuple = field(default=(), repr=False, compare=False)
    _final: bool = field(default=False, repr=False, compare=False)

    @property
    def line(self) -> str:
        """The last line of whichever was written last: the .output or the file of its own."""
        newer = self.log_mtime is not None and (self.output_mtime is None or self.log_mtime >= self.output_mtime)
        return self.log_line if self.log_line and (newer or not self.last_line) else self.last_line

    def elapsed(self, at: datetime) -> float:
        if not self.started:
            return 0.0
        end = at if self.status == RUNNING else (self.ended or at)
        return max(0.0, (end - self.started).total_seconds())

    def apply(self, n: Notice) -> None:
        self.status = n.status or "completed"
        self.ended = n.ts
        self.summary = n.summary
        self.output_path = n.output_file or self.output_path
        m = _EXIT_CODE.search(n.summary)
        self.exit_code = int(m[1]) if m else None
        if not self.description and (q := _QUOTED.search(n.summary)):
            self.description = q[1]


class SessionLog:
    """Workflow launches, background commands and task notifications of one session transcript.

    The transcript is read once (in chunks, only the lines that matter) and shared by every
    run of the session and by its SessionWatch."""

    def __init__(self, path: Path):
        self.path = path
        self.tail = Tail(
            path, needles=(_LAUNCH, _NOTIFY, _BG_LAUNCH, _BG_ID, *_BASH, *_STOP_IDS), skip_prefixes=_API_LINES
        )
        self.generation = 0  # bumped when the transcript was replaced and everything was read anew
        self.launches: list[Launch] = []
        self.notices: dict[str, Notice] = {}  # task id -> first notification (or stop)
        self.bg: dict[str, BgTask] = {}  # task id -> background command, in start order
        # recent Bash calls: tool_use id -> (time, description); one may come back as a background task
        self._pending: dict[str, tuple[datetime | None, str]] = {}
        self._stops: dict[str, str] = {}  # TaskStop calls waiting for their result: tool_use id -> task id

    def poll(self) -> None:
        for ln in self.tail.lines():
            self._check_reset()
            try:
                self._line(ln)
            except RECORD_ERRORS:
                continue
        self._check_reset()

    def _check_reset(self) -> None:
        if self.tail.reset:  # the transcript was replaced: everything is read anew
            self.tail.reset = False
            self.generation += 1
            self.launches, self.notices, self.bg, self._pending, self._stops = [], {}, {}, {}, {}

    def _line(self, ln: bytes) -> None:
        d = _loads(ln)
        if d is None:  # not a JSON object: nothing in it can be trusted to be a notification
            return
        ts = parse_ts(d.get("timestamp"))
        tur = d.get("toolUseResult")
        if isinstance(tur, dict):
            if tur.get("taskType") == "local_workflow" and _s(tur.get("runId")) and _s(tur.get("taskId")):
                self.launches.append(
                    Launch(
                        ts or now(),
                        _s(tur["taskId"]),
                        _s(tur["runId"]),
                        _s(tur.get("workflowName")),
                        _s(tur.get("summary")),
                        _s(d.get("cwd")),
                    )
                )
            if _s(tur.get("backgroundTaskId")):
                self._bg_started(_s(tur["backgroundTaskId"]), d, ts)
            if self._stops and d.get("type") == "user":
                self._stop_result(tur, d, ts)
        if d.get("type") == "assistant":
            self._bash_calls(d, ts)
        elif _NOTIFY in ln and (text := _notice_text(d)):
            self._notice(text, ts)

    PENDING_MAX = 256

    def _bash_calls(self, d: dict, ts: datetime | None) -> None:
        """Remember Bash calls: started with run_in_background, or still running when their timeout
        hits, they come back with a backgroundTaskId. Remember TaskStop calls too."""
        for c in _content(d):
            if not isinstance(c, dict) or c.get("type") != "tool_use" or not _s(c.get("id")):
                continue
            inp = c.get("input")
            if not isinstance(inp, dict):
                continue
            if c.get("name") == "Bash":
                self._pending[_s(c["id"])] = (ts, first_line(inp.get("description") or inp.get("command"), 120))
            elif c.get("name") in _STOP_TOOLS and (task_id := clean(_s(inp.get("task_id") or inp.get("shell_id")))):
                self._stops[_s(c["id"])] = task_id
        for calls in (self._pending, self._stops):
            while len(calls) > self.PENDING_MAX:
                del calls[next(iter(calls))]

    def _stop_result(self, tur: dict, d: dict, ts: datetime | None) -> None:
        """A task stopped with TaskStop gets no notification: the call's successful result is the
        only record of it. Counted only as the result of a TaskStop call seen before, for the task
        that call named."""
        for c in _content(d):
            if not isinstance(c, dict) or c.get("type") != "tool_result" or c.get("is_error"):
                continue
            task_id = self._stops.pop(_s(c.get("tool_use_id")), "")
            if not task_id or clean(_s(tur.get("task_id") or tur.get("shell_id"))) != task_id:
                continue
            if task_id not in self.notices:  # a notification that came first stays
                n = self.notices[task_id] = Notice(ts or now(), "killed")
                if task_id in self.bg:
                    self.bg[task_id].apply(n)

    def _bg_started(self, task_id: str, d: dict, ts: datetime | None) -> None:
        tool_use_id, output = "", ""
        for c in _content(d):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                tool_use_id = _s(c.get("tool_use_id"))
                text = c.get("content")
                if isinstance(text, list):
                    text = " ".join(_raw(x.get("text")) for x in text if isinstance(x, dict))
                if isinstance(text, str) and (m := _OUTPUT_AT.search(text)) and output_allowed(m[1]):
                    output = m[1]
        started, desc = self._pending.pop(tool_use_id, (None, ""))
        t = self.bg.get(task_id)
        if t is None:
            t = self.bg[task_id] = BgTask(task_id, desc, started or ts or now(), output_path=output)
            if task_id in self.notices:
                t.apply(self.notices[task_id])

    def _notice(self, text: str, ts: datetime | None) -> None:
        """One notification (a record holds one: anything after it is not looked at)."""
        block = _BLOCK.match(text)
        if not block:
            return
        body = block[1]
        tid, st = _TAG["task-id"].search(body), _TAG["status"].search(body)
        if not tid or not st:
            return
        task_id = clean(tid[1]).strip()
        if not task_id or task_id in self.notices:
            return
        summary, out = _TAG["summary"].search(body), _TAG["output-file"].search(body)
        output = out[1].strip() if out else ""
        n = self.notices[task_id] = Notice(
            ts or now(),
            clean(st[1]).strip(),
            clean(summary[1]).strip() if summary else "",
            output if output and output_allowed(output) else "",
        )
        if task_id in self.bg:
            self.bg[task_id].apply(n)


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")


def last_output_line(path: Path, max_bytes: int = 8192) -> tuple[str, int, datetime] | None:
    """(last non-empty line without ANSI sequences, size, mtime) of a command output file.
    Only a regular file is read: a pipe in its place must not block (hence O_NONBLOCK)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError):
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None
            if st.st_size > max_bytes:
                f.seek(st.st_size - max_bytes)
            data = f.read(max_bytes)
    except (OSError, ValueError):
        return None
    mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc)
    text = _ANSI.sub("", data.decode("utf-8", "replace"))
    for ln in reversed(re.split(r"[\r\n]", text)):
        ln = clean(ln).strip()
        if ln:
            return ln[:300], st.st_size, mtime
    return "", st.st_size, mtime


# ---- project names ----


def _slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", name)


@lru_cache(maxsize=256)
def project_from_slug(slug: str) -> str:
    """Best-effort project name from a projects/<slug> directory name.

    Claude Code builds the slug from the working directory by replacing every character that
    is not a letter or digit with '-', so it cannot be decoded exactly. Walk the filesystem,
    matching existing directories (starting from the home directory if the slug begins with
    it), and return the deepest matched directory, or the part of the slug that did not match."""
    home = _slugify(str(Path.home()))
    if home.strip("-") and slug.startswith(home + "-"):
        node, rest = Path.home(), slug[len(home) + 1 :]
    else:
        node, rest = Path(Path.home().anchor or "/"), slug.lstrip("-")
    for _ in range(64):
        if not rest:
            return clean(node.name or slug.strip("-"))
        try:
            names = os.listdir(node)
        except OSError:
            break
        best = max(
            (n for n in names if _slugify(n) == rest or rest.startswith(_slugify(n) + "-")), key=len, default=None
        )
        if best is None:
            break
        node, rest = node / best, rest[len(_slugify(best)) + 1 :]
    return clean(rest or slug.strip("-") or slug)


def _basename(path: str) -> str:
    return clean(Path(path).name or path)


# ---- workflow runs ----


def _peek_times(path: Path, size: int = 16384) -> tuple[datetime | None, datetime | None, str]:
    """(first timestamp, last timestamp, cwd) from the head and tail of a transcript, without reading it all."""
    try:
        with open(path, "rb") as f:
            head = f.read(size)
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size))
            tail = f.read(size)
    except (OSError, ValueError):
        return None, None, ""
    first = _TS.search(head)
    last = None
    for m in _TS.finditer(tail):
        last = m
    cwd = _CWD.search(head)
    return _ts_of(first), _ts_of(last), clean(_unescape(cwd[1])) if cwd else ""


def _ts_of(m: re.Match | None) -> datetime | None:
    return parse_ts(m[1].decode("utf-8", "replace")) if m else None


RESUME_SLACK_SEC = 10


class Run:
    """One workflow run: its phases, agents and log."""

    def __init__(self, run_dir: Path, log: SessionLog | None = None, light: bool = False):
        self.run_dir = run_dir
        self.run_id = clean(run_dir.name)
        self.session_dir = run_dir.parents[2]  # <project>/<session>
        self.session_id = clean(self.session_dir.name)
        self.journal = Tail(run_dir / "journal.jsonl")
        self.log = log if log is not None else SessionLog(self.session_dir.with_suffix(".jsonl"))
        self.light = light  # --list: agent transcripts are only peeked at, not parsed
        self.name = ""
        self.description = ""
        self.script_phases: list[str] = []
        self._script_mtime: float | None = None
        self.agents: dict[str, Agent] = {}
        self.events: list[Event] = []
        self.launches: list[tuple[datetime, str]] = []  # (time, taskId); a resume launches the run again
        self.notices: dict[str, tuple[datetime, str]] = {}  # taskId -> (time, status)
        self.session: SessionInfo | None = None
        self.cwd = ""  # from agent transcripts, in case the session is gone from the registry
        self.launch_cwd = ""
        self.error = ""  # last unexpected failure while reading this run
        self._log_gen = 0
        self._log_pos = 0
        self._seq = 0

    # ---- reading ----

    def poll(self, sessions: dict[str, SessionInfo] | None = None) -> None:
        if sessions is not None:
            self.session = sessions.get(self.session_id)
        self._poll_script()
        self.log.poll()
        self._sync_log()
        for e in self.journal.read():
            try:
                self._on_journal(e)
            except RECORD_ERRORS:
                continue
        if not self.light:
            for a in self.agents.values():
                self._poll_agent(a)
        self._settle()

    def _poll_script(self) -> None:
        try:
            scripts = sorted(
                (self.session_dir / "workflows" / "scripts").glob(f"*-{glob_escape(self.run_dir.name)}.js")
            )
            if not scripts:
                return
            p = scripts[-1]
            mt = p.stat().st_mtime
            if mt == self._script_mtime:
                return
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._script_mtime = mt
        name, desc, self.script_phases = parse_meta(text)
        self.name = name or clean(p.stem[: -len(self.run_dir.name) - 1]) or self.name
        self.description = desc or self.description

    def _sync_log(self) -> None:
        log = self.log
        if log.generation != self._log_gen:  # transcript replaced: take everything again
            self._log_gen, self._log_pos, self.launches, self.notices = log.generation, 0, [], {}
        for ln in log.launches[self._log_pos :]:
            if ln.run_id != self.run_id:
                continue
            self.launches.append((ln.ts, ln.task_id))
            self.description = self.description or ln.summary
            self.name = self.name or ln.name
            self.launch_cwd = self.launch_cwd or ln.cwd
            self._event(Event(ln.ts, "", "run", "launched" if len(self.launches) == 1 else "relaunched"))
        self._log_pos = len(log.launches)
        for _, tid in self.launches:
            n = log.notices.get(tid)
            if n is not None and tid not in self.notices:
                self.notices[tid] = (n.ts, n.status)
                self._event(Event(n.ts, "", "run", "finished", n.status))

    def _on_journal(self, e: dict) -> None:
        t, aid = e.get("type"), e.get("agentId")
        if not isinstance(aid, str) or not aid:
            return
        if t == "started":
            a = self.agents.get(aid)
            if a is None:
                label, phase = _s(e.get("label")), e.get("phase")
                safe = re.sub(r"[^\w.-]", "_", aid)
                a = Agent(
                    aid,
                    label or clean(aid),
                    clean(phase) if isinstance(phase, str) else "",
                    Tail(self.run_dir / f"agent-{safe}.jsonl"),
                )
                self._seq += 1
                a.seq = self._seq
                self.agents[aid] = a
            a.status = a.journal_status = RUNNING
            self._refresh_agent(a)
            self._event(Event(a.started or now(), a.label, "start"))
        elif t in ("result", "failed") and aid in self.agents:
            a = self.agents[aid]
            self._refresh_agent(a)
            if t == "result":
                a.status = a.journal_status = DONE
                a.result = result_text(e.get("result"))
                self._event(Event(a.last_ts or now(), a.label, "done", detail=first_line(a.result, 120)))
            else:
                a.status = a.journal_status = FAILED
                self._event(Event(a.last_ts or now(), a.label, "failed"))

    def _refresh_agent(self, a: Agent) -> None:
        if not self.light:
            self._poll_agent(a)
        elif a.started is None:
            a.started, a.last_ts, cwd = _peek_times(a.tail.path)
            self._see_cwd(cwd)

    def _poll_agent(self, a: Agent) -> None:
        entries = a.tail.read()
        if a.tail.reset:
            a.tail.reset = False
            a.reset_stats()
        for ts, action in feed_agent(a, entries, self._see_cwd):
            self._event(Event(ts, a.label, "tool", action=action))

    def _see_cwd(self, cwd: str) -> None:
        self.cwd = self.cwd or cwd

    def _settle(self) -> None:
        """Effective statuses, recomputed on every poll (so a transient misreading is not sticky):
        agents without a result are interrupted once the run is over, and attempts replaced by a
        later one (resume, retry) are marked superseded."""
        live = self.state in ("running", "unknown")
        # agents of a resumed run may start a fraction of a second before its launch record
        last_launch = self.launches[-1][0] - timedelta(seconds=RESUME_SLACK_SEC) if len(self.launches) > 1 else None
        groups: dict[tuple[str, str], list[Agent]] = {}
        for a in sorted(self.agents.values(), key=lambda a: a.seq):
            groups.setdefault((a.phase, a.label), []).append(a)
        for group in groups.values():
            for i, a in enumerate(group):
                if a.journal_status == RUNNING and last_launch and a.started and a.started < last_launch:
                    a.superseded = True  # left behind by a resume
                elif a.journal_status == FAILED or (a.journal_status == RUNNING and not live):
                    end = a.last_ts
                    a.superseded = any(b.started is None or end is None or b.started >= end for b in group[i + 1 :])
                else:
                    a.superseded = False
                if a.journal_status == RUNNING:
                    a.status = STALE if a.superseded or not live else RUNNING

    def _event(self, e: Event) -> None:
        bisect.insort(self.events, e, key=lambda x: x.ts)
        if len(self.events) > 500:
            del self.events[:100]

    # ---- derived ----

    @property
    def state(self) -> str:
        """running | completed | failed | killed | stopped (session closed) | unknown."""
        if self.launches:
            ts, tid = self.launches[-1]
            if tid in self.notices and self.notices[tid][0] >= ts:
                return self.notices[tid][1]
        if self.session is not None and self.session.alive:
            if self.launches or any(a.journal_status == RUNNING for a in self.agents.values()):
                return "running"
            return "unknown"
        return "stopped" if self.session is not None or self.launches else "unknown"

    @property
    def session_name(self) -> str:
        return self.session.name if self.session is not None and self.session.name else self.session_id[:8]

    @property
    def project(self) -> str:
        cwd = (self.session.cwd if self.session is not None else "") or self.launch_cwd or self.cwd
        return _basename(cwd) if cwd else project_from_slug(self.session_dir.parent.name)

    @property
    def started(self) -> datetime | None:
        if self.launches:
            return self.launches[0][0]
        ts = [a.started for a in self.agents.values() if a.started]
        return min(ts) if ts else None

    @property
    def ended(self) -> datetime | None:
        if self.state == "running":
            return None
        if self.launches and self.launches[-1][1] in self.notices:
            return self.notices[self.launches[-1][1]][0]
        ts = [a.last_ts for a in self.agents.values() if a.last_ts]
        return max(ts) if ts else None

    def current_agents(self) -> list[Agent]:
        """Agents that count (superseded attempts left out)."""
        return [a for a in self.agents.values() if not a.superseded]

    def current_phase(self) -> int:
        """Index of the phase to follow (see current_phase)."""
        return current_phase(self.phases())

    def phases(self) -> list[Phase]:
        order = list(self.script_phases)
        for a in self.agents.values():
            if a.phase not in order:
                order.append(a.phase)
        by: dict[str, list[Agent]] = {p: [] for p in order}
        for a in self.agents.values():
            by[a.phase].append(a)
        return [Phase(p, by[p]) for p in order if p or by[p]]  # agents without a phase: title "", named by the views

    def last_activity(self) -> float:
        return _activity(self.run_dir)


# ---- ordinary sessions ----


class SessionWatch:
    """A session (with or without workflows): what it is doing, its subagents and background commands."""

    TAIL_BYTES = 512_000  # of a long transcript only the tail is parsed for recent actions
    SUB_ACTIVE_SEC = 90  # plain subagents have no journal: one is working while its file changes

    def __init__(self, session_id: str, transcript: Path, info: SessionInfo, log: SessionLog | None = None):
        self.session_id = session_id
        self.transcript = transcript
        self.info = info
        self.log = log if log is not None else SessionLog(transcript)
        self.main = Agent("main", info.name or session_id[:8], "", Tail(transcript, last_bytes=self.TAIL_BYTES))
        self.subagents: dict[Path, Agent] = {}
        self.events: list[Event] = []
        self.procs: list[Proc] = []  # what its Bash tool shells are running now, busiest first
        # .output of a running background command (real path) -> the file its current process writes to instead
        self.redirects: dict[str, str] = {}
        self.cwd = ""
        self.error = ""

    @property
    def name(self) -> str:
        return self.info.name or self.session_id[:8]

    @property
    def project(self) -> str:
        cwd = self.info.cwd or self.cwd
        return _basename(cwd) if cwd else project_from_slug(self.transcript.parent.name)

    @property
    def status(self) -> str:
        """Registry status: busy | waiting | idle."""
        return self.info.status

    @property
    def state(self) -> str:
        """What to show: busy | waiting | background (idle, but a background command runs) | idle."""
        if self.status in ACTIVE_STATUSES:
            return self.status
        return BACKGROUND if self.running_bg_tasks() else IDLE

    @property
    def since(self) -> datetime | None:
        return self.info.since

    @property
    def where(self) -> str:
        return self.info.entrypoint

    @property
    def current_proc(self) -> Proc | None:
        return self.procs[0] if self.procs else None

    @property
    def bg_tasks(self) -> list[BgTask]:
        return list(self.log.bg.values())

    def running_bg_tasks(self) -> list[BgTask]:
        return [t for t in self.log.bg.values() if t.status == RUNNING]

    def recent_bg_tasks(self, at: datetime | None = None, within: float = IDLE_KEEP_SEC) -> list[BgTask]:
        """Running background commands and those finished within the last `within` seconds."""
        at = at or now()
        return [
            t
            for t in self.log.bg.values()
            if t.status == RUNNING or (t.ended is not None and (at - t.ended).total_seconds() < within)
        ]

    def poll(self, info: SessionInfo | None = None) -> None:
        if info is not None:
            self.info = info
            self.main.label = self.name
        entries = self.main.tail.read()
        if self.main.tail.reset:
            self.main.tail.reset = False
            self.main.reset_stats()
        for ts, action in feed_agent(self.main, entries, self._see_cwd):
            self._event(ts, self.main.label, action)
        self._poll_subagents()
        self.log.poll()
        self.poll_bg()

    def _see_cwd(self, cwd: str) -> None:
        self.cwd = self.cwd or cwd

    def _poll_subagents(self) -> None:
        cutoff = now().timestamp() - self.SUB_ACTIVE_SEC
        try:
            files = sorted((self.transcript.with_suffix("") / "subagents").glob("agent-*.jsonl"))
        except OSError:
            return
        for f in files:
            try:
                fresh = f.stat().st_mtime >= cutoff
            except OSError:
                continue
            a = self.subagents.get(f)
            if a is None and fresh:
                label = f.stem
                try:
                    meta = json.loads(f.with_name(f.stem + ".meta.json").read_text(encoding="utf-8", errors="replace"))
                    label = _s(meta.get("description")) if isinstance(meta, dict) else ""
                except (OSError, ValueError, RecursionError):
                    pass
                a = self.subagents[f] = Agent(f.stem, label or clean(f.stem), "", Tail(f, last_bytes=self.TAIL_BYTES))
            if a is not None:
                entries = a.tail.read()
                if a.tail.reset:
                    a.tail.reset = False
                    a.reset_stats()
                for ts, action in feed_agent(a, entries):
                    self._event(ts, a.label, action)
                a.status = RUNNING if fresh else DONE

    def poll_bg(self) -> None:
        """Background commands: started, finished, their last output line."""
        proc_start = self.info.started_at
        for t in self.log.bg.values():
            if t.status == RUNNING and proc_start and t.started and t.started < proc_start:
                t.status = STALE  # started by an earlier process of this session: no notification will come
            if t._final:
                continue
            path = self.bg_output(t)
            try:
                if not output_allowed(path):  # a task id is data too: "../" in it must lead nowhere
                    raise ValueError(path)
                st = path.stat()
                key = (st.st_size, st.st_mtime)
            except (OSError, ValueError):
                key = ()
            if key and key != t._output_key:
                got = last_output_line(path)
                if got:
                    t.last_line, t.output_size, t.output_mtime = got
                t._output_key = key
            self._poll_log(t, os.path.realpath(path) if key and t.status == RUNNING and self.redirects else "")
            t._final = t.status != RUNNING

    def _poll_log(self, t: BgTask, output: str) -> None:
        """The last line of the file the running command writes to instead of its .output (`output`,
        a real path; "" when the command is over). Which file that is the Tracker learns from the system."""
        log = self.redirects.get(output, "") if output else ""
        if log != t.log_path:
            t.log_path, t.log_line, t.log_mtime, t._log_key = log, "", None, ()
        if not log:
            return
        try:
            st = os.stat(log)
            key = (st.st_size, st.st_mtime)
        except (OSError, ValueError):
            return
        if key != t._log_key:
            got = last_output_line(Path(log))
            if got:
                t.log_line, _, t.log_mtime = got
            t._log_key = key

    def bg_output(self, t: BgTask) -> Path:
        """The .output of a background command: as the transcript gave it, or where it should be."""
        return Path(t.output_path) if t.output_path else self.output_path(t.task_id)

    def output_path(self, task_id: str) -> Path:
        """Where Claude Code writes the output of a background command."""
        return (
            CLAUDE_TMP
            / f"claude-{_uid()}"
            / self.transcript.parent.name
            / self.session_id
            / "tasks"
            / f"{task_id}.output"
        )

    def quiet_sec(self, at: datetime | None = None) -> float:
        """Seconds since the last event in the history of the session and its subagents.
        File mtimes are no good: Claude Code appends service lines even to long-silent sessions."""
        ts = [a.last_ts for a in (self.main, *self.subagents.values()) if a.last_ts]
        return ((at or now()) - max(ts)).total_seconds() if ts else float("inf")

    def shown(self, at: datetime | None = None) -> bool:
        """Show in the overview: a background command runs; or it works or waits (and is not stuck);
        or it went idle a moment ago."""
        at = at or now()
        if self.running_bg_tasks():
            return True
        if self.status in ACTIVE_STATUSES:
            return self.quiet_sec(at) < QUIET_MAX[self.status]
        return self.info.idle_recently(at)

    def animating(self) -> bool:
        return self.status == BUSY or bool(self.running_subagents()) or bool(self.running_bg_tasks())

    def running_subagents(self) -> list[Agent]:
        return [a for a in self.subagents.values() if a.status == RUNNING]

    def _event(self, ts: datetime, who: str, action: Action) -> None:
        bisect.insort(self.events, Event(ts, who, "tool", action=action), key=lambda x: x.ts)
        if len(self.events) > 300:
            del self.events[:100]


def find_transcript(session_id: str) -> Path | None:
    try:
        for p in (CLAUDE_DIR / "projects").glob(f"*/{glob_escape(session_id)}.jsonl"):
            return p
    except (OSError, ValueError):
        pass
    return None


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


class Tracker:
    """Runs being tracked (all running plus recently finished) and live sessions, across all projects."""

    TRACK_SEC = 3 * 3600  # look for runs with activity within the last 3 hours
    KEEP_SEC = 30 * 60  # finished runs stay visible for 30 more minutes
    MISSING_SCANS = 2  # a session is gone when it is missing from the registry this many scans in a row

    def __init__(self, extra: Path | None = None):
        self.extra = extra  # an explicitly chosen run is always shown
        self.runs: dict[Path, Run] = {}
        self.sessions: dict[str, SessionInfo] = {}
        self.watches: dict[str, SessionWatch] = {}
        self.logs: dict[Path, SessionLog] = {}  # session transcript -> its shared log
        self.procs: ProcTable | None = None
        self._registry: dict[Path, dict] = {}  # last good registry records
        self._missing: dict[str, int] = {}
        self._transcripts: dict[str, Path] = {}

    def log(self, transcript: Path) -> SessionLog:
        if transcript not in self.logs:
            self.logs[transcript] = SessionLog(transcript)
        return self.logs[transcript]

    @staticmethod
    def guard(obj, fn) -> None:
        """Run one item's update; an unexpected failure is recorded on it instead of stopping everything."""
        try:
            fn()
        except Exception as e:  # last line of defence, see --debug
            if DEBUG:
                raise
            obj.error = describe_error(e)
        else:
            obj.error = ""

    def scan(self) -> list[Run]:
        """Refresh the session registry and the list of runs; return the new runs."""
        self.procs = ProcTable.snapshot()
        self.sessions = load_sessions(self.procs, self._registry)
        dirs = discover_runs(self.TRACK_SEC)
        if self.extra and self.extra not in dirs:
            dirs.append(self.extra)
        new = []
        for d in dirs:
            if d not in self.runs:
                r = Run(d, log=self.log(d.parents[2].with_suffix(".jsonl")))
                self.guard(r, lambda r=r: r.poll(self.sessions))
                self.runs[d] = r
                new.append(r)
        keep = set(dirs)
        for d in [d for d, r in self.runs.items() if d not in keep and r.state != "running"]:
            del self.runs[d]
        self._scan_sessions()
        used = {r.log.path for r in self.runs.values()} | {w.log.path for w in self.watches.values()}
        for p in set(self.logs) - used:
            del self.logs[p]
        return new

    def _scan_sessions(self) -> None:
        """Start watching a session when it gets busy (or runs background commands); stop when its
        process has been gone for MISSING_SCANS scans. An idle session is kept: its detailed view
        may be open."""
        alive = {sid for sid, i in self.sessions.items() if i.alive}
        for sid in list(self.watches):
            if sid in alive:
                self._missing.pop(sid, None)
                continue
            self._missing[sid] = self._missing.get(sid, 0) + 1
            if self._missing[sid] >= self.MISSING_SCANS:
                del self.watches[sid]
                del self._missing[sid]
        at = now()
        for sid in alive - set(self.watches):
            info = self.sessions[sid]
            if not (info.recently_active(at) or self._maybe_background(sid, info, at)):
                continue
            path = self._transcript(sid)
            if path is None:
                continue
            w = SessionWatch(sid, path, info, log=self.log(path))
            self.guard(w, w.poll)
            self.watches[sid] = w
        for sid, w in self.watches.items():
            info = self.sessions.get(sid)
            w.procs = self.procs.tool_processes(info.pid) if self.procs is not None and info else []
        self._find_redirects()

    def _find_redirects(self) -> None:
        """Where running background commands send their output when not to their .output
        (`blender -b scene.blend -a > render.log 2>&1`): the shell of such a command has its .output
        open (on stdout, or on a spare descriptor when the shell itself redirects: `for ...; done > log`),
        the process it runs now writes its stdout or stderr to a file of its own. The system is asked
        (open_files) only about the sessions that run background commands."""
        shells: dict[str, list[tuple[Proc, Proc]]] = {}
        for sid, w in self.watches.items():
            info = self.sessions.get(sid)
            if self.procs is not None and info and w.running_bg_tasks():
                shells[sid] = self.procs.tool_shells(info.pid)
        pids = {p.pid for pairs in shells.values() for pair in pairs for p in pair}
        files = open_files(pids) if pids else {}
        for sid, w in self.watches.items():
            redirects: dict[str, str] = {}
            outputs = {os.path.realpath(w.bg_output(t)) for t in w.running_bg_tasks()} if sid in shells else set()
            for sh, leaf in shells.get(sid, ()):
                mine = {os.path.realpath(f) for _, f in files.get(sh.pid, ())} & outputs
                own = [f for fd, f in files.get(leaf.pid, ()) if fd in (1, 2) and os.path.realpath(f) not in outputs]
                if mine and own:
                    redirects.update(dict.fromkeys(mine, own[0]))
            if redirects != w.redirects:
                w.redirects = redirects
                self.guard(w, w.poll_bg)  # --once draws right after the scan

    def _maybe_background(self, sid: str, info: SessionInfo, at: datetime) -> bool:
        """An idle session may still run background commands: its Bash tool shells are alive
        (without ps: its transcript changed within TRACK_SEC)."""
        if self.procs is not None:
            return bool(self.procs.tool_processes(info.pid))
        path = self._transcript(sid)
        try:
            return path is not None and at.timestamp() - path.stat().st_mtime < self.TRACK_SEC
        except OSError:
            return False

    def _transcript(self, sid: str) -> Path | None:
        p = self._transcripts.get(sid)
        if p is None:
            p = find_transcript(sid)
            if p is not None:
                self._transcripts[sid] = p
        return p

    def poll(self) -> None:
        for r in self.runs.values():
            self.guard(r, lambda r=r: r.poll(self.sessions))
        for sid, w in self.watches.items():
            self.guard(w, lambda w=w, sid=sid: w.poll(self.sessions.get(sid)))

    def errors(self) -> list[str]:
        """Items whose last update failed: 'what: error'."""
        out = [f"{r.run_id}: {r.error}" for r in self.runs.values() if r.error]
        return out + [f"{w.name}: {w.error}" for w in self.watches.values() if w.error]

    def visible(self) -> list[Run]:
        """Ordered by start time so the numbers do not jump."""
        at = now()

        def keep(r: Run) -> bool:
            if r.run_dir == self.extra or r.state == "running":
                return True
            return r.ended is not None and (at - r.ended).total_seconds() < self.KEEP_SEC

        return sorted((r for r in self.runs.values() if keep(r)), key=lambda r: r.started or at)

    def active_sessions(self) -> list[SessionWatch]:
        """Sessions to show – including those running workflows: a session works alongside its run."""
        at = now()
        return sorted((w for w in self.watches.values() if w.shown(at)), key=lambda w: w.name)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except (OSError, ValueError):
        return 0.0


def _activity(d: Path) -> float:
    try:
        return max(f.stat().st_mtime for f in d.iterdir())
    except (OSError, ValueError):
        return 0.0


LONG_AGENT_SEC = 6 * 3600  # an agent may write its transcript this long without touching the journal


def discover_runs(within_sec: float | None = None) -> list[Path]:
    """Workflow run directories, most recently active first; within_sec – only those active within that time.

    Activity is the newer of the directory and journal mtimes (new agents, results, resumes);
    every file is looked at only for runs close to the cutoff, so a long history stays cheap."""
    try:
        dirs = [p.parent for p in (CLAUDE_DIR / "projects").glob("*/*/subagents/workflows/wf_*/journal.jsonl")]
    except OSError:
        return []
    act = {d: max(_mtime(d), _mtime(d / "journal.jsonl")) for d in dirs}
    if within_sec is not None:
        cutoff = now().timestamp() - within_sec
        for d in dirs:
            if cutoff - LONG_AGENT_SEC <= act[d] < cutoff:
                act[d] = max(act[d], _activity(d))
        dirs = [d for d in dirs if act[d] >= cutoff]
    return sorted(dirs, key=act.__getitem__, reverse=True)


def find_run(query: str) -> Path | None:
    runs = discover_runs()
    for d in runs:
        if d.name == query or d.name.startswith(query):
            return d
    q = query.lower()
    for d in runs:
        try:
            scripts = list((d.parents[2] / "workflows" / "scripts").glob(f"*-{d.name}.js"))
        except OSError:
            continue
        if any(q in s.name.lower() for s in scripts):
            return d
    return None
