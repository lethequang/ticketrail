"""Picker state: data, filters, override writes. UI-free so plain unittest can drive it."""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

from . import config, digests, groups as G, labeler as L, live as LV, signals
from .i18n import t
from .links import Link, backlinks, key_session, links
from .util import fold

RELOAD_SEC = 5
SPAWN_EVERY_SEC = 60
SOURCES = ["all", "claude", "codex"]
DAYS = [30, 7, None]


class Model:
    """load() only reads disk and returns a snapshot (safe in a worker thread); apply() installs a snapshot."""

    def __init__(self, no_llm=False, load_sessions=None, load_labels=None, load_live=None, load_prs=None):
        self.no_llm = no_llm  # --no-llm: never label, never ask
        self.llm_state = L.state()
        self._load_sessions = load_sessions or digests.load_sessions
        self._load_labels = load_labels or L.load_labels
        self._load_live = load_live or LV.claude_live
        self._load_prs = load_prs or signals.github_prs
        self.source = 0
        self.days = 0
        self.query = ""
        self.last_spawn = 0
        self.error = None
        self.seq = 0
        self._lock = threading.Lock()
        self._next_seq = 0
        self.reload()

    def load(self, fetch=True):
        """Serialised (lock): one scan at a time, or two scans would write the digest cache / reset Codex names
        under each other. Snapshots are numbered so apply() can drop one that arrives late."""
        with self._lock:
            self._next_seq += 1
            seq = self._next_seq
            st = self._load(fetch)
        st.seq = seq
        return st

    def _load(self, fetch=True):
        sessions = self._load_sessions()
        labels = self._load_labels()
        try:
            ov, ov_error = G.load_overrides(), None
        except G.OverrideError as e:
            ov, ov_error = {"groups": {}, "sessions": {}}, str(e)
        groups, member_of, related, base = G.resolve_groups(sessions, labels, ov)
        by_id = {d["id"]: d for d in sessions}
        key_of = {k: key_session(g, by_id, ov) for k, g in groups.items()}
        hay = {d["id"]: fold(" ".join([
            d["id"], digests.title_of(d), d.get("title_custom") or "", " ".join(d.get("tickets") or {}),
            " ".join(str(v) for k, v in (labels.get(d["id"]) or {}).items() if k in ("topic", "title", "summary")),
            " ".join(p["text"] for p in d["prompts"]),
            " ".join(os.path.basename(f) for f in d.get("files_written") or []),
        ])) for d in sessions}
        now = time.time()
        return SimpleNamespace(
            sessions=sessions, by_id=by_id, labels=labels, ov_error=ov_error, ov=ov, key_of=key_of, backlinks=backlinks(),
            groups=groups, member_of=member_of, related=related, base=base, hay=hay, now=now,
            running=L.labeler_running(), queue=len(L.label_queue(sessions, labels, now)),
            # external commands only on the periodic scan; an override write re-reads the disk and keeps the rest
            live=self._load_live() if fetch or not hasattr(self, "live") else self.live,
            prs=(self._load_prs() if fetch or not hasattr(self, "prs") else self.prs) or signals.EMPTY)

    def apply(self, st):
        """→ False when the snapshot is older than the one in use (a background scan landing after an override write)."""
        if st.seq < self.seq:
            return False
        self.__dict__.update(vars(st))
        self.error = None
        return True

    def reload(self, fetch=True):
        try:
            self.apply(self.load(fetch))
        except Exception as e:  # noqa: BLE001 — transcript deleted mid-scan, cache half written…: keep the old data
            if not hasattr(self, "sessions"):
                raise
            self.error = t("scan_error", err=e)

    def maybe_spawn(self):
        """Background labeler for new idle sessions."""
        if not self.llm_on() or self.running or not self.queue or time.time() - self.last_spawn < SPAWN_EVERY_SEC:
            return
        self.last_spawn = time.time()
        L.spawn_background()
        self.running = True

    def llm_on(self):
        return not self.no_llm and self.llm_state == L.ON

    def should_ask_llm(self):
        return not self.no_llm and self.llm_state == L.ASK and bool(self.queue)

    def answer_llm(self, yes):
        L.set_consent(yes)
        self.llm_state = L.state()

    # --- filters ---
    def live_state(self, sid):
        """BUSY / OPEN / None. Claude sessions use `claude agents` when it answered; otherwise (Codex, older Claude
        Code) a transcript changed in the last 3 minutes counts as busy."""
        d = self.by_id[sid]
        if d["tool"] == "claude" and self.live is not None:
            return self.live.get(sid)
        return LV.BUSY if self.now - d["mtime"] < G.ACTIVE_SEC else None

    def active(self, sid):
        return self.live_state(sid) is not None

    def group_live(self, sids):
        states = {self.live_state(s) for s in sids}
        return LV.BUSY if LV.BUSY in states else (LV.OPEN if LV.OPEN in states else None)

    def label(self, sid):
        return self.labels.get(sid) or {}

    def pass_filters(self, sid):
        d = self.by_id[sid]
        src = SOURCES[self.source]
        if src != "all" and d["tool"] != src:
            return False
        days = DAYS[self.days]
        return not days or self.now - d["mtime"] < days * 86400

    @staticmethod
    def match(text_folded, q):
        return all(w in text_folded for w in fold(q).split())

    def group_hit(self, g):
        return bool(self.query) and self.match(fold(g["key"] + " " + g["title"]), self.query)

    def visible_groups(self):
        """Groups with ≥ 1 session passing the source/date filters and matching the query; name hits first, then newest."""
        out = []
        for g in self.groups.values():
            sids = [s for s in g["sessions"] if self.pass_filters(s)]
            if not sids:
                continue
            hit = self.group_hit(g)
            if self.query and not hit and not any(self.match(self.hay[s], self.query) for s in sids):
                continue
            out.append(dict(g, vis=sids, last=max(self.by_id[s]["mtime"] for s in sids), hit=hit))
        out.sort(key=lambda g: (g["hit"], g["last"]), reverse=True)
        return out

    def visible_sessions(self, key):
        """Sessions of a group, newest first. Group matched by key/name ⇒ all of them; matched by content ⇒ the hits."""
        g = self.groups.get(key)
        if not g:
            return []
        sids = [s for s in g["sessions"] if self.pass_filters(s)]
        if self.query and not self.group_hit(g):
            sids = [s for s in sids if self.match(self.hay[s], self.query)]
        return sorted(sids, key=lambda s: self.by_id[s]["mtime"], reverse=True)

    def members(self, key):
        return [self.by_id[s] for s in self.groups[key]["sessions"]]

    def newest(self, sids):
        """Newest session (by mtime) — the single source for a group's status."""
        return max(sids, key=lambda s: self.by_id[s]["mtime"])

    def session_prs(self, sid, key=None):
        """PRs of the branches this session was on — minus base branches (develop, main) and branches that name a
        ticket of another work item (the session started on an old branch, then moved on)."""
        d = self.by_id[sid]
        key = key or self.member_of.get(sid)
        own = set(self.groups[key]["tickets"]) if key in self.groups else set()
        heads, bases = self.prs.get("heads", {}), set(self.prs.get("bases", []))
        out = []
        for b in d.get("branches") or ([d["branch"]] if d.get("branch") and d["branch"] != "HEAD" else []):
            bt = G.tickets().search(b)
            if b in bases or (bt and bt not in own):
                continue
            out += heads.get(b, [])
        return out

    def group_prs(self, key):
        seen, out = set(), []
        for sid in self.groups[key]["sessions"]:
            for p in self.session_prs(sid, key):
                if p["number"] not in seen:
                    seen.add(p["number"])
                    out.append(p)
        return sorted(out, key=lambda p: p["number"])

    def work_status(self, key):
        """Status of a work item, over all its sessions whatever the filters: a merged PR (nothing open, no work after
        the merge) settles it as done; otherwise the LLM label of its newest session. `reason` marks a hard signal."""
        sids = self.groups[key]["sessions"]
        newest = self.newest(sids)
        pr = signals.merged_verdict(self.group_prs(key), self.by_id[newest]["mtime"])
        if pr:
            return {"status": "done", "next": "", "reason": "PR #%d merged" % pr["number"]}
        return self.label(newest)

    def links(self, sid):
        return links(self.by_id[sid], self.backlinks, self.session_prs(sid))

    def group_links(self, key):
        """Group tickets + docs / artifacts / backlinks / plans of every session, deduplicated (memory and other files
        only appear in the session detail)."""
        cfg = config.current()
        g = self.groups[key]
        out = [Link("ticket", k, cfg.tickets.url(k), True, None, None) for k in g["tickets"] if cfg.tickets.url(k)]
        out += [Link("pr", "#%d %s" % (p["number"], p["state"].lower()), p["url"], True, None, None) for p in self.group_prs(key)]
        seen = {(lk.target, lk.line) for lk in out}
        for sid in g["sessions"]:
            for lk in self.links(sid):
                if lk.kind in ("doc", "artifact", "backlink", "plan", "pr") and (lk.target, lk.line) not in seen:
                    seen.add((lk.target, lk.line))
                    out.append(lk)
        order = ["ticket", "pr", "artifact", "doc", "backlink", "plan"]
        return sorted(out, key=lambda lk: order.index(lk.kind))  # stable: newest session first within a kind

    def related_text(self, sid):
        parts = [k for kind, k, _ in self.related.get(sid, []) if kind == "ticket"]
        seen = set()
        for kind, k, f in self.related.get(sid, []):
            if kind == "session" and k not in seen and k in self.by_id:
                seen.add(k)
                parts.append("%s (%s)" % (k[:8], f))
        return ", ".join(parts[:10])

    # --- override writes ---
    def apply_ov(self, fn):
        """→ None when written, an error string when not (a broken override file is never overwritten)."""
        try:
            G.update_overrides(fn)
        except (G.OverrideError, OSError) as e:
            self.reload(fetch=False)
            return str(e)
        self.reload(fetch=False)  # runs on the UI thread: disk only, never `gh` / `claude`
        return None
