"""Claude Code の画面（herdr pane read）の読み取り。画面の形は Claude Code の版で変わりうる。"""
import re
import subprocess

from . import util

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
DIM = "\x1b[2m"
SEPARATOR = "────"
# 画面下の permission mode の表示 → --permission-mode の値
FOOTER_MODES = {
    "auto mode on": "auto",
    "manual mode on": "default",
    "plan mode on": "plan",
    "accept edits on": "acceptEdits",
    "bypass permissions on": "bypassPermissions",
}
TOOL_LINE_RE = re.compile(r"^⏺ (?:[A-Za-z_][\w-]*\(|Stop Task|Agent |Task |Update\(|Read |Ran |Bash\()")


def read(pane_id, source="visible", ansi=False, lines=None):
    args = ["herdr", "pane", "read", pane_id, "--source", source]
    if ansi:
        args += ["--format", "ansi"]
    if lines:
        args += ["--lines", str(lines)]
    # 状態が変わった直後の読み取りは空で返ることがある
    for _ in range(3):
        try:
            r = subprocess.run(args, capture_output=True, **util.TEXT, timeout=30)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    return ""


def parse_input_box(raw):
    """ANSI 付きの画面から (入力欄の文字, それが入力候補の dim 表示か) を返す。空なら ("", False)。"""
    rows = raw.splitlines()
    # 入力欄は区切り線 2 本の間にある。その外の ❯（サブエージェント一覧の選択カーソルなど）を拾わない
    seps = [i for i, row in enumerate(rows) if ANSI_RE.sub("", row).startswith(SEPARATOR)]
    if len(seps) < 2:
        return "", False
    for i in range(seps[-2] + 1, seps[-1]):
        plain = ANSI_RE.sub("", rows[i]).strip()
        if plain.startswith("❯"):
            text = plain[1:].strip()
            if not text:
                return "", False
            return text, DIM in rows[i].split("❯", 1)[1][:40]
    return "", False


def input_box(pane_id):
    return parse_input_box(read(pane_id, ansi=True))


def footer_mode(text):
    for label, mode in FOOTER_MODES.items():
        if label in text:
            return mode
    return None


def parse_last_reply(text, max_lines=40):
    """画面の最後の応答（ツール呼び出しでない ⏺ 行から入力欄の手前まで）を返す。"""
    rows = text.splitlines()
    seps = [i for i, r in enumerate(rows) if r.startswith(SEPARATOR)]
    end = seps[-2] if len(seps) >= 2 else len(rows)
    for i in range(end - 1, -1, -1):
        if rows[i].startswith("⏺ ") and not TOOL_LINE_RE.match(rows[i]):
            body = [r for r in rows[i:end] if r.strip()]
            return "\n".join(body[:max_lines])
    return ""


def last_reply(pane_id, max_lines=40):
    return parse_last_reply(read(pane_id, source="recent-unwrapped", lines=200), max_lines)


def last_line(pane_id):
    lines = [ln for ln in read(pane_id, lines=5).splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def at_prompt(pane_id, prompt_regex):
    """シェルのプロンプトで止まっているか。agent の無いペインでも別のプロセスが前面にいることがある。"""
    last = last_line(pane_id)
    return bool(re.search(prompt_regex, last)), last
