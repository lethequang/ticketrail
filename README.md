# ticketrail

[![tests](https://github.com/lethequang/ticketrail/actions/workflows/ci.yml/badge.svg)](https://github.com/lethequang/ticketrail/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ticketrail)](https://pypi.org/project/ticketrail/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**A work tracker for your coding-agent sessions.** `ticketrail` turns the Claude Code and Codex CLI transcripts of a
workspace into **work items**, one per ticket or topic. For each item it tells you whether the work is done, still in
progress, or waiting, and on whom it is waiting. It reopens the right session with one key.

![ticketrail: work items with their status, links, and a hand-off to Codex](https://raw.githubusercontent.com/lethequang/ticketrail/main/docs/images/demo.gif)

## Quickstart

```sh
uv tool install ticketrail      # or: pipx install ticketrail
cd your-repo                    # any directory where you ran Claude Code or Codex
ticketrail
```

Nothing is sent anywhere until you say so: on first run ticketrail asks before using an LLM to label sessions.

## What it does

After a few weeks with coding agents, one ticket is spread over five to ten sessions, often across both agents:
investigation, code, tests, docs, follow-ups. The questions that matter are about the *work*, not the sessions:
"is SHOP-142 done?", "what are we waiting for on the search bug?", "which session has the real fix?".

- **Work items, not session lists.** Sessions from Claude Code and Codex are gathered by ticket key, using:
  - the session name;
  - the title;
  - the git branch;
  - mentions in the prompts.
  Sessions without a ticket can get a topic from a cheap LLM call (opt-in). You can move or merge anything by hand.
- **Status of the work, with a reason.** For example "⏳ waiting on others — QA retest on staging", or "✓ done". "Done"
  can be confirmed by a merged pull request when the repository is on GitHub (opt-in).
- **What each item left behind.** Tickets, PRs, docs, plans, artifacts and changelog entries that cite the session.
  All of them are clickable, and Markdown can be previewed in place.
- **Pick up where it stopped.** Resume or fork any session. Or start a new one, Claude or Codex, whose first prompt
  already summarises every earlier session of the work item.

## How it differs

Many tools list or search agent sessions, and several manage running agents. `ticketrail` sits one level up: the unit
is the piece of work, and the status is the status of that work. The comparison was made in October 2026 and is
summarised from each project's README.

| Tool | What it is | Where ticketrail differs |
|---|---|---|
| cass, memex, claude-history, agent-session-browser | Search and browse sessions of many agents, and resume them | They list sessions; ticketrail groups them into work items with a work status, and shows the docs and tickets around them |
| agent-deck, claude-squad, ccmanager | Run and supervise agents in tmux, with running / waiting-for-input / idle states | That state is about the process; ticketrail's is about the work (done, in progress, waiting on a review…), and it is read from past sessions as well |
| claude-code-karma, claude-tracker | Web dashboards that link Claude Code sessions to Jira / Linear / GitHub tickets | ticketrail also covers Codex, runs in the terminal, and groups ticketless sessions by topic |
| Claude Code agent view (`claude agents`) | Native list of background Claude sessions and their live state | ticketrail uses it when available for the live dot, and adds Codex, tickets and history |

ticketrail does not index or search transcripts as a search engine, and it does not run agents. For those jobs, use the
tools above.

## Install

Requires Python ≥ 3.11 on macOS or Linux. Install it as a tool, so it gets its own environment:
`uv tool install ticketrail` or `pipx install ticketrail`.

Straight from GitHub: `uv tool install git+https://github.com/lethequang/ticketrail`.
From a clone, for development: `uv tool install -e .`

Optionally add a short alias. The package does not install one, because `ss` is a system command on Linux:

```sh
alias ss='ticketrail pick'
```

## Use

Run it from anywhere inside a workspace:

```sh
ticketrail                 # picker (same as `ticketrail pick`)
ticketrail pick ABC-142    # picker, pre-filtered
ticketrail list --days 7
ticketrail find "checkout retry" --brief
ticketrail show aaaa1111   # one session: prompts, files written, artifacts, final answer
ticketrail index           # markdown index by ticket → .ticketrail/INDEX.md
ticketrail doctor          # what it found and where
```

The **workspace** is the nearest parent directory with a `.ticketrail.toml` file. Without one, it is the git
repository root, or else the current directory. Use `--root DIR` to choose it yourself. Sessions started in
subdirectories of the workspace are included.

### Picker keys

| Key | Does |
|---|---|
| `/` | filter (accents optional, several words = AND); `Esc` clears |
| `← →` `Tab` | switch between the work items and sessions panes |
| `Enter` | work item: go to its sessions · session: resume it (sessions over 5 MB ask whether to fork) |
| `f` / `y` | fork the session / print the command and quit |
| `n` | start a new Claude or Codex session that picks up the work item, its history in the first prompt |
| `m` `g` `r` | move a session to another work item / merge work items / rename one |
| `o` | links of the session or work item: preview Markdown in place, open URLs, `e` editor (`open.editor`), `c` copy |
| `p` | pin the key session of the work item (`⚑`) |
| `v` | show every prompt with its time |
| `s` `d` | source (claude + codex / claude / codex) and date range (30 days / 7 days / all) |
| `?` | all keys |

The bottom pane depends on which pane has focus:

- **Work items pane:** a summary of the whole item. It shows the key session, the status of the work, the history
  from oldest to newest, and every ticket, pull request, doc, plan and artifact the item produced.
- **Sessions pane:** the details of one session.

Links are clickable.

[![Work items, their sessions, and the work item summary](https://raw.githubusercontent.com/lethequang/ticketrail/main/docs/images/picker-group.svg)](https://raw.githubusercontent.com/lethequang/ticketrail/main/docs/images/picker-group.svg)

## How sessions become work items

Each session belongs to exactly one work item. The rules are tried in this order:

1. your manual override (`m`, `g`);
2. a ticket key in the name you gave the session (`/rename ABC-142 checkout retry`);
3. a ticket key in the session's title (the name you gave it, else the AI-generated title);
4. the ticket of the git branch (`feature/ABC-142-…`), if the session also mentions it and did not hop between ticket
   branches;
5. the ticket mentioned most often;
6. the ticket of the git branch, for a session that mentions no ticket at all;
7. the LLM topic;
8. "Unsorted".

Files several sessions wrote and secondary tickets only mark sessions as *related*; they never merge work items.

The status of a work item is the LLM's judgement of its newest session ("waiting on others — QA retest"), unless a
hard signal settles it. With `github.pull_requests = true`, the item counts as done when its branches' pull requests are
merged and none is still open. The dot in front of a session shows whether an agent is working (`●`) or waiting for
you (`◐`). For Claude sessions this comes from `claude agents`; for Codex sessions it comes from the transcript's last
change. Details are in [docs/how-grouping-works.md](docs/how-grouping-works.md).

Manual changes are saved in `.ticketrail/groups.json` inside the workspace. Commit it to share the grouping with
your team. (`ticketrail index` writes `.ticketrail/INDEX.md` next to it, with a `.gitignore`: the index quotes
prompts.)

## Configure

Everything workspace-specific is configured; the defaults work with no configuration file at all. Run
`ticketrail config init` to write a starter `.ticketrail.toml`:

```toml
lang = "en"                                   # or "vi"

[tickets]
patterns = ['\bSHOP-\d+\b']                   # default: any Jira-style key
url = "https://jira.example.com/browse/{key}"

[naming]
tags = ["OPS", "DOC"]                         # session-name prefixes for work without a ticket

[labeler]
context = "An online shop: checkout, search, payments."
```

All keys and more examples (Jira, GitHub issues, Linear) are in
[docs/configuration.md](docs/configuration.md). You can also keep the configuration outside the repository, in
`~/.config/ticketrail/config.toml`, under a `[workspace."/path/to/repo"]` table.

## Privacy

Everything runs locally. The tool reads transcripts and never changes them. Caches are kept in
`~/.cache/ticketrail/`.

Data leaves your machine only in these cases, each of which you choose:

- **LLM labels.** On first run the picker asks before labeling anything; the answer is kept per workspace. Set
  `labeler.enabled = true` or `false` to decide in config, or run with `--no-llm` to keep it off for a run.
- **Pull requests.** `gh pr list` runs when you enable `github.pull_requests`.
- **Hand-off to Codex.** Its first prompt carries the summaries of the item's earlier sessions, including Claude
  sessions, to your Codex provider.

For each session, the labeler sends the following through your own `claude` CLI login (Claude Haiku by default):

- the session name;
- its tickets;
- the names of the files it wrote;
- up to 10 prompts;
- the end of the final answer;
- the topic names already used in this workspace;
- `labeler.context`, if you set it.

**Cost and time.** Measured averages are about 6.5 s and $0.0006 per session, run one at a time in the background.
A first pass over 250 sessions takes about half an hour and costs about $0.15. After that, only new or grown sessions
are labeled. The consent dialog shows the estimate for your own queue.

**Codex only?** Labels need the `claude` CLI. Without it, the labeler stays off and never retries. Sessions are still
grouped by ticket key (name, title, branch, mentions) and by your manual changes, and the status column stays empty.

See [docs/privacy.md](docs/privacy.md) for every file read or written.

## Development

```sh
uv run --no-project --with textual python -m unittest discover -s tests
```

The tests build fake Claude Code and Codex transcripts in a temporary home directory. They never read your real
sessions.

## License

MIT
