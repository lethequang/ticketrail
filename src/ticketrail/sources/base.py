"""The digest: a compact, cacheable summary of one transcript, the same shape for every tool."""
from __future__ import annotations

import collections
import os
import re

from .. import config

PLANS_RE = re.compile(r"/plans/[\w.-]+\.md$")


def new_digest(tool, sid, path):
    return {
        "tool": tool, "id": sid, "path": path,
        "title_custom": None, "title_ai": None, "slug": None,
        "first_ts": None, "last_ts": None, "branch": None, "branches": [], "cwd": None,
        "prompts": [], "tickets": collections.Counter(), "files_written": [],
        "artifacts": [], "plan_file": None, "memory_files": [], "last_assistant": "",
        "size_kb": os.path.getsize(path) // 1024,
    }


def note_branch(d, branch):
    """Every git branch the session was on, in order (an agent may create a new branch mid-session)."""
    if branch and branch != "HEAD" and (not d["branches"] or d["branches"][-1] != branch):
        if branch not in d["branches"]:
            d["branches"].append(branch)


def count_tickets(d, text):
    for k in config.current().tickets.find(text):
        d["tickets"][k] += 1


def finish_digest(d):
    for fp in d["files_written"]:
        count_tickets(d, os.path.basename(fp))
    d["tickets"] = dict(collections.Counter(d["tickets"]).most_common())
    d["n_prompts"] = len(d["prompts"])
    return d


def add_file(d, fp, seen_files, seen_mem):
    """Agent memory files are listed apart; the first plan-like markdown file becomes the session's plan."""
    if "/memory/" in fp:
        if fp not in seen_mem:
            seen_mem.add(fp)
            d["memory_files"].append(fp)
    elif fp not in seen_files:
        seen_files.add(fp)
        d["files_written"].append(fp)
        if d["plan_file"] is None and PLANS_RE.search(fp):
            d["plan_file"] = fp
