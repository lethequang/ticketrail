"""Picker UI, headless (Textual Pilot)."""
import io
import json
import os
import unittest
from unittest import mock

from helpers import NOW, AsyncSandboxTest, Sandbox, TEST_CONFIG, mk

from ticketrail import config, groups as G
from ticketrail.model import Model
from ticketrail.tui import app as P


def fixtures():
    def s(sid, tickets=None, size_kb=100, age=3600, title=None):
        d = mk(sid, tickets, title_custom=title, size_kb=size_kb, mtime=NOW - age, prompts=[{"ts": None, "text": "prompt " + sid}])
        d["title_ai"] = "t " + sid
        d["last_assistant"] = "conclusion " + sid
        return d
    return [
        s("aaaaaaaa-0000-0000-0000-000000000001", {"ABC-1": 2}, age=60),
        s("bbbbbbbb-0000-0000-0000-000000000002", {"ABC-1": 1}, size_kb=9000, age=7200),
        s("cccccccc-0000-0000-0000-000000000003", {"ABC-2": 1}, age=86400),
        s("dddddddd-0000-0000-0000-000000000004", age=600),
        s("eeeeeeee-0000-0000-0000-000000000005", {"ABC-2": 1}, age=90000),
        s("ffffffff-0000-0000-0000-000000000006", {"ABC-2": 1}, age=95000, title="ABC-2 [/bold] name with brackets"),
    ]


def detail_text(app):
    from rich.console import Console
    body = app.query_one("#detail-body")
    con = Console(width=body.size.width, record=True, file=io.StringIO())
    con.print(body.content)
    return con.export_text()


class TestPicker(AsyncSandboxTest):
    def setUp(self):
        super().setUp()
        self.S = fixtures()
        self.model = Model(no_llm=True, load_sessions=lambda: [dict(d) for d in self.S], load_labels=lambda: {}, load_live=lambda: None)

    def app(self, query=""):
        return P.Picker(self.model, query)

    def keys_of(self, app, table):
        return [rk.value for rk in app.query_one("#" + table).rows]

    async def test_panes_follow_each_other(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.keys_of(app, "groups")[0], "ABC-1")  # newest first
            self.assertEqual(self.keys_of(app, "sessions"), [self.S[0]["id"], self.S[1]["id"]])
            await pilot.press("down")  # next group ⇒ sessions change at once, no Enter needed
            await pilot.pause()
            self.assertEqual(app.cur_group, self.keys_of(app, "groups")[1])
            self.assertNotIn(self.S[0]["id"], self.keys_of(app, "sessions"))

    async def test_rescan_keeps_the_cursor_when_group_is_not_first(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press(*["down"] * self.keys_of(app, "groups").index("ABC-2"), "right", "down", "down")
            await pilot.pause()
            chosen = app.cur_session
            self.assertEqual(chosen, self.S[5]["id"])
            for _ in range(3):  # three 5 s rescans
                app.on_loaded(self.model.load())
                await pilot.pause()
            self.assertEqual((app.cur_group, app.cur_session), ("ABC-2", chosen))
            self.assertEqual(app.query_one("#sessions").cursor_row, 2)
            await pilot.press("y")
        self.assertEqual(app.return_value[1], ["claude", "--resume", chosen])  # never the first session of the group

    async def test_late_snapshot_is_dropped(self):
        old, new = self.model.load(), self.model.load()
        self.assertTrue(self.model.apply(new))
        self.assertFalse(self.model.apply(old))
        self.assertEqual(self.model.seq, new.seq)

    async def test_tab_switches_panes_never_enters_the_filter(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            seen = []
            for _ in range(4):
                await pilot.press("tab")
                await pilot.pause()
                seen.append(app.focused.id)
            self.assertEqual(seen, ["sessions", "groups", "sessions", "groups"])

    async def test_first_click_selects_second_click_opens(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#sessions", offset=(2, 2))  # one click from the other pane: no exec
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual(app.focused.id, "sessions")
            await pilot.click("#sessions", offset=(30, 3))  # row 2: only moves the cursor
            await pilot.pause(0.4)
            self.assertTrue(app.is_running)
            self.assertEqual(app.cur_session, self.S[1]["id"])
            await pilot.click("#sessions", offset=(30, 3))  # click the selected row again = Enter (> 5 MB ⇒ asks)
            await pilot.pause()
            self.assertIsInstance(app.screen, P.Confirm)

    async def test_move_back_to_ungrouped(self):
        sid = self.S[3]["id"]
        G.update_overrides(lambda ov: ov["sessions"].update({sid: "ABC-1"}))
        self.model.reload()
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right", *["down"] * self.keys_of(app, "sessions").index(sid), "m")
            await pilot.pause()
            ol = app.screen.query_one(P.OptionList)
            self.assertIn(G.UNGROUPED, [ol.get_option_at_index(i).id for i in range(ol.option_count)])
            await pilot.press(*"unsorted", "enter")
            await pilot.pause()
        self.assertEqual(G.load_overrides()["sessions"], {})

    async def test_brackets_in_names_do_not_crash(self):
        app = self.app("brackets")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            self.assertIsInstance(app.screen, P.AskText)

    async def test_no_horizontal_overflow_in_both_languages(self):
        for lang in ("en", "vi"):
            self.sb.cfg.lang = lang
            for size in ((100, 30), (140, 40)):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    for tid in ("groups", "sessions"):
                        tb = app.query_one("#" + tid)
                        self.assertLessEqual(tb.virtual_size.width, tb.size.width, (lang, size, tid))

    async def test_letters_typed_in_the_filter_never_trigger_actions(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("slash", "a", "b", "c", "-", "2", "q", "f", "m", "y")
            await pilot.pause()
            self.assertIsInstance(app.focused, P.Input)
            self.assertEqual(self.model.query, "abc-2qfmy")
            self.assertTrue(app.is_running)  # q/f/m/y are letters, not quit / fork / move / print
            await pilot.press(*["backspace"] * 4)
            await pilot.pause()
            self.assertEqual(self.keys_of(app, "groups"), ["ABC-2"])
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual((self.model.query, app.focused.id), ("", "groups"))

    async def test_enter_then_y_prints(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.focused.id, "sessions")
            await pilot.press("y")
        self.assertEqual(app.return_value, ("print", ["claude", "--resume", self.S[0]["id"]], self.root))

    async def test_large_session_asks_and_defaults_to_fork(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("right", "down", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, P.Confirm)
            await pilot.press("enter")
        self.assertEqual(app.return_value, ("exec", ["claude", "--fork-session", "--resume", self.S[1]["id"]], self.root))

    async def test_large_session_threshold_is_configurable(self):
        self.sb.cfg.large_kb = 20000
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("right", "down", "enter")
        self.assertEqual(app.return_value[1], ["claude", "--resume", self.S[1]["id"]])

    async def test_f_only_works_in_the_sessions_pane(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("f")
            await pilot.pause()
            self.assertTrue(app.is_running)

    async def test_m_writes_the_full_session_id(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("right", "m")
            await pilot.pause()
            self.assertIsInstance(app.screen, P.PickGroup)
            await pilot.press(*"abc-2", "enter")  # Enter picks the matching group, not '+ New group'
            await pilot.pause()
        self.assertEqual(G.load_overrides()["sessions"], {self.S[0]["id"]: "ABC-2"})

    async def test_broken_override_file_warns_and_is_never_written(self):
        bad = '{"groups": {,}}'
        os.makedirs(os.path.dirname(G.overrides_path()), exist_ok=True)
        with open(G.overrides_path(), "w") as f:
            f.write(bad)
        self.model.reload()
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertIn("⚠", str(app.query_one("#topbar").render()))
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("x", "enter")
            await pilot.pause()
        with open(G.overrides_path()) as f:
            self.assertEqual(f.read(), bad)

    async def test_n_hands_off_with_the_configured_prompt(self):
        self.sb.cfg.handoff_prompt = "/task-resume {subject}"
        app = self.app()
        with mock.patch.object(P.shutil, "which", lambda c: "/bin/" + c if c == "claude" else None):  # only claude installed
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.press("n")
        self.assertEqual(app.return_value[1][-1], "/task-resume ABC-1")

    async def test_n_asks_which_agent_when_both_are_installed(self):
        app = self.app()
        with mock.patch.object(P.shutil, "which", lambda c: "/bin/" + c):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.press("n")
                await pilot.pause()
                self.assertIsInstance(app.screen, P.PickAgent)
                await pilot.press("x")
        cmd = app.return_value[1]
        self.assertEqual(cmd[0], "codex")
        self.assertIn(self.S[0]["id"], cmd[1])  # history of the work item is in the first prompt


class TestLive(AsyncSandboxTest):
    async def test_live_state_from_claude_agents_with_fallback(self):
        S = fixtures()
        live = {S[1]["id"]: "busy", S[2]["id"]: "open"}
        S[3]["tool"] = "codex"  # Codex: mtime only (600 s ago ⇒ not running)
        model = Model(no_llm=True, load_sessions=lambda: [dict(d) for d in S], load_labels=lambda: {}, load_live=lambda: live)
        self.assertEqual(model.live_state(S[0]["id"]), None)  # changed 60 s ago, but Claude said it is not live
        self.assertEqual(model.live_state(S[1]["id"]), "busy")
        self.assertEqual(model.live_state(S[2]["id"]), "open")
        self.assertIsNone(model.live_state(S[3]["id"]))
        unknown = Model(no_llm=True, load_sessions=lambda: [dict(d) for d in S], load_labels=lambda: {}, load_live=lambda: None)
        self.assertEqual(unknown.live_state(S[0]["id"]), "busy")  # no `claude agents` ⇒ mtime heuristic
        app = P.Picker(model, "")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(str(app.query_one("#groups").get_row("ABC-1")[0]), "●")
            self.assertEqual(str(app.query_one("#groups").get_row("ABC-2")[0]), "◐")


class TestPullRequestStatus(AsyncSandboxTest):
    async def test_merged_pr_marks_the_work_item_done(self):
        S = fixtures()
        for d in S[:2]:
            d["branches"] = ["feature/ABC-1"]
        labels = {S[0]["id"]: {"topic": "x", "status": "waiting", "next": "review", "summary": "s", "role": "code", "v": 1}}
        prs = {"heads": {"feature/ABC-1": [{"number": 42, "url": "https://github.com/a/b/pull/42", "state": "MERGED", "mergedAt": None}]},
               "bases": []}
        model = Model(no_llm=True, load_sessions=lambda: [dict(d) for d in S], load_labels=lambda: labels,
                      load_live=lambda: None, load_prs=lambda: prs)
        app = P.Picker(model, "")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(str(app.query_one("#groups").get_row("ABC-1")[2]), "✓")
            txt = detail_text(app)
            self.assertIn("✓ done — PR #42 merged", txt)
            self.assertNotIn("judged by LLM", txt.split("PR #42 merged")[0].splitlines()[-1])
            self.assertIn("#42 merged", txt)  # PR link in the work item's links


class TestDetail(AsyncSandboxTest):
    """Detail pane: group summary, clickable links, preview, key session, all prompts, status."""

    def setUp(self):
        super().setUp()
        self.md = os.path.join(self.root, "notes.md")
        with open(self.md, "w") as f:
            f.write("# Notes\n\npreview content\n")
        a = mk("aaaaaaaa-0000-0000-0000-000000000001", {"ABC-1": 2}, mtime=NOW - 60, files=[self.md, self.root + "/gone/away.md"],
               prompts=[{"ts": "2026-10-01T0%d:00:00Z" % i, "text": "prompt number %d" % i} for i in range(1, 4)])
        b = mk("bbbbbbbb-0000-0000-0000-000000000002", {"ABC-1": 1}, mtime=NOW - 7200, prompts=[{"ts": None, "text": "prompt b"}])
        self.ids = (a["id"], b["id"])
        self.labels = {a["id"]: {"topic": "x", "summary": "summary a", "role": "code", "status": "waiting", "next": "wait QA", "v": 1},
                       b["id"]: {"topic": "x", "summary": "summary b", "role": "docs", "status": "done", "next": "", "v": 1}}
        self.model = Model(no_llm=True, load_sessions=lambda: [dict(a), dict(b)], load_labels=lambda: self.labels, load_live=lambda: None)
        self.opened = []

    def app(self):
        app = P.Picker(self.model, "")
        app.open_url = lambda url, **k: self.opened.append(url)  # never open a real browser
        return app

    async def test_groups_pane_shows_group_summary_sessions_pane_shows_session(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#detail").border_title.startswith("Work item"))
            txt = detail_text(app)
            self.assertIn("History", txt)
            self.assertLess(txt.index("summary b"), txt.rindex("summary a"))  # oldest → newest
            self.assertIn("⏳ waiting on others — wait QA", txt)  # status of the newest session
            await pilot.press("right")
            await pilot.pause()
            self.assertTrue(app.query_one("#detail").border_title.startswith("Session"))

    async def test_clicking_a_link_in_the_detail_opens_it(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("right")
            await pilot.pause()
            lines = detail_text(app).splitlines()
            y = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("Ticket"))
            await pilot.click("#detail-body", offset=(lines[y].index("ABC-1") + 1, y))
            await pilot.pause()
        self.assertEqual(self.opened, ["https://tracker.example/browse/ABC-1"])

    async def test_o_enter_previews_md_and_missing_file_does_not_open(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("right", "o")
            await pilot.pause()
            self.assertIsInstance(app.screen, P.LinkList)
            kinds = [lk.kind for lk in app.screen.items]
            await pilot.press(*["down"] * kinds.index("code"), "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, P.Preview)
            self.assertEqual(app.screen.link.target, self.md)
            self.assertTrue(app.screen.query(P.Markdown))
            await pilot.press("escape", "down", "enter")  # next row: the file that is gone
            await pilot.pause()
            self.assertIsInstance(app.screen, P.LinkList)

    async def test_links_inside_a_preview_never_crash_the_picker(self):
        other = os.path.join(self.root, "other.md")
        with open(other, "w") as f:
            f.write("# Other\n")
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("right")
            await pilot.pause()
            app.open_target(next(lk for lk in self.model.links(self.ids[0]) if lk.target == self.md))
            await pilot.pause()
            md = app.screen.query_one(P.Markdown)
            for href in ("https://example.com/x", "missing.md", "other.md"):
                md.post_message(P.Markdown.LinkClicked(md, href))
                await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual(self.opened, ["https://example.com/x"])
            self.assertEqual(app.screen.link.target, other)

    async def test_group_status_comes_from_the_newest_session(self):
        self.labels.pop(self.ids[0])  # the newest has no label yet
        self.model.reload()
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            txt = detail_text(app)
            self.assertIn("not judged yet", txt)
            self.assertNotIn("✓ done", txt)

    async def test_p_pins_and_unpins_the_key_session(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.model.key_of["ABC-1"], (self.ids[0], False))  # most files left behind
            await pilot.press("right", "down", "p")
            await pilot.pause()
            self.assertEqual(G.load_overrides()["groups"]["ABC-1"]["key_session"], self.ids[1])
            self.assertIn("⚑ key session 📌", detail_text(app))
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(self.model.key_of["ABC-1"], (self.ids[0], False))

    async def test_v_shows_every_prompt(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("right")
            await pilot.pause()
            self.assertNotIn("prompt number 2", detail_text(app))
            await pilot.press("v")
            await pilot.pause()
            txt = detail_text(app)
            self.assertIn("Prompts (3)", txt)
            self.assertIn("prompt number 2", txt)

    async def test_vietnamese_interface(self):
        self.sb.cfg.lang = "vi"
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            txt = detail_text(app)
            self.assertIn("Lịch sử", txt)
            self.assertIn("⏳ chờ người khác — wait QA", txt)
            self.assertIn("Tổng hợp nhóm", app.query_one("#detail").border_title)


if __name__ == "__main__":
    unittest.main()
