"""manager の状態ファイル（state.json）。

割り当て・待ち行列・ユーザーへの質問・既出のイベントを manager の会話の外に持つ。会話が圧縮されたり
manager が再起動したりしても status で状況を取り戻せるようにするため。CLI と常駐監視が同時に書くので、
読み書きは txn() で排他する。
"""
import contextlib
import fcntl
import json
import os
import stat

from . import util

STATE_FILE = "state.json"
SEEN_KEEP = 500


class InsecureStateDir(Exception):
    pass


def ensure_private_dir(path):
    """path を本人だけが書けるディレクトリとして用意する。

    待ち行列の cmd は manager がそのまま実行するので、他のユーザーが state.json を書き換えられると
    任意のコマンドを実行させられる。作るディレクトリは 0700 にし、既存のものは本人の所有で
    symlink でなく、group / other に書き込みを許していないことを確かめる。
    """
    path = os.path.abspath(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise InsecureStateDir(f"{path} がディレクトリでない（symlink を含む）")
    if st.st_uid != os.getuid():
        raise InsecureStateDir(f"{path} の所有者が自分でない（uid {st.st_uid}）")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)
    _check_ancestors(path)
    return path


def _check_ancestors(path):
    # 祖先のどれかを他人が書き換えられると、その下を丸ごと差し替えられる（/tmp のような sticky bit 付きは除く）。
    # 解決前のパスでは、途中の symlink が他人のものでないことを見る（他人の symlink は確認の後に向け先を変えられる）。
    # 実体のパスでは、各ディレクトリの所有者と書き込み権を見る（macOS の /tmp は root の /private/tmp への symlink）
    for cur in (os.path.abspath(path), os.path.realpath(path)):
        follow = cur != os.path.abspath(path)
        while cur != os.path.dirname(cur):
            cur = os.path.dirname(cur)
            pst = os.stat(cur) if follow else os.lstat(cur)
            if pst.st_uid not in (0, os.getuid()):
                raise InsecureStateDir(f"{cur} の所有者が自分でも root でもない")
            if not stat.S_ISLNK(pst.st_mode) and stat.S_IMODE(pst.st_mode) & 0o022 and not pst.st_mode & stat.S_ISVTX:
                raise InsecureStateDir(f"{cur} を他のユーザーが書き換えられる")


def _check_own_file(f, path):
    fst = os.fstat(f.fileno())
    if fst.st_uid != os.getuid() or stat.S_IMODE(fst.st_mode) & 0o022:
        raise InsecureStateDir(f"{path} の所有者が自分でないか、他のユーザーが書ける")


def read_own_json(path):
    """以前の版の状態など、本人が書いたはずの JSON を読む。

    取り込む値には manager が後で使うものがあるので、他のユーザーが置けたファイル（symlink、所有者が自分でない、
    本人以外が書ける、またはそのようなディレクトリの下にある）なら InsecureStateDir。"""
    path = os.path.abspath(os.path.expanduser(path))
    d = os.path.dirname(path)
    dst = os.lstat(d)
    if stat.S_ISLNK(dst.st_mode) or not stat.S_ISDIR(dst.st_mode) or dst.st_uid != os.getuid() \
            or stat.S_IMODE(dst.st_mode) & 0o022:
        raise InsecureStateDir(f"{d} が自分だけの書けるディレクトリでない")
    _check_ancestors(d)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise InsecureStateDir(f"{path} を開けない（symlink を含む）: {e}") from e
    with os.fdopen(fd, encoding="utf-8") as f:
        _check_own_file(f, path)
        return json.load(f)


def open_private(path, mode="w"):
    """symlink を辿らず、0600 で開く。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if "w" in mode else 0)
    return os.fdopen(os.open(path, flags, 0o600), mode, encoding=None if "b" in mode else "utf-8")

COMMON_DEFAULTS = {"assignments": {}, "queue": [], "next_queue": 1, "asks": [], "next_ask": 1,
                   "seen": [], "supervisor": {}}


class Store:
    def __init__(self, state_dir, defaults=None):
        self.dir = state_dir
        self.path = os.path.join(state_dir, STATE_FILE)
        self.defaults = {**COMMON_DEFAULTS, **(defaults or {})}

    def fresh(self):
        return json.loads(json.dumps(self.defaults))

    def load(self):
        # status の表示（待ち行列の cmd を含む）も、検証していないディレクトリからは読まない
        ensure_private_dir(self.dir)
        try:
            with os.fdopen(os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as f:
                _check_own_file(f, self.path)
                st = json.load(f)
        except (OSError, json.JSONDecodeError):
            st = {}
        for k, v in self.fresh().items():
            st.setdefault(k, v)
        return st

    @contextlib.contextmanager
    def txn(self):
        ensure_private_dir(self.dir)
        with open_private(self.path + ".lock") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            st = self.load()
            yield st
            tmp = self.path + ".tmp"
            with open_private(tmp) as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)


# ---- 既出の記録 ----

def fresh_items(st, items):
    """(key, line) のうち未出のものを返し、既出として記録する。記録は SEEN_KEEP 件まで。"""
    seen = set(st["seen"])
    fresh = [(k, line) for k, line in items if k not in seen]
    st["seen"] = (st["seen"] + [k for k, _ in fresh])[-SEEN_KEEP:]
    return fresh


# ---- あなた待ち ----

def ask_add(st, text, pr=None, where=None, cmd=None):
    qid = f"Q{st['next_ask']}"
    st["next_ask"] += 1
    st["asks"].append({"id": qid, "text": text, "pr": pr, "where": where, "cmd": cmd, "at": util.iso()})
    return qid


def ask_done(st, ids):
    ids = set(ids)
    st["asks"] = [q for q in st["asks"] if q["id"] not in ids]


def ask_lines(st):
    out = []
    for q in st["asks"]:
        pr = f"#{q['pr']} " if q.get("pr") else ""
        out.append(f"  {q['id']} {pr}{q['text']}（{q.get('where') or 'manager'}・{util.ago(q['at'])}）")
        if q.get("cmd"):
            out.append(f"     {q['cmd']}")
    return out


# ---- 待ち行列 ----

def enqueue(st, key, **fields):
    """key が同じものは上書きする。W 番号を返す。"""
    for q in st["queue"]:
        if q["key"] == key:
            q.update(fields, at=util.iso())
            return q["id"]
    qid = f"W{st['next_queue']}"
    st["next_queue"] += 1
    st["queue"].append({"id": qid, "key": key, "since": util.iso(), **fields})
    return qid


def dequeue(st, key=None, ids=None):
    ids = set(ids or [])
    st["queue"] = [q for q in st["queue"] if q["key"] != key and q["id"] not in ids]
