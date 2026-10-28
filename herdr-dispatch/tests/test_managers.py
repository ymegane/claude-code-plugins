import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from hdlib import config, herdr  # noqa: E402
from hdlib import launch as hlaunch  # noqa: E402
import rm  # noqa: E402
import sm  # noqa: E402
from sources import gh_review_requests as src  # noqa: E402


def make_cfg(over=None):
    return config.Config(config.deep_merge(config.BUILTIN, over or {}), "/nonexistent/config.json", "/repo", "o/r", True)


class FeedbackTest(unittest.TestCase):
    def pr(self, reviews=(), comments=()):
        return {"reviews": {"nodes": list(reviews)}, "comments": {"nodes": list(comments)}}

    def review(self, who, state, body="", ncom=0, at="2026-01-01T00:00:00Z"):
        return {"id": f"{who}-{state}", "author": {"login": who}, "state": state, "submittedAt": at,
                "body": body, "comments": {"totalCount": ncom}}

    def test_kinds(self):
        pr = self.pr([
            self.review("alice", "APPROVED"),
            self.review("bob", "COMMENTED", ncom=2),
            self.review("bob", "COMMENTED"),
            self.review("me", "CHANGES_REQUESTED"),
            self.review("coderabbitai", "COMMENTED", body="**Actionable comments posted: 3**"),
            self.review("coderabbitai", "COMMENTED", body="スレッド内の返答"),
            self.review("github-actions", "COMMENTED", body="bot"),
        ], [{"id": "c1", "author": {"login": "carol"}, "createdAt": "2026-01-02T00:00:00Z", "body": "x"}])
        ev = sm.feedback_events(pr, "me", "coderabbitai", {"github-actions"})
        self.assertEqual(sorted(e["kind"] for e in ev), ["approved", "coderabbit", "comment", "review_comment"])

    def test_trust(self):
        fb = config.BUILTIN["session_manager"]["feedback"]
        self.assertTrue(sm.is_trusted({"kind": "comment", "by": "alice", "assoc": "MEMBER"}, fb))
        self.assertTrue(sm.is_trusted({"kind": "coderabbit", "by": "coderabbitai", "assoc": "NONE"}, fb))
        self.assertFalse(sm.is_trusted({"kind": "comment", "by": "stranger", "assoc": "NONE"}, fb))
        self.assertTrue(sm.is_trusted({"kind": "comment", "by": "stranger", "assoc": "NONE"},
                                      {**fb, "trusted_authors": ["stranger"]}))
        self.assertTrue(sm.is_trusted({"kind": "comment", "by": "github-actions", "assoc": "NONE", "bot": True}, fb))

    def test_count_untrusted_counts_all_nodes(self):
        """対応を要するイベントに絞らず数える。DISMISSED や本文の無い review、ignored_authors の人も読まれうる。"""
        fb = config.BUILTIN["session_manager"]["feedback"]

        def node(login, assoc, typename="User", **kw):
            return {"author": {"login": login, "__typename": typename}, "authorAssociation": assoc, **kw}
        pr = {"reviews": {"totalCount": 3, "nodes": [
                  node("stranger", "NONE", state="DISMISSED", body=""),
                  node("alice", "MEMBER", state="APPROVED"),
                  node("me", "OWNER", state="COMMENTED")]},
              "comments": {"totalCount": 3, "nodes": [
                  node("github-actions", "NONE", "Bot"),
                  node("vercel", "NONE"),
                  {"author": None, "authorAssociation": "NONE"}]}}
        # stranger・ignored_authors に名のある人（vercel を名乗る User）・削除されたアカウント
        self.assertEqual(sm.count_untrusted(pr, "me", fb), 3)
        self.assertEqual(sm.count_untrusted({**pr, "comments": {**pr["comments"], "totalCount": 99}}, "me", fb), "unknown")

    def test_poll_separates_untrusted(self):
        sm.CFG = make_cfg()
        pr = {"number": 7, "headRefName": "feat/x", "title": "t", "reviews": {"nodes": []}, "comments": {"nodes": [
            {"id": "c1", "author": {"login": "alice"}, "authorAssociation": "MEMBER", "createdAt": "2026-01-01T00:00:00Z"},
            {"id": "c2", "author": {"login": "stranger"}, "authorAssociation": "NONE", "createdAt": "2026-01-02T00:00:00Z"},
        ]}}
        def poll(pr):
            gh = types.SimpleNamespace(returncode=0, stdout=sm.json.dumps({"data": {"search": {"nodes": [pr]}}}), stderr="")
            with mock.patch.dict(os.environ, {"HERDR_ENV": "0"}), \
                    mock.patch.object(sm.subprocess, "run", lambda *a, **k: gh), mock.patch.object(sm, "load_index", lambda *a: {}):
                return dict(sm.poll(types.SimpleNamespace(state_dir="/nonexistent"), "me"))
        items = poll(pr)
        self.assertEqual(sorted(items), ["7@c1", "untrusted:7@c2"])
        self.assertIn("by=alice ", items["7@c1"])
        self.assertIn(" untrusted=1 ", items["7@c1"])
        self.assertTrue(items["untrusted:7@c2"].startswith("UNTRUSTED_FEEDBACK #7 events=1 by=stranger"))
        # 取得の上限を超えて古い書き込みが残っていれば数えきれない。restore は外部の書き込みがあるものとして扱う
        items = poll({**pr, "comments": {**pr["comments"], "totalCount": 80}})
        self.assertIn(" untrusted=unknown ", items["7@c1"])
        self.assertEqual(sm.restore_mode("auto", "review", "auto", items["7@c1"]), "default")

    def test_restore_mode(self):
        clean, dirty = "FEEDBACK #7 … untrusted=0 title=\"t\"", "FEEDBACK #7 … untrusted=2 title=\"t\""
        self.assertEqual(sm.restore_mode("bypassPermissions", "review", "auto", clean), "auto")
        self.assertEqual(sm.restore_mode("bypassPermissions", "review", "bypassPermissions", clean), "default")
        self.assertEqual(sm.restore_mode("auto", "review", "auto", clean), "auto")
        self.assertEqual(sm.restore_mode("auto", "review", "auto", dirty), "default")
        self.assertEqual(sm.restore_mode("plan", "review", "auto", dirty), "plan")
        # untrusted の無い行（手で書いた行など）は外部の書き込みがあるものとして扱う
        self.assertEqual(sm.restore_mode("auto", "review", "auto", "FEEDBACK #7"), "default")
        self.assertEqual(sm.restore_mode("bypassPermissions", "resume", "auto"), "bypassPermissions")


class TranscriptTest(unittest.TestCase):
    def test_fold_and_interruption(self):
        st = sm.empty_fold_state()
        lines = [
            {"type": "user", "entrypoint": "cli", "cwd": "/w", "gitBranch": "feat/x", "timestamp": "t1",
             "message": {"content": "やって"}},
            {"type": "pr-link", "prRepository": "o/r", "prNumber": 5},
            {"type": "pr-link", "prRepository": "other/r", "prNumber": 6},
            {"type": "assistant", "timestamp": "t2",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        ]
        for d in lines:
            sm.fold_line(st, d, "o/r")
        self.assertEqual((st["entrypoint"], st["branch"], st["prs"], st["messages"]), ("cli", "feat/x", [5], 2))
        self.assertEqual(sm.interruption(st), "mid_tool")

    def test_rank_prefers_branch_then_dedicated(self):
        rows = [
            {"session_id": "plan", "prs": [1, 2, 3, 4], "branch": "main", "last_activity": "3"},
            {"session_id": "dedicated", "prs": [4], "branch": "feat/a", "last_activity": "1"},
            {"session_id": "branch", "prs": [], "branch": "feat/b", "last_activity": "2"},
        ]
        self.assertEqual([r["session_id"] for r in sm.rank_for_pr(rows, 4, "feat/b")], ["branch", "dedicated", "plan"])


class SourceDecideTest(unittest.TestCase):
    def test_decide(self):
        d = {"isDraft": False, "author": {"login": "a"}, "headRefOid": "h2"}
        self.assertEqual(src.decide(d, [], "me")[0], "review")
        self.assertEqual(src.decide(d, [{"id": 1, "state": "PENDING", "commit_id": "h2"}], "me")[0], "skip")
        self.assertEqual(src.decide(d, [{"id": 1, "state": "PENDING", "commit_id": "h1"}], "me")[0], "re-review")
        self.assertEqual(src.decide(d, [{"state": "COMMENTED", "commit_id": "h1", "submitted_at": "x"}], "me")[0],
                         "re-review")
        self.assertEqual(src.decide(d, [{"state": "COMMENTED", "commit_id": "h2", "submitted_at": "x"}], "me")[0],
                         "skip")
        self.assertEqual(src.decide({**d, "isDraft": True}, [], "me")[0], "skip")
        self.assertEqual(src.decide({**d, "author": {"login": "me"}}, [], "me")[0], "skip")

    def test_untrusted_review_request(self):
        rm.CFG = make_cfg()
        member = {"number": 1, "action": "review", "head_sha": "a" * 40, "author": "alice", "author_association": "MEMBER"}
        fork = {"number": 2, "action": "review", "head_sha": "b" * 40, "author": "x", "author_association": "CONTRIBUTOR"}
        custom = {"number": 3, "action": "review", "head_sha": "c" * 40, "author": "y"}
        talk = {**member, "number": 4, "participants": [
            {"login": "coderabbitai[bot]", "association": "NONE", "bot": True},
            {"login": "bob", "association": "MEMBER", "bot": False},
            {"login": "stranger", "association": "NONE", "bot": False}]}
        self.assertEqual([rm.untrusted_reason(t) for t in (member, fork, custom, talk)],
                         [None, "作者 x（CONTRIBUTOR）", None, "会話の書き手 stranger"])
        with mock.patch.object(rm, "list_targets", lambda path: ({"targets": [member, fork, talk]}, None)):
            lines = [line for _, line in rm.poll_targets()]
        self.assertTrue(lines[0].startswith("REVIEW_REQUEST #1"))
        self.assertTrue(lines[1].startswith("UNTRUSTED_REVIEW_REQUEST #2"))
        self.assertTrue(lines[2].startswith("UNTRUSTED_REVIEW_REQUEST #4"))

    def test_participants(self):
        items = [{"user": {"login": "a", "type": "User"}, "author_association": "MEMBER"},
                 {"user": {"login": "coderabbitai[bot]", "type": "Bot"}, "author_association": "NONE"},
                 {"user": {"login": "a", "type": "User"}, "author_association": "MEMBER"}]
        self.assertEqual(src.participants(items), [
            {"login": "a", "association": "MEMBER", "bot": False},
            {"login": "coderabbitai[bot]", "association": "NONE", "bot": True}])


class PromptTest(unittest.TestCase):
    def test_build_prompt_with_template(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "t.md"), "w") as f:
                f.write("ロック {lock_started_at} は manager のもの。\nhead {head}")
            cfg = make_cfg({"review_manager": {"kinds": {"review": {"command": "/review-pr {n}", "template": "@t.md"}}}})
            cfg.dir = d
            rm.CFG = cfg
            p = rm.build_prompt(12, "review", lock_started_at="2026-01-01T00:00", head="abc1234", extra="追記")
            self.assertEqual(p, "/review-pr 12 （review-manager からの依頼。ロック 2026-01-01T00:00 は manager のもの。"
                                " head abc1234。追記）")

    def test_build_prompt_without_template(self):
        rm.CFG = make_cfg()
        self.assertEqual(rm.build_prompt(3, "re-review"), "/code-review 3")

    def test_launch_command(self):
        lc = {"cli": "claude-wrapper", "template": "cd {cwd} && {cli} {args}"}
        cmd = hlaunch.build_command(lc, "/a b", hlaunch.claude_args(resume="S", title="review session 1",
                                                                    permission_mode="auto"))
        self.assertEqual(cmd, "cd '/a b' && claude-wrapper --resume S -n 'review session 1' --permission-mode auto")


SID = "11111111-2222-3333-4444-555555555555"


class FakeHerdr:
    """sm.run_launch が使う herdr の呼び出しを、ペインの状態の列で置き換える。"""

    def __init__(self, pane_states, before_run="%"):
        self.pane_states = iter(pane_states)
        self.last = None
        self.prompts = []
        self.runs = []
        self.before_run = before_run

    def last_line(self, pane):
        """pane run の前はシェルの入力欄、後は claude の起動中の画面。"""
        return "starting" if self.runs else self.before_run

    def call(self, *args, timeout=60):
        if args[:2] == ("pane", "run"):
            self.runs.append(args[3])
            return True, {}
        if args[:2] == ("pane", "get"):
            self.last = next(self.pane_states, self.last)
            return True, {"pane": self.last}
        if args[:2] == ("agent", "rename"):
            return True, {}
        if args[:2] == ("agent", "wait"):
            return True, {"agent": {"agent_status": "idle", "state_change_seq": 5}}
        if args[:2] == ("agent", "prompt"):
            self.prompts.append(args[3])
            return True, {"agent": {"agent_status": "working", "state_change_seq": 6}}
        if args[:2] == ("agent", "get"):
            return True, {"agent": {"name": "x"}}
        return True, {}


class TrustDialogTest(unittest.TestCase):
    """信頼確認ダイアログで agent_session が未発行のとき、MISMATCH にせず watch へ引き継ぐ。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        sm.CFG = make_cfg()
        self.args = types.SimpleNamespace(state_dir=self.dir.name, pane="auto", start_timeout=60)
        self.meta = {"key": "start:t", "kind": "start", "pr": None, "title": "t", "mode": "auto"}
        self.patches = [
            mock.patch("time.sleep", lambda s: None),
            mock.patch.object(sm, "free_panes", lambda tab, strict=True: [{"pane_id": "w1:p1", "at_prompt": True}]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def launch(self, states, before_run="%"):
        fake = FakeHerdr(states, before_run)
        out = io.StringIO()
        clock = iter(range(0, 10 ** 6, 5))
        # wait_until の締め切りを実時間で待たないよう、時計を 1 回ごとに 5 秒進める
        with mock.patch.object(herdr, "call", fake.call), mock.patch("hdlib.screen.last_line", fake.last_line), \
                mock.patch("time.time", lambda: next(clock)), contextlib.redirect_stdout(out):
            sm.run_launch(self.args, "cmd", SID, "abc-1", "依頼", self.meta)
        return out.getvalue().strip(), fake

    def test_pane_busy_does_not_type(self):
        """入力欄にユーザーの打ちかけが残っていたら、つながって実行されるので打ち込まない。"""
        out, fake = self.launch([{"agent": None}], before_run="% nodenv install 26.12.")
        self.assertTrue(out.startswith("PANE_BUSY"), out)
        self.assertEqual(fake.runs, [])
        st = sm.store(self.args).load()
        self.assertEqual([q["reason"] for q in st["queue"]], ["pane_busy"])
        self.assertNotIn(SID, st["assignments"])

    def watch(self, live):
        fake = FakeHerdr([{"agent": "claude"}])
        with mock.patch.object(herdr, "call", fake.call), sm.store(self.args).txn() as st:
            ev = sm.check_assignments(st, live)
        return ev, sm.store(self.args).load(), fake

    def test_normal(self):
        out, fake = self.launch([{"agent": "claude", "agent_status": "idle", "agent_session": {"value": SID}}])
        self.assertTrue(out.startswith("SENT"))
        self.assertEqual(fake.prompts, ["依頼"])

    def test_session_appears_late(self):
        idle = {"agent": "claude", "agent_status": "idle"}
        out, _ = self.launch([idle, idle, {**idle, "agent_session": {"value": SID}}])
        self.assertTrue(out.startswith("SENT"))

    def test_trust_then_adopted_by_watch(self):
        out, _ = self.launch([{"agent": "claude", "agent_status": "blocked"}])
        self.assertTrue(out.startswith("BLOCKED stage=trust"), out)
        self.assertEqual(sm.store(self.args).load()["assignments"][SID]["phase"], "trust")
        ev, st, fake = self.watch({})
        self.assertEqual((ev, st["assignments"][SID]["phase"]), ([], "trust"))
        ev, st, fake = self.watch({SID: {"pane_id": "w1:p1", "agent_status": "idle", "seq": 9}})
        a = st["assignments"][SID]
        self.assertEqual((ev, a["phase"], a["name"], SID in st["launched"]), ([], "sent", "abc-1", True))
        self.assertEqual(fake.prompts, ["依頼"])

    def test_trust_then_other_session(self):
        self.launch([{"agent": "claude", "agent_status": "blocked"}])
        ev, st, _ = self.watch({"other": {"pane_id": "w1:p1", "agent_status": "idle", "seq": 1}})
        self.assertTrue(ev[0].startswith("MISMATCH"))
        self.assertNotIn(SID, st["assignments"])

    def test_real_mismatch(self):
        out, _ = self.launch([{"agent": "claude", "agent_status": "idle", "agent_session": {"value": "other"}}])
        self.assertTrue(out.startswith("MISMATCH"))


class HerdrUnreachableTest(unittest.TestCase):
    """sandbox の中では herdr の socket が拒否される。「動いているセッションが無い」と取り違えない。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        sm.CFG = rm.CFG = make_cfg()
        self.args = types.SimpleNamespace(state_dir=self.dir.name)
        self.patches = [
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
            mock.patch.object(herdr, "call", lambda *a, **k: (False, "connect: Operation not permitted")),
            mock.patch("time.sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def run_cmd(self, f, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            f(args)
        return out.getvalue()

    def test_listing_raises_only_inside_herdr(self):
        self.assertRaises(herdr.Unreachable, herdr.live_sessions)
        self.assertRaises(herdr.Unreachable, herdr.panes)
        with mock.patch.dict(os.environ, {"HERDR_ENV": "0"}):
            self.assertEqual((herdr.live_sessions(), herdr.panes()), ({}, []))

    def test_poll_does_not_report_live_dash(self):
        pr = {"number": 7, "headRefName": "feat/x", "title": "t",
              "reviews": {"nodes": [{"id": "r1", "author": {"login": "bob"}, "state": "CHANGES_REQUESTED",
                                     "submittedAt": "2026-01-01T00:00:00Z", "body": "", "comments": {"totalCount": 1}}]},
              "comments": {"nodes": []}}
        gh = types.SimpleNamespace(returncode=0, stdout=sm.json.dumps({"data": {"search": {"nodes": [pr]}}}), stderr="")
        with mock.patch.object(sm.subprocess, "run", lambda *a, **k: gh), mock.patch.object(sm, "load_index", lambda *a: {}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                items = sm.poll(self.args, "me")
        self.assertIsNone(items)
        self.assertIn("WATCH_ERROR herdr_unreachable", out.getvalue())
        self.assertNotIn("FEEDBACK", out.getvalue())

    def test_watch_keeps_assignment_and_stops_after_errors(self):
        with sm.store(self.args).txn() as st:
            st["assignments"][SID] = {"phase": "sent", "pane": "w1:p1", "kind": "review", "misses": 0}
        args = types.SimpleNamespace(state_dir=self.dir.name, once=False, reset=False, interval=300, slot_interval=0,
                                     health_interval=60, exit_on_event=True, max_watch_errors=3)
        with mock.patch.object(sm.util, "run", lambda *a, **k: types.SimpleNamespace(stdout="me\n")), \
                mock.patch.object(sm, "poll", lambda *a: []), mock.patch.object(sm.supervisor, "install_sigterm"):
            out = self.run_cmd(sm.cmd_watch, args)
        self.assertEqual([line.split()[2] for line in out.splitlines()], ["count=1", "count=3"])
        a = sm.store(self.args).load()["assignments"][SID]
        self.assertEqual((a["phase"], a["misses"]), ("sent", 0))

    def test_status_says_unknown(self):
        with sm.store(self.args).txn() as st:
            st["assignments"][SID] = {"phase": "sent", "pane": "w1:p1", "kind": "review", "at": sm.iso()}
        out = self.run_cmd(sm.cmd_status, types.SimpleNamespace(state_dir=self.dir.name, json=False, stale_sec=600))
        self.assertIn("Herdr に届かない", out)
        self.assertIn("w1:p1 不明", out)
        rdir = os.path.join(self.dir.name, "rm")
        with rm.store(types.SimpleNamespace(state_dir=rdir)).txn() as st:
            st["assignments"]["1"] = {"pr": 7, "kind": "review", "sent_at": sm.iso(), "phase": "working"}
        out = self.run_cmd(rm.cmd_status, types.SimpleNamespace(state_dir=rdir, json=False))
        self.assertIn("枠: 不明", out)
        self.assertIn("#7 review", out)


if __name__ == "__main__":
    unittest.main()
