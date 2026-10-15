"""Hard signals that confirm or refine the LLM's judgement. Opt-in: they call external services.

GitHub pull requests: one `gh pr list` per workspace, refreshed in the background every 5 minutes (the picker never
waits for the network), mapped to sessions by git branch.
"""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import threading
import time

from . import config

TTL = 300
EMPTY = {"heads": {}, "bases": []}
_lock = threading.Lock()
_cache = {"root": None, "at": 0.0, "data": EMPTY, "refreshing": False}


def _fetch(cfg):
    """→ {"heads": {branch: [pr…]}, "bases": [branch…]}. Pull requests from forks are dropped (their head names
    collide with local branches), and so are release PRs whose head is itself the base of other PRs (develop → main):
    a session on develop is not the work of that release."""
    exe = shutil.which("gh")
    if not exe:
        return EMPTY
    try:
        r = subprocess.run([exe, "pr", "list", "--state", "all", "--limit", "300", "--json",
                            "number,url,state,headRefName,baseRefName,isCrossRepository,mergedAt"],
                           cwd=cfg.root, capture_output=True, text=True, timeout=15)
        rows = json.loads(r.stdout) if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return EMPTY
    bases = sorted({p.get("baseRefName") for p in rows if isinstance(p, dict) and p.get("baseRefName")})
    heads = {}
    for p in rows:
        try:
            if p.get("isCrossRepository") or p["headRefName"] in bases:
                continue
            heads.setdefault(p["headRefName"], []).append(
                {"number": p["number"], "url": p["url"], "state": p["state"], "mergedAt": p.get("mergedAt")})
        except (KeyError, TypeError, AttributeError):
            continue
    return {"heads": heads, "bases": bases}


def _refresh(cfg):
    data = _fetch(cfg)
    with _lock:
        _cache.update(root=cfg.root, at=time.time(), data=data, refreshing=False)


def github_prs(wait=False):
    """Cached PR data for the workspace ({} shape EMPTY when disabled or unavailable). Stale ⇒ refresh in a background
    thread and return what is cached; `wait=True` refreshes synchronously (CLI, tests). Failures are cached too, so a
    missing `gh` login is not retried every 5 seconds."""
    cfg = config.current()
    if not cfg.github_prs:
        return EMPTY
    with _lock:
        fresh = _cache["root"] == cfg.root and time.time() - _cache["at"] < TTL
        if fresh or (_cache["refreshing"] and not wait):
            return _cache["data"] if _cache["root"] == cfg.root else EMPTY
        _cache["refreshing"] = True
    if wait:
        _refresh(cfg)
    else:
        threading.Thread(target=_refresh, args=(cfg,), daemon=True).start()
    with _lock:
        return _cache["data"] if _cache["root"] == cfg.root else EMPTY


def ts(iso):
    try:
        return datetime.datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def merged_verdict(prs, last_activity=None):
    """→ the merged PR that settles the work item as done, or None. Done only when nothing is still open and no
    session of the item was active after the merge (work resumed: a QA bug fixed after merging is not done yet)."""
    if not prs or any(p["state"] == "OPEN" for p in prs):
        return None
    merged = [p for p in prs if p["state"] == "MERGED"]
    if not merged:
        return None
    pr = max(merged, key=lambda p: p["number"])
    at = max((ts(p.get("mergedAt")) or 0 for p in merged), default=0)
    if last_activity and at and last_activity > at + 60:
        return None
    return pr
