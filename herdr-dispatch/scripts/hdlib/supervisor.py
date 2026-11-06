"""常駐監視（watch）の登録・引き継ぎ・生存確認。

監視は Bash のバックグラウンド実行で張り、イベントを 1 行でも出したら終了する（manager は終了の通知で起こされる）。
manager のセッションが再起動すると前の監視が取り残されて 2 本が並び、既出の記録とイベントが二重になるので、
新しい監視は前の監視を止めて引き継ぐ。
"""
import json
import os
import signal
import subprocess
import sys

from . import herdr, procs, util


def take_over(st, markers):
    """前の監視が残っていれば SIGTERM で止める。markers は ps の command に全て含まれる文字列（別プロセスを殺さないため）。"""
    old = (st.get("supervisor") or {}).get("pid")
    if not old or old == os.getpid():
        return
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(old)], capture_output=True, **util.TEXT).stdout
    except OSError:
        # Claude Code の sandbox では ps の起動自体が拒否される。確かめられない pid は再利用の恐れがあるので止めない。
        # 前の監視は次の心拍で state の pid が自分でないことに気づき、STOPPED reason=replaced で抜ける
        return
    if cmd and all(m in cmd for m in markers):
        try:
            os.kill(old, signal.SIGTERM)
        except OSError:
            pass


def register(st, markers):
    take_over(st, markers)
    st["supervisor"] = {"pid": os.getpid(), "started": util.iso(), "heartbeat": util.iso()}


def beat(st):
    """心拍を記録する。別の監視に引き継がれていたら STOPPED を出して False。"""
    sup = st.get("supervisor") or {}
    if sup.get("pid") != os.getpid():
        util.emit("STOPPED", reason="replaced", by=sup.get("pid"))
        return False
    sup["heartbeat"] = util.iso()
    st["supervisor"] = sup
    return True


def install_sigterm():
    def on_term(signum, _frame):
        util.emit("STOPPED", reason="signal", signal=signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # sandbox 越しだとシグナルを送れないだけで、EPERM はプロセスが在ることを示す
        return True
    except OSError:
        return False
    return True


def status_line(st, stale_sec=600):
    sup = st.get("supervisor") or {}
    hb = sup.get("heartbeat")
    d = util.parse_ts(hb)
    alive = bool(d) and (util.now() - d).total_seconds() < stale_sec and pid_alive(sup.get("pid"))
    return f"監視: {'稼働中' if alive else '止まっている可能性'}（最終 {util.local_hm(hb)}・pid {sup.get('pid', '-')}）"


def self_version(pmap, installed, live=None):
    """manager 自身（このプロセスの HERDR_PANE_ID のペイン）の (動いている版, インストール済みの版, session id)。
    揃っているか読めなければ None。session id はペインから引けなければ None。"""
    pane = os.environ.get("HERDR_PANE_ID")
    gap = procs.version_gap(pane, pmap, installed) if pane else None
    if not gap:
        return None
    live = herdr.live_sessions() if live is None else live
    sid = next((s for s, v in live.items() if v.get("pane_id") == pane), None)
    return gap[0], gap[1], sid


def restart_hint(cli, sid):
    return f"/exit → {cli} --resume {sid or '<session id>'}"


def self_outdated_events(st, pmap, installed, cli, live=None):
    """manager 自身の古い版を、版の組ごとに 1 回だけ知らせる。
    manager は自分を立ち上げ直さない。/exit から --resume で戻るまでの間は監視を張り直す者がいないので、
    途中で止まると誰も気づけない。手順を添えてユーザーに任せる。"""
    v = self_version(pmap, installed, live)
    if not v:
        st.pop("self_outdated", None)
        return []
    key = f"{v[2]}:{v[0]}->{v[1]}"
    if st.get("self_outdated") == key:
        return []
    st["self_outdated"] = key
    return [f"MANAGER_OUTDATED running={v[0]} installed={v[1]} "
            f"restart={json.dumps(restart_hint(cli, v[2]), ensure_ascii=False)}"]


def self_version_line(pmap, installed, cli, live=None):
    v = self_version(pmap, installed, live)
    if not v:
        return None
    return (f"⚠ manager 自身が古い版: {v[0]} で動いている（インストール済みは {v[1]}）。"
            f"手の空いたときに {restart_hint(cli, v[2])}")
