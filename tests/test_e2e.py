"""End to end: fake Claude Code + Codex transcripts on disk, the real CLI in a subprocess."""
import json
import os
import subprocess
import sys
import unittest

from helpers import SandboxTest

from ticketrail import config, digests

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class TestCli(SandboxTest):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, config.CONFIG_FILE), "w") as f:
            f.write('[tickets]\npatterns = [\'\\bABC-\\d+\\b\']\nurl = "https://tracker.example/browse/{key}"\n'
                    '[labeler]\nenabled = false\n')
        self.c1 = "11111111-aaaa-4000-8000-000000000001"
        self.c2 = "22222222-aaaa-4000-8000-000000000002"
        self.x1 = "33333333-aaaa-7000-8000-000000000003"
        self.sb.claude_session(self.c1, ["Fix the login bug ABC-12", "add a test"], title="ABC-12 login bug",
                               files=[self.root + "/docs/login.md"], answer="Fixed; waiting for review.")
        sub = os.path.join(self.root, "pkg")
        os.makedirs(sub)
        self.sb.claude_session(self.c2, ["Refactor the cache module"], cwd=sub)  # started in a subdirectory
        self.sb.codex_session(self.x1, ["Investigate https://tracker.example/browse/ABC-12 timeout"],
                              patch_files=["src/timeout.py"])
        # transcripts that must never show up: a sibling dir sharing the name prefix, and a Codex run elsewhere
        sibling = os.path.join(os.path.dirname(self.root), os.path.basename(self.root) + "-other")
        os.makedirs(sibling)
        self.sb.claude_session("44444444-aaaa-4000-8000-000000000004", ["unrelated ABC-99"], cwd=sibling)
        self.sb.codex_session("55555555-aaaa-7000-8000-000000000005", ["unrelated ABC-98"], cwd="/somewhere/else")

    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=SRC)
        r = subprocess.run([sys.executable, "-m", "ticketrail", "--root", self.root, *args], capture_output=True, text=True,
                           env=env, cwd=self.root, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_list_keeps_workspace_and_subdirs_only(self):
        out = self.cli("list", "--all")
        for sid in (self.c1, self.c2, self.x1):
            self.assertIn(sid[:8], out)
        self.assertNotIn("44444444", out)  # sibling directory with the same encoded prefix
        self.assertNotIn("55555555", out)  # Codex session of another directory

    def test_find_counts_hits_and_tracker_links(self):
        out = self.cli("find", "ABC-12")
        self.assertIn(self.c1[:8], out)
        self.assertIn(self.x1[:8], out)
        self.assertIn("Resume: codex resume " + self.x1, out)  # the newest of the equally hit sessions

    def test_show_lists_files_and_tickets(self):
        out = self.cli("show", self.x1[:8])
        self.assertIn(os.path.join("src", "timeout.py"), out)
        self.assertIn("ABC-12×2", out)  # bare mention in the URL + the tracker link

    def test_dump_groups_by_ticket(self):
        out = json.loads(self.cli("dump", "--json"))
        self.assertEqual(out["member_of"][self.c1], "ABC-12")
        self.assertEqual(out["member_of"][self.x1], "ABC-12")
        self.assertEqual(out["member_of"][self.c2], "~ungrouped")

    def test_index_writes_inside_the_workspace(self):
        self.cli("index")
        with open(os.path.join(self.root, ".ticketrail", "INDEX.md")) as f:
            text = f.read()
        self.assertIn("## ABC-12", text)
        self.assertIn(self.c2[:8], text)
        with open(os.path.join(self.root, ".ticketrail", ".gitignore")) as f:
            self.assertEqual(f.read().split(), ["INDEX.md"])  # the index quotes prompts: not committed by default

    def test_doctor_and_config(self):
        out = self.cli("doctor")
        self.assertIn("claude transcripts", out)
        self.assertIn(self.root, self.cli("config", "show"))

    def test_lang_flag(self):
        self.assertIn("tên tự đặt", self.cli("--lang", "vi", "list", "--all"))

    def test_cache_lives_outside_the_workspace(self):
        self.cli("list", "--all")
        self.assertTrue(os.path.exists(self.sb.cfg.digest_cache))
        self.assertFalse(self.sb.cfg.in_root(self.sb.cfg.digest_cache))

    def test_digests_in_process(self):
        ids = {d["id"] for d in digests.load_sessions()}
        self.assertEqual(ids, {self.c1, self.c2, self.x1})


if __name__ == "__main__":
    unittest.main()
