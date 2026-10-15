"""ticketrail — a work tracker for coding-agent sessions: Claude Code and Codex CLI sessions of a workspace grouped
into work items by ticket or topic, with their status, links, and one-key resume.

  ticketrail [pick] [query]        interactive picker (Textual); opens the chosen session
  ticketrail list  [--days N] [--task KEY] [--all]
  ticketrail find  <words…> [--deep] [--brief] [--limit N]
  ticketrail show  <id-prefix | words in the name>
  ticketrail index [--out FILE]    markdown index of sessions by ticket
  ticketrail label [--limit N]     LLM labels (topic, role, status) for idle sessions
  ticketrail dump  [--json]        groups as text
  ticketrail config show|init      effective configuration / write a starter .ticketrail.toml
  ticketrail doctor                check paths, tools and caches
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import shlex
import shutil
import subprocess
import sys

from . import __version__, config, digests, groups as G, labeler as L
from .i18n import t
from .sources import SOURCES, claude
from .util import fmt_date, fmt_dt, fmt_epoch_date, one_line, rel_path


def resume_cmds(d):
    if d["tool"] == "codex":
        return "codex resume %s" % d["id"], "codex fork %s" % d["id"]
    return "claude --resume %s" % d["id"], "claude --fork-session --resume %s" % d["id"]


# ---------- list / find / show / index ----------
def cmd_list(args):
    ds = digests.load_digests(args.source)
    cutoff = None
    if not args.all:
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)).isoformat()
    rows = []
    for d in ds:
        if cutoff and (d["last_ts"] or "") < cutoff:
            continue
        if args.task:
            k = args.task.upper()
            if k not in d["tickets"] and k not in digests.title_of(d).upper():
                continue
        rows.append(d)
    rows.sort(key=lambda d: d["last_ts"] or "", reverse=True)
    print("%-13s %-6s %-8s %-4s %-6s %-55s %s" % (t("cli.h_date"), "tool", "id", "#p", "KB", t("cli.h_name"), t("cli.h_tickets")))
    for d in rows:
        a, b = fmt_date(d["first_ts"]), fmt_date(d["last_ts"])
        rng = a + ("→" + b if b != a else "")
        name = ("★ " if d["title_custom"] else "  ") + one_line(digests.title_of(d), 52)
        print("%-13s %-6s %-8s %-4d %-6d %-55s %s" % (rng, d["tool"], d["id"][:8], d["n_prompts"], d["size_kb"], name, " ".join(d["tickets"])))
    print("\n" + t("cli.list_footer", n=len(rows)))


def cmd_find(args):
    query = " ".join(args.query).strip()
    if not query:
        sys.exit(t("cli.no_query"))
    files = digests.list_files(args.source)
    paths = [p for _, p in files]
    matched = []
    try:
        for i in range(0, len(paths), 500):  # stay under the argument-length limit
            r = subprocess.run(["grep", "-l", "-i", "-F", "--", query] + paths[i:i + 500], capture_output=True, text=True)
            matched += [ln for ln in r.stdout.splitlines() if ln]
    except FileNotFoundError:
        matched = paths
    tool_of = {p: name for name, p in files}
    by_path = {d["path"]: d for d in digests.load_digests(args.source)}
    results = []
    for p in matched:
        d = by_path.get(p)
        if not d:
            continue
        h = SOURCES[tool_of[p]].hits(p, query, args.deep)
        total = sum(h.values())
        if total:
            results.append((total, h, d))
    if args.brief:
        results.sort(key=lambda r: r[2]["last_ts"] or "", reverse=True)
    else:
        results.sort(key=lambda r: (r[0], r[2]["last_ts"] or ""), reverse=True)
    results = results[: args.limit]

    print(t("cli.find_head", q=query, n=len(results), deep=" " + t("cli.find_deep") if args.deep else ""))
    for total, h, d in results:
        print("\n%s %s  %s → %s  %s%s" % (d["tool"], d["id"][:8], fmt_dt(d["first_ts"]), fmt_dt(d["last_ts"]),
                                         "★ " if d["title_custom"] else "", digests.title_of(d)))
        print("   " + t("cli.find_hits", total=total, p=h["prompt"], a=h["assistant"], tl=h["tool_input"],
                          r=(" · result %d" % h["tool_result"]) if args.deep else "", n=d["n_prompts"], kb=d["size_kb"],
                          branch=("  branch=" + d["branch"]) if d.get("branch") and d["branch"] != "HEAD" else ""))
        if d["prompts"]:
            print("   %s: %s" % (t("cli.first"), one_line(d["prompts"][0]["text"], 160)))
        if d["last_assistant"]:
            print("   %s: %s" % (t("cli.conclusion"), one_line(d["last_assistant"], 400 if args.brief else 300)))

    # Claude sessions removed by retention: only their prompts are left in history.jsonl
    if not args.brief and "claude" in digests.source_names(args.source):
        ondisk = {d["id"] for d in by_path.values()}
        gone = []
        for sid, e in claude.deleted_history(config.current()).items():
            if sid in ondisk:
                continue
            hits = [p for p in e["prompts"] if query.lower() in p.lower()]
            if hits:
                gone.append((e["first_ms"] or 0, sid, hits))
        if gone:
            print("\n" + t("cli.deleted_head", n=len(gone)))
            for ms, sid, hits in sorted(gone, reverse=True)[:15]:
                day = fmt_epoch_date(ms / 1000) if ms else "??"
                print("  %s %s  %s" % (day, (sid or "")[:8], one_line(hits[0], 120)))
    if results:
        r, fk = resume_cmds(results[0][2])
        print("\n" + t("cli.resume_line", r=r, f=fk))


def cmd_show(args):
    key = args.key.strip()
    ds = digests.load_digests(args.source)
    cands = [d for d in ds if d["id"].startswith(key)] or [d for d in ds if key.lower() in digests.title_of(d).lower()]
    if not cands:
        sys.exit(t("cli.no_match", key=key))
    if len(cands) > 1:
        print(t("cli.many"))
        for d in sorted(cands, key=lambda d: d["last_ts"] or "", reverse=True):
            print("  %s %s %s %s" % (d["tool"], d["id"][:8], fmt_date(d["first_ts"]), digests.title_of(d)))
        return
    d = cands[0]
    print("# %s%s" % ("★ " if d["title_custom"] else "", digests.title_of(d)))
    print("%-9s: %s" % (t("cli.tool"), d["tool"]))
    print("id       : %s" % d["id"])
    print("%-9s: %s → %s   (%d prompt, %d KB)" % (t("cli.time"), fmt_dt(d["first_ts"]), fmt_dt(d["last_ts"]), d["n_prompts"], d["size_kb"]))
    if d.get("branch") or d.get("cwd"):
        print("branch   : %s   cwd: %s" % (d.get("branch"), d.get("cwd")))
    if d["tickets"]:
        print("%-9s: %s" % (t("cli.h_tickets"), ", ".join("%s×%d" % kv for kv in d["tickets"].items())))
    if d["plan_file"]:
        print("plan     : %s" % d["plan_file"])
    print("\n## " + t("cli.prompts"))
    for p in d["prompts"]:
        print("- [%s] %s" % (fmt_dt(p["ts"]), p["text"]))
    if d["files_written"]:
        print("\n## " + t("cli.files", n=len(d["files_written"])) + (" — " + t("cli.codex_files") if d["tool"] == "codex" else ""))
        for fp in d["files_written"]:
            print("- " + rel_path(fp))
    if d["memory_files"]:
        print("\n## " + t("cli.memory"))
        for fp in d["memory_files"]:
            print("- " + os.path.basename(fp))
    if d["artifacts"]:
        print("\n## " + t("cli.artifacts"))
        for a in d["artifacts"]:
            print("- %s — %s" % (a.get("title"), a.get("url")))
    if d["last_assistant"]:
        print("\n## " + t("cli.final") + "\n" + d["last_assistant"].strip())
    r, fk = resume_cmds(d)
    print("\n%-6s: %s\n%-6s: %s" % (t("cli.resume"), r, t("cli.fork"), fk))


def cmd_index(args):
    cfg = config.current()
    ds = digests.load_digests(args.source)
    by_ticket = collections.defaultdict(list)
    no_ticket = []
    for d in ds:
        keys = set(d["tickets"]) | set(cfg.tickets.find(digests.title_of(d)))
        if keys:
            for k in keys:
                by_ticket[k].append(d)
        else:
            no_ticket.append(d)

    def row(d, with_range=True):
        rng = fmt_date(d["first_ts"])
        if with_range and fmt_date(d["last_ts"]) != fmt_date(d["first_ts"]):
            rng += "→" + fmt_date(d["last_ts"])
        first = one_line(d["prompts"][0]["text"], 110).replace("|", "\\|") if d["prompts"] else ""
        return "| %s | %s | `%s` | %s%s | %s |" % (rng, d["tool"], d["id"][:8], "★ " if d["title_custom"] else "",
                                                 one_line(digests.title_of(d), 60).replace("|", "\\|"), first)

    n_codex = sum(1 for d in ds if d["tool"] == "codex")
    lines = ["# " + t("idx.title", name=os.path.basename(cfg.root)), "",
             "> " + t("idx.note", when=config.now_local().strftime("%Y-%m-%d %H:%M"), n=len(ds), codex=n_codex,
                      claude=len(ds) - n_codex), ""]
    hdr = ["| %s | Tool | id | %s | %s |" % (t("cli.h_date"), t("cli.h_name"), t("cli.first")), "|---|---|---|---|---|"]
    for k in sorted(by_ticket, key=G.ticket_order):
        lines += ["## %s" % k, ""] + hdr
        for d in sorted(by_ticket[k], key=lambda d: d["last_ts"] or "", reverse=True):
            lines.append(row(d))
        lines.append("")
    if no_ticket:
        lines += ["## " + t("idx.no_ticket"), ""] + hdr
        for d in sorted(no_ticket, key=lambda d: d["last_ts"] or "", reverse=True):
            lines.append(row(d, with_range=False))
        lines.append("")
    out = cfg.abspath(args.out) if args.out else cfg.index_file
    os.makedirs(os.path.dirname(out), exist_ok=True)
    ignore = os.path.join(os.path.dirname(out), ".gitignore")
    if os.path.basename(os.path.dirname(out)) == ".ticketrail" and not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as f:  # the index quotes first prompts: keep it out of commits by default
            f.write(os.path.basename(out) + "\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(t("idx.written", path=out, tickets=len(by_ticket), rest=len(no_ticket)))


# ---------- groups / labels ----------
def cmd_label(args):
    L.run(limit=args.limit, retry_errors=args.retry_errors)


def cmd_dump(args):
    ss = digests.load_sessions()
    labels = {} if args.no_labels else L.load_labels()
    groups, member_of, _, _ = G.resolve_groups(ss, labels, G.load_overrides())  # a broken override file raises here
    if args.json:
        json.dump({"groups": groups, "member_of": member_of}, sys.stdout, ensure_ascii=False, indent=1, default=list)
        print()
        return
    by_id = {d["id"]: d for d in ss}
    for g in sorted(groups.values(), key=lambda g: g["last"], reverse=True):
        print("\n## %s — %s  (%d)" % (g["key"], g["title"], len(g["sessions"])))
        for sid in g["sessions"]:
            d = by_id[sid]
            lb = labels.get(sid) or {}
            print("   %s %s %-6s %s" % (fmt_date(d["last_ts"]), sid[:8], d["tool"], one_line(lb.get("summary") or digests.title_of(d), 90)))
    print("\n" + t("cli.dump_footer", n=len(ss), g=len(groups), unlabeled=sum(1 for d in ss if d["id"] not in labels)))


# ---------- picker ----------
def cmd_pick(args):
    from .model import Model
    from .tui.app import Picker
    act = Picker(Model(no_llm=args.no_llm), " ".join(args.query)).run()
    if not act or act[0] == "quit":
        return
    _, cmd, cwd = act
    shown = cmd if act[0] == "print" or len(cmd[-1]) < 200 else cmd[:-1] + ["<prompt: %d chars>" % len(cmd[-1])]
    print("cd %s && %s" % (shlex.quote(cwd), shlex.join(shown)))
    if act[0] == "print":
        return
    os.chdir(cwd)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        sys.exit(t("cli.exec_failed", cmd=cmd[0], err=e))


# ---------- config / doctor ----------
STARTER = '''# ticketrail workspace configuration — see docs/configuration.md
# lang = "en"                       # "en" or "vi"

[tickets]
# patterns = ['\\bPROJ-\\d+\\b']     # default: any Jira-style key (ABC-123)
# url = "https://jira.example.com/browse/{key}"

[naming]
# tags = ["OPS", "DOC"]             # session-name prefixes for work without a ticket

[labeler]
# enabled = true
# context = "What this workspace is about, in one or two sentences."
'''


def cmd_config(args):
    cfg = config.current()
    if args.action == "init":
        p = os.path.join(cfg.root, config.CONFIG_FILE)
        if os.path.exists(p):
            sys.exit(t("cfg.exists", path=p))
        with open(p, "w", encoding="utf-8") as f:
            f.write(STARTER)
        print(t("cfg.written", path=p))
        return
    print("root        : " + cfg.root)
    print("config files: " + (", ".join(cfg.sources) or "(defaults only)"))
    print("cache       : " + cfg.cache_dir)
    print("groups file : " + cfg.groups_file)
    print()
    print(json.dumps(cfg.data, indent=2, ensure_ascii=False))


def cmd_doctor(args):
    cfg = config.current()
    ok = True

    def line(good, label, detail=""):
        nonlocal ok
        ok = ok and good is not False
        mark = {True: "ok ", False: "ERR", None: " - "}[good]
        print("[%s] %-22s %s" % (mark, label, detail))

    line(True, "workspace root", cfg.root)
    line(True, "config files", ", ".join(cfg.sources) or "(defaults only)")
    for name, src in SOURCES.items():
        n = len(src.files(cfg))
        line(True if n else None, "%s transcripts" % name, str(n))
    for tool in ("claude", "codex"):
        p = shutil.which(tool)
        line(True if p else None, "%s on PATH" % tool, p or "not found (cannot resume %s sessions)" % tool)
    if shutil.which("claude"):
        from . import live
        line(True if live.claude_live() is not None else None, "live state",
             "claude agents --json" if live.claude_live() is not None else "unavailable (Claude Code < 2.1.139?): transcript activity is used")
    if cfg.github_prs:
        from . import signals
        g = shutil.which("gh")
        n = len(signals.github_prs(wait=True)["heads"]) if g else 0
        line(True if g and n else False, "github pull requests", "%d branches with PRs" % n if g else "gh not found — install the GitHub CLI or set github.pull_requests = false")
    st = L.state()
    line({"on": True, "off": None, "ask": None, "missing": None}[st], "LLM labels", {
        "on": "on (%s)" % shutil.which(cfg.labeler_command),
        "off": "off",
        "ask": "not decided yet — the picker asks on first run (or set labeler.enabled)",
        "missing": "%s not found: grouping uses tickets, branches and manual changes only" % cfg.labeler_command}[st])
    line(True, "cache", cfg.cache_dir)
    labels = L.load_labels()
    line(True, "labels", str(len(labels)))
    try:
        ov = G.load_overrides()
        line(True, "groups file", "%s (%d groups, %d moved sessions)" % (cfg.groups_file, len(ov["groups"]), len(ov["sessions"]))
             if os.path.exists(cfg.groups_file) else "%s (not created yet)" % cfg.groups_file)
    except G.OverrideError as e:
        line(False, "groups file", str(e))
    sys.exit(0 if ok else 1)


def build_parser():
    ap = argparse.ArgumentParser(prog="ticketrail", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--root", help="workspace root (default: nearest .ticketrail.toml, git toplevel, or cwd)")
    ap.add_argument("--lang", choices=["en", "vi"], help="interface language")
    sub = ap.add_subparsers(dest="cmd")

    def add_source(p):
        p.add_argument("--source", choices=["all", "codex", "claude"], default="all")

    a = sub.add_parser("pick", help="interactive picker")
    a.add_argument("query", nargs="*")
    a.add_argument("--no-llm", action="store_true", help="never start the background labeler")
    a.set_defaults(fn=cmd_pick)
    a = sub.add_parser("list", help="list sessions")
    a.add_argument("--days", type=int, default=30)
    a.add_argument("--task", help="filter by ticket key or words in the name")
    a.add_argument("--all", action="store_true", help="no date limit")
    add_source(a)
    a.set_defaults(fn=cmd_list)
    a = sub.add_parser("find", help="full-text search")
    a.add_argument("query", nargs="+")
    a.add_argument("--deep", action="store_true", help="also count tool results (slower, noisier)")
    a.add_argument("--brief", action="store_true", help="newest first, longer conclusions (for hand-offs)")
    a.add_argument("--limit", type=int, default=12)
    add_source(a)
    a.set_defaults(fn=cmd_find)
    a = sub.add_parser("show", help="digest of one session")
    a.add_argument("key")
    add_source(a)
    a.set_defaults(fn=cmd_show)
    a = sub.add_parser("index", help="write a markdown index by ticket")
    a.add_argument("--out", help="output file (default: paths.index_file)")
    add_source(a)
    a.set_defaults(fn=cmd_index)
    a = sub.add_parser("label", help="label idle sessions with the LLM")
    a.add_argument("--limit", type=int, default=0)
    a.add_argument("--retry-errors", action="store_true")
    a.set_defaults(fn=cmd_label)
    a = sub.add_parser("dump", help="print work items and their sessions")
    a.add_argument("--json", action="store_true")
    a.add_argument("--no-labels", action="store_true", help="ignore LLM labels (tickets and manual changes only)")
    a.set_defaults(fn=cmd_dump)
    a = sub.add_parser("config", help="show the effective configuration or write a starter file")
    a.add_argument("action", nargs="?", choices=["show", "init"], default="show")
    a.set_defaults(fn=cmd_config)
    a = sub.add_parser("doctor", help="check paths, tools and caches")
    a.set_defaults(fn=cmd_doctor)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd is None:  # bare `ticketrail` opens the picker
        args = ap.parse_args((argv or sys.argv[1:]) + ["pick"])
    try:
        config.use(config.load(root=args.root, overrides={"lang": args.lang} if args.lang else None))
    except config.ConfigError as e:
        sys.exit("config: %s" % e)
    args.fn(args)
