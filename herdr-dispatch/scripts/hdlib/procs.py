"""ペインで動いている claude プロセスと、起こし直しによる劣化の判定。

Herdr の再起動やセッションの復元は素の `claude --resume` で起こすので、launch.cli のラッパーが
環境に足したもの（認証情報など）と、--permission-mode の指定が消える。それを env と画面下の表示で見分ける。
"""
import os
import re
import subprocess

from . import screen, util

_PS_LINE_RE = re.compile(r"^\s*(\d+)\s+(\S+)")


def claude_procs_by_pane():
    """pane_id -> {pid, env_names}。ps を起動できないとき（Claude Code の sandbox）は None。
    Herdr はペインのプロセスの環境に HERDR_PANE_ID を入れるので、起動引数に頼らず引ける。"""
    try:
        r = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, **util.TEXT)
        out = {}
        for line in r.stdout.splitlines():
            m = _PS_LINE_RE.match(line)
            # ラッパーが絶対パスで起動することがあるので、argv[0] の basename で見る
            if not m or os.path.basename(m.group(2)) != "claude":
                continue
            env = subprocess.run(["ps", "eww", "-o", "command=", "-p", m.group(1)],
                                 capture_output=True, **util.TEXT).stdout
            pm = re.search(r"\bHERDR_PANE_ID=(\S+)", env)
            if pm:
                names = set(re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", env))
                out[pm.group(1)] = {"pid": m.group(1), "env_names": names}
        return out
    except OSError:
        return None


def health_reasons(pane_id, require_env=(), expect_mode=None, procs=None):
    """劣化の理由のリスト（空なら健全）。procs を渡すと ps を省く。
    ps を起動できなければプロセスと環境は判定しない（見えないだけで劣化と報じると、全ペインが誤報になる）。"""
    procs = claude_procs_by_pane() if procs is None else procs
    reasons = []
    if procs is not None:
        p = procs.get(pane_id)
        if not p:
            reasons.append("ペインに claude のプロセスが見つからない")
        else:
            missing = [n for n in require_env if n not in p["env_names"]]
            if missing:
                reasons.append(f"環境に {', '.join(missing)} が無い（launch.cli で起動していない可能性）")
    if expect_mode:
        mode = screen.footer_mode(screen.read(pane_id))
        if mode and mode != expect_mode:
            reasons.append(f"{mode} mode（起動時は {expect_mode}）")
    return reasons
