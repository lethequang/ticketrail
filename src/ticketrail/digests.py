"""Digest cache: parse each transcript once, re-parse only when its size or mtime changes."""
from __future__ import annotations

import os
import sys

from . import config
from .i18n import t
from .sources import SOURCES, codex
from .util import read_json, write_json

CACHE_VERSION = 2  # 2: digests record every branch (`branches`)


def source_names(source="all"):
    return list(SOURCES) if (source or "all") == "all" else [source]


def list_files(source="all"):
    cfg = config.current()
    return [(name, p) for name in source_names(source) for p in SOURCES[name].files(cfg)]


def title_of(d):
    return d.get("title_custom") or d.get("title_ai") or d.get("slug") or (d["prompts"][0]["text"] if d["prompts"] else t("no_prompt"))


def load_digests(source="all", verbose=False):
    cfg = config.current()
    cache = read_json(cfg.digest_cache, None)
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        cache = {"version": CACHE_VERSION, "files": {}}
    files = list_files(source)
    out, changed = [], 0
    for name, p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        key = "%s|%d|%d" % (p, st.st_size, int(st.st_mtime))
        ent = cache["files"].get(p)
        if ent and ent.get("key") == key:
            out.append(ent["digest"])
            continue
        if verbose:
            print("  parse", name, os.path.basename(p), file=sys.stderr)
        dg = SOURCES[name].digest(p)
        cache["files"][p] = {"key": key, "digest": dg}
        out.append(dg)
        changed += 1
    # drop entries of deleted files — only for the sources scanned now, so the other source's cache survives
    scanned = {p for _, p in files}
    for p in list(cache["files"]):
        if p not in scanned and any(SOURCES[n].owns(cfg, p) for n in source_names(source)):
            del cache["files"][p]
            changed += 1
    if changed:
        write_json(cfg.digest_cache, cache, indent=None)
    # `codex resume` appends a new rollout with the same session id ⇒ keep the copy with most prompts / newest
    best = {}
    for d in out:
        k = (d["tool"], d["id"])
        if k not in best or (d["n_prompts"], d["last_ts"] or "") > (best[k]["n_prompts"], best[k]["last_ts"] or ""):
            best[k] = d
    return list(best.values())


def is_labeler(d):
    cfg = config.current()
    cwd = d.get("cwd") or ""
    return cwd == cfg.labeler_dir or cwd.startswith(cfg.labeler_dir + "/")


def load_sessions(source="all"):
    """Digests that still have a transcript, minus empty sessions and the labeler's own; with mtime attached."""
    codex.reset()
    out = []
    for d in load_digests(source):
        if not d.get("n_prompts") or is_labeler(d):
            continue
        try:
            d["mtime"] = os.path.getmtime(d["path"])
        except OSError:
            continue
        out.append(d)
    return out
