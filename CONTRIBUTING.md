# Contributing

Thanks for taking the time. Bug reports, ideas and pull requests are all welcome.

## Questions and ideas

Open a [discussion](https://github.com/lethequang/ticketrail/discussions). Use issues for bugs and concrete feature
requests.

## Reporting a bug

Use the bug report template. Please include:
- the output of `ticketrail doctor`;
- your OS and Python version;
- what you did, what you expected, and what happened.

Never paste session transcripts or prompts; describe their shape instead (agent, rough size, where they are stored).

## Development

```sh
git clone https://github.com/lethequang/ticketrail && cd ticketrail
uv tool install -e .                                                     # the `ticketrail` command, from your clone
uv run --no-project --with textual python -m unittest discover -s tests  # the test suite
```

- The tests build fake Claude Code and Codex transcripts in a temporary home directory and never read your sessions.
- `tests/test_no_hardcoding.py` fails if a workspace-specific path, user name or ticket key slips into the repo. Use
  neutral examples (`ABC-123`, `/home/me/project`).
- The UI is [Textual](https://textual.textualize.io/). Picker tests run headless with Textual's Pilot (`tests/test_tui.py`).
- `scripts/make_demo.py` rebuilds `docs/images/demo.gif` and the screenshots from synthetic data (macOS).

## Pull requests

- Keep each PR to one purpose, with a test that fails without the change.
- New user-facing strings go through `t()` in `src/ticketrail/i18n.py`, in both `en` and `vi` (a test checks this).
- Anything that sends data off the machine must be opt-in and documented in `docs/privacy.md`.

## Adding an agent

Each agent is a module in `src/ticketrail/sources/` exposing:
- `NAME`;
- `files(cfg)`: the transcripts of the workspace;
- `owns(cfg, path)`;
- `digest(path)`: the summary dict, built with the helpers in `sources/base.py`;
- `hits(path, query, deep)`.

Then register it in `sources/__init__.py`. Use `claude.py` and `codex.py` as references, and add fake transcripts
for it in `tests/helpers.py`.
