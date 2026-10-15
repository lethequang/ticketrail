# Configuration

`ticketrail` works with no configuration. This page lists every key and how to adapt it to a workspace's
conventions.

## Where configuration comes from

Later layers win:

1. built-in defaults;
2. **user config**: `$XDG_CONFIG_HOME/ticketrail/config.toml` (usually `~/.config/ticketrail/config.toml`).
   - A `[defaults]` table applies to every workspace.
   - A `[workspace."<absolute path>"]` table applies to one workspace.
   - This layer is useful to configure a repository without adding files to it.
3. **workspace config**: `<root>/.ticketrail.toml`. Commit it to share settings with a team.
4. command-line flags: `--lang`, `--root`.

`ticketrail config show` prints the effective configuration and the files it was read from. An unknown key, a
value of the wrong type, a bad regex or an unknown `{placeholder}` stops the program with a message naming the key,
so typos never go unnoticed. (`config show` and `doctor` print in English whatever `lang` is.)

### Which directory is the workspace

In order:
1. `--root`;
2. the nearest parent directory containing `.ticketrail.toml`;
3. the git repository root;
4. the current directory.

The workspace includes sessions whose working directory is the root or any directory below it:
- Claude Code: transcripts under `~/.claude/projects/<encoded path>*`. A directory whose name only shares the prefix
  must prove its `cwd` is inside the root.
- Codex CLI: rollouts whose `session_meta.cwd` is inside the root.

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honoured.

## Keys

### `lang`
`"en"` (default) or `"vi"`. It sets the interface language and the language the labeler writes titles, summaries and
next steps in.

### `timezone`
An IANA name such as `"Europe/Berlin"`. Empty (the default) means the machine's local time zone.

### `[tickets]`

| Key | Default | Meaning |
|---|---|---|
| `patterns` | `['\b[A-Z][A-Z0-9]{1,9}-\d+\b']` | Regexes for ticket keys. The whole match is the key. |
| `ignore` | `UTF`, `SHA`, `ISO`, `RFC`, `CVE`, `GPT`, … | Letter prefixes that are never tickets (`UTF-8`, `SHA-256`, `GPT-4`). |
| `url` | `""` | Link template. `{key}` is the whole key, `{num}` its trailing number. If set, tickets become links, and a link to the tracker (with `{key}`) counts as an extra mention. |

Examples:

```toml
# Jira, two projects
[tickets]
patterns = ['\b(?:SHOP|OPS)-\d+\b']
url = "https://yourcompany.atlassian.net/browse/{key}"
```

```toml
# GitHub issues and pull requests: "#123"
[tickets]
patterns = ['(?<![\w/])#\d+\b']
url = "https://github.com/acme/shop/issues/{num}"   # {num}: "#123" ⇒ …/issues/123
```

```toml
# Linear
[tickets]
patterns = ['\bENG-\d+\b']
url = "https://linear.app/acme/issue/{key}"
```

The default pattern is broad. If a workspace mentions other `ABC-123`-shaped things, list its own projects
explicitly. `ignore` applies to every pattern, including your own: a project really called `TLS` needs `ignore = []`.

### `[naming]`

| Key | Default | Meaning |
|---|---|---|
| `tags` | `[]` | Prefixes you put in session names for work without a ticket, such as `/rename OPS disk full`. When the picker hands off a group without a ticket, the new session is named with the most common tag. |

### `[paths]`

All paths are relative to the workspace root unless absolute.

| Key | Default | Meaning |
|---|---|---|
| `docs` | `["docs"]` | Directories whose files are shown as *Doc* links and scanned for backlinks. |
| `groups_file` | `.ticketrail/groups.json` | Manual grouping (moves, merges, renames, pinned key sessions). Commit it to share. |
| `index_file` | `.ticketrail/INDEX.md` | Output of `ticketrail index`. |

### `[backlinks]`

Docs can point back at the session that produced them. The picker shows these as *Cited in* links, which open the
cited entry.

| Key | Default | Meaning |
|---|---|---|
| `pattern` | ``Session:\s*\**\s*`?([0-9a-f]{8})\b`` | Regex whose first group is the first 8 characters of a session id. Empty disables backlinks. |
| `single_entry_dirs` | `["adr"]` | In these directories one file holds one entry, so a backlink points at the file title instead of the nearest heading. |

A changelog entry like this links back to session `aaaa1111…`:

```markdown
## 2026-10-12 — Retry on 502
…
Session: aaaa1111
```

### `[related]`

| Key | Default | Meaning |
|---|---|---|
| `ignore` | scratchpads, plans, `MEMORY.md`, `.ticketrail/` | Regexes for files that never make two sessions related. Typically logs every session appends to, such as `_CHANGELOG\.md$`. |
| `hub_max` | `4` | A file written by more than this many sessions is a hub. It does not relate them. |

### `[labeler]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` turns all LLM calls off. Grouping then uses tickets and manual overrides only. |
| `command` | `"claude"` | The Claude Code CLI binary. It is called in print mode. |
| `model` | `"haiku"` | Model alias passed to `--model`. |
| `context` | `""` | One or two sentences about the workspace. They help the model name topics consistently. |

### `[open]`

| Key | Default | Meaning |
|---|---|---|
| `large_session_mb` | `5` | Opening a transcript larger than this asks whether to fork rather than resume. |
| `editor` | `"code -g {path}:{line}"` | Command for `e` on a file link, as an argument list (no shell). Examples: `"vim +{line} {path}"`, `"subl {path}:{line}"`. |

### `[handoff]`

The `n` key starts a new session that picks up a work item. You choose Claude Code or Codex when both are installed.

| Key | Default | Meaning |
|---|---|---|
| `name` | `"{key} {title}"` | Claude session name. `{key}` is the ticket (or tag), `{title}` is at most 6 words of the work item title. Codex has no name flag. |
| `prompt` | "Continue the work on "{subject}". {history} … summarise …" | First prompt. `{subject}` is the ticket or the title; `{history}` expands to one line per earlier session (date, agent and id, role, status, summary, next step, key session). Omit `{history}` to call your own skill or slash command with exactly one argument, e.g. `"/resume-task {subject}"`. |
| `max_sessions` | `15` | How many of the newest sessions `{history}` lists, 1 to 50. The history is also capped at 24 000 characters. |
| `codex_prompt` | the default prompt with `{history}` | First prompt when you hand off to Codex. Codex cannot run Claude skills or slash commands, so it has its own template. |

### `[github]`

| Key | Default | Meaning |
|---|---|---|
| `pull_requests` | `false` | Read the workspace repository's pull requests with `gh pr list` (cached 5 minutes) and match them to sessions by git branch. A work item whose PRs are all merged or closed, with at least one merged, shows as **done**, with the PR as the reason. Its PRs appear among its links. Needs the GitHub CLI, logged in. |

## A complete example

```toml
# .ticketrail.toml
lang = "en"

[tickets]
patterns = ['\b(?:SHOP|OPS)-\d+\b']
url = "https://yourcompany.atlassian.net/browse/{key}"

[naming]
tags = ["OPS", "DOC", "ENV"]

[paths]
docs = ["docs", "adr"]
groups_file = "docs/session-groups.json"

[related]
ignore = ['/scratchpad/', '/plans/', '_CHANGELOG\.md$', '/MEMORY\.md$']

[labeler]
context = "An online shop: checkout, search, payments, a mobile app and the data pipeline."

[handoff]
prompt = "/resume-task {subject}"
```
