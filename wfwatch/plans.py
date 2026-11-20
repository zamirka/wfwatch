"""Explicit, session-scoped plans. Only the plan CLI writes; the monitor only reads."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

MAX_BYTES = 256_000
STATUSES = ("pending", "in_progress", "completed")


def root() -> Path:
    return Path(os.environ.get("WFWATCH_PLAN_DIR") or Path.home() / ".local/share/wfwatch/plans")


def identity(value: str) -> str:
    return str(UUID(value.removeprefix("codex:")))


def plan_path(session: str) -> Path:
    return root() / (identity(session) + ".json")


def validate(data: object, session: str) -> dict:
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("session_id") != identity(session):
        raise ValueError("Invalid plan identity or schema")
    if not isinstance(data.get("approval"), str) or not data["approval"].strip():
        raise ValueError("An explicit user approval reference is required")
    steps = data.get("plan")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("A plan needs 1 to 100 steps")
    for step in steps:
        if not isinstance(step, dict) or step.get("status") not in STATUSES:
            raise ValueError("Invalid step status")
        if not isinstance(step.get("step"), str) or not step["step"].strip() or len(step["step"]) > 500:
            raise ValueError("Each step needs text of 1 to 500 characters")
    if sum(step["status"] == "in_progress" for step in steps) > 1:
        raise ValueError("Only one step may be in progress")
    if type(data.get("revision")) is not int or data["revision"] < 1:
        raise ValueError("Invalid plan revision")
    return data


def read(session: str) -> dict | None:
    try:
        with plan_path(session).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES:
        raise ValueError("Plan file is too large")
    return validate(json.loads(raw), session)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.with_suffix(".lock")
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError(f"Plan is locked: {lock}. Retry; if its writer crashed, remove the stale lock.") from exc
    try:
        yield
    finally:
        lock.rmdir()


def write(path: Path, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if len(raw) > MAX_BYTES:
        raise ValueError("Plan file is too large")
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            name = stream.name
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wfwatch plan", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "set", "step"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--session", default=os.environ.get("CODEX_THREAD_ID"), help="Full Codex thread UUID")
        if name == "set":
            cmd.add_argument("--step", action="append", required=True, help="Approved step; repeat in order")
            cmd.add_argument("--approval", required=True, help="User approval quote/reference, never inferred")
            cmd.add_argument("--replace", action="store_true", help="Replace an existing plan after fresh approval")
        if name == "step":
            cmd.add_argument("number", type=int, help="Step number, starting at 1")
            cmd.add_argument("status", choices=STATUSES)
            cmd.add_argument("--revision", type=int, required=True, help="Revision returned by show or previous write")
    args = parser.parse_args(argv)
    try:
        if not args.session:
            raise ValueError("CODEX_THREAD_ID is missing; supply --session with the full verified thread UUID")
        sid = identity(args.session)
        path = plan_path(sid)
        if args.command == "show":
            data = read(sid)
        else:
            with locked(path):
                previous = read(sid)
                if args.command == "set":
                    if previous and not args.replace:
                        raise ValueError("Plan exists: use show to resume, or --replace after fresh user approval")
                    data = {
                        "schema": 1,
                        "session_id": sid,
                        "approval": args.approval,
                        "plan": [{"step": text, "status": "pending"} for text in args.step],
                        "revision": previous["revision"] + 1 if previous else 1,
                    }
                else:
                    if previous is None:
                        raise ValueError("No approved plan exists")
                    if previous["revision"] != args.revision:
                        raise ValueError("Plan changed; read it again before updating")
                    data = previous
                    if not 1 <= args.number <= len(data["plan"]):
                        raise ValueError("Step number is out of range")
                    step = data["plan"][args.number - 1]
                    if args.status != step["status"] and (step["status"], args.status) not in (
                        ("pending", "in_progress"),
                        ("in_progress", "completed"),
                        ("in_progress", "pending"),
                    ):
                        raise ValueError("Start a step before completing it; replace the plan to reopen completed work")
                    step["status"] = args.status
                    data["revision"] += 1
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                validate(data, sid)
                write(path, data)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"wfwatch plan: {exc}", file=sys.stderr)
        return 2
