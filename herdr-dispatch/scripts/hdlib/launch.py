"""ペインで claude を起動し、agent の検出・セッション ID の照合・命名までを行う。

起動は `herdr pane run` でペインの対話シェルへコマンドを打ち込む。シェルのエイリアスや関数がそのまま解決されるので、
launch.cli に認証情報を足すラッパーなどを書ける。`herdr agent start` は claude を直接起動してシェルを通らないので使わない。
"""
import shlex
import time

from . import herdr, screen, util

SESSION_WAIT_SEC = 60
# /exit の直後などはプロンプトの描画が遅れるので、入力欄が空になるまで少し待つ
PROMPT_WAIT_SEC = 10


def build_command(launch_cfg, cwd, args):
    argstr = " ".join(shlex.quote(a) for a in args)
    return util.fill(launch_cfg["template"], cwd=shlex.quote(cwd), cli=launch_cfg["cli"], args=argstr)


def claude_args(session_id=None, resume=None, title=None, permission_mode=None):
    args = []
    if resume:
        args += ["--resume", resume]
    if session_id:
        # ID を先に決めておき、起動後に Herdr が検出した ID と照合する
        args += ["--session-id", session_id]
    if title:
        args += ["-n", title]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    return args


def run_until_agent(pane, command, timeout, prompt_regex, poll=10):
    """command を打って agent の出現を待つ。

    ("agent", ペインの情報) / ("pane_busy", 最後の行) / ("start_failed", 画面の末尾) / ("timeout", 最後の行) / ("error", 詳細)。
    launch.cli が承認（パスワードマネージャーなど）を待つ間は agent が現れないので、timeout は長めに取る。
    """
    # pane run は入力欄の続きに打ち込む。ユーザーの打ちかけの文字が残っていると、つながったコマンドが実行される。
    # 入力欄を消すとユーザーの入力を失うので、空でなければ打たずに返す
    if not util.wait_until(lambda: screen.at_prompt(pane, prompt_regex)[0], PROMPT_WAIT_SEC, interval=1):
        return "pane_busy", screen.last_line(pane)
    ok, res = herdr.call("pane", "run", pane, command)
    if not ok:
        return "error", str(res)[:200]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        p = herdr.pane_get(pane) or {}
        if p.get("agent"):
            return "agent", p
        # 起動コマンドが失敗してシェルへ戻ったら、待っても agent は現れない
        back, _ = screen.at_prompt(pane, prompt_regex)
        if back:
            return "start_failed", screen.read(pane, source="recent-unwrapped", lines=6)
    return "timeout", screen.last_line(pane)


def wait_session(pane, agent, wait_sec=SESSION_WAIT_SEC):
    """agent が現れてから Herdr がセッション ID を出すまで待つ。(ID か None, ペインの情報)。

    初めての cwd での信頼確認ダイアログの間は ID が出ない。None を別セッションの起動と取り違えないこと。
    """
    deadline = time.time() + wait_sec
    while True:
        got = herdr.session_of(agent)
        if got or agent.get("agent_status") == "blocked" or time.time() > deadline:
            return got, agent
        time.sleep(3)
        agent = herdr.pane_get(pane) or agent


def name_agent(pane, name, alternatives=True):
    """agent に名前を付ける。使用中なら -2 … を試す。付けた名前（全て失敗ならペイン ID）を返す。"""
    cands = [name] + ([f"{name}-{i}" for i in range(2, 6)] if alternatives else [])
    for cand in cands:
        ok, _ = herdr.call("agent", "rename", pane, cand)
        if ok:
            return cand
    return pane


def wait_idle(target, timeout_sec=120):
    ok, res = herdr.call("agent", "wait", target, "--timeout", str(timeout_sec * 1000), timeout=timeout_sec + 30)
    return herdr.status_of(res) if ok else (None, {})
