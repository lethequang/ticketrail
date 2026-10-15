"""Pure logic: grouping, override file, labeler rules, links/backlinks, configuration, ticket matching, i18n."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import NOW, Sandbox, SandboxTest, mk

from ticketrail import config, digests, groups as G, i18n, labeler as L, links as K
from ticketrail.util import fmt_dt, slugify, write_json


def resolve(sessions, labels=None, ov=None):
    return G.resolve_groups(sessions, labels or {}, ov or {"groups": {}, "sessions": {}})


class TestResolve(SandboxTest):
    def test_every_session_in_exactly_one_group(self):
        ss = [mk("a1111111", {"ABC-1": 2, "ABC-2": 1}), mk("b2222222", {"ABC-2": 3}), mk("c3333333"), mk("d4444444")]
        labels = {"c3333333": {"topic": "gmail-watch", "title": "Gmail watch"}}
        groups, member_of, _, _ = resolve(ss, labels)
        self.assertEqual(set(member_of), {d["id"] for d in ss})
        listed = [s for g in groups.values() for s in g["sessions"]]
        self.assertEqual(sorted(listed), sorted(d["id"] for d in ss))  # never in 0 or 2 groups
        self.assertEqual(member_of["a1111111"], "ABC-1")
        self.assertEqual(member_of["c3333333"], "topic:gmail-watch")
        self.assertEqual(member_of["d4444444"], G.UNGROUPED)

    def test_ticket_in_user_name_beats_most_hits(self):
        ss = [mk("a1111111", {"ABC-1": 9, "ABC-2": 1}, title_custom="ABC-2 e2e local")]
        _, member_of, related, _ = resolve(ss)
        self.assertEqual(member_of["a1111111"], "ABC-2")
        self.assertIn(("ticket", "ABC-1", ""), related["a1111111"])  # a secondary ticket is only related

    def test_override_wins_and_moving_back_removes_it(self):
        ss = [mk("a1111111", {"ABC-1": 2})]
        ov = {"groups": {}, "sessions": {}}
        G.ov_move(ov, "a1111111", "u:gmail", "ABC-1", "Gmail")
        _, member_of, _, base = resolve(ss, ov=ov)
        self.assertEqual(member_of["a1111111"], "u:gmail")
        G.ov_move(ov, "a1111111", "ABC-1", base["a1111111"])
        self.assertEqual(ov["sessions"], {})

    def test_hub_file_is_not_related_and_never_merges_groups(self):
        hub = self.root + "/src/helpers.js"
        rare = self.root + "/docs/X.md"
        ss = [mk("t%d000000" % i, {"ABC-%d" % i: 1}, files=[hub]) for i in range(5)]
        ss.append(mk("r0000000", files=[rare]))
        ss[0]["files_written"].append(rare)
        _, member_of, related, _ = resolve(ss)
        self.assertEqual(len({member_of[d["id"]] for d in ss}), 6)  # shared files never merge groups
        rel0 = [r for r in related.get("t0000000", []) if r[0] == "session"]
        self.assertEqual(rel0, [("session", "r0000000", "X.md")])  # only files few sessions wrote

    def test_ignored_files_are_not_related(self):
        f = self.root + "/docs/ABC-1_CHANGELOG.md"
        _, _, related, _ = resolve([mk("a1111111", {"ABC-1": 1}, files=[f]), mk("b2222222", files=[f])])
        self.assertFalse([r for r in related.get("a1111111", []) if r[0] == "session"])

    def test_topic_attaches_to_ticket_when_dominant(self):
        ss = [mk("a1111111", {"ABC-9": 1}), mk("b2222222", {"ABC-9": 1}), mk("c3333333"), mk("d4444444", {"ABC-5": 1}), mk("e5555555")]
        labels = {"a1111111": {"topic": "billing"}, "b2222222": {"topic": "billing"}, "c3333333": {"topic": "billing"},
                  "d4444444": {"topic": "le"}, "e5555555": {"topic": "le"}}
        _, member_of, _, _ = resolve(ss, labels)
        self.assertEqual(member_of["c3333333"], "ABC-9")
        self.assertEqual(member_of["e5555555"], "topic:le")  # one session with a ticket is not enough

    def test_merge_and_alias_cycle_does_not_hang(self):
        ss = [mk("a1111111", {"ABC-401": 1}), mk("b2222222", {"ABC-402": 1})]
        ov = {"groups": {}, "sessions": {}}
        G.ov_merge(ov, "ABC-401", "ABC-402")
        groups, member_of, _, _ = resolve(ss, ov=ov)
        self.assertEqual(member_of["a1111111"], "ABC-402")
        self.assertEqual(groups["ABC-402"]["tickets"], ["ABC-401", "ABC-402"])
        ov["groups"]["ABC-401"] = {"members": ["ABC-402"]}  # a hand edit creating a cycle
        resolve(ss, ov=ov)

    def test_override_by_full_id_never_drags_a_same_prefix_session(self):
        # Codex ids are UUIDv7: sessions opened < 65 s apart share the first 8 characters
        a, b = "01a0fbc7-1e9c-7000-8000-000000000001", "01a0fbc7-414f-7000-8000-000000000002"
        ov = {"groups": {}, "sessions": {}}
        G.ov_move(ov, a, "u:x", G.UNGROUPED, "X")
        _, member_of, _, _ = resolve([mk(a, tool="codex"), mk(b, tool="codex")], ov=ov)
        self.assertEqual((member_of[a], member_of[b]), ("u:x", G.UNGROUPED))

    def test_override_beats_user_name(self):
        _, member_of, _, _ = resolve([mk("a1111111", title_custom="ABC-2 e2e")], ov={"groups": {}, "sessions": {"a1111111": "u:x"}})
        self.assertEqual(member_of["a1111111"], "u:x")

    def test_ticket_in_title_matches_tokens_not_substrings(self):
        d = mk("a1111111", {"AB-50731": 5, "AB-50": 1})
        d["title_ai"] = "AB-50731 reduce delay"
        _, member_of, _, _ = resolve([d])
        self.assertEqual(member_of["a1111111"], "AB-50731")
        d2 = mk("b2222222", {"ABC-311": 12, "ABC-27": 1})
        d2["title_ai"] = "Review ABC-27"
        _, member_of, _, _ = resolve([d2])
        self.assertEqual(member_of["b2222222"], "ABC-27")  # the ticket named in the title beats hit counts (on purpose)

    def test_git_branch_ticket(self):
        quiet = mk("a1111111")
        quiet["branch"] = "feature/ABC-12-export"
        busy = mk("b2222222", {"ABC-5": 9, "ABC-12": 1})
        busy["branch"] = "feature/ABC-12-export"
        stale = mk("c3333333", {"ABC-7": 3})
        stale["branch"] = "feature/ABC-12-export"  # branch outlived its ticket: the session talks about ABC-7
        head = mk("d4444444")
        head["branch"] = "HEAD"
        _, member_of, _, _ = resolve([quiet, busy, stale, head])
        self.assertEqual(member_of["a1111111"], "ABC-12")  # no mention at all: the branch decides
        self.assertEqual(member_of["b2222222"], "ABC-12")  # mentioned: the branch breaks the tie with hit counts
        self.assertEqual(member_of["c3333333"], "ABC-7")   # never overrides what the session is about
        self.assertEqual(member_of["d4444444"], G.UNGROUPED)
        hop = mk("e5555555")
        hop["branches"] = ["feature/ABC-12-x", "feature/ABC-13-y"]  # went through two ticket branches
        self.assertEqual(resolve([hop])[1]["e5555555"], G.UNGROUPED)

    def test_moved_session_does_not_carry_its_ticket(self):
        groups, _, related, _ = resolve([mk("a1111111", {"ABC-1": 3})], ov={"groups": {}, "sessions": {"a1111111": "u:gmail"}})
        self.assertEqual(groups["u:gmail"]["tickets"], [])
        self.assertIn(("ticket", "ABC-1", ""), related["a1111111"])

    def test_ungrouped_never_merges(self):
        ov = {"groups": {}, "sessions": {}}
        G.ov_merge(ov, G.UNGROUPED, "ABC-1")
        G.ov_merge(ov, "ABC-1", G.UNGROUPED)
        self.assertEqual(ov, {"groups": {}, "sessions": {}})

    def test_ticket_order_is_numeric(self):
        self.assertEqual(sorted(["ABC-10", "ABC-9", "#3"], key=G.ticket_order), ["#3", "ABC-9", "ABC-10"])


class TestLiveParsing(SandboxTest):
    def setUp(self):
        super().setUp()
        from ticketrail import live
        live._failed_at[0] = 0.0

    def run_live(self, rows, code=0):
        from ticketrail import live
        out = mock.Mock(returncode=code, stdout=json.dumps(rows))
        with mock.patch.object(live.shutil, "which", lambda c: "/bin/claude"), mock.patch.object(live.subprocess, "run", lambda *a, **k: out):
            return live.claude_live()

    def test_claude_agents_rows(self):
        r = self.run_live([
            {"sessionId": "a", "cwd": self.root, "pid": 1, "status": "busy"},
            {"sessionId": "b", "cwd": self.root + "/sub", "pid": 2, "status": "idle"},
            {"sessionId": "c", "cwd": self.root, "kind": "background", "state": "done"},  # finished: not live
            {"sessionId": "d", "cwd": "/elsewhere", "pid": 3, "status": "busy"},  # other workspace
            {"sessionId": "b", "cwd": self.root, "kind": "background", "state": "working"},  # same id twice: busy wins
        ])
        self.assertEqual(r, {"a": "busy", "b": "busy"})

    def test_unknown_when_the_command_fails_and_backs_off(self):
        self.assertIsNone(self.run_live([], code=1))
        self.assertIsNone(self.run_live([{"sessionId": "a", "cwd": self.root, "pid": 1, "status": "busy"}]))  # not retried for 60 s
        from ticketrail import live
        with mock.patch.object(live.shutil, "which", lambda c: None):
            self.assertIsNone(live.claude_live())


class TestGithubSignals(SandboxTest):
    def setUp(self):
        super().setUp()
        from ticketrail import signals
        signals._cache.update(root=None, at=0.0, data=signals.EMPTY, refreshing=False)

    def test_merged_verdict(self):
        from ticketrail.signals import merged_verdict
        m = lambda n, s, at=None: {"number": n, "url": "u%d" % n, "state": s, "mergedAt": at}  # noqa: E731
        self.assertIsNone(merged_verdict([]))
        self.assertIsNone(merged_verdict([m(1, "MERGED"), m(2, "OPEN")]))  # something still open
        self.assertIsNone(merged_verdict([m(1, "CLOSED")]))  # closed without merging is not done
        self.assertEqual(merged_verdict([m(1, "MERGED"), m(3, "MERGED"), m(2, "CLOSED")])["number"], 3)
        merged = m(4, "MERGED", "2026-10-01T10:00:00Z")
        at = 1790848800  # 2026-10-01T10:00:00Z
        self.assertIsNotNone(merged_verdict([merged], at + 30))
        self.assertIsNone(merged_verdict([merged], at + 3600))  # work resumed after the merge

    def test_off_by_default_and_never_calls_gh(self):
        from ticketrail import signals
        with mock.patch.object(signals.subprocess, "run", side_effect=AssertionError("network")):
            self.assertEqual(signals.github_prs(), signals.EMPTY)

    def gh(self, rows, code=0):
        from ticketrail import signals
        calls = []

        def run(*a, **k):
            calls.append(a)
            return mock.Mock(returncode=code, stdout=json.dumps(rows))
        return signals, calls, run

    def test_drops_forks_and_release_prs_and_caches(self):
        rows = [{"number": 42, "url": "u42", "state": "MERGED", "headRefName": "feature/ABC-1", "baseRefName": "develop"},
                {"number": 99, "url": "u99", "state": "MERGED", "headRefName": "develop", "baseRefName": "main"},
                {"number": 7, "url": "u7", "state": "OPEN", "headRefName": "patch-1", "baseRefName": "main", "isCrossRepository": True}]
        self.sb.cfg.github_prs = True
        signals, calls, run = self.gh(rows)
        with mock.patch.object(signals.shutil, "which", lambda c: "/bin/gh"), mock.patch.object(signals.subprocess, "run", run):
            data = signals.github_prs(wait=True)
            signals.github_prs(wait=True)
        self.assertEqual(list(data["heads"]), ["feature/ABC-1"])  # develop → main and the fork's PR are gone
        self.assertEqual(data["bases"], ["develop", "main"])
        self.assertEqual(len(calls), 1)  # cached for TTL

    def test_failures_are_cached_and_never_block(self):
        self.sb.cfg.github_prs = True
        signals, calls, run = self.gh([], code=1)
        with mock.patch.object(signals.shutil, "which", lambda c: "/bin/gh"), mock.patch.object(signals.subprocess, "run", run):
            self.assertEqual(signals.github_prs(wait=True), signals.EMPTY)
            signals.github_prs(wait=True)
        self.assertEqual(len(calls), 1)
        signals._cache.update(root=None, at=0.0, data=signals.EMPTY, refreshing=False)
        started = []
        with mock.patch.object(signals.threading, "Thread", lambda **k: mock.Mock(start=lambda: started.append(k))):
            self.assertEqual(signals.github_prs(), signals.EMPTY)  # returns at once, refresh runs in the background
        self.assertEqual(len(started), 1)

    def model(self, sessions, prs, labels=None):
        from ticketrail.model import Model
        return Model(no_llm=True, load_sessions=lambda: [dict(d) for d in sessions], load_labels=lambda: labels or {},
                     load_live=lambda: None, load_prs=lambda: prs)

    def test_work_status_prefers_a_merged_pr(self):
        a, b = mk("a1111111", {"ABC-1": 1}, mtime=NOW - 7200), mk("b2222222", {"ABC-1": 1}, mtime=NOW - 3600)
        a["branches"] = b["branches"] = ["feature/ABC-1"]
        labels = {"b2222222": {"topic": "x", "status": "waiting", "next": "review"}}
        pr = lambda s: {"heads": {"feature/ABC-1": [{"number": 42, "url": "u", "state": s, "mergedAt": None}]}, "bases": []}  # noqa: E731
        for state, expect in (("MERGED", ("done", "PR #42 merged")), ("OPEN", ("waiting", None))):
            m = self.model([a, b], pr(state), labels)
            ws = m.work_status("ABC-1")
            self.assertEqual((ws["status"], ws.get("reason")), expect)
            self.assertIn("pr", [lk.kind for lk in m.group_links("ABC-1")])
            self.assertIn("pr", [lk.kind for lk in m.links("b2222222")])

    def test_old_branch_pr_never_marks_a_new_ticket_done(self):
        # started on an old ticket branch whose PR is merged, then moved to a new branch for ABC-12
        d = mk("a1111111", {"ABC-12": 3})
        d["branches"] = ["feature/ABC-7-old", "feature/ABC-12"]
        prs = {"heads": {"feature/ABC-7-old": [{"number": 7, "url": "u7", "state": "MERGED", "mergedAt": None}]}, "bases": []}
        m = self.model([d], prs)
        self.assertEqual(m.member_of["a1111111"], "ABC-12")
        self.assertEqual(m.group_prs("ABC-12"), [])
        self.assertNotEqual(m.work_status("ABC-12").get("reason"), "PR #7 merged")

    def test_sessions_on_a_base_branch_get_no_pr(self):
        d = mk("a1111111", {"ABC-3": 2})
        d["branches"] = ["develop"]
        prs = {"heads": {"develop": [{"number": 99, "url": "u", "state": "MERGED", "mergedAt": None}]}, "bases": ["develop", "main"]}
        self.assertEqual(self.model([d], prs).group_prs("ABC-3"), [])

    def test_override_write_never_calls_external_commands(self):
        calls = []
        from ticketrail.model import Model
        m = Model(no_llm=True, load_sessions=lambda: [mk("a1111111", {"ABC-1": 1})], load_labels=lambda: {},
                  load_live=lambda: calls.append("live"), load_prs=lambda: calls.append("prs") or {})
        calls.clear()
        m.apply_ov(lambda ov: G.ov_rename(ov, "ABC-1", "x"))
        self.assertEqual(calls, [])  # UI thread: disk only


class TestOverrideFile(SandboxTest):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.root, "groups.json")

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_missing_file_is_empty(self):
        self.assertEqual(G.load_overrides(self.path), {"groups": {}, "sessions": {}})

    def test_broken_file_is_never_overwritten(self):
        bad = '{"groups": {"ABC-1": {"members": ["ABC-2"],}}}'
        self.write(bad)
        with self.assertRaises(G.OverrideError):
            G.update_overrides(lambda ov: G.ov_rename(ov, "ABC-9", "x"), self.path)
        with open(self.path) as f:
            self.assertEqual(f.read(), bad)

    def test_wrong_shape_raises(self):
        for bad in ('{"groups": {"ABC-1": ["ABC-2"]}}', '{"groups": {"ABC-1": {"title": 123}}}', '{"sessions": {"a": 1}}', "[]",
                    '{"groups": {"ABC-1": {"key_session": 1}}}'):
            self.write(bad)
            with self.assertRaises(G.OverrideError, msg=bad):
                G.load_overrides(self.path)

    def test_update_rereads_the_file(self):
        G.update_overrides(lambda ov: G.ov_rename(ov, "ABC-1", "A"), self.path)
        with open(self.path) as f:
            o = json.load(f)
        o["groups"]["ABC-2"] = {"title": "by hand", "members": []}  # another editor while the picker is open
        write_json(self.path, o)
        G.update_overrides(lambda ov: G.ov_rename(ov, "ABC-3", "C"), self.path)
        self.assertEqual(set(G.load_overrides(self.path)["groups"]), {"ABC-1", "ABC-2", "ABC-3"})

    def test_pin_unpin_and_merge_keeps_pin(self):
        ov = {"groups": {}, "sessions": {}}
        G.ov_pin(ov, "ABC-1", "a")
        self.assertEqual(ov["groups"]["ABC-1"]["key_session"], "a")
        G.ov_pin(ov, "ABC-1", "a")
        self.assertNotIn("key_session", ov["groups"]["ABC-1"])
        G.ov_pin(ov, "ABC-1", "a")
        G.ov_merge(ov, "ABC-1", "ABC-2")
        self.assertEqual(ov["groups"]["ABC-2"]["key_session"], "a")

    def test_default_location_is_inside_the_workspace(self):
        self.assertEqual(G.overrides_path(), os.path.join(self.root, ".ticketrail", "groups.json"))


class TestLabeler(SandboxTest):
    def test_running_session_is_not_queued(self):
        self.assertFalse(L.needs_label(mk("a", mtime=NOW - 60), None, NOW))
        self.assertTrue(L.needs_label(mk("a", mtime=NOW - L.IDLE_SEC - 1), None, NOW))

    def test_relabel_after_enough_new_prompts(self):
        d = mk("a", n_prompts=8)
        self.assertFalse(L.needs_label(d, {"topic": "x", "n_prompts": 4, "v": L.LABEL_VER}, NOW))
        self.assertTrue(L.needs_label(d, {"topic": "x", "n_prompts": 3, "v": L.LABEL_VER}, NOW))

    def test_older_label_version_is_queued(self):
        d = mk("a", n_prompts=8)
        self.assertTrue(L.needs_label(d, {"topic": "x", "n_prompts": 8}, NOW))
        self.assertFalse(L.needs_label(d, {"topic": "x", "n_prompts": 8, "v": L.LABEL_VER}, NOW))

    def test_version_upgrade_never_changes_topic_so_groups_stay(self):
        d = mk("a", n_prompts=8)
        old = {"topic": "billing", "title": "Billing", "role": "code", "summary": "s", "n_prompts": 8}
        new = L.apply_result(old, d, {"topic": "other", "title": "Other", "role": "docs", "summary": "s2", "status": "done", "next": ""}, NOW)
        self.assertEqual((new["topic"], new["title"]), ("billing", "Billing"))
        self.assertEqual((new["role"], new["status"], new["v"]), ("docs", "done", L.LABEL_VER))
        self.assertEqual(resolve([d], {"a": old})[1], resolve([d], {"a": new})[1])

    def test_version_upgrade_keeps_the_topic_mark(self):
        old = {"topic": "billing", "title": "Billing", "n_prompts": 3}
        new = L.apply_result(old, mk("a", n_prompts=7), {"topic": "x", "title": "X", "role": "docs", "summary": "s"}, NOW)
        self.assertEqual((new["topic"], new["topic_n"]), ("billing", 3))
        self.assertTrue(L.needs_label(mk("a", n_prompts=3 + L.RELABEL_DELTA), new, NOW))  # 8, not 12

    def test_grown_session_may_change_topic(self):
        old = {"topic": "billing", "title": "Billing", "n_prompts": 8, "v": L.LABEL_VER}
        new = L.apply_result(old, mk("a", n_prompts=8 + L.RELABEL_DELTA), {"topic": "other", "title": "O", "role": "docs", "summary": "s"}, NOW)
        self.assertEqual(new["topic"], "other")

    def test_failure_never_erases_the_old_label(self):
        d = mk("a", n_prompts=8)
        old = {"topic": "billing", "title": "Billing", "role": "code", "summary": "s", "n_prompts": 3}
        lb = L.apply_result(old, d, {"error": "timeout"}, NOW)
        self.assertEqual((lb["topic"], lb["n_prompts"]), ("billing", 3))
        self.assertFalse(L.needs_label(d, lb, NOW))  # retry later, not right away
        self.assertTrue(L.needs_label(d, dict(lb, error_at=NOW - L.RETRY_SEC - 1), NOW))
        self.assertEqual(resolve([d], {"a": lb})[1]["a"], "topic:billing")

    def test_success_after_failure_clears_the_error(self):
        d = mk("a", n_prompts=8)
        lb = L.apply_result({"topic": "billing", "n_prompts": 3}, d, {"error": "timeout"}, NOW)
        ok = L.apply_result(lb, d, {"topic": "billing", "title": "t", "role": "code", "summary": "s"}, NOW)
        self.assertNotIn("error", ok)
        self.assertEqual(ok["n_prompts"], 8)

    def test_run_stops_after_max_consecutive_failures(self):
        self.sb.cfg.labeler_setting = True
        self.enterContext(mock.patch.object(L.shutil, "which", lambda c: "/bin/" + c))
        for i in range(5):
            p = self.sb.claude_session("%d0000000-0000-4000-8000-00000000000%d" % (i, i), ["prompt %d" % i])
            os.utime(p, (NOW - 7200, NOW - 7200))
        calls = []

        def boom(prompt):
            calls.append(prompt)
            raise RuntimeError("quota")
        with mock.patch.object(L, "call_llm", boom), mock.patch("sys.stdout"):
            L.run()
        labels = L.load_labels()
        self.assertEqual(len(calls), L.MAX_FAILS)  # the rest of the queue is not marked as failed
        self.assertEqual(sum(1 for lb in labels.values() if lb.get("error")), L.MAX_FAILS)

    def test_disabled_labeler_never_calls_the_llm(self):
        self.sb.cfg.labeler_setting = False
        with mock.patch.object(L, "call_llm", side_effect=AssertionError("called")), mock.patch("sys.stderr"):
            L.run()

    def which(self, present):
        return mock.patch.object(L.shutil, "which", (lambda c: "/bin/" + c) if present else (lambda c: None))

    def test_state_order_explicit_then_answer_then_ask(self):
        with self.which(True):
            self.assertEqual(L.state(), L.ASK)  # default: nothing decided ⇒ nothing is sent
            L.set_consent(True)
            self.assertEqual(L.state(), L.ON)
            L.set_consent(False)
            self.assertEqual(L.state(), L.OFF)
            self.sb.cfg.labeler_setting = True  # explicit config wins over the stored answer
            self.assertEqual(L.state(), L.ON)
        self.sb.cfg.labeler_setting = False
        with self.which(False):
            self.assertEqual(L.state(), L.OFF)

    def test_codex_only_machine_never_spawns_the_labeler(self):
        from ticketrail.model import Model
        self.sb.cfg.labeler_setting = True
        with self.which(False):
            self.assertEqual(L.state(), L.MISSING)
            m = Model(load_sessions=lambda: [mk("a", mtime=NOW - 7200)], load_labels=lambda: {}, load_live=lambda: None)
            with mock.patch.object(L, "spawn_background", side_effect=AssertionError("spawned")):
                m.maybe_spawn()
            self.assertFalse(m.should_ask_llm())

    def test_nothing_is_spawned_before_consent(self):
        from ticketrail.model import Model
        with self.which(True):
            m = Model(load_sessions=lambda: [mk("a", mtime=NOW - 7200)], load_labels=lambda: {}, load_live=lambda: None)
            self.assertTrue(m.should_ask_llm())
            with mock.patch.object(L, "spawn_background", side_effect=AssertionError("spawned")):
                m.maybe_spawn()
            spawned = []
            with mock.patch.object(L, "spawn_background", lambda: spawned.append(1)):
                m.answer_llm(True)
                m.maybe_spawn()
            self.assertEqual(spawned, [1])
            self.assertEqual(json.load(open(self.sb.cfg.consent_file))["labeler"], True)
            no = Model(no_llm=True, load_sessions=lambda: [mk("a", mtime=NOW - 7200)], load_labels=lambda: {}, load_live=lambda: None)
            self.assertFalse(no.should_ask_llm())  # --no-llm never asks

    def test_label_command_without_a_terminal_refuses(self):
        with self.which(True), mock.patch.object(L.sys.stdin, "isatty", lambda: False), \
                mock.patch.object(L, "call_llm", side_effect=AssertionError("called")), mock.patch("sys.stderr") as err:
            L.run()
        self.assertFalse(os.path.exists(self.sb.cfg.consent_file))

    def test_estimate(self):
        self.assertEqual(L.estimate(250), (28, 250 * L.USD_PER_SESSION))
        self.assertEqual(L.estimate(1)[0], 1)

    def test_background_labeler_keeps_root_and_language(self):
        self.sb.cfg.lang = "vi"
        argv = L.spawn_argv(self.sb.cfg)
        self.assertEqual(argv[argv.index("--root") + 1], self.root)
        self.assertEqual(argv[argv.index("--lang") + 1], "vi")

    def test_empty_and_labeler_sessions_never_show(self):
        self.sb.claude_session("a0000000-0000-4000-8000-000000000001", ["real work"])
        self.sb.claude_session("b0000000-0000-4000-8000-000000000002", [])  # no typed prompt
        self.assertEqual([d["id"][:8] for d in digests.load_sessions()], ["a0000000"])
        self.assertTrue(digests.is_labeler({"cwd": self.sb.cfg.labeler_dir}))

    def test_unknown_status_and_role_are_not_guessed(self):
        res = L.parse_answer('x {"topic": "Gmail Watch!", "title": "t", "role": "boss", "summary": "s", "status": "almost", "next": "QA"}')
        self.assertEqual((res["topic"], res["role"], res["status"], res["next"]), ("gmail-watch", "qa", "", "QA"))
        with self.assertRaises(ValueError):
            L.parse_answer('{"topic": ""}')

    def test_prompt_uses_configured_language_and_context(self):
        sb = Sandbox(dict(self.CONFIG, lang="vi", labeler={"context": "An online shop."}))
        try:
            p = L.build_prompt(mk("a"), [("billing", "Billing")])
            self.assertIn("in Vietnamese", p)
            self.assertIn("About the workspace: An online shop.", p)
            self.assertIn("- billing — Billing", p)
        finally:
            sb.close()
            config.use(self.sb.cfg)

    def test_labeler_cli_flags_never_persist_a_session(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, cwd=kw.get("cwd"))
            return mock.Mock(stdout=json.dumps({"result": '{"topic": "a-b", "title": "t", "role": "code", "summary": "s"}'}))
        with mock.patch.object(subprocess, "run", fake_run):
            L.call_llm("prompt")
        self.assertIn("--no-session-persistence", seen["cmd"])
        self.assertFalse(self.sb.cfg.in_root(seen["cwd"]))  # runs outside the workspace


class TestLinks(SandboxTest):
    def test_open_commands(self):
        self.assertEqual(K.resume_cmd(mk("x", tool="codex")), ["codex", "resume", "x"])
        self.assertEqual(K.resume_cmd(mk("x", tool="codex"), fork=True), ["codex", "fork", "x"])
        self.assertEqual(K.resume_cmd(mk("x"), fork=True), ["claude", "--fork-session", "--resume", "x"])

    def test_claude_cwd_is_the_dir_matching_the_transcript_project(self):
        sub = os.path.join(self.root, "docs", "sub")
        os.makedirs(sub)
        d = mk("x", cwd=sub)
        self.assertEqual(K.session_cwd(d), self.root)  # never resume from a subdirectory
        d["path"] = os.path.join(self.sb.cfg.claude_projects, config.encode_path(sub), "x.jsonl")
        self.assertEqual(K.session_cwd(d), sub)

    def test_handoff_name_and_prompt_with_history(self):
        g = {"key": "ABC-402", "title": "Export to CSV", "tickets": ["ABC-402"]}
        a, b = mk("a1111111", mtime=NOW - 7200), mk("b2222222", mtime=NOW - 60)
        labels = {"a1111111": {"role": "research", "status": "done", "summary": "Found the slow query."},
                  "b2222222": {"role": "code", "status": "waiting", "summary": "Streaming export.", "next": "review PR"}}
        cmd = K.handoff_cmd(g, [b, a], labels, key_sid="b2222222")
        self.assertEqual(cmd[:3], ["claude", "-n", "ABC-402 Export to CSV"])
        p = cmd[3]
        self.assertIn('Continue the work on "ABC-402"', p)
        self.assertLess(p.index("Found the slow query."), p.index("Streaming export."))  # oldest first
        self.assertIn("claude b2222222 · code · waiting — Streaming export. Next: review PR [key session]", p)
        self.assertNotIn("{", p)

    def test_handoff_to_codex_and_history_limit(self):
        self.sb.cfg.handoff_max = 2
        ms = [mk("s%07d" % i, mtime=NOW - 1000 + i) for i in range(5)]
        cmd = K.handoff_cmd({"key": "ABC-1", "title": "T", "tickets": ["ABC-1"]}, ms, {}, agent="codex")
        self.assertEqual(cmd[0], "codex")
        self.assertEqual(len(cmd), 2)
        self.assertIn("s0000004", cmd[1])
        self.assertNotIn("s0000001", cmd[1])
        self.assertIn("(3 older sessions not listed)", cmd[1])
        g = {"key": "topic:gmail-watch", "title": "Gmail watch expired", "tickets": []}
        self.assertEqual(K.handoff_cmd(g, [mk("a", title_custom="OPS gmail watch"), mk("b")])[2], "OPS Gmail watch expired")

    def test_handoff_prompt_is_configurable(self):
        self.sb.cfg.handoff_prompt = "/task-resume {subject}"  # a slash command must get exactly its argument, no history
        g = {"key": "ABC-1", "title": "T", "tickets": ["ABC-1"]}
        self.assertEqual(K.handoff_cmd(g, [mk("a")], {})[3], "/task-resume ABC-1")
        codex = K.handoff_cmd(g, [mk("a")], {}, agent="codex")[1]  # Codex cannot run the Claude skill
        self.assertIn('Continue the work on "ABC-1"', codex)
        self.assertIn("Earlier sessions", codex)

    def test_key_session_pin_wins_but_a_stale_pin_is_ignored(self):
        by_id = {"a": mk("a", files=[self.root + "/docs/x.md", self.root + "/b.py"], n_prompts=2), "b": mk("b", n_prompts=20),
                 "c": mk("c", files=["/private/tmp/x/scratchpad/%d" % i for i in range(5)] + [self.root + "/plans/p.md"], n_prompts=1)}
        g = {"key": "ABC-1", "sessions": ["a", "b", "c"]}
        self.assertEqual(K.key_session(g, by_id, {}), ("a", False))  # most real files; scratch and plans don't count
        self.assertEqual(K.key_session(g, by_id, {"groups": {"ABC-1": {"key_session": "b"}}}), ("b", True))
        self.assertEqual(K.key_session(g, by_id, {"groups": {"ABC-1": {"key_session": "zzz"}}}), ("a", False))

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_backlinks_point_at_entry_heading_and_adr_title(self):
        cl = self.write("docs/X_CHANGELOG.md", "# X\n\n## 14/10 — new\ntext\nSession: aaaaaaaa\n\n## 13/10 — old\n```\n# not a heading\n```\n"
                                               "Session: bbbbbbbb\nSession: <id8>\n")
        adr = self.write("docs/adr/X-y.md", "# X-y decision\n\n## Consequences\n...\nSession: aaaaaaaa\n")
        bl = K.backlinks()
        self.assertEqual(set(bl), {"aaaaaaaa", "bbbbbbbb"})
        self.assertIn((cl, 3, "14/10 — new"), bl["aaaaaaaa"])
        self.assertIn((adr, 1, "X-y decision"), bl["aaaaaaaa"])
        self.assertEqual(bl["bbbbbbbb"][0][1:], (7, "13/10 — old"))

    def test_backlink_inside_code_block_does_not_count(self):
        self.write("docs/a.md", "# A\n~~~\nSession: cccccccc\n~~~\n#123 not a heading\nSession: dddddddd\n")
        bl = K.backlinks()
        self.assertEqual(set(bl), {"dddddddd"})
        self.assertEqual(bl["dddddddd"][0][1:], (1, "A"))

    def test_backlinks_can_be_disabled(self):
        self.write("docs/a.md", "Session: dddddddd\n")
        self.sb.cfg.backlink_re = None
        self.assertEqual(K.backlinks(), {})

    def test_md_section(self):
        self.assertEqual(K.md_section("# T\n## A\na\n### A1\nx\n## B\nb", 2), "## A\na\n### A1\nx")
        text = "# T\n## C inserted\nc\n## A\na\n## B\nb"  # "## A" was on line 2 when scanned
        self.assertEqual(K.md_section(text, 2, "A"), "## A\na")
        self.assertEqual(K.md_section(text, 2, "gone"), text)  # never guess another entry

    def test_links_skip_scratch_and_mark_missing_files(self):
        with tempfile.NamedTemporaryFile(suffix=".md", dir=self.root) as f:
            d = mk("abcdefgh-1", tickets={"ABC-1": 1}, files=[self.root + "/docs/missing.md", f.name, "/private/tmp/x/scratchpad/n.py"])
            d["artifacts"] = [{"title": "QA", "url": "https://example.com/a"}]
            ls = K.links(d, {"abcdefgh": [(f.name, 3, "entry")]})
        self.assertEqual([lk.kind for lk in ls], ["ticket", "artifact", "doc", "backlink", "code"])
        self.assertEqual(ls[0].target, "https://tracker.example/browse/ABC-1")
        self.assertFalse(ls[2].exists)
        self.assertTrue(ls[3].exists and ls[4].exists)
        self.assertFalse(any("scratchpad" in lk.target for lk in ls))

    def test_workspace_under_tmp_keeps_its_files(self):
        sb = Sandbox(self.CONFIG, tmp_parent="/tmp")  # Linux runners keep temp dirs here
        try:
            f = sb.root + "/docs/x.md"
            d = mk("a", files=[f, sb.root + "/src/y.py", "/tmp/elsewhere/z.py"])
            self.assertEqual([lk.kind for lk in K.links(d)], ["doc", "code"])
            self.assertEqual(K.meaningful_files(d), [f, sb.root + "/src/y.py"])
        finally:
            sb.close()
            config.use(self.sb.cfg)

    def test_no_ticket_url_means_no_ticket_link(self):
        self.sb.cfg.tickets.url_template = ""
        self.assertEqual([lk.kind for lk in K.links(mk("a", tickets={"ABC-1": 1}))], [])


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.close()

    def test_default_ticket_pattern_skips_standards_and_models(self):
        tk = config.Tickets(config.DEFAULTS["tickets"]["patterns"], config.DEFAULTS["tickets"]["ignore"])
        self.assertEqual(tk.find("fix PROJ-12 and UTF-8, SHA-256, ISO-8601, GPT-4, then OPS-3"), ["PROJ-12", "OPS-3"])
        self.assertFalse(tk.is_ticket("UTF-8"))
        self.assertTrue(tk.is_ticket("PROJ-12"))

    def test_tracker_links_count_and_parse(self):
        tk = config.Tickets([r"\bABC-\d+\b"], [], "https://tracker.example/browse/{key}")
        self.assertEqual(tk.find("see https://tracker.example/browse/ABC-7"), ["ABC-7", "ABC-7"])
        self.assertEqual(tk.url("ABC-7"), "https://tracker.example/browse/ABC-7")
        self.assertIsNone(config.Tickets([r"#\d+"]).url("#1"))

    def test_github_style_issue_numbers(self):
        tk = config.Tickets([r"(?<![\w/])#\d+\b"], [], "https://github.com/acme/shop/issues/{num}")
        self.assertEqual(tk.find("closes #12 and #7, not a/b#3"), ["#12", "#7"])
        self.assertTrue(tk.is_ticket("#12"))
        self.assertEqual(tk.url("#12"), "https://github.com/acme/shop/issues/12")

    def test_bad_regex_is_a_config_error(self):
        with self.assertRaises(config.ConfigError):
            config.Tickets(["(unclosed"])

    def test_root_discovery_order(self):
        ws = os.path.join(self.sb.base, "ws")
        sub = os.path.join(ws, "a", "b")
        os.makedirs(sub)
        self.assertEqual(config.find_root(sub), os.path.realpath(sub))  # no git, no config ⇒ cwd
        subprocess.run(["git", "init", "-q", ws], check=True)
        self.assertEqual(config.find_root(sub), os.path.realpath(ws))  # git toplevel
        with open(os.path.join(ws, "a", config.CONFIG_FILE), "w") as f:
            f.write("")
        self.assertEqual(config.find_root(sub), os.path.realpath(os.path.join(ws, "a")))  # nearest config wins
        self.assertEqual(config.load(root=sub).root, os.path.realpath(sub))  # --root wins over everything

    def test_three_layers_merge(self):
        ws = os.path.join(self.sb.base, "ws")
        os.makedirs(ws)
        up = config.user_config_path()
        os.makedirs(os.path.dirname(up))
        with open(up, "w") as f:
            f.write('[defaults]\nlang = "vi"\n[defaults.tickets]\nurl = "https://u/{key}"\n'
                    '[workspace."%s".naming]\ntags = ["OPS"]\n[workspace."/elsewhere"]\nlang = "en"\n' % ws)
        with open(os.path.join(ws, config.CONFIG_FILE), "w") as f:
            f.write('[tickets]\npatterns = ["\\\\bZZ-\\\\d+\\\\b"]\n')
        cfg = config.load(root=ws)
        self.assertEqual(cfg.lang, "vi")
        self.assertEqual(cfg.tickets.url("ZZ-1"), "https://u/ZZ-1")
        self.assertTrue(cfg.tickets.is_ticket("ZZ-1"))
        self.assertTrue(cfg.tag_re.match("OPS x"))
        self.assertEqual(config.load(root=ws, overrides={"lang": "en"}).lang, "en")  # command line wins

    def test_broken_toml_is_reported(self):
        ws = os.path.join(self.sb.base, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, config.CONFIG_FILE), "w") as f:
            f.write("lang = \n")
        with self.assertRaises(config.ConfigError):
            config.load(root=ws)

    def test_capture_groups_in_patterns_and_tracker_links(self):
        tk = config.Tickets([r"\b(SHOP|OPS)-\d+\b"], [], "https://t.example/browse/{key}")
        self.assertEqual(tk.find("SHOP-1 and https://t.example/browse/OPS-2"), ["SHOP-1", "OPS-2", "OPS-2"])

    def test_typos_and_wrong_types_are_errors(self):
        for bad in ({"ticket": {}}, {"tickets": {"patterns": "SHOP"}}, {"open": {"large_session_mb": "big"}},
                    {"labeler": {"enabled": "yes"}}, {"labeler": {"enabled": 1}}, {"lang": "fr"}, {"handoff": {"prompt": "go {where}"}}, {"handoff": {"max_sessions": 0}},
                    {"open": {"editor": "vim +{lin} {path}"}}):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.Config(root=self.sb.base, data=config.deep_merge(config.DEFAULTS, bad))

    def test_user_config_settings_must_be_in_a_table(self):
        up = config.user_config_path()
        os.makedirs(os.path.dirname(up))
        with open(up, "w") as f:
            f.write('lang = "vi"\n')
        with self.assertRaises(config.ConfigError):
            config.load(root=self.sb.base)

    def test_workspace_key_with_tilde(self):
        ws = os.path.join(self.sb.base, "home", "code", "shop")
        os.makedirs(ws)
        up = config.user_config_path()
        os.makedirs(os.path.dirname(up))
        with open(up, "w") as f:
            f.write('[workspace."~/code/shop"]\nlang = "vi"\n')
        with mock.patch.dict(os.environ, {"HOME": os.path.join(self.sb.base, "home")}):
            self.assertEqual(config.load(root=ws).lang, "vi")

    def test_timezone(self):
        for tz, expect in (("UTC", "Oct 01 23:30"), ("Asia/Tokyo", "Oct 02 08:30")):
            config.use(config.Config(root=self.sb.base, data=config.deep_merge(config.DEFAULTS, {"timezone": tz})))
            self.assertEqual(fmt_dt("2026-10-01T23:30:00Z"), expect)
        config.use(self.sb.cfg)

    def test_encode_path_matches_claude_code(self):
        self.assertEqual(config.encode_path("/home/me/My Proj.v2/x_y"), "-home-me-My-Proj-v2-x-y")

    def test_cache_is_per_workspace_even_when_names_encode_alike(self):
        roots = [os.path.join(self.sb.base, x) for x in ("my-app", "my.app", "my_app", os.path.join("my", "app"))]
        dirs = {config.Config(root=r).cache_dir for r in roots}
        self.assertEqual(len(dirs), len(roots))
        self.assertTrue(all(d.startswith(self.sb.env["XDG_CACHE_HOME"]) for d in dirs))


class TestExamples(unittest.TestCase):
    def test_every_example_config_loads(self):
        import tomllib
        ex = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
        sb = Sandbox()
        try:
            for name in sorted(os.listdir(ex)):
                with open(os.path.join(ex, name), "rb") as f:
                    data = tomllib.load(f)
                tables = [data] if "defaults" not in data else [data["defaults"]] + list(data.get("workspace", {}).values())
                for tbl in tables:
                    config.Config(root=sb.root, data=config.deep_merge(config.DEFAULTS, tbl))  # unknown key / bad regex raises
        finally:
            sb.close()


class TestI18n(unittest.TestCase):
    def test_every_key_exists_in_every_language(self):
        self.assertEqual(set(i18n.EN), set(i18n.VI))

    def test_placeholders_match(self):
        import string
        fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}  # noqa: E731
        for k in i18n.EN:
            self.assertEqual(fields(i18n.EN[k]), fields(i18n.VI[k]), k)

    def test_slug_strips_accents(self):
        self.assertEqual(slugify("Đơn hàng bị trùng!"), "don-hang-bi-trung")


if __name__ == "__main__":
    unittest.main()
