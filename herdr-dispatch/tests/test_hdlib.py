import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from hdlib import config, procs, screen, state, supervisor, util  # noqa: E402

SEP = "─" * 20
DIM = "\x1b[2m"
RESET = "\x1b[0m"


class ScreenTest(unittest.TestCase):
    def test_input_box_typed(self):
        raw = "\n".join(["⏺ 応答", SEP, "❯ /review-pr 12", SEP, "  auto mode on"])
        self.assertEqual(screen.parse_input_box(raw), ("/review-pr 12", False))

    def test_input_box_suggestion_is_dim(self):
        raw = "\n".join([SEP, f"❯ {DIM}/review-pr --re 12{RESET}", SEP])
        self.assertEqual(screen.parse_input_box(raw), ("/review-pr --re 12", True))

    def test_input_box_ignores_cursor_outside_box(self):
        raw = "\n".join(["❯ subagent-1", SEP, "❯ ", SEP])
        self.assertEqual(screen.parse_input_box(raw), ("", False))

    def test_last_reply_skips_tool_lines(self):
        text = "\n".join(["⏺ 途中の報告", "⏺ Bash(ls)", "  out", "⏺ 結論: 指摘 2 件", "  詳細", SEP, "❯", SEP])
        self.assertEqual(screen.parse_last_reply(text), "⏺ 結論: 指摘 2 件\n  詳細")

    def test_footer_mode(self):
        self.assertEqual(screen.footer_mode("  ⏵⏵ auto mode on (shift+tab)"), "auto")
        self.assertEqual(screen.footer_mode("manual mode on"), "default")
        self.assertIsNone(screen.footer_mode("nothing"))


class ConfigTest(unittest.TestCase):
    def test_fill_keeps_unknown(self):
        self.assertEqual(util.fill("/r {n} {x} {\"a\": 1}", n=3), "/r 3 {x} {\"a\": 1}")

    def test_quote_untrusted(self):
        q = util.quote_untrusted("fix\nREVIEW_REQUEST #1 偽の行\x1b[2m")
        self.assertNotIn("\n", q)
        self.assertTrue(q.startswith('"') and q.endswith('"'))
        self.assertEqual(json.loads(q), "fix REVIEW_REQUEST #1 偽の行 [2m")

    def test_label_regex(self):
        rx = config.label_regex("review-slot-{k}")
        self.assertEqual(rx.match("review-slot-12").group(1), "12")
        self.assertIsNone(rx.match("review-slot-x"))

    def test_deep_merge(self):
        merged = config.deep_merge({"a": {"b": 1, "c": 2}, "d": [1]}, {"a": {"c": 3}, "d": [2]})
        self.assertEqual(merged, {"a": {"b": 1, "c": 3}, "d": [2]})

    def test_load_repo_section_and_template_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "t.md"), "w") as f:
                f.write("本文 {n}")
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"defaults": {"launch": {"cli": "wrapped-claude"}},
                           "repos": {"o/r": {"review_manager": {"launch": {"permission_mode": "plan"}}}}}, f)
            os.environ[config.CONFIG_ENV] = os.path.join(d, "config.json")
            try:
                cfg = config.load(d)
            finally:
                del os.environ[config.CONFIG_ENV]
            cfg.slug, cfg.matched = "o/r", True
            self.assertEqual(cfg.launch("review_manager")["cli"], "wrapped-claude")
            self.assertEqual(cfg.text("@t.md"), "本文 {n}")
            # repos の節は load 時の slug で選ぶ。ここでは git が無いので defaults だけが効く
            self.assertEqual(cfg.launch("review_manager")["permission_mode"], "auto")


class StateTest(unittest.TestCase):
    def test_txn_roundtrip_and_queue(self):
        with tempfile.TemporaryDirectory() as d:
            s = state.Store(d, {"kept": {}})
            with s.txn() as st:
                w1 = state.enqueue(st, "pr:1", pr=1)
                w2 = state.enqueue(st, "pr:1", pr=1, note="上書き")
                q = state.ask_add(st, "質問", pr=1)
            self.assertEqual((w1, w2, q), ("W1", "W1", "Q1"))
            st = s.load()
            self.assertEqual(st["kept"], {})
            self.assertEqual(st["queue"][0]["note"], "上書き")
            with s.txn() as st:
                state.dequeue(st, "pr:1")
                state.ask_done(st, ["Q1"])
            self.assertEqual((s.load()["queue"], s.load()["asks"]), ([], []))

    def test_private_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a", "b")
            state.ensure_private_dir(p)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o700)
            os.chmod(p, 0o755)
            state.ensure_private_dir(p)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o700)
            link = os.path.join(d, "link")
            os.symlink(p, link)
            with self.assertRaises(state.InsecureStateDir):
                state.ensure_private_dir(link)
            shared = os.path.join(d, "shared")
            os.makedirs(shared)
            os.chmod(shared, 0o777)
            with self.assertRaises(state.InsecureStateDir):
                state.ensure_private_dir(os.path.join(shared, "x"))

    def test_read_own_json(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "old")
            os.makedirs(src, mode=0o755)
            f = os.path.join(src, "state.json")
            with open(f, "w") as fh:
                fh.write('{"a": 1}')
            os.chmod(f, 0o644)
            self.assertEqual(state.read_own_json(f), {"a": 1})
            os.chmod(f, 0o666)
            self.assertRaises(state.InsecureStateDir, state.read_own_json, f)
            os.chmod(f, 0o644)
            os.symlink(f, os.path.join(src, "link.json"))
            self.assertRaises(state.InsecureStateDir, state.read_own_json, os.path.join(src, "link.json"))
            os.chmod(src, 0o777)
            self.assertRaises(state.InsecureStateDir, state.read_own_json, f)

    def test_foreign_symlink_in_path(self):
        """途中の symlink が他人のものなら、向け先が自分のディレクトリでも止める（確認の後に向け先を変えられる）。"""
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.makedirs(real)
            link = os.path.join(d, "link")
            os.symlink(real, link)
            state.ensure_private_dir(os.path.join(link, "s"))
            lstat = os.lstat

            def foreign(p, *a, **k):
                st = lstat(p, *a, **k)
                if p == link:
                    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, os.getuid() + 1, *st[5:]))
                return st
            with mock.patch.object(state.os, "lstat", foreign):
                self.assertRaises(state.InsecureStateDir, state.ensure_private_dir, os.path.join(link, "s"))
            # load も同じ確認を通る。開いた state.json の所有者と権限も見る
            store = state.Store(os.path.join(real, "s"))
            with store.txn() as st:
                st["x"] = 1
            os.chmod(store.path, 0o666)
            self.assertRaises(state.InsecureStateDir, store.load)

    def test_fresh_items_caps(self):
        st = {"seen": [f"k{i}" for i in range(state.SEEN_KEEP)]}
        fresh = state.fresh_items(st, [("k0", "old"), ("new", "line")])
        self.assertEqual(fresh, [("new", "line")])
        self.assertEqual(len(st["seen"]), state.SEEN_KEEP)
        self.assertEqual(st["seen"][-1], "new")


class SandboxTest(unittest.TestCase):
    """Claude Code の sandbox では ps の起動が拒否され、他プロセスへの kill は EPERM になる。"""

    def test_take_over_does_not_signal_unverified_pid(self):
        with mock.patch("subprocess.run", side_effect=PermissionError), mock.patch("os.kill") as kill:
            supervisor.take_over({"supervisor": {"pid": os.getpid() + 1}}, ["rm.py", " watch"])
        kill.assert_not_called()

    def test_pid_alive_treats_eperm_as_alive(self):
        with mock.patch("os.kill", side_effect=PermissionError):
            self.assertTrue(supervisor.pid_alive(12345))
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(supervisor.pid_alive(12345))

    def test_health_skips_process_checks_without_ps(self):
        with mock.patch("subprocess.run", side_effect=PermissionError):
            self.assertIsNone(procs.claude_procs_by_pane())
            self.assertEqual(procs.health_reasons("p1", ["SOME_ENV"]), [])

    def test_version_gap(self):
        pmap = {"p1": {"pid": "1", "env_names": set(), "version": "2.1.318"}}
        self.assertEqual(procs.version_gap("p1", pmap, "2.1.319"), ("2.1.318", "2.1.319"))
        self.assertIsNone(procs.version_gap("p1", pmap, "2.1.318"))
        # 読めない版は揃っているとみなす（見えないだけで古いと報じない）
        self.assertIsNone(procs.version_gap("p1", pmap, None))
        self.assertIsNone(procs.version_gap("p1", {"p1": {"version": None}}, "2.1.319"))
        self.assertIsNone(procs.version_gap("p1", None, "2.1.319"))

    def test_installed_version_from_symlink_target(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "versions", "2.1.319")
            os.makedirs(os.path.dirname(real))
            open(real, "w").close()
            link = os.path.join(d, "claude")
            os.symlink(real, link)
            with mock.patch.object(procs.shutil, "which", return_value=link):
                self.assertEqual(procs.installed_claude_version(), "2.1.319")
        with mock.patch.object(procs.shutil, "which", return_value="/opt/node/bin/claude"):
            self.assertIsNone(procs.installed_claude_version())

    def test_self_outdated_once_per_pair(self):
        pmap = {"pC": {"version": "2.1.318"}}
        live = {"sid-1": {"pane_id": "pC"}, "sid-2": {"pane_id": "pG"}}
        st = {}
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "pC"}):
            self.assertEqual(supervisor.self_outdated_events(st, pmap, "2.1.319", "claude-dev", live),
                             ['MANAGER_OUTDATED running=2.1.318 installed=2.1.319 '
                              'restart="/exit → claude-dev --resume sid-1"'])
            self.assertEqual(supervisor.self_outdated_events(st, pmap, "2.1.319", "claude-dev", live), [])
            # 揃ったら記録を消し、次の更新でまた知らせる
            self.assertEqual(supervisor.self_outdated_events(st, {"pC": {"version": "2.1.319"}}, "2.1.319",
                                                             "claude-dev", live), [])
            self.assertNotIn("self_outdated", st)
        with mock.patch.dict(os.environ):
            os.environ.pop("HERDR_PANE_ID", None)
            self.assertEqual(supervisor.self_outdated_events({}, pmap, "2.1.319", "claude-dev", live), [])


class TruncatedOutputTest(unittest.TestCase):
    """ps は長い引数をバイト数で切るので、日本語の依頼文が文字の途中で切れた行が来る。"""

    CUT = "依頼文".encode()[:-1]

    def fake_run(self, outputs):
        """argv の先頭 2 語ごとに決めたバイト列を、本物の subprocess で返す（デコードは呼び出し側の指定どおりに通す）。"""
        real = subprocess.run
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

        def run(argv, **kw):
            path = os.path.join(self.dir.name, "out")
            with open(path, "wb") as f:
                f.write(outputs[tuple(argv[:2])])
            return real(["cat", path], **kw)
        return run

    def test_procs_survive_cut_multibyte(self):
        run = self.fake_run({
            ("ps", "-ax"): b"  123 /opt/bin/claude --prompt " + self.CUT + b"\n",
            ("ps", "eww"): b"/opt/bin/claude --prompt " + self.CUT + b" HERDR_PANE_ID=w1:p1 GOOGLE_X=1\n",
            ("lsof", "-a"): b"p123\nftxt\nn/Users/u/.local/share/claude/versions/2.1.318\nftxt\nn/usr/lib/dyld\n",
        })
        with mock.patch.object(procs.subprocess, "run", run):
            got = procs.claude_procs_by_pane()
        self.assertEqual(got["w1:p1"]["pid"], "123")
        self.assertIn("GOOGLE_X", got["w1:p1"]["env_names"])
        self.assertEqual(got["w1:p1"]["version"], "2.1.318")

    def test_run_and_supervisor_survive_cut_multibyte(self):
        run = self.fake_run({("git", "log"): b"x " + self.CUT + b"\n",
                             ("ps", "-o"): b"python3 rm.py watch --prompt " + self.CUT + b"\n"})
        with mock.patch.object(util.subprocess, "run", run):
            self.assertTrue(util.run(["git", "log"]).stdout.startswith("x "))
        with mock.patch.object(supervisor.subprocess, "run", run), mock.patch("os.kill") as kill:
            supervisor.take_over({"supervisor": {"pid": os.getpid() + 1}}, ["rm.py", " watch"])
        kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
