"""What a session left behind that a reader wants to open, and how to open a session again.

UI-free so it is testable with plain unittest.
"""
from __future__ import annotations

import collections
import os
import re

from . import config, groups as G
from .util import rel_path

SCRATCH_RE = re.compile(r"/scratchpad/")
TMP_RE = re.compile(r"^/(private/)?tmp/")
HEADING_RE = re.compile(r"#{1,6}\s")
FENCE_RE = re.compile(r"(```|~~~)")
Link = collections.namedtuple("Link", "kind label target exists src line")  # src = session id; line = heading line
HISTORY_MAX = 24000


def is_scratch(path):
    """Agent scratch files, and temp files outside the workspace (a workspace may itself live under /tmp)."""
    return bool(SCRATCH_RE.search(path) or (TMP_RE.search(path) and not config.current().in_root(path)))


# ---------- opening sessions ----------
def resume_cmd(d, fork=False):
    if d["tool"] == "codex":
        return ["codex", "fork" if fork else "resume", d["id"]]
    return ["claude"] + (["--fork-session"] if fork else []) + ["--resume", d["id"]]


def session_cwd(d):
    """Claude finds a session by the project directory encoded from the cwd. The cwd at the first prompt may be a
    subdirectory of where the session was started ⇒ walk up to the parent whose encoding names the transcript dir."""
    root = config.current().root
    c = d.get("cwd") or ""
    if d["tool"] == "claude":
        proj = os.path.basename(os.path.dirname(d["path"]))
        while c and c != "/":
            if config.encode_path(c) == proj:
                break
            c = os.path.dirname(c)
    return c if c and c != "/" and os.path.isdir(c) else root


def handoff_history(members, labels, key_sid=None, limit=15):
    """Earlier sessions of a work item, oldest first, one line each — the context a new session starts from.
    Built from digests and labels already on disk: no model call."""
    from .digests import title_of
    rows = sorted(members, key=lambda d: d.get("mtime") or 0)[-limit:]
    out = ["Earlier sessions of this work item (oldest first):"]
    for d in rows:
        lb = labels.get(d["id"]) or {}
        status = lb.get("status") or "unknown"
        line = "- %s · %s %s · %s · %s — %s" % ((d.get("last_ts") or "")[:10], d["tool"], d["id"], lb.get("role") or "?",
                                                status, lb.get("summary") or title_of(d))
        if lb.get("next"):
            line += " Next: " + lb["next"]
        if d["id"] == key_sid:
            line += " [key session]"
        out.append(line)
    if len(members) > len(rows):
        out.append("(%d older sessions not listed)" % (len(members) - len(rows)))
    text = "\n".join(out)
    return text if len(text) <= HISTORY_MAX else text[:HISTORY_MAX] + "\n(truncated)"  # one argv string: stay far below 128 KB


def handoff_cmd(g, members=(), labels=None, key_sid=None, agent="claude"):
    """A new agent session that picks up a work item. Claude sessions are named `<KEY|tag> <topic ≤ 6 words>`;
    the first prompt carries the item's history when the template asks for {history}."""
    cfg = config.current()
    key = g["key"]
    head = key if G.is_ticket(key) else (g["tickets"][0] if len(g["tickets"]) == 1 else None)
    tag = None
    if not head:
        tags = collections.Counter(x for x in (G.session_tag(d) for d in members) if x)
        tag = tags.most_common(1)[0][0] if tags else None
    words = " ".join([w for w in g["title"].split() if w != head][:6])
    name = " ".join(cfg.handoff_name.format(key=head or tag or "", title=words).split())
    subject = head or g["title"]
    template = cfg.handoff_codex_prompt if agent == "codex" else cfg.handoff_prompt
    history = handoff_history(members, labels or {}, key_sid, cfg.handoff_max) if "{history}" in template else ""
    prompt = template.format(subject=subject, key=head or "", title=g["title"], history=history)
    if agent == "codex":
        return ["codex", prompt]  # Codex has no name flag; /rename inside the session
    return ["claude", "-n", name, prompt]


# ---------- links ----------
def is_url(s):
    return s.startswith(("http://", "https://"))


_TITLE_CACHE = {}


def md_title(path):
    """First `# ` line of a markdown file (plan files have random names), cached by mtime."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    c = _TITLE_CACHE.get(path)
    if not c or c[0] != mt:
        title = None
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for _, line in zip(range(40), f):
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except OSError:
            pass
        c = _TITLE_CACHE[path] = (mt, title)
    return c[1]


def in_docs(path):
    return any(path.startswith(d + "/") for d in config.current().docs_dirs)


def links(d, backlinks=None, prs=()):
    """ticket · pull request · artifact · doc · backlink · plan · memory · other files, in that order."""
    cfg = config.current()
    out, seen = [], set()

    def add(kind, label, target, line=None):
        if not target or (target, line) in seen:
            return
        seen.add((target, line))
        out.append(Link(kind, label, target, is_url(target) or os.path.exists(target), d["id"], line))

    for k in d.get("tickets") or {}:
        add("ticket", k, cfg.tickets.url(k))
    for p in prs:
        add("pr", "#%d %s" % (p["number"], p["state"].lower()), p["url"])
    for a in d.get("artifacts") or []:
        add("artifact", a.get("title") or a["url"], a["url"])
    files = [f for f in d.get("files_written") or [] if not is_scratch(f)]
    for f in files:
        if in_docs(f):
            add("doc", rel_path(f), f)
    for path, line, heading in (backlinks or {}).get(d["id"][:8], []):
        add("backlink", "%s › %s" % (os.path.basename(path), heading), path, line)
    if d.get("plan_file"):
        add("plan", md_title(d["plan_file"]) or os.path.basename(d["plan_file"]), d["plan_file"])
    for f in d.get("memory_files") or []:
        add("memory", os.path.basename(f), f)
    for f in files:
        if not in_docs(f) and f != d.get("plan_file"):
            add("code", rel_path(f), f)
    return out


_BL_CACHE = {}


def _scan_backlinks(path, backlink_re, single_entry):
    """Files of a single-entry kind (one decision per file) point at their H1; others at the nearest heading above."""
    res, heading, hline, fence = [], "", 0, False
    with open(path, encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f, 1):
            if FENCE_RE.match(line):
                fence = not fence
                continue
            if fence:
                continue  # an example inside a code block is not a real backlink
            if HEADING_RE.match(line) and not (single_entry and hline):
                heading, hline = line.strip("# \n"), i
            for m in backlink_re.finditer(line):
                res.append((m.group(1), hline or i, heading or os.path.basename(path)))
    return res


def backlinks(roots=None):
    """{id8: [(markdown file, heading line, heading)]} for docs mentioning a session id. Cached per file by mtime."""
    cfg = config.current()
    if cfg.backlink_re is None:
        return {}
    out = collections.defaultdict(list)
    single = tuple("/%s/" % x.strip("/") for x in cfg.single_entry_dirs)
    for root in roots if roots is not None else cfg.docs_dirs:
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = sorted(x for x in dirnames if not x.startswith(".") and x != "node_modules")
            for fn in sorted(files):
                if not fn.endswith(".md"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                key = (p, cfg.backlink_re.pattern, single)
                c = _BL_CACHE.get(key)
                if not c or c[0] != mt:
                    try:
                        c = _BL_CACHE[key] = (mt, _scan_backlinks(p, cfg.backlink_re, any(s in p for s in single)))
                    except OSError:
                        continue
                for id8, line, heading in c[1]:
                    out[id8].append((p, line, heading))
    return dict(out)


def md_section(text, line, heading=None):
    """Markdown from the heading at `line` (1-based) to the next heading of the same or higher level.
    `line` was captured when scanning; if the file changed since (a new entry on top) ⇒ find `heading` again,
    and if it is gone return the whole file rather than guess another entry."""
    lines = text.splitlines()
    if heading is not None and not (line and line <= len(lines) and lines[line - 1].strip("# ") == heading):
        line = next((i for i, ln in enumerate(lines, 1) if HEADING_RE.match(ln) and ln.strip("# ") == heading), None)
    if not line or line > len(lines) or not lines[line - 1].startswith("#"):
        return text
    level = len(lines[line - 1]) - len(lines[line - 1].lstrip("#"))
    out, fence = [lines[line - 1]], False
    for ln in lines[line:]:
        if FENCE_RE.match(ln):
            fence = not fence
        elif not fence and HEADING_RE.match(ln) and len(ln) - len(ln.lstrip("#")) <= level:
            break
        out.append(ln)
    return "\n".join(out)


def meaningful_files(d):
    return [f for f in d.get("files_written") or [] if not is_scratch(f) and "/plans/" not in f]


def key_session(g, by_id, ov):
    """→ (sid, pinned). A pin that is still in the group wins; else the session that left the most real files,
    then the one with most prompts, then the newest."""
    pin = ((ov or {}).get("groups", {}).get(g["key"]) or {}).get("key_session")
    if pin in g["sessions"]:
        return pin, True
    return max(g["sessions"], key=lambda s: (len(meaningful_files(by_id[s])), by_id[s]["n_prompts"], by_id[s]["mtime"])), False
