"""常駐監視（watch）の登録・引き継ぎ・生存確認。

監視は Bash のバックグラウンド実行で張り、イベントを 1 行でも出したら終了する（manager は終了の通知で起こされる）。
manager のセッションが再起動すると前の監視が取り残されて 2 本が並び、既出の記録とイベントが二重になるので、
新しい監視は前の監視を止めて引き継ぐ。
"""
import os
import signal
import subprocess
import sys

from . import util


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
