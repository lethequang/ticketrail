"""Workspace discovery and configuration.

Precedence (later wins): built-in defaults < user config < workspace config < command-line flags.

  user config       $XDG_CONFIG_HOME/ticketrail/config.toml  ([defaults] + [workspace."<abs path>"] tables)
  workspace config  <root>/.ticketrail.toml                  (commit it to share with a team)

The workspace root is, in order: --root, the nearest parent with .ticketrail.toml, the git toplevel, the cwd.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field

CONFIG_FILE = ".ticketrail.toml"

DEFAULTS = {
    "lang": "en",
    "timezone": "",  # IANA name; empty = the machine's local time zone
    "tickets": {
        # Jira-style keys (ABC-123). Prefixes in `ignore` are never tickets (UTF-8, SHA-256, GPT-4…).
        "patterns": [r"\b[A-Z][A-Z0-9]{1,9}-\d+\b"],
        "ignore": ["UTF", "SHA", "ISO", "RFC", "CVE", "HTTP", "TLS", "SSL", "IPV", "PEP", "GPT", "AES", "RSA", "ECMA",
                   "WCAG", "COVID"],
        "url": "",  # e.g. "https://jira.example.com/browse/{key}" or ".../issues/{num}"; empty = no links
    },
    "naming": {"tags": []},  # session-name prefixes for work without a ticket, e.g. ["OPS", "DOC"]
    "paths": {
        "docs": ["docs"],
        "groups_file": ".ticketrail/groups.json",
        "index_file": ".ticketrail/INDEX.md",
    },
    "backlinks": {
        "pattern": r"Session:\s*\**\s*`?([0-9a-f]{8})\b",
        "single_entry_dirs": ["adr"],  # one decision per file ⇒ a backlink points at the file title, not the section
    },
    "related": {
        "ignore": [r"/scratchpad/", r"/plans/", r"/MEMORY\.md$", r"/\.ticketrail/"],
        "hub_max": 4,
    },
    "labeler": {"enabled": True, "command": "claude", "model": "haiku", "context": ""},
    "open": {"large_session_mb": 5, "editor": "code -g {path}:{line}"},
    "github": {"pull_requests": False},  # confirm "done" with merged PRs via `gh` (network; opt-in)
    "handoff": {
        "name": "{key} {title}",
        "prompt": 'Continue the work on "{subject}".\n\n{history}\n\nRead what you need from those sessions '
                  '(`ticketrail show <id>` prints one), summarise the current state, the open items and who we are '
                  "waiting on, then wait for my instructions.",
        "max_sessions": 15,  # newest sessions listed in {history} (1–50)
        # Codex cannot run Claude skills or slash commands: it always gets a prompt with the history
        "codex_prompt": 'Continue the work on "{subject}".\n\n{history}\n\nSummarise the current state, the open items '
                        "and who we are waiting on, then wait for my instructions.",
    },
}


class ConfigError(Exception):
    pass


def encode_path(p):
    """How Claude Code names a project directory: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def validate(data, schema=DEFAULTS, where=""):
    """Unknown keys and wrong types are errors: a typo like [ticket] must not be silently ignored."""
    if not isinstance(data, dict):
        raise ConfigError("%s: expected a table" % (where or "config"))
    for k, v in data.items():
        name = where + k
        if k not in schema:
            raise ConfigError("unknown key %s (see docs/configuration.md)" % name)
        ref = schema[k]
        if isinstance(ref, dict):
            validate(v, ref, name + ".")
        elif isinstance(ref, list):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ConfigError("%s: expected a list of strings" % name)
        elif isinstance(ref, bool):
            if not isinstance(v, bool):
                raise ConfigError("%s: expected true or false" % name)
        elif isinstance(ref, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ConfigError("%s: expected a number" % name)
        elif not isinstance(v, str):
            raise ConfigError("%s: expected a string" % name)


def check_template(name, template, **fields):
    try:
        template.format(**fields)
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError("%s: bad placeholder %s (allowed: %s)" % (name, e, ", ".join("{%s}" % f for f in fields)))


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def read_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError("%s: %s" % (path, e))


def user_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ticketrail", "config.toml")


def cache_home():
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def find_root(cwd=None):
    """Nearest parent with .ticketrail.toml, else the git toplevel, else cwd."""
    cwd = os.path.realpath(cwd or os.getcwd())
    d = cwd
    while True:
        if os.path.isfile(os.path.join(d, CONFIG_FILE)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.realpath(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd


class Tickets:
    """Ticket keys from configurable regexes: find in text, test a key, build its link, read it back from a link."""

    def __init__(self, patterns, ignore=(), url=""):
        try:
            self.res = [re.compile(p) for p in patterns]
            self.any_re = re.compile("|".join("(?:%s)" % p for p in patterns)) if patterns else None
        except re.error as e:
            raise ConfigError("tickets.patterns: %s" % e)
        self.ignore = {s.upper() for s in ignore}
        self.url_template = url
        self.url_re = None
        if url and "{key}" in url and self.any_re is not None:
            prefix = re.sub(r"^https?://", "", url.split("{key}", 1)[0])
            self.url_re = re.compile(re.escape(prefix) + "(" + self.any_re.pattern + ")")

    def _ok(self, key):
        prefix = re.match(r"[A-Za-z]*", key).group(0).upper()
        return prefix not in self.ignore

    def find(self, text):
        """Every ticket key in text, in order, repeats kept (callers count hits). Links to the tracker count too."""
        if not text or self.any_re is None:
            return []
        out = [m.group(0) for m in self.any_re.finditer(text) if self._ok(m.group(0))]
        if self.url_re is not None:
            # a link to the tracker is a stronger signal than a bare mention: it counts once more
            out += [m.group(1) for m in self.url_re.finditer(text) if self._ok(m.group(1))]
        return out

    def search(self, text):
        found = self.find(text)
        return found[0] if found else None

    def is_ticket(self, key):
        return bool(key) and any(r.fullmatch(key) for r in self.res) and self._ok(key)

    def url(self, key):
        """`{key}` = the whole key, `{num}` = its trailing number (GitHub issues: "#12" ⇒ …/issues/12)."""
        if not self.url_template:
            return None
        m = re.search(r"\d+$", key)
        return self.url_template.replace("{key}", key).replace("{num}", m.group(0) if m else key)


@dataclass
class Config:
    root: str
    data: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    sources: list = field(default_factory=list)  # config files that were read, for `config show` / `doctor`

    def __post_init__(self):
        self.root = os.path.realpath(self.root)
        validate(self.data)
        d = self.data
        if d["lang"] not in ("en", "vi"):
            raise ConfigError('lang: expected "en" or "vi"')
        self.lang = d["lang"]
        self.tz = None
        if d.get("timezone"):
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(d["timezone"])
            except Exception as e:  # noqa: BLE001 — unknown zone name
                raise ConfigError("timezone: %s" % e)
        t = d["tickets"]
        self.tickets = Tickets(t.get("patterns") or [], t.get("ignore") or [], t.get("url") or "")
        tags = [re.escape(x) for x in d["naming"].get("tags") or []]
        self.tag_re = re.compile(r"^\s*(%s)\b" % "|".join(tags)) if tags else None
        p = d["paths"]
        self.docs_dirs = [self.abspath(x) for x in p.get("docs") or []]
        self.groups_file = self.abspath(p["groups_file"])
        self.index_file = self.abspath(p["index_file"])
        b = d["backlinks"]
        try:
            self.backlink_re = re.compile(b["pattern"]) if b.get("pattern") else None
            ign = d["related"].get("ignore") or []
            self.related_ignore_re = re.compile("|".join("(?:%s)" % x for x in ign)) if ign else None
        except re.error as e:
            raise ConfigError("regex: %s" % e)
        self.single_entry_dirs = list(b.get("single_entry_dirs") or [])
        self.hub_max = int(d["related"].get("hub_max", 4))
        lab = d["labeler"]
        self.labeler_enabled = bool(lab.get("enabled", True))
        self.labeler_command = lab.get("command") or "claude"
        self.labeler_model = lab.get("model") or "haiku"
        self.labeler_context = lab.get("context") or ""
        self.large_kb = int(float(d["open"].get("large_session_mb", 5)) * 1024)
        self.editor = d["open"].get("editor") or ""
        self.github_prs = bool(d["github"].get("pull_requests"))
        self.handoff_name = d["handoff"].get("name") or "{key} {title}"
        self.handoff_prompt = d["handoff"].get("prompt") or ""
        check_template("handoff.name", self.handoff_name, key="", title="")
        self.handoff_max = int(d["handoff"].get("max_sessions", 15))
        if not 1 <= self.handoff_max <= 50:
            raise ConfigError("handoff.max_sessions: expected 1 to 50")
        self.handoff_codex_prompt = d["handoff"].get("codex_prompt") or DEFAULTS["handoff"]["codex_prompt"]
        check_template("handoff.prompt", self.handoff_prompt, key="", title="", subject="", history="")
        check_template("handoff.codex_prompt", self.handoff_codex_prompt, key="", title="", subject="", history="")
        check_template("open.editor", self.editor, path="", line=1)
        # where things live
        self.claude_home = claude_home()
        self.codex_home = codex_home()
        self.claude_projects = os.path.join(self.claude_home, "projects")
        self.claude_project_prefix = encode_path(self.root)
        # the encoding is lossy ('/a/my-app' = '/a/my.app' = '/a/my/app'), so a hash of the real path keeps caches apart
        slug = (encode_path(self.root).strip("-")[-60:] or "root") + "-" + hashlib.sha1(self.root.encode()).hexdigest()[:8]
        self.cache_dir = os.path.join(cache_home(), "ticketrail", slug)
        self.digest_cache = os.path.join(self.cache_dir, "digests.json")
        self.labels_file = os.path.join(self.cache_dir, "labels.json")
        self.labeler_dir = os.path.join(self.cache_dir, "labeler")
        self.labeler_lock = os.path.join(self.cache_dir, "labeler.lock")
        self.labeler_log = os.path.join(self.cache_dir, "labeler.log")

    def abspath(self, p):
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else os.path.join(self.root, p)

    def in_root(self, path):
        path = path or ""
        return path == self.root or path.startswith(self.root + "/")


def load(root=None, cwd=None, overrides=None):
    """Build the effective Config for a workspace. `overrides` is a dict merged last (command-line flags)."""
    root = os.path.realpath(os.path.expanduser(root)) if root else find_root(cwd)
    data, sources = copy.deepcopy(DEFAULTS), []
    up = user_config_path()
    u = read_toml(up)
    extra = set(u) - {"defaults", "workspace"}
    if extra:
        raise ConfigError("%s: put settings under [defaults] or [workspace.\"<path>\"], not at the top (%s)" % (up, ", ".join(sorted(extra))))
    if u:
        data = deep_merge(data, u.get("defaults") or {})
        for k, v in (u.get("workspace") or {}).items():
            if os.path.realpath(os.path.expanduser(k)) == root:
                data = deep_merge(data, v)
        sources.append(up)
    wp = os.path.join(root, CONFIG_FILE)
    w = read_toml(wp)
    if w:
        data = deep_merge(data, w)
        sources.append(wp)
    data = deep_merge(data, overrides or {})
    return Config(root=root, data=data, sources=sources)


_current = None


def current():
    """The Config every module reads at call time. Commands call use(load(...)) once at start-up."""
    global _current
    if _current is None:
        _current = load()
    return _current


def use(cfg):
    global _current
    _current = cfg
    return cfg


def now_local():
    cfg = current()
    return datetime.datetime.now(cfg.tz) if cfg.tz else datetime.datetime.now().astimezone()
