"""Group sessions by task / problem. Pure functions plus the manual-override file.

Every session belongs to exactly one group, by precedence:
  manual override > ticket in the user-given session name > ticket in the AI title > ticket of the git branch, if the
  session mentions it > ticket with most hits > ticket of the git branch > LLM topic (attached to a ticket when that
  topic is almost only used for one ticket) > "Unsorted".
Secondary tickets and files written by a few sessions only make sessions "related"; they never merge groups.
"""
from __future__ import annotations

import collections
import os
import re

from . import config
from .digests import title_of
from .i18n import t
from .util import read_json, slugify, write_json

ACTIVE_SEC = 180       # transcript changed in the last 3 minutes ⇒ the session is running
UNGROUPED = "~ungrouped"


def tickets():
    return config.current().tickets


def is_ticket(key):
    return tickets().is_ticket(key or "")


def ticket_order(k):
    m = re.match(r"(.*?)(\d+)$", k)
    return (m.group(1), int(m.group(2))) if m else (k, 0)


# ---------- override file ----------
class OverrideError(Exception):
    pass


def overrides_path():
    return config.current().groups_file


def load_overrides(path=None):
    """Missing file ⇒ empty. Broken / wrong shape ⇒ OverrideError (never treated as empty, or the next write would
    wipe every manual grouping)."""
    path = path or overrides_path()
    if not os.path.exists(path):
        return {"groups": {}, "sessions": {}}
    o = read_json(path, OverrideError)
    if o is OverrideError:
        raise OverrideError(t("ov.broken", file=os.path.basename(path)))
    ok = isinstance(o, dict)
    if ok:
        o.setdefault("groups", {})
        o.setdefault("sessions", {})
    ok = ok and isinstance(o["groups"], dict) and isinstance(o["sessions"], dict)
    ok = ok and all(isinstance(k, str) and isinstance(v, str) for k, v in o["sessions"].items())
    for g in o["groups"].values() if ok else []:
        ok = ok and isinstance(g, dict) and isinstance(g.get("title", ""), str) \
            and isinstance(g.get("members", []), list) and all(isinstance(m, str) for m in g.get("members", [])) \
            and isinstance(g.get("key_session", ""), str)
    if not ok:
        raise OverrideError(t("ov.shape", file=os.path.basename(path)))
    return o


def update_overrides(fn, path=None):
    """Re-read the file right before changing it (keeps edits made by hand or by another picker), apply fn, write."""
    path = path or overrides_path()
    o = load_overrides(path)
    fn(o)
    write_json(path, o)
    return o


def ov_move(ov, sid, target, base, title=None):
    """Move a session to `target`; moving it back to its natural group removes the override."""
    if target == base:
        ov["sessions"].pop(sid, None)
    else:
        ov["sessions"][sid] = target
    if title:
        ov["groups"].setdefault(target, {"members": []})["title"] = title


def ov_rename(ov, key, title):
    ov["groups"].setdefault(key, {"members": []})["title"] = title


def ov_merge(ov, src, dst):
    """Merge group src into dst (src becomes an alias of dst). "Ungrouped" never merges: it is where sessions fall."""
    if src == dst or UNGROUPED in (src, dst):
        return
    g = ov["groups"].setdefault(dst, {"members": []})
    old = ov["groups"].pop(src, {})
    if old.get("key_session") and not g.get("key_session"):
        g["key_session"] = old["key_session"]  # target had no pinned session ⇒ keep the merged group's pin
    for m in [src] + (old.get("members") or []):
        if m != dst and m not in g["members"]:
            g["members"].append(m)
    for k, v in list(ov["sessions"].items()):
        if v == src:
            ov["sessions"][k] = dst


def ov_pin(ov, key, sid):
    """Pin the key session of a group; pinning the pinned session again unpins it."""
    g = ov["groups"].setdefault(key, {"members": []})
    if g.get("key_session") == sid:
        g.pop("key_session")
    else:
        g["key_session"] = sid


def new_group_key(title):
    return "u:" + slugify(title)


# ---------- grouping (pure) ----------
def branch_tickets(d):
    """Tickets named by the branches the session was on, in order (deduplicated)."""
    out = []
    for b in d.get("branches") or ([d["branch"]] if d.get("branch") and d["branch"] != "HEAD" else []):
        k = tickets().search(b)
        if k and k not in out:
            out.append(k)
    return out


def branch_ticket(d):
    """The ticket of the session's branch — only when unambiguous: a session that hopped between ticket branches
    says nothing reliable through its branch."""
    ks = branch_tickets(d)
    return ks[0] if len(ks) == 1 else None


def base_key(d, labels, topic_ticket):
    k = tickets().search(d.get("title_custom") or "")
    if k:
        return k
    tk = list(d.get("tickets") or {})
    br = branch_ticket(d)
    if tk:
        in_title = set(tickets().find(title_of(d)))
        # a branch can outlive its ticket: it only counts when the session itself mentions that ticket
        return ([k for k in tk if k in in_title] or ([br] if br in tk else []) or tk)[0]
    if br:
        return br  # silent session on a ticket branch (`feature/SHOP-12-…`): better than a guessed topic
    topic = (labels.get(d["id"]) or {}).get("topic")
    if topic:
        return topic_ticket.get(topic) or "topic:" + topic
    return UNGROUPED


def topic_ticket_map(sessions, labels):
    """A topic used by ≥ 2 sessions with tickets, ≥ 2/3 of them the same ticket ⇒ ticketless sessions of that topic
    join that ticket's group."""
    by_topic = collections.defaultdict(collections.Counter)
    for d in sessions:
        topic = (labels.get(d["id"]) or {}).get("topic")
        tk = list(d.get("tickets") or {})
        if topic and tk:
            by_topic[topic][base_key(d, {}, {})] += 1
    out = {}
    for topic, c in by_topic.items():
        k, n = c.most_common(1)[0]
        total = sum(c.values())
        if total >= 2 and n * 3 >= total * 2:
            out[topic] = k
    return out


def follow_alias(key, alias):
    seen = set()
    while key in alias and key not in seen:
        seen.add(key)
        key = alias[key]
    return key


def resolve_groups(sessions, labels, overrides):
    """→ (groups{key: {...}}, member_of{id: key}, related{id: [...]}, base{id: group without override}).

    groups[key] = {key, title, sessions:[id…] newest first, tickets:[…], last}.
    """
    cfg = config.current()
    alias = {}
    for gkey, g in overrides.get("groups", {}).items():
        for m in g.get("members") or []:
            if m != gkey:
                alias[m] = gkey
    sess_over = overrides.get("sessions", {})
    topic_ticket = topic_ticket_map(sessions, labels)

    member_of, base = {}, {}
    for d in sessions:
        b = base_key(d, labels, topic_ticket)
        base[d["id"]] = b
        member_of[d["id"]] = follow_alias(sess_over.get(d["id"]) or b, alias)

    by_id = {d["id"]: d for d in sessions}
    groups = {}
    for sid, key in member_of.items():
        g = groups.setdefault(key, {"key": key, "sessions": [], "tickets": set()})
        g["sessions"].append(sid)
        if is_ticket(base[sid]) and sid not in sess_over:
            g["tickets"].add(base[sid])
    for key, g in groups.items():
        g["sessions"].sort(key=lambda i: by_id[i].get("last_ts") or "", reverse=True)
        g["tickets"] = sorted(g["tickets"] | ({key} if is_ticket(key) else set()), key=ticket_order)
        g["last"] = max(by_id[i]["mtime"] for i in g["sessions"])
        g["title"] = group_title(key, g, by_id, labels, overrides)

    related = collections.defaultdict(list)
    for d in sessions:
        for k in list(d.get("tickets") or {}) + ([base[d["id"]]] if is_ticket(base[d["id"]]) else []):
            if ("ticket", k, "") in related[d["id"]]:
                continue
            if k not in groups.get(member_of[d["id"]], {}).get("tickets", []):
                related[d["id"]].append(("ticket", k, ""))
    writers = collections.defaultdict(set)
    for d in sessions:
        for fp in d.get("files_written") or []:
            if not (cfg.related_ignore_re and cfg.related_ignore_re.search(fp)):
                writers[fp].add(d["id"])
    for fp, ids in writers.items():
        if not 2 <= len(ids) <= cfg.hub_max:
            continue
        for a in ids:
            for b in ids:
                if a != b and member_of[a] != member_of[b]:
                    related[a].append(("session", b, os.path.basename(fp)))
    return groups, member_of, dict(related), base


def group_title(key, g, by_id, labels, overrides):
    title = (overrides.get("groups", {}).get(key) or {}).get("title")
    if title:
        return title
    if key == UNGROUPED:
        return t("ungrouped")
    if is_ticket(key):
        for sid in g["sessions"]:
            tc = by_id[sid].get("title_custom") or ""
            if key in tickets().find(tc):
                rest = tc.replace(key, "").strip(" -:·")
                if rest:
                    return rest
    names = collections.Counter((labels.get(i) or {}).get("title") for i in g["sessions"])
    names.pop(None, None)
    if names:
        return names.most_common(1)[0][0]
    return title_of(by_id[g["sessions"][0]])


def session_tag(d):
    """A configured tag (naming.tags) at the start of the user-given session name: work without a ticket."""
    tag_re = config.current().tag_re
    m = tag_re.match(d.get("title_custom") or "") if tag_re else None
    return m.group(1) if m else None
