# Changelog

## 0.1.1 — 2026-10-16

- LLM labels are opt-in. On first run the picker shows what would be sent, with a time and cost estimate for your
  sessions, and asks once per workspace. `ticketrail label` asks in a terminal. `labeler.enabled` now defaults to
  `"ask"`.
- Fixed: on a machine without the `claude` CLI (Codex-only), the background labeler was started every minute and
  failed. It now stays off, and the top bar says why.
- README: animated demo, a 30-second quickstart, and the comparison moved below them. `scripts/make_demo.py` rebuilds
  the demo from synthetic data.

## 0.1.0 — unreleased

First public version. Sessions are organised into work items (one per ticket or topic) instead of plain session lists.

- CLI: `list`, `find`, `show`, `index`, `dump`, `label`, `config`, `doctor`. Reads Claude Code and Codex CLI transcripts
  of a workspace and its subdirectories.
- Picker (`ticketrail pick`):
  - groups and sessions panes, with a detail pane below;
  - resume or fork the selected session;
  - hand off a group's work to a new session;
  - move, merge and rename groups;
  - pin the key session of a group;
  - clickable links with Markdown preview.
- Grouping by manual override, ticket in the session name or title, git branch, ticket hits, then LLM topic.
- Work status: LLM judgement with a reason ("waiting on others — QA retest"). Merged GitHub pull requests confirm
  "done" (`github.pull_requests`, opt-in).
- Live state of Claude sessions from `claude agents --json`; Codex falls back to transcript activity.
- Hand-off (`n`) to a new Claude or Codex session whose first prompt lists the work item's earlier sessions.
- Optional LLM labels through the `claude` CLI: topic, title, role, status, summary, next step.
- Configuration in `.ticketrail.toml` or `~/.config/ticketrail/config.toml`. It covers ticket patterns and
  links, naming tags, paths, backlinks, the labeler and hand-off.
- Interface in English and Vietnamese.
