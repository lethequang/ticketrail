"""Claude Code transcripts: <claude home>/projects/<encoded cwd>/<session id>.jsonl (+ history.jsonl for deleted ones)."""
from __future__ import annotations

import collections
import glob
import json
import os
import re

from .. import config
from ..util import content_text, one_line
from .base import add_file, count_tickets, finish_digest, new_digest, note_branch

NAME = "claude"
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def first_cwd(path, max_lines=30):
    """cwd recorded in the first records of a transcript ('' if none)."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for _, line in zip(range(max_lines), f):
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return ""


def files(cfg):
    """Transcripts of the workspace and its subdirectories. Directory names are ambiguous ('/a/b-c', '/a/b.c' and
    '/a/b/c' all encode to '-a-b-c'), so every transcript must prove its cwd is inside the root."""
    prefix = cfg.claude_project_prefix
    out = []
    for d in sorted(glob.glob(os.path.join(glob.escape(cfg.claude_projects), glob.escape(prefix) + "*"))):
        name = os.path.basename(d)
        if name != prefix and not name.startswith(prefix + "-"):
            continue
        for p in sorted(glob.glob(os.path.join(glob.escape(d), "*.jsonl"))):
            cwd = first_cwd(p)
            if cfg.in_root(cwd) or (name == prefix and not cwd):  # no cwd recorded yet ⇒ trust the exact directory
                out.append(p)
    return out


def owns(cfg, path):
    """Is this cache entry a transcript this source scans (so it may be dropped when the file disappears)?"""
    return path.startswith(cfg.claude_projects + "/")


def digest(path):
    cfg = config.current()
    plan_re = re.compile(re.escape(os.path.join(cfg.claude_home, "plans")) + r"/[\w.-]+\.md")
    d = new_digest(NAME, os.path.basename(path)[:-6], path)
    seen_files, seen_mem = set(), set()
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"type":"attachment"' in line[:200] or '"type":"file-history' in line[:200]:
                continue
            if d["plan_file"] is None and "plans/" in line:
                m = plan_re.search(line)
                if m:
                    d["plan_file"] = m.group(0)
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            if t == "custom-title":
                d["title_custom"] = o.get("customTitle")
            elif t == "ai-title":
                d["title_ai"] = o.get("aiTitle")
            elif t == "frame-link":
                if o.get("frameUrl") and not any(a["url"] == o["frameUrl"] for a in d["artifacts"]):
                    d["artifacts"].append({"title": o.get("title") or os.path.basename(o.get("path") or ""),
                                           "url": o["frameUrl"], "ts": o.get("timestamp")})
            elif t == "user":
                note_branch(d, o.get("gitBranch"))
                ts = o.get("timestamp")
                if ts:
                    d["last_ts"] = max(d["last_ts"] or ts, ts)
                if o.get("promptSource") == "typed" and not o.get("isMeta"):
                    txt = content_text(o.get("message", {}).get("content"))
                    txt = re.sub(r"<command-message>.*?</command-message>\s*", "", txt, flags=re.S)
                    if not txt.strip():
                        continue
                    if d["first_ts"] is None:
                        d["first_ts"] = ts
                        d["slug"] = o.get("slug")
                        d["branch"] = o.get("gitBranch")
                        d["cwd"] = o.get("cwd")
                    d["prompts"].append({"ts": ts, "text": one_line(txt, 200)})
                    count_tickets(d, txt)
            elif t == "assistant":
                ts = o.get("timestamp")
                if ts:
                    d["last_ts"] = max(d["last_ts"] or ts, ts)
                for b in o.get("message", {}).get("content", []) or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        d["last_assistant"] = b["text"][-1500:]
                    elif b.get("type") == "tool_use" and b.get("name") in WRITE_TOOLS:
                        inp = b.get("input") or {}
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp:
                            add_file(d, fp, seen_files, seen_mem)
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
            t = o.get("type")
            if t == "user":
                c = o.get("message", {}).get("content")
                if o.get("promptSource") == "typed":
                    h["prompt"] += content_text(c).lower().count(q)
                elif deep:
                    h["tool_result"] += json.dumps(c, ensure_ascii=False).lower().count(q)
            elif t == "assistant":
                for b in o.get("message", {}).get("content", []) or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        h["assistant"] += b.get("text", "").lower().count(q)
                    elif b.get("type") == "tool_use":
                        h["tool_input"] += json.dumps(b.get("input"), ensure_ascii=False).lower().count(q)
            elif t == "frame-link":
                h["tool_input"] += line.lower().count(q)
    return h


def deleted_history(cfg):
    """sessionId -> {first_ms, prompts[]} from history.jsonl: the only trace left of transcripts removed by retention."""
    h = collections.OrderedDict()
    path = os.path.join(cfg.claude_home, "history.jsonl")
    if not os.path.exists(path):
        return h
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not cfg.in_root(o.get("project") or ""):
                continue
            e = h.setdefault(o.get("sessionId"), {"first_ms": o.get("timestamp"), "prompts": []})
            e["prompts"].append(o.get("display", ""))
    return h
