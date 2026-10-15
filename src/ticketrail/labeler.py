"""LLM labels: topic (for grouping), title, role, summary, status, next step — one cheap model call per session.

Runs through the `claude` CLI in print mode, from a directory outside the workspace and without session persistence,
so labeling never creates sessions that would show up in the list. Labels are cached per workspace.
"""
from __future__ import annotations

import collections
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

from . import config
from .digests import load_sessions, title_of
from .i18n import t
from .util import fold, one_line, read_json, slugify, write_json

IDLE_SEC = 600         # label only sessions idle for 10 minutes
RELABEL_DELTA = 5      # re-evaluate the topic after ≥ 5 more prompts
RETRY_SEC = 3600       # failed call ⇒ retry after an hour
MAX_FAILS = 3          # consecutive failures ⇒ stop the run (CLI broken / out of quota), don't mark the whole queue
LABEL_VER = 1
ROLES = ["research", "code", "test", "docs", "ops", "env", "qa"]
STATUSES = ["done", "in_progress", "waiting", "abandoned"]
LANG_NAME = {"en": "English", "vi": "Vietnamese"}
SEC_PER_SESSION = 6.5     # measured: one Haiku call through `claude -p`, sequential
USD_PER_SESSION = 0.0006  # measured on real sessions (prompt ≈ 10 prompts + final answer)
ON, OFF, ASK, MISSING = "on", "off", "ask", "missing"


def state():
    """on / off / ask (not decided yet: nothing is ever sent) / missing (no `claude` CLI, e.g. Codex-only users).
    An explicit `labeler.enabled` in the config wins over the answer given on first run."""
    cfg = config.current()
    if cfg.labeler_setting is False:
        return OFF
    if not shutil.which(cfg.labeler_command):
        return MISSING
    if cfg.labeler_setting is True:
        return ON
    answer = read_json(cfg.consent_file, {}).get("labeler")
    return ON if answer is True else OFF if answer is False else ASK


def set_consent(value):
    write_json(config.current().consent_file, {"labeler": bool(value), "at": int(time.time())})


def estimate(n):
    """(minutes, dollars) to label n sessions, from measured averages; shown before asking."""
    return max(1, math.ceil(n * SEC_PER_SESSION / 60)), n * USD_PER_SESSION


def load_labels():
    return read_json(config.current().labels_file, {})


def needs_label(d, label, now, retry_errors=False):
    if now - d["mtime"] < IDLE_SEC:
        return False
    if not label:
        return True
    if label.get("error") and not retry_errors and now - (label.get("error_at") or 0) < RETRY_SEC:
        return False
    if not label.get("topic") or (label.get("v") or 0) < LABEL_VER:
        return True
    return d["n_prompts"] - topic_mark(label) >= RELABEL_DELTA


def topic_mark(label):
    """Prompt count when the topic was set (a version upgrade keeps the topic, so it must not move this mark)."""
    return label.get("topic_n", label.get("n_prompts")) or 0


def apply_result(old, d, result, now):
    """Success ⇒ new label. Failure ⇒ keep the old label (if any) and only mark the error for a later retry."""
    if "error" in result:
        lb = dict(old or {"n_prompts": d["n_prompts"]})
        lb.update(error=result["error"], error_at=int(now))
        return lb
    lb = dict(result, n_prompts=d["n_prompts"], topic_n=d["n_prompts"], at=int(now), v=LABEL_VER)
    if old and old.get("topic") and d["n_prompts"] - topic_mark(old) < RELABEL_DELTA:
        # relabeled for another reason than growth (version upgrade…) ⇒ keep the topic so groups don't reshuffle
        lb.update(topic=old["topic"], title=old.get("title") or lb.get("title"), topic_n=topic_mark(old))
    return lb


def label_queue(sessions, labels, now=None, retry_errors=False):
    now = now or time.time()
    q = [d for d in sessions if needs_label(d, labels.get(d["id"]), now, retry_errors)]
    q.sort(key=lambda d: d["mtime"], reverse=True)
    return q


def labeler_running():
    cfg = config.current()
    os.makedirs(cfg.cache_dir, exist_ok=True)
    with open(cfg.labeler_lock, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def build_prompt(d, topics):
    cfg = config.current()
    lang = LANG_NAME[cfg.lang]
    lines = ["You classify one working session of a software engineer with an AI coding agent."]
    if cfg.labeler_context:
        lines.append("About the workspace: " + cfg.labeler_context.strip())
    lines += [
        'Return ONLY one JSON object: {"topic": "...", "title": "...", "role": "...", "summary": "...", "status": "...", "next": "..."}',
        "- topic: kebab-case ASCII slug, 2-5 words, naming the PROBLEM or SUBJECT (not the action). "
        "If the session is about the same problem as one of the existing topics below, reuse that exact slug.",
        "- title: human name of the topic in %s, at most 8 words." % lang,
        "- role: one of %s — research = investigating or analysing; code = writing or changing code; test = testing, "
        "E2E, QA verification; docs = writing documentation; ops = operations or incidents; env = dev environment or "
        "tooling; qa = questions and discussion." % json.dumps(ROLES),
        "- summary: at most 25 words in %s: what the session did / concluded." % lang,
        "- status: one of %s, how far the session's problem got resolved, judged from the last prompt and the final "
        "answer: done = goal reached, nothing left; in_progress = main work still to do; waiting = our part is done but "
        "it waits on someone else (a reviewer, product owner, QA, another team) to answer or act; abandoned = stopped "
        "midway with no conclusion." % json.dumps(STATUSES),
        "- next: at most 20 words in %s: what is left or who/what we are waiting on; empty if status is done." % lang,
        "",
        "Existing topics:",
    ]
    lines += ["- %s — %s" % kv for kv in topics] or ["(none)"]
    lines += [
        "",
        "Session:",
        "Name: " + title_of(d),
        "Tickets: " + (", ".join(d.get("tickets") or {}) or "(none)"),
        "Files written: " + (", ".join(os.path.basename(f) for f in (d.get("files_written") or [])[:25]) or "(none)"),
        "User prompts:",
    ]
    lines += ["%d. %s" % (i + 1, p["text"]) for i, p in enumerate(d["prompts"][:10])]
    lines += ["Final answer of the assistant:", (d.get("last_assistant") or "")[-1500:]]
    return "\n".join(lines)


def known_topics(labels, limit=200):
    c = collections.Counter()
    title = {}
    for lb in labels.values():
        if lb.get("topic"):
            c[lb["topic"]] += 1
            title.setdefault(lb["topic"], lb.get("title") or "")
    return [(tp, title[tp]) for tp, _ in c.most_common(limit)]


def parse_answer(res):
    m = re.search(r"\{.*\}", res, re.S)
    if not m:
        raise ValueError("no JSON: " + res[:200])
    o = json.loads(m.group(0))
    if not fold(o.get("topic") or "").strip():
        raise ValueError("empty topic")
    return {"topic": slugify(o["topic"]), "title": one_line(o.get("title") or "", 60),
            "role": o.get("role") if o.get("role") in ROLES else "qa",
            "summary": one_line(o.get("summary") or "", 200),
            "status": o.get("status") if o.get("status") in STATUSES else "",  # unknown ⇒ unknown; never guess
            "next": one_line(o.get("next") or "", 160)}


def call_llm(prompt):
    cfg = config.current()
    os.makedirs(cfg.labeler_dir, exist_ok=True)
    r = subprocess.run(
        [cfg.labeler_command, "-p", "--model", cfg.labeler_model, "--output-format", "json", "--no-session-persistence",
         "--strict-mcp-config", "--tools", "", "--disable-slash-commands", "--setting-sources", ""],
        input=prompt, capture_output=True, text=True, cwd=cfg.labeler_dir, timeout=120)
    try:
        res = json.loads(r.stdout).get("result") or ""
    except ValueError:
        raise ValueError(one_line(r.stderr or r.stdout or "exit %d" % r.returncode, 200))
    return parse_answer(res)


def run(limit=0, retry_errors=False):
    cfg = config.current()
    st = state()
    if st == MISSING:
        print(t("lab.missing", cmd=cfg.labeler_command), file=sys.stderr)
        return
    if st == OFF:
        print(t("lab.disabled"), file=sys.stderr)
        return
    if st == ASK:
        if not sys.stdin.isatty():
            print(t("lab.ask_first"), file=sys.stderr)
            return
        n = len(label_queue(load_sessions(), load_labels(), retry_errors=retry_errors))
        minutes, usd = estimate(n)
        answer = input(t("lab.prompt", what=t("consent.what"), n=n, min=minutes, usd="%.2f" % usd) + " [y/N] ")
        set_consent(answer.strip().lower() in ("y", "yes"))
        if state() != ON:
            return
    os.makedirs(cfg.cache_dir, exist_ok=True)
    with open(cfg.labeler_lock, "a") as lockf:  # the lock lives as long as the file stays open
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(t("lab.running"), file=sys.stderr)
            return
        _run_queue(cfg, limit, retry_errors)


def _run_queue(cfg, limit, retry_errors):
    q = label_queue(load_sessions(), load_labels(), retry_errors=retry_errors)[: limit or None]
    print(t("lab.queue", when=time.strftime("%Y-%m-%d %H:%M:%S"), n=len(q)), flush=True)
    fails = 0
    for i, d in enumerate(q, 1):
        try:
            d["mtime"] = os.path.getmtime(d["path"])
        except OSError:
            continue
        if time.time() - d["mtime"] < IDLE_SEC:  # resumed while waiting in the queue
            continue
        labels = load_labels()  # re-read every turn: the topic list grows
        try:
            res = call_llm(build_prompt(d, known_topics(labels)))
            fails = 0
        except Exception as e:  # noqa: BLE001 — one failing session must not stop the queue
            res = {"error": one_line(str(e), 200)}
            fails += 1
        labels[d["id"]] = apply_result(labels.get(d["id"]), d, res, time.time())
        write_json(cfg.labels_file, labels)
        print("[%d/%d] %s %s %s" % (i, len(q), d["id"][:8], res.get("topic") or "ERR", res.get("error") or res.get("title")),
              flush=True)
        if fails >= MAX_FAILS:
            print(t("lab.stop", n=fails), flush=True)
            return


def spawn_argv(cfg):
    """Config files are re-read by the child; flags are not, so pass the resolved language explicitly."""
    return [sys.executable, "-m", "ticketrail", "--root", cfg.root, "--lang", cfg.lang, "label"]


def spawn_background():
    """Detached labeler process: it outlives the picker after the picker execs into the agent CLI."""
    cfg = config.current()
    os.makedirs(cfg.labeler_dir, exist_ok=True)
    with open(cfg.labeler_log, "a") as log:
        subprocess.Popen(spawn_argv(cfg),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cfg.labeler_dir, start_new_session=True)
