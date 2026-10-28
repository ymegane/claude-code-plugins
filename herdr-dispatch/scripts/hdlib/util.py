import datetime as dt
import json
import re
import subprocess
import sys
import time

# subprocess の出力を読むときの指定。ps は長い引数をバイト数で切るので、日本語の依頼文（--prompt）を含む
# command 行が文字の途中で切れて届く。厳密なデコードでは UnicodeDecodeError で watch ごと落ちる
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(d=None):
    return (d or now()).isoformat()


def parse_ts(s):
    if not s:
        return None
    try:
        # GitHub と transcript は 'Z' 表記。fromisoformat は 3.10 では 'Z' を解せない
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_hm(s):
    d = parse_ts(s)
    return d.astimezone().strftime("%m/%d %H:%M") if d else "-"


def ago(s):
    d = parse_ts(s)
    if not d:
        return "-"
    m = int((now() - d).total_seconds() // 60)
    return f"{m}分前" if m < 120 else f"{m // 60}時間{m % 60}分前"


def run(cmd, cwd=None, timeout=120):
    return subprocess.run(cmd, capture_output=True, **TEXT, cwd=cwd, timeout=timeout)


def one_line(s):
    return re.sub(r"\s+", " ", s or "").strip()


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def quote_untrusted(s, limit=60):
    """他者が書いた文字列（PR のタイトルなど）をイベント行に載せる形にする。

    イベント行は manager（LLM）が読むので、改行で偽の行を作らせず、引用符で囲んでデータだと分かるようにする。
    """
    return json.dumps(_CONTROL_RE.sub(" ", s or "")[:limit], ensure_ascii=False)


def emit(tag, **kw):
    print(f"{tag} " + " ".join(f"{k}={v}" for k, v in kw.items() if v is not None), flush=True)


def die(msg):
    print(f"中止: {msg}", flush=True)
    sys.exit(1)


def wait_until(pred, timeout, interval=2):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return None


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def fill(template, **values):
    """{name} を values で置き換える。知らない名前はそのまま残す（テンプレートに JSON などの波括弧が入っても壊さない）。"""
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)
