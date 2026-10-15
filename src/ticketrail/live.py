"""Live state of Claude Code sessions from `claude agents --json` (Claude Code ≥ 2.1.139).

Only Claude reports this; Codex sessions (and Claude without the command) fall back to "transcript changed recently".
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time

from . import config

BUSY, OPEN = "busy", "open"  # agent working / session open and waiting for you
RETRY_SEC = 60
_failed_at = [0.0]


def claude_live():
    """{session id: BUSY | OPEN} for live Claude sessions of the workspace, or None when unknown (no CLI, old CLI,
    error) so callers keep the mtime heuristic instead of declaring everything stopped."""
    cfg = config.current()
    exe = shutil.which("claude")
    if not exe or time.time() - _failed_at[0] < RETRY_SEC:  # a hanging CLI must not cost 5 s on every scan
        return None
    try:
        r = subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, timeout=5, cwd=cfg.root)
        rows = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        rows = None
    if not isinstance(rows, list):
        _failed_at[0] = time.time()
        return None
    out = {}
    for x in rows:
        if not isinstance(x, dict) or not x.get("sessionId") or not cfg.in_root(x.get("cwd") or ""):
            continue
        working = x.get("status") == "busy" or x.get("state") == "working"
        if not (x.get("pid") or working):
            continue  # a finished background session is not live
        state = BUSY if working else OPEN
        if out.get(x["sessionId"]) != BUSY:  # the same session may be listed twice; busy wins
            out[x["sessionId"]] = state
    return out
