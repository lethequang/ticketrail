"""Small helpers shared by every module: text, JSON files, dates in the configured time zone and language."""
from __future__ import annotations

import datetime
import json
import os
import re
import time
import unicodedata

from . import config
from .i18n import t


def fold(s):
    """Strip accents + casefold so typing without diacritics still matches."""
    s = unicodedata.normalize("NFD", s or "").replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").casefold()


def slugify(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", fold(s))).strip("-")[:48] or "group"


def one_line(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def content_text(c):
    """Join text blocks: Claude {type:text,text}, Codex {type:input_text|output_text,text}."""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text"))
    return ""


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj, indent=2):
    """Atomic write; the temp name carries the pid so two writers never share a temp file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent, sort_keys=indent is not None)
        if indent is not None:
            f.write("\n")
    os.replace(tmp, path)


# ---------- dates ----------
def to_local(iso):
    if not iso:
        return None
    try:
        d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    tz = config.current().tz
    return d.astimezone(tz) if tz else d.astimezone()


def fmt_date(iso):
    d = to_local(iso)
    return d.strftime(t("fmt.date")) if d else "??"


def fmt_dt(iso):
    d = to_local(iso)
    return d.strftime(t("fmt.datetime")) if d else "?"


def fmt_epoch_date(sec):
    tz = config.current().tz
    d = datetime.datetime.fromtimestamp(sec, tz) if tz else datetime.datetime.fromtimestamp(sec).astimezone()
    return d.strftime(t("fmt.date"))


def rel_time(ts, now):
    d = max(0, now - ts)
    if d < 60:
        return t("rel.now")
    if d < 3600:
        return t("rel.min", n=int(d // 60))
    if d < 86400:
        return t("rel.hour", n=int(d // 3600))
    if d < 30 * 86400:
        return t("rel.day", n=int(d // 86400))
    return fmt_epoch_date(ts)


def fmt_size(kb):
    return "%dK" % kb if kb < 1024 else "%.1fM" % (kb / 1024)


def rel_path(p):
    """Path relative to the workspace (the root itself shows as its name), else with ~ for the home directory."""
    root = config.current().root
    if p == root:
        return os.path.basename(root)
    return (p or "").replace(root + "/", "").replace(os.path.expanduser("~"), "~")


def now():
    return time.time()
