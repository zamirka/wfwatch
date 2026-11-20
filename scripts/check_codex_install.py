"""Exercise an installed wheel outside the checkout, with isolated config and synthetic sessions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wfwatch-install-") as temporary:
        root = Path(temporary)
        home = root / "codex"
        env = {
            **os.environ,
            "CODEX_HOME": str(home),
            "WFWATCH_PLAN_DIR": str(root / "plans"),
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "WFWATCH_OPENCODE_DB": str(root / "missing.db"),
            "WFWATCH_LANG": "en",
        }
        sid = "12345678-1234-4234-8234-123456789abc"
        env["CODEX_THREAD_ID"] = sid

        def run(*args):
            result = subprocess.run(
                [sys.executable, "-m", "wfwatch", *args],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            return result.stdout

        assert "Preview only" in run("codex", "setup")
        assert not home.exists()
        run("codex", "setup", "--apply")
        assert json.loads(run("codex", "status"))["mode"] == "standard"
        assert "No changes needed" in run("codex", "setup", "--apply")
        run("codex", "setup", "--require-approval", "--apply")
        assert json.loads(run("codex", "status"))["mode"] == "require-approval"
        folder = home / "sessions" / datetime.now().strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        for identity in (sid, "12345678-1234-4234-8234-123456789def"):
            now = datetime.now(timezone.utc).isoformat()
            records = [
                {"timestamp": now, "type": "session_meta", "payload": {"id": identity, "cwd": str(root)}},
                {"timestamp": now, "type": "event_msg", "payload": {"type": "task_started"}},
            ]
            (folder / f"rollout-{identity}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
            )
        assert "No structured plan" in run("--once", "--overview", "--width", "160")
        run(
            "plan",
            "set",
            "--approval",
            "Synthetic user approval for installation test",
            "--step",
            "Check first result",
            "--step",
            "Check second result",
        )
        run("plan", "step", "1", "in_progress", "--revision", "1")
        run("plan", "step", "1", "completed", "--revision", "2")
        assert "1/2" in run("--once", "--overview", "--width", "160")
        assert json.loads(run("plan", "show"))["plan"][0]["status"] == "completed"
        assert json.loads(run("plan", "show", "--session", "12345678-1234-4234-8234-123456789def")) is None
        run("codex", "remove", "--apply")
        assert not json.loads(run("codex", "status"))["installed"]
        assert json.loads(run("plan", "show"))["revision"] == 3
        print("Clean installation: preview, modes, updates, two sessions, rendering, restart and removal passed")


if __name__ == "__main__":
    main()
