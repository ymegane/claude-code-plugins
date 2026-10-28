import json
import os
import subprocess

from . import util


def call(*args, timeout=60):
    """herdr を呼んで (ok, result) を返す。失敗時の result は stderr の JSON の error か文字列。"""
    try:
        r = subprocess.run(["herdr", *args], capture_output=True, **util.TEXT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "herdr not found"
    out = r.stdout if r.returncode == 0 else r.stderr
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return r.returncode == 0, out.strip()
    if not isinstance(data, dict):
        return r.returncode == 0, data
    return r.returncode == 0, data.get("result", data.get("error", data))


def result(*args, timeout=60):
    ok, res = call(*args, timeout=timeout)
    return res if ok and isinstance(res, dict) else None


def in_herdr():
    return os.environ.get("HERDR_ENV") == "1"


class Unreachable(Exception):
    """Herdr の中なのに一覧を取れない（Claude Code の sandbox では socket が Operation not permitted になる）。

    空の一覧として返すと「動いているセッションが無い」と区別できず、動いているセッションを二重に起こしたり、
    割り当てを GONE にしたりする。呼び出し側は状態を不明として扱う。
    """


def unreachable_message(e):
    return f"Herdr に届かない（{e}）。Claude Code の sandbox の中では herdr の socket が拒否されるので、sandbox の外で実行する"


def unreachable_line(e, count=None):
    """watch のイベント行。"""
    return f"WATCH_ERROR herdr_unreachable{'' if count is None else f' count={count}'} reason={util.quote_untrusted(str(e), 200)}"


def _listing(*args):
    ok, res = call(*args)
    if not ok or not isinstance(res, dict):
        raise Unreachable(f"herdr {' '.join(args)}: {' '.join(str(res).split())[:200]}")
    return res


def panes():
    """Herdr の外なら空。Herdr の中で取れなければ Unreachable。"""
    if not in_herdr():
        return []
    return _listing("pane", "list").get("panes", [])


def pane_get(pane_id):
    res = result("pane", "get", pane_id)
    return (res or {}).get("pane", res) if res else None


def agent_get(target):
    return (result("agent", "get", target) or {}).get("agent")


def session_of(pane_or_agent):
    return ((pane_or_agent or {}).get("agent_session") or {}).get("value")


def live_sessions():
    """session_id -> {pane_id, agent_status, seq, tab_id}。Herdr の外なら空。Herdr の中で取れなければ Unreachable。"""
    if not in_herdr():
        return {}
    live = {}
    for a in _listing("agent", "list").get("agents", []):
        sid = session_of(a)
        if sid:
            live[sid] = {"pane_id": a.get("pane_id"), "agent_status": a.get("agent_status"),
                         "seq": a.get("state_change_seq"), "tab_id": a.get("tab_id")}
    return live


def status_of(res):
    agent = res.get("agent", res) if isinstance(res, dict) else {}
    return agent.get("agent_status"), agent
