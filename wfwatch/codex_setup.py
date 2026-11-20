"""Opt-in Codex integration: preview, install, diagnose and remove only managed content."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from . import __version__
from .plans import locked

BEGIN = "<!-- wfwatch:codex-plan:start -->"
END = "<!-- wfwatch:codex-plan:end -->"
SKILL = Path("skills/wfwatch-plan/SKILL.md")
MANIFEST = Path(".wfwatch-integration.json")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError(f"Refusing to modify a symbolic link: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def skill() -> bytes:
    return files("wfwatch").joinpath("data/codex-plan.md").read_bytes()


def block(require_approval: bool, separator: str) -> str:
    policy = (
        "Before ANY tool use (including reading files, memory, skills, diagnostics and writing a plan), "
        "propose the action plan in chat and wait for explicit user approval. A task request alone does not "
        "approve your proposed plan. Before approval, only discuss and ask questions without tools. "
        "For unknowns, agree on a limited investigation first. Do not ask again for individual steps "
        "of an approved plan. Obtain approval for added scope. A later explicit user instruction can "
        "override this policy for that task.\n"
        if require_approval
        else "Follow the user's existing authorization policy. This integration does not require an extra "
        "approval round solely to track an already authorized task.\n"
    )
    return (
        separator
        + BEGIN
        + "\n## wfwatch session plans\n\n"
        + policy
        + "For authorized multi-step work, read the wfwatch-plan skill in the skills/wfwatch-plan directory "
        "of this Codex home, then record the plan before executing its steps. Use CODEX_THREAD_ID, "
        "update status before each step and after verification, and reread on resume. "
        "Use concise action titles, usually 3–7 words. Never invent consent or completion. "
        "If tracking fails, report it and restore it or agree on an alternative. "
        "These instructions are not a technical tool-execution lock.\n" + END + "\n"
    )


def inspect(home: Path) -> tuple[dict | None, bytes, bytes | None]:
    if (home / SKILL.parent).is_symlink():
        raise ValueError("Managed skill directory is a symbolic link")
    manifest_raw = read(home / MANIFEST)
    manifest = json.loads(manifest_raw) if manifest_raw is not None else None
    agents = read(home / "AGENTS.md") or b""
    installed = read(home / SKILL)
    if manifest is not None:
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise ValueError("Unsupported integration manifest")
        if not isinstance(manifest.get("block"), str) or not manifest["block"]:
            raise ValueError("Invalid managed instruction block")
        if manifest["block"].count(BEGIN) != 1 or manifest["block"].count(END) != 1:
            raise ValueError("Invalid managed instruction markers")
        if agents.decode("utf-8").count(manifest["block"]) != 1:
            raise ValueError("Managed AGENTS.md block was changed or removed; reconcile it before setup/remove")
        if installed is None or digest(installed) != manifest.get("skill_sha256"):
            raise ValueError("Managed skill was changed or removed; reconcile it before setup/remove")
        if type(manifest.get("require_approval")) is not bool:
            raise ValueError("Invalid approval mode")
    elif installed is not None or BEGIN in agents.decode("utf-8") or END in agents.decode("utf-8"):
        raise ValueError("Existing unmanaged wfwatch integration; preserve it and resolve the name conflict first")
    return manifest, agents, installed


def changes(home: Path, remove: bool, mode: bool | None) -> dict[Path, bytes | None]:
    manifest, agents, _ = inspect(home)
    if remove and manifest is None:
        return {}
    text = agents.decode("utf-8")
    if remove:
        new_agents = text.replace(manifest["block"], "", 1).encode("utf-8")
        proposed = {Path("AGENTS.md"): new_agents, SKILL: None, MANIFEST: None}
        if not new_agents and not manifest.get("agents_existed", True):
            proposed[Path("AGENTS.md")] = None
    else:
        required = mode if mode is not None else bool(manifest and manifest["require_approval"])
        separator = manifest["block"].split(BEGIN)[0] if manifest else ("\n\n" if text else "")
        managed = block(required, separator)
        new_text = text.replace(manifest["block"], managed, 1) if manifest else text + managed
        data = {
            "schema": 1,
            "version": __version__,
            "require_approval": required,
            "block": managed,
            "skill_sha256": digest(skill()),
            "agents_existed": manifest.get("agents_existed", True) if manifest else (home / "AGENTS.md").exists(),
        }
        proposed = {
            Path("AGENTS.md"): new_text.encode("utf-8"),
            SKILL: skill(),
            MANIFEST: (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        }
    return {rel: content for rel, content in proposed.items() if read(home / rel) != content}


def apply(home: Path, proposed: dict[Path, bytes | None]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    backup = home / ".wfwatch-backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    originals = {rel: read(home / rel) for rel in proposed}
    for rel, data in originals.items():
        if data is not None:
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_bytes(dest, data)
    changed = []
    try:
        for rel, data in proposed.items():
            dest = home / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            changed.append(rel)
            if data is None:
                dest.unlink(missing_ok=True)
            else:
                atomic_bytes(dest, data)
    except OSError:
        for rel in reversed(changed):
            dest = home / rel
            if originals[rel] is None:
                dest.unlink(missing_ok=True)
            else:
                atomic_bytes(dest, originals[rel])
        raise
    return backup


def atomic_bytes(path: Path, data: bytes) -> None:
    # Reuse the atomic writer's temp-file/replace discipline for exact UTF-8 content.
    import tempfile

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wfwatch codex", description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "status", "remove"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
        if name != "status":
            cmd.add_argument(
                "--apply", action="store_true", help="Apply the displayed changes; default is preview only"
            )
        if name == "setup":
            group = cmd.add_mutually_exclusive_group()
            group.add_argument("--require-approval", dest="mode", action="store_const", const=True)
            group.add_argument("--standard", dest="mode", action="store_const", const=False)
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    try:
        if args.command == "status":
            manifest, _, _ = inspect(home)
            print(
                json.dumps(
                    {
                        "home": str(home),
                        "installed": manifest is not None,
                        "mode": ("require-approval" if manifest["require_approval"] else "standard")
                        if manifest
                        else None,
                        "skill_current": manifest is not None and manifest["skill_sha256"] == digest(skill()),
                        "global_override_present": (home / "AGENTS.override.md").exists(),
                        "note": (
                            "Instructions are not a runtime lock. "
                            "Project rules and runtime instructions may override them."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        remove = args.command == "remove"
        mode = getattr(args, "mode", None)
        proposed = changes(home, remove, mode)
        for rel, data in proposed.items():
            if rel == MANIFEST:
                print(f"{'Remove' if data is None else 'Update'} integration ownership manifest: {home / rel}")
                continue
            before = (read(home / rel) or b"").decode("utf-8")
            after = (data or b"").decode("utf-8")
            print(
                "".join(
                    difflib.unified_diff(
                        before.splitlines(True),
                        after.splitlines(True),
                        fromfile=str(home / rel),
                        tofile=str(home / rel),
                    )
                ),
                end="",
            )
        if not proposed:
            print("No changes needed.")
            return 0
        if not args.apply:
            print("Preview only. Re-run with --apply to write these changes.")
            return 0
        if not remove and (home / "AGENTS.override.md").exists():
            raise ValueError(
                "AGENTS.override.md exists: it can hide AGENTS.md. Reconcile your global instructions first."
            )
        with locked(home / ".wfwatch-setup.json"):
            if changes(home, remove, mode) != proposed:
                raise ValueError("Files changed since preview; run setup/remove again")
            backup = apply(home, proposed)
        print(f"Applied. Original changed files backed up to {backup}")
        print("Start a new Codex session. Plan files and unrelated instructions were preserved.")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"wfwatch codex: {exc}", file=sys.stderr)
        return 2
