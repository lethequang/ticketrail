"""Codex CLI rollouts: <codex home>/sessions/YYYY/MM/DD/rollout-*.jsonl; names and titles from state_*.sqlite."""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3

from .. import config
from ..util import content_text, one_line
from .base import add_file, count_tickets, finish_digest, new_digest, note_branch

NAME = "codex"
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
# user messages Codex injects itself (not typed by a person)
INJECTED_RE = re.compile(r"^\s*(<(environment_context|skills_instructions|multi_agent_role|permissions_instructions|"
                         r"user_instructions|collaboration_mode|turn_aborted|app_context|plugin_instructions|AGENTS\.md)\b|# AGENTS\.md)")

_threads = None


def reset():
    """Forget cached thread names (a /rename may just have happened)."""
    global _threads
    _threads = None


def threads():
    """id -> {name, title, cwd, git_branch} from the newest state_*.sqlite, opened read-only."""
    global _threads
    if _threads is not None:
        return _threads
    _threads = {}
    home = config.current().codex_home
    try:
        dbs = sorted(p for p in os.listdir(home) if re.fullmatch(r"state_\d+\.sqlite", p))
        if dbs:
            con = sqlite3.connect("file:%s?mode=ro" % os.path.join(home, dbs[-1]), uri=True)
            for row in con.execute("select id, name, title, cwd, git_branch from threads"):
                _threads[row[0]] = {"name": row[1], "title": row[2], "cwd": row[3], "git_branch": row[4]}
            con.close()
    except Exception:  # noqa: BLE001 — no database / older schema: names are optional
        pass
    return _threads


def session_meta(path):
    """First line (session_meta), read to filter by cwd before parsing the whole file."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            o = json.loads(f.readline())
        if o.get("type") == "session_meta":
            return o.get("payload") or {}
    except (ValueError, OSError):
        pass
    return {}


def files(cfg):
    pat = os.path.join(glob.escape(cfg.codex_home), "sessions", "*", "*", "*", "rollout-*.jsonl")
    return [p for p in sorted(glob.glob(pat)) if cfg.in_root(session_meta(p).get("cwd") or "")]


def owns(cfg, path):
    return path.startswith(os.path.join(cfg.codex_home, "sessions") + "/")


def patch_files(args_or_input, cwd):
    """File paths inside an apply_patch call (function_call.arguments JSON or custom_tool_call.input)."""
    s = args_or_input
    if isinstance(s, str) and s.lstrip().startswith("{"):
        try:
            s = json.loads(s).get("input") or json.loads(s).get("patch") or ""
        except ValueError:
            pass
    if isinstance(s, dict):
        s = s.get("input") or s.get("patch") or ""
    out = []
    for fp in PATCH_FILE_RE.findall(s or ""):
        fp = fp.strip()
        out.append(fp if os.path.isabs(fp) else os.path.normpath(os.path.join(cwd or config.current().root, fp)))
    return out


def digest(path):
    meta = session_meta(path)
    sid = meta.get("id") or meta.get("session_id") or os.path.basename(path)[-42:-6]
    d = new_digest(NAME, sid, path)
    d["cwd"] = meta.get("cwd")
    th = threads().get(sid) or {}
    d["title_custom"] = th.get("name") or None
    d["title_ai"] = th.get("title") or None
    d["branch"] = (meta.get("git") or {}).get("branch") or th.get("git_branch") or None
    note_branch(d, d["branch"])
    seen_files, seen_mem = set(), set()
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"type":"session_meta"' in line[:120] or '"type":"token_usage_record"' in line[:120]:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") != "response_item":
                continue
            p = o.get("payload") or {}
            pt = p.get("type")
            ts = o.get("timestamp")
            if pt == "message":
                role = p.get("role")
                txt = content_text(p.get("content"))
                if role == "user":
                    if not txt.strip() or INJECTED_RE.match(txt):
                        continue
                    if ts:
                        d["last_ts"] = max(d["last_ts"] or ts, ts)
                    if d["first_ts"] is None:
                        d["first_ts"] = ts
                    d["prompts"].append({"ts": ts, "text": one_line(txt, 200)})
                    count_tickets(d, txt)
                elif role == "assistant":
                    if ts:
                        d["last_ts"] = max(d["last_ts"] or ts, ts)
                    if txt.strip():
                        d["last_assistant"] = txt[-1500:]
            elif pt in ("function_call", "custom_tool_call"):
                if ts:
                    d["last_ts"] = max(d["last_ts"] or ts, ts)
                if p.get("name") == "apply_patch":
                    for fp in patch_files(p.get("arguments") if pt == "function_call" else p.get("input"), d["cwd"]):
                        add_file(d, fp, seen_files, seen_mem)
    if d["first_ts"] is None:
        d["first_ts"] = meta.get("timestamp")
        d["last_ts"] = d["last_ts"] or d["first_ts"]
    return finish_digest(d)


def hits(path, query, deep):
    q = query.lower()
    h = {"prompt": 0, "assistant": 0, "tool_input": 0, "tool_result": 0}
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if q not in line.lower():
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") != "response_item":
                continue
            p = o.get("payload") or {}
            pt = p.get("type")
            if pt == "message":
                txt = content_text(p.get("content"))
                if p.get("role") == "user":
                    if not INJECTED_RE.match(txt):
                        h["prompt"] += txt.lower().count(q)
                elif p.get("role") == "assistant":
                    h["assistant"] += txt.lower().count(q)
            elif pt in ("function_call", "custom_tool_call"):
                h["tool_input"] += json.dumps(p.get("arguments") or p.get("input") or "", ensure_ascii=False).lower().count(q)
            elif deep and pt in ("function_call_output", "custom_tool_call_output"):
                h["tool_result"] += json.dumps(p.get("output") or "", ensure_ascii=False).lower().count(q)
    return h
