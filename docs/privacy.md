# Privacy and safety

## What is read

- **Claude Code transcripts:** `~/.claude/projects/<encoded workspace path>*/*.jsonl`, plus `~/.claude/history.jsonl`
  for sessions already deleted by retention. Only entries whose project is the workspace are read.
- **Codex CLI rollouts** and its thread database: `~/.codex/sessions/**/rollout-*.jsonl` and `~/.codex/state_*.sqlite`.
  Only rollouts started inside the workspace are read. The database is opened read-only.
- **Markdown files** under the configured docs directories, for backlinks.

Transcripts are never modified.

## What is written

| File | Where | Contents |
|---|---|---|
| Digest cache | `~/.cache/ticketrail/<workspace>/digests.json` | Per session: prompts (≤ 200 chars each), titles, tickets, written file paths, last answer (≤ 1500 chars) |
| Labels | `~/.cache/ticketrail/<workspace>/labels.json` | Topic, title, role, status, summary, next step |
| Consent | `~/.cache/ticketrail/<workspace>/consent.json` | Your answer to "label sessions with an LLM?" and when you gave it |
| Labeler log | `~/.cache/ticketrail/<workspace>/labeler.log` | One line per labeled session (id, topic, title) and the CLI's error output |
| Manual groups | `paths.groups_file` (in the workspace) | Group renames, merges, moved session ids, pinned sessions; no transcript content |
| Index | `paths.index_file`, only when you run `index` | Session ids, names, first prompts |

The index contains the first prompt of every session. When it is written to its default place, a
`.ticketrail/.gitignore` keeps it out of commits; review it before sharing it anyway.

## What leaves the machine

Three things can send data:
- the labeler, through `claude`;
- `gh` when `github.pull_requests` is on;
- a hand-off to Codex.

The **labeler** goes through your own `claude` CLI, with whatever account and data policy that CLI uses. For each idle
session it sends:

- the session name;
- its ticket keys;
- the names (not contents) of up to 25 files it wrote;
- up to 10 of your prompts (≤ 200 characters each);
- the last 1500 characters of the final answer;
- the existing topics of this workspace (up to 200 slugs, each with the title the model gave it earlier), so it can
  reuse them;
- `labeler.context`, if you set it.

The call runs from a directory outside the workspace, with tools, MCP servers, slash commands and settings disabled,
and with `--no-session-persistence`. It therefore cannot touch your files, and it does not create new transcripts.

Caches and labels are kept per workspace: topics of one workspace are never sent while labeling another.

Nothing is sent before you agree. On first run the picker shows what would be sent and an estimate (about 6.5 s and
$0.0006 per session, measured), then keeps your answer for the workspace. Without the `claude` CLI the labeler stays
off. You can also decide in config with `labeler.enabled = false` (or `true`), or per run with
`ticketrail pick --no-llm`. Grouping
then relies on tickets and manual overrides.

With `github.pull_requests = true`, `gh pr list` is called in the workspace with your GitHub CLI login, at most once
every 5 minutes. It reads the repository's pull requests; nothing is sent besides that request.

`claude agents --json` runs locally to read which Claude sessions are live. It sends nothing.

## Opening things

- **Resuming or forking a session** runs `claude` or `codex` in the session's own directory. With `y` the command is
  only printed.
- **Handing off** (`n`) starts `claude` or `codex` with a first prompt listing the work item's earlier sessions: dates,
  ids, roles, statuses, summaries, next steps. A hand-off to Codex therefore sends summaries of Claude sessions to your
  Codex provider.
- **Links** to web pages open in your browser.
- **Files** open in the built-in preview (read-only, at most 300 KB) or in your editor (`e`, see `open.editor`).
  Commands run as an argument list, never through a shell.
