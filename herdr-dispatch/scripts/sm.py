"""session-manager の決定論部分。サブコマンドは --help を参照。どれも作業ディレクトリのリポジトリを対象にする。

transcript（~/.claude/projects/<cwd を符号化した名前>/<session>.jsonl）は Claude Code の内部形式で、
将来読めなくなる可能性がある。読むのは type が user / assistant / custom-title / ai-title / pr-link /
permission-mode / last-prompt の行と、各行の cwd / gitBranch / entrypoint / timestamp だけにする。
"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hdlib import config as cfgmod  # noqa: E402
from hdlib import herdr, launch, procs, screen, state, supervisor, util  # noqa: E402
from hdlib.util import emit, iso, local_hm, ago, one_line, parse_ts  # noqa: E402

MANAGER = "session-manager"
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+)-(\d+)\b")
# Claude Code が user として記録するが、人の依頼でもセッションの応答待ちでもないもの
LOCAL_COMMAND_PREFIXES = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                          "<command-name>", "<command-message>")
USER_INTERRUPT_PREFIX = "[Request interrupted by user"
CACHE_VERSION = 3
CODERABBIT_ACTIONABLE = re.compile(
    r"Actionable comments posted: [1-9]|Nitpick comments \(\d+\)|Outside diff range comments \(\d+\)")
FEEDBACK_RE = re.compile(
    r"#(?P<pr>\d+) .*?events=(?P<events>\d+) kinds=(?P<kinds>\S+) by=(?P<by>\S+) "
    r"latest=(?P<latest>\S+) since=(?P<since>\S+)")

RECENT_KEEP = 10
LAUNCHED_KEEP_DAYS = 14
# herdr agent list に一時的に出ないこと（Herdr の再起動中など）で割り当てを捨てないよう、続けて見えないときだけ GONE にする
GONE_AFTER_MISSES = 3

CFG = None


def store(args):
    return state.Store(args.state_dir, {"launched": {}, "degraded": {}, "outdated": {}, "recent": []})


def finish(st, sid, outcome):
    a = st["assignments"].pop(sid, None)
    if a:
        st["recent"].insert(0, {**a, "session": sid, "outcome": outcome, "ended_at": iso()})
        st["recent"] = st["recent"][:RECENT_KEEP]
    return a


def retry_command():
    """この restore / start を、枠を選び直して再実行するコマンド。"""
    out, argv, i = [], sys.argv[1:], 0
    while i < len(argv):
        a = argv[i]
        if a == "--pane":
            out += ["--pane", "auto"]
            i += 2
            continue
        out.append("--pane=auto" if a.startswith("--pane=") else a)
        i += 1
    return " ".join(["python3", shlex.quote(os.path.abspath(__file__))] + [shlex.quote(a) for a in out])


def enqueue(st, meta, reason, detail=None):
    return state.enqueue(st, meta["key"], pr=meta.get("pr"), kind=meta["kind"], title=meta.get("title"),
                         reason=reason, detail=detail, cmd=retry_command())


def slot_title_regex():
    t = CFG.get("review_manager", "slots", "title")
    return "^" + r"\d+".join(re.escape(p) for p in t.split("{k}")) + "$" if t else None


def exclude_title_regex():
    parts = [p for p in (slot_title_regex(), CFG.get("session_manager", "exclude_title")) if p]
    return "|".join(f"(?:{p})" for p in parts) or None


# ---------------------------------------------------------------- transcript index

def encode_project_dir(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def empty_fold_state():
    return {
        "entrypoint": None, "first_cwd": None, "last_cwd": None, "branch": None,
        "custom_title": None, "ai_title": None, "prs": [], "permission_mode": None,
        "last_prompt": None, "last_activity": None, "last_msg": None, "messages": 0,
    }


def message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def fold_line(st, d, repo_slug):
    t = d.get("type")
    if st["entrypoint"] is None and d.get("entrypoint"):
        st["entrypoint"] = d["entrypoint"]
    if d.get("cwd") and not d.get("isSidechain"):
        st["first_cwd"] = st["first_cwd"] or d["cwd"]
        st["last_cwd"] = d["cwd"]
        st["branch"] = d.get("gitBranch") or st["branch"]
    if t == "custom-title":
        st["custom_title"] = d.get("customTitle")
    elif t == "ai-title":
        st["ai_title"] = d.get("aiTitle")
    elif t == "pr-link":
        if (not repo_slug or d.get("prRepository") == repo_slug) and d.get("prNumber") not in st["prs"]:
            st["prs"].append(d.get("prNumber"))
    elif t == "permission-mode":
        st["permission_mode"] = d.get("permissionMode")
    elif t == "last-prompt":
        st["last_prompt"] = (d.get("lastPrompt") or "")[:300]
    elif t in ("user", "assistant") and not d.get("isSidechain"):
        content = (d.get("message") or {}).get("content")
        text = message_text(content)
        # セッション名の通知などの自動挿入とスラッシュコマンドの出力は、ターンの途中を意味しない
        if t == "user" and (d.get("isMeta") or text.startswith(LOCAL_COMMAND_PREFIXES)):
            return
        blocks = content if isinstance(content, list) else []
        st["messages"] += 1
        st["last_activity"] = d.get("timestamp") or st["last_activity"]
        st["last_msg"] = {
            "role": t,
            "api_error": d.get("error") if d.get("isApiErrorMessage") else None,
            "user_interrupt": t == "user" and text.startswith(USER_INTERRUPT_PREFIX),
            "tool_use": any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks),
        }


def interruption(st):
    """最後のターンが終わっていない兆候。終わっていれば None。"""
    m = st.get("last_msg")
    if not m:
        return None
    if m["api_error"]:
        return f"api_error:{m['api_error']}"
    if m.get("user_interrupt"):
        return "user_interrupt"
    if m["role"] == "user":
        return "mid_turn"
    if m["tool_use"]:
        return "mid_tool"
    return None


def load_index(repo_root, repo_slug, state_dir, days):
    state.ensure_private_dir(state_dir)
    cache_path = os.path.join(state_dir, "index-cache.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("version") != CACHE_VERSION or cache.get("repo_slug") != repo_slug:
            cache = None
    except (OSError, json.JSONDecodeError):
        cache = None
    cache = cache or {"version": CACHE_VERSION, "repo_slug": repo_slug, "files": {}}
    files = cache["files"]
    dirty = False

    prefix = encode_project_dir(repo_root)
    cutoff = time.time() - days * 86400
    seen_paths = set()
    for proj in os.listdir(PROJECTS_DIR):
        if not proj.startswith(prefix):
            continue
        pdir = os.path.join(PROJECTS_DIR, proj)
        try:
            names = os.listdir(pdir)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, name)
            try:
                stt = os.stat(path)
            except OSError:
                continue
            if stt.st_mtime < cutoff:
                continue
            seen_paths.add(path)
            ent = files.get(path)
            if ent and ent["size"] == stt.st_size:
                continue
            if not ent or stt.st_size < ent["offset"]:
                ent = {"offset": 0, "state": empty_fold_state()}
            # 追記された分だけ読む。最後の行が書きかけなら次回に回す
            with open(path, "rb") as f:
                f.seek(ent["offset"])
                data = f.read()
            end = data.rfind(b"\n") + 1
            st = ent["state"]
            for raw in data[:end].splitlines():
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(d, dict):
                    fold_line(st, d, repo_slug)
            ent.update({"offset": ent["offset"] + end, "size": stt.st_size, "mtime": stt.st_mtime,
                        "session_id": name[:-len(".jsonl")]})
            files[path] = ent
            dirty = True
    for path in list(files):
        if path not in seen_paths:
            del files[path]
            dirty = True
    if dirty:
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, cache_path)
    return files


def session_rows(files, exclude_title_re, live, entrypoints):
    rows = []
    for path, ent in files.items():
        st = ent["state"]
        if st["entrypoint"] not in entrypoints or st["messages"] == 0:
            continue
        title = st["custom_title"] or st["ai_title"] or ""
        if exclude_title_re and re.search(exclude_title_re, title):
            continue
        sid = ent["session_id"]
        text = " ".join(filter(None, [title, st["branch"], os.path.basename(st["last_cwd"] or "")]))
        rows.append({
            "session_id": sid,
            "title": title,
            "entrypoint": st["entrypoint"],
            "launch_cwd": st["first_cwd"],
            "last_cwd": st["last_cwd"],
            "branch": st["branch"],
            "prs": st["prs"],
            "keys": sorted({f"{p}-{n}" for p, n in KEY_RE.findall(text)}),
            "permission_mode": st["permission_mode"],
            "last_prompt": st["last_prompt"],
            "last_activity": st["last_activity"],
            "transcript_mtime": ent["mtime"],
            "interrupted": interruption(st),
            "live": live.get(sid),
            "transcript": path,
        })
    rows.sort(key=lambda r: r["last_activity"] or "", reverse=True)
    return rows


def load_rows(args, exclude_title=True):
    files = load_index(CFG.repo_root, CFG.slug, args.state_dir, CFG.get("session_manager", "days"))
    return session_rows(files, exclude_title_regex() if exclude_title else None, herdr.live_sessions(),
                        set(CFG.get("session_manager", "entrypoints")))


def number_in(text, num):
    # キーの接頭辞の打ち間違いも拾うため、接頭辞は問わず数字の境界だけ見る
    return bool(re.search(rf"(?<![0-9]){num}(?![0-9])", text or ""))


def rank_for_pr(rows, pr, head_branch=None):
    out = []
    for r in rows:
        linked = pr in r["prs"]
        on_branch = bool(head_branch) and r["branch"] == head_branch
        if not (linked or on_branch):
            continue
        # 計画用のセッションは多数の PR に pr-link を持つ。その PR 専用に動いたセッションを優先する
        dedicated = len(r["prs"]) <= 2
        out.append(((on_branch, linked, dedicated, r["last_activity"] or ""), r))
    out.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in out]


# ---------------------------------------------------------------- info / find / slots

def cmd_info(args):
    lc = CFG.launch("session_manager")
    print(json.dumps({
        "config": CFG.path, "config_found": os.path.exists(CFG.path), "repo": CFG.slug, "repo_configured": CFG.matched,
        "state_dir": args.state_dir, "notes": CFG.path_of(CFG.get("session_manager", "notes")),
        "launch": lc, "health": CFG.health("session_manager"), "exclude_title": exclude_title_regex(),
    }, ensure_ascii=False, indent=1))


def cmd_find(args):
    rows = load_rows(args)
    if args.session:
        rows = [r for r in rows if r["session_id"].startswith(args.session)]
    if args.pr:
        r = subprocess.run(["gh", "pr", "view", str(args.pr), "--json", "headRefName", "-q", ".headRefName"],
                           capture_output=True, **util.TEXT, cwd=CFG.repo_root)
        rows = rank_for_pr(rows, args.pr, r.stdout.strip() or None if r.returncode == 0 else None)
    if args.key:
        num = re.sub(r"^\D+", "", args.key)
        rows = [r for r in rows if number_in(" ".join([r["title"], r["branch"] or "", r["last_cwd"] or ""]), num)]
    if args.query:
        q = args.query.lower()
        rows = [r for r in rows if q in " ".join(
            [r["title"], r["branch"] or "", r["last_cwd"] or "", r["last_prompt"] or ""]).lower()]
    if args.not_live:
        rows = [r for r in rows if not r["live"]]
    if args.interrupted:
        rows = [r for r in rows if r["interrupted"]]
    json.dump(rows[:args.limit], sys.stdout, ensure_ascii=False, indent=1)
    print()


def reserved_panes(st):
    """restore / start が起動中で、まだ agent が現れていないペイン。"""
    return {a["pane"] for a in st["assignments"].values() if a.get("phase") == "starting"}


def free_panes(tab, strict=True):
    me = os.environ.get("HERDR_PANE_ID")
    ok, res = herdr.call("pane", "list")
    if not ok or not isinstance(res, dict):
        if strict:
            sys.exit(f"herdr pane list failed: {res}")
        return []
    # review-manager の枠は空いていても使わない
    pool_re = cfgmod.label_regex(CFG.get("review_manager", "slots", "label"))
    prompt_re = CFG.get("shell", "prompt_regex")
    out = []
    for p in res.get("panes", []):
        if p.get("tab_id") != tab or p["pane_id"] == me or p.get("agent"):
            continue
        if pool_re.match(p.get("label") or ""):
            continue
        is_prompt, last = screen.at_prompt(p["pane_id"], prompt_re)
        out.append({"pane_id": p["pane_id"], "label": p.get("label"), "cwd": p.get("foreground_cwd"),
                    "at_prompt": is_prompt, "last_line": last[-80:]})
    return out


def usable_panes(st):
    reserved = reserved_panes(st)
    return [p["pane_id"] for p in free_panes(os.environ.get("HERDR_TAB_ID"), strict=False)
            if p["at_prompt"] and p["pane_id"] not in reserved]


def cmd_slots(args):
    reserved = reserved_panes(store(args).load())
    slots = free_panes(args.tab or os.environ.get("HERDR_TAB_ID"))
    for p in slots:
        p["reserved"] = p["pane_id"] in reserved
    json.dump(slots, sys.stdout, ensure_ascii=False, indent=1)
    print()


# ---------------------------------------------------------------- restore / start

def restore_warnings(r, busy_sec):
    """(警告, 強制しても無視できない警告か) の一覧。"""
    w = []
    if r["live"]:
        w.append((f"live: {r['live']['pane_id']} で動いている", True))
    age = time.time() - r["transcript_mtime"]
    if not r["live"] and age < busy_sec:
        # 2 つのプロセスが同じ transcript に書くと会話が壊れる
        w.append((f"transcript が {int(age)} 秒前に更新された。Herdr の外（Desktop や別のターミナル）で動いている疑い", True))
    if not r["launch_cwd"] or not os.path.isdir(r["launch_cwd"]):
        w.append((f"起動ディレクトリが無い: {r['launch_cwd']}（worktree を撤去した可能性）", True))
    if r["entrypoint"] != "cli":
        w.append((f"entrypoint={r['entrypoint']}。Desktop のセッションを CLI で再開する経路は実機で確かめていない", False))
    return w


def request_template(kind):
    return CFG.text(CFG.get("session_manager", "requests", kind))


def build_request(args, r):
    where = f"作業ディレクトリは {r['last_cwd']}（ブランチ {r['branch']}）。"
    if args.request == "review":
        m = FEEDBACK_RE.search(args.feedback or "")
        if not m:
            sys.exit("--request review には watch の FEEDBACK 行を --feedback で渡す")
        return one_line(util.fill(request_template("review"), where=where, **m.groupdict()))
    text = one_line(util.fill(request_template("resume"), where=where, state=r["interrupted"] or "ターンは完了",
                              last_prompt=one_line(r["last_prompt"])[:200]))
    return text + " " + one_line(args.extra) if args.extra else text


UNATTENDED_DENIED_MODES = {"bypassPermissions"}
UNTRUSTED_RE = re.compile(r"\buntrusted=(\d+)\b")


def restore_mode(previous, request, configured, feedback=None):
    """--request review は watch の FEEDBACK から manager が自動で起こし、他者の書いた指摘を読ませる。
    ユーザーが見ていない起動で確認無しの権限を引き継がないよう、前回の mode が bypassPermissions なら設定の mode に下げる。
    PR に外部の人の書き込みがある（FEEDBACK の untrusted が 1 以上、または行に untrusted が無い）ときは、
    セッションがそれも読むので default に下げ、操作のたびにユーザーの承認を求めさせる。"""
    if request != "review":
        return previous
    m = UNTRUSTED_RE.search(feedback or "")
    if not m or int(m.group(1)) > 0:
        return previous if previous in ("plan", "default") else "default"
    if previous not in UNATTENDED_DENIED_MODES:
        return previous
    return "default" if configured in UNATTENDED_DENIED_MODES else configured


def cmd_restore(args):
    rows = [r for r in load_rows(args, exclude_title=False) if r["session_id"].startswith(args.session_id)]
    if len(rows) != 1:
        sys.exit(f"session {args.session_id}: {len(rows)} 件一致（1 件に絞れる ID を渡す）")
    r = rows[0]
    sid = r["session_id"]
    lc = CFG.launch("session_manager")
    mode = restore_mode(r["permission_mode"], args.request, lc["permission_mode"], args.feedback)
    command = launch.build_command(lc, r["launch_cwd"] or ".", launch.claude_args(resume=sid, permission_mode=mode))
    name = args.name or (f"pr-{r['prs'][-1]}" if len(r["prs"]) == 1 else f"s-{sid[:8]}")
    request = build_request(args, r) if args.request != "none" else None
    warnings = restore_warnings(r, args.busy_sec)
    blocking = [w for w, hard in warnings if hard or not args.accept_warnings]

    if args.dry_run:
        json.dump({"session_id": sid, "title": r["title"], "command": command, "name": name,
                   "warnings": [w for w, _ in warnings], "blocking": blocking, "request": request},
                  sys.stdout, ensure_ascii=False, indent=1)
        print()
        return
    if blocking:
        emit("WARN", session=sid[:8], reasons=json.dumps(blocking, ensure_ascii=False))
        return
    m = FEEDBACK_RE.search(args.feedback or "")
    pr = int(m.group("pr")) if m else (r["prs"][-1] if len(r["prs"]) == 1 else None)
    meta = {"key": f"restore:{sid}", "kind": args.request, "pr": pr, "title": r["title"], "mode": mode}
    run_launch(args, command, sid, name, request, meta)


def cmd_start(args):
    sid = str(uuid.uuid4())
    cwd = os.path.abspath(args.cwd or CFG.repo_root)
    lc = CFG.launch("session_manager")
    mode = args.permission_mode or lc["permission_mode"]
    command = launch.build_command(lc, cwd, launch.claude_args(session_id=sid, title=args.title, permission_mode=mode))
    if args.prompt:
        request = one_line(args.prompt)
    elif args.key:
        request = one_line(util.fill(request_template("start"), key=args.key))
    else:
        sys.exit("start には --prompt か --key が要る")
    name = args.name or (args.key.lower() if args.key else None)
    if not name:
        sys.exit("start には --name が要る（--key を渡せばキーの小文字が既定）")
    if args.dry_run:
        json.dump({"session_id": sid, "command": command, "name": name, "request": request},
                  sys.stdout, ensure_ascii=False, indent=1)
        print()
        return
    meta = {"key": f"start:{args.title}", "kind": "start", "pr": None, "title": args.title, "mode": mode}
    run_launch(args, command, sid, name, request, meta)


def reserve_pane(args, sid, meta, request, start_timeout):
    """枠を選んで割り当てを記録する。--pane auto で空きが無ければ待ち行列に積んで None を返す。"""
    with store(args).txn() as st:
        slots = usable_panes(st)
        if args.pane == "auto":
            if not slots:
                emit("NO_SLOT", queued=enqueue(st, meta, "no_slot", "空いている枠が無い"))
                return None
            pane = slots[0]
        elif args.pane in slots:
            pane = args.pane
        else:
            emit("SLOT_BUSY", pane=args.pane)
            return None
        st["assignments"][sid] = {
            "pane": pane, "name": None, "pr": meta.get("pr"), "kind": meta["kind"], "title": meta.get("title"),
            "key": meta["key"], "request": request, "phase": "starting", "at": iso(),
            "stale_at": time.time() + start_timeout + 300,
        }
    return pane


def update_assignment(args, sid, **kw):
    with store(args).txn() as st:
        a = st["assignments"].get(sid)
        if a:
            a.update(kw)


def launched_record(pane, name, meta):
    lc = CFG.launch("session_manager")
    hc = CFG.health("session_manager")
    return {"pane": pane, "name": name, "mode": meta.get("mode"), "title": meta.get("title"), "at": iso(),
            "require_env": hc["require_env"], "check_mode": hc["check_mode"], "cli": lc["cli"]}


def run_launch(args, command, sid, name, request, meta):
    """空きペインで command を実行し、agent の検出・ID の照合・命名・依頼の送信までを行って結果を 1 行出す。

    依頼したターンの終わりは待たない。watch が割り当て中のペインの状態変化を見て SETTLED を出す。
    """
    lc = CFG.launch("session_manager")
    start_timeout = args.start_timeout or lc["start_timeout"]
    pane = reserve_pane(args, sid, meta, request, start_timeout)
    if not pane:
        return
    outcome, detail = launch.run_until_agent(pane, command, start_timeout, CFG.get("shell", "prompt_regex"))
    if outcome != "agent":
        with store(args).txn() as st:
            finish(st, sid, outcome)
            qid = enqueue(st, meta, outcome, one_line(detail)[-200:]) if outcome != "error" else None
        if outcome == "pane_busy":
            emit("PANE_BUSY", pane=pane, queued=qid, last_line=json.dumps(detail, ensure_ascii=False))
        elif outcome == "start_failed":
            emit("START_FAILED", pane=pane, queued=qid, tail=json.dumps(detail, ensure_ascii=False))
        elif outcome == "timeout":
            emit("TIMEOUT", stage="start", pane=pane, queued=qid, last_line=json.dumps(detail, ensure_ascii=False))
        else:
            emit("ERROR", stage="pane_run", detail=json.dumps(detail, ensure_ascii=False))
        return
    got, agent = launch.wait_session(pane, detail)
    if got is None:
        # ここで MISMATCH にすると、ユーザーが信頼確認に答えた後のセッションを誰も引き取らない。watch が ID の出現を待って引き取る
        tag = "BLOCKED" if agent.get("agent_status") == "blocked" else "TIMEOUT"
        update_assignment(args, sid, phase="trust", want_name=name, launch_meta=meta)
        emit(tag, stage="trust", pane=pane, session=sid, request="pending" if request else None)
        return
    if got != sid:
        with store(args).txn() as st:
            finish(st, sid, "mismatch")
        emit("MISMATCH", pane=pane, expected=sid, got=got)
        return

    name = launch.name_agent(pane, name)
    with store(args).txn() as st:
        st["launched"][sid] = launched_record(pane, name, meta)
        if sid in st["assignments"]:
            st["assignments"][sid].update(phase="launching", name=name)

    status, agent = launch.wait_idle(name, 120)
    if status not in ("idle", "done"):
        tag = "BLOCKED" if status == "blocked" else "TIMEOUT"
        if request:
            # 起動時のダイアログにユーザーが答えて idle になったら、watch が依頼を送る
            update_assignment(args, sid, phase="startup", seq=(agent or {}).get("state_change_seq"))
            emit(tag, stage="startup", name=name, pane=pane, session=sid, request="pending")
        else:
            with store(args).txn() as st:
                finish(st, sid, "startup_" + tag.lower())
            emit(tag, stage="startup", name=name, pane=pane, session=sid)
        return
    if not request:
        with store(args).txn() as st:
            finish(st, sid, "restored")
            state.dequeue(st, meta["key"])
        emit("RESTORED", name=name, pane=pane, session=sid)
        return

    ok, res = herdr.call("agent", "prompt", name, request)
    if not ok:
        # 届いたか分からない。再送はしない
        update_assignment(args, sid, phase="uncertain")
        emit("PROMPT_UNCERTAIN", name=name, pane=pane, session=sid,
             code=res.get("code") if isinstance(res, dict) else res)
        return
    _, agent = herdr.status_of(res)
    with store(args).txn() as st:
        if sid in st["assignments"]:
            st["assignments"][sid].update(phase="sent", seq=agent.get("state_change_seq"), sent_at=iso())
        state.dequeue(st, meta["key"])
    emit("SENT", name=name, pane=pane, session=sid)


# ---------------------------------------------------------------- watch

PR_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes { ... on PullRequest {
      number title headRefName isDraft
      reviews(last: 50) { totalCount nodes { id author { login __typename } authorAssociation state submittedAt body
                                              comments { totalCount } } }
      comments(last: 50) { totalCount nodes { id author { login __typename } authorAssociation createdAt lastEditedAt body } }
    } }
  }
}
"""


def feedback_events(pr, me, coderabbit, ignored, markers=()):
    """自分以外からの、対応を要しうるレビュー・コメント。

    markers は CI のレビューのコメントの先頭に付く文字列。bot が書いたものに限り、作者が ignored でも数える。"""
    ev = []
    for rv in pr["reviews"]["nodes"]:
        who = (rv.get("author") or {}).get("login") or ""
        if who == me or who in ignored:
            continue
        st, body, ncom = rv["state"], rv.get("body") or "", rv["comments"]["totalCount"]
        if coderabbit and who == coderabbit:
            # 本文の無い review は自分の返信へのスレッド内の返答。新しい指摘は本文に件数が出る
            if not CODERABBIT_ACTIONABLE.search(body):
                continue
            kind = "coderabbit"
        elif st == "APPROVED":
            kind = "approved"
        elif st == "CHANGES_REQUESTED":
            kind = "changes_requested"
        elif st == "COMMENTED" and (body.strip() or ncom):
            kind = "review_comment"
        else:
            continue
        ev.append({"id": rv["id"], "at": rv["submittedAt"], "by": who, "kind": kind, **_trust_fields(rv)})
    for c in pr["comments"]["nodes"]:
        who = (c.get("author") or {}).get("login") or ""
        if who == me:
            continue
        trust = _trust_fields(c)
        # マーカーを書けば人も名乗れるので、bot の書いたものだけを CI のレビューとみなす
        if markers and trust["bot"] and (c.get("body") or "").lstrip().startswith(tuple(markers)):
            # CI のレビューは push のたびに同じコメントを書き換える。作成日時では 2 回目以降が既出に見え、
            # id だけでは既出判定のキーが変わらないので、最後の編集日時で新しさを見てキーにも入れる
            at = c.get("lastEditedAt") or c["createdAt"]
            ev.append({"id": f"{c['id']}:{at}", "at": at, "by": who, "kind": "ci_review", **trust})
            continue
        # CodeRabbit の会話欄のコメントは walkthrough の要約と、こちらのコマンドへの返答だけ
        if who in ignored or (coderabbit and who == coderabbit):
            continue
        ev.append({"id": c["id"], "at": c["createdAt"], "by": who, "kind": "comment", **trust})
    ev.sort(key=lambda e: e["at"])
    return ev


def _trust_fields(node):
    return {"assoc": node.get("authorAssociation"), "bot": (node.get("author") or {}).get("__typename") == "Bot"}


def is_trusted(e, fb):
    """FEEDBACK は復元したセッションに本文を読ませる。誰でもコメントできる公開リポジトリで、
    外部の人の書いた指示を、復元したセッションの権限で実行させないため。
    bot（GitHub App）はリポジトリの管理者が入れたものとして信頼する（review-manager と同じ）。"""
    return (e["kind"] == "coderabbit" or e.get("bot") or e["by"] in set(fb.get("trusted_authors") or [])
            or e.get("assoc") in set(fb.get("trusted_associations") or []))


def count_untrusted(pr, me, fb):
    """PR の会話のうち、信頼できない書き手のものの数。復元したセッションは gh で会話をまとめて読むので、
    対応を要するイベントに絞らず、state・本文・ignored_authors を問わず全件で数える。
    取得の上限（最新 50 件）を超えていて数えきれなければ "unknown"。"""
    if any(len(pr[k]["nodes"]) < pr[k].get("totalCount", 0) for k in ("reviews", "comments")):
        return "unknown"
    n = 0
    for node in pr["reviews"]["nodes"] + pr["comments"]["nodes"]:
        who = (node.get("author") or {}).get("login") or ""
        if who and who == me:
            continue
        if not is_trusted({"kind": "-", "by": who, **_trust_fields(node)}, fb):
            n += 1
    return n


def poll(args, me):
    """(既出判定のキー, 出す行) の一覧。取得に失敗したら WATCH_ERROR を出して None。"""
    r = subprocess.run(["gh", "api", "graphql", "-f", f"query={PR_QUERY}",
                        "-F", f"q=repo:{CFG.slug} is:pr is:open author:{me}"],
                       capture_output=True, **util.TEXT, cwd=CFG.repo_root)
    if r.returncode != 0:
        print(f"WATCH_ERROR rc={r.returncode} {r.stderr.strip()[-200:]}", flush=True)
        return None
    try:
        prs = json.loads(r.stdout)["data"]["search"]["nodes"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"WATCH_ERROR {e}", flush=True)
        return None
    fb = CFG.get("session_manager", "feedback")
    try:
        rows = load_rows(args)
    except herdr.Unreachable as e:
        # live を空とみなすと、ペインで動いているセッションまで live=- で出し、manager に二重に復元させる。
        # 既出にしないので、届くようになった回でもう一度出る
        print(herdr.unreachable_line(e), flush=True)
        return None
    out = []
    for pr in prs:
        events = feedback_events(pr, me, fb.get("coderabbit_login"), set(fb.get("ignored_authors") or []),
                                 tuple(fb.get("review_comment_markers") or ()))
        if not events:
            continue
        cands = rank_for_pr(rows, pr["number"], pr["headRefName"])
        sess = cands[0] if cands else None
        since = parse_ts(sess["last_activity"]) if sess else None
        new = [e for e in events if since is None or parse_ts(e["at"]) > since]
        untrusted = [e for e in new if not is_trusted(e, fb)]
        if untrusted:
            out.append((f"untrusted:{pr['number']}@{untrusted[-1]['id']}",
                        f"UNTRUSTED_FEEDBACK #{pr['number']} events={len(untrusted)} "
                        f"by={','.join(sorted({e['by'] for e in untrusted}))} latest={untrusted[-1]['at']} "
                        f"title={util.quote_untrusted(pr['title'], 50)}"))
        new = [e for e in new if is_trusted(e, fb)]
        if not new:
            continue
        kinds = sorted({e["kind"] for e in new})
        who = ",".join(sorted({e["by"] for e in new}))
        tag = "APPROVED" if kinds == ["approved"] else "FEEDBACK"
        live = sess["live"] if sess else None
        n_untrusted = count_untrusted(pr, me, fb)
        out.append((f"{pr['number']}@{new[-1]['id']}",
                    f"{tag} #{pr['number']} session={sess['session_id'] if sess else 'none'} "
                    f"live={live['pane_id'] + ':' + live['agent_status'] if live else '-'} "
                    f"events={len(new)} kinds={','.join(kinds)} by={who} latest={new[-1]['at']} "
                    f"since={sess['last_activity'] if sess else '-'} untrusted={n_untrusted} "
                    f"title={util.quote_untrusted(pr['title'], 50)}"))
    return out


def assignment_label(sid, a):
    return (f"session={sid[:8]} pane={a.get('pane')} name={a.get('name')} "
            f"pr={'#' + str(a['pr']) if a.get('pr') else '-'} kind={a['kind']}")


def adopt_after_trust(st, sid, a, live):
    """信頼確認ダイアログで止まった起動を、セッション ID が出たところで引き取る。(events, startup の処理へ進むか)。"""
    if sid not in live:
        other = next((s for s, v in live.items() if v["pane_id"] == a["pane"]), None)
        if other:
            finish(st, sid, "mismatch")
            return [f"MISMATCH {assignment_label(sid, a)} got={other}"], False
        if not (herdr.pane_get(a["pane"]) or {}).get("agent"):
            a["misses"] = a.get("misses", 0) + 1
            if a["misses"] >= GONE_AFTER_MISSES:
                finish(st, sid, "gone")
                return [f"GONE {assignment_label(sid, a)} reason=closed_before_session"], False
        return [], False
    a["misses"] = 0
    pane = live[sid]["pane_id"]
    name = launch.name_agent(pane, a.get("want_name") or f"s-{sid[:8]}")
    st["launched"][sid] = launched_record(pane, name, a.get("launch_meta") or {})
    a.update(name=name, pane=pane)
    if not a.get("request"):
        finish(st, sid, "restored")
        state.dequeue(st, a.get("key"))
        return [f"RESTORED {assignment_label(sid, a)} stage=trust"], False
    a.update(phase="startup", seq=None)
    return [], True


def check_assignments(st, live):
    events = []
    for sid, a in list(st["assignments"].items()):
        phase = a.get("phase")
        if phase in ("starting", "launching"):
            # restore / start のプロセスが進めている。プロセスが落ちて残った記録だけ片付ける
            if time.time() > a.get("stale_at", 0):
                finish(st, sid, "launch_stale")
                events.append(f"GONE {assignment_label(sid, a)} reason=launch_stale")
            continue
        if phase == "trust":
            ev, proceed = adopt_after_trust(st, sid, a, live)
            events += ev
            if not proceed:
                continue
            phase = "startup"
        lv = live.get(sid)
        if not lv:
            a["misses"] = a.get("misses", 0) + 1
            if a["misses"] >= GONE_AFTER_MISSES:
                finish(st, sid, "gone")
                events.append(f"GONE {assignment_label(sid, a)} reason=not_in_herdr")
            continue
        a["misses"] = 0
        a["pane"] = lv["pane_id"]
        status, seq = lv["agent_status"], lv["seq"]
        if phase == "startup":
            if status in ("idle", "done"):
                ok, res = herdr.call("agent", "prompt", lv["pane_id"], a["request"])
                if ok:
                    a.update(phase="sent", seq=herdr.status_of(res)[1].get("state_change_seq"), sent_at=iso(),
                             last_event=f"{local_hm(iso())} 起動時の停止が解けたので依頼を送った")
                    state.dequeue(st, a.get("key"))
                else:
                    a["phase"] = "uncertain"
                    events.append(f"PROMPT_UNCERTAIN {assignment_label(sid, a)} stage=startup")
            elif status == "blocked" and seq != a.get("seq"):
                a["seq"] = seq
                events.append(f"SETTLED {assignment_label(sid, a)} status=blocked stage=startup")
            continue
        if phase not in ("sent", "blocked"):
            continue
        if status not in ("idle", "done", "blocked") or seq == a.get("seq"):
            continue
        a["seq"] = seq
        a["last_event"] = f"{local_hm(iso())} {status}"
        events.append(f"SETTLED {assignment_label(sid, a)} status={status}")
        if status == "blocked":
            a["phase"] = "blocked"
        else:
            # 依頼したターンが終わった。以降はユーザーがペインで引き継ぐ
            finish(st, sid, "done")
    return events


def check_version_enabled():
    return CFG.health("session_manager").get("check_version", True)


def check_health(st, live, pmap=None):
    """restore / start で起こしたセッションの劣化（Herdr の再起動で素の claude --resume になった など）と古い版。"""
    events = []
    cutoff = util.now() - dt.timedelta(days=LAUNCHED_KEEP_DAYS)
    for sid in [s for s, l in st["launched"].items() if (parse_ts(l.get("at")) or cutoff) < cutoff]:
        del st["launched"][sid]
        st["degraded"].pop(sid, None)
        st["outdated"].pop(sid, None)
    targets = [(sid, l, live[sid]) for sid, l in st["launched"].items()
               if sid in live and st["assignments"].get(sid, {}).get("phase") not in ("starting", "launching")]
    if not targets:
        return events
    pmap = procs.claude_procs_by_pane() if pmap is None else pmap
    installed = procs.installed_claude_version() if check_version_enabled() else None
    for sid, l, lv in targets:
        pane = lv["pane_id"]
        l["pane"] = pane
        info = herdr.agent_get(pane)
        if info is not None and info.get("name") is None and l.get("name"):
            herdr.call("agent", "rename", pane, l["name"])
        reasons = procs.health_reasons(pane, l.get("require_env") or [],
                                       l.get("mode") if l.get("check_mode", True) else None, pmap)
        key = " / ".join(reasons)
        if reasons and st["degraded"].get(sid) != key:
            events.append(f"DEGRADED session={sid[:8]} pane={pane} name={l.get('name')} "
                          f"reasons={json.dumps(key, ensure_ascii=False)}")
        if reasons:
            st["degraded"][sid] = key
        else:
            st["degraded"].pop(sid, None)
        # ユーザーの作業セッションなので知らせるだけにする。立ち上げ直すとバックグラウンドのシェル（emulator など）も消える。
        # 作業中に知らせても立ち上げ直せないので、idle / done のときだけ知らせる
        gap = procs.version_gap(pane, pmap, installed)
        vkey = f"{gap[0]}->{gap[1]}" if gap else None
        if gap and lv.get("agent_status") in ("idle", "done") and st["outdated"].get(sid) != vkey:
            st["outdated"][sid] = vkey
            events.append(f"OUTDATED session={sid[:8]} pane={pane} name={l.get('name')} "
                          f"running={gap[0]} installed={gap[1]}")
        elif not gap:
            st["outdated"].pop(sid, None)
    return events


def check_queue(st):
    ready = [q for q in st["queue"] if q.get("reason") == "no_slot"]
    if not ready:
        st.pop("queue_ready_key", None)
        return []
    free = usable_panes(st)
    if not free:
        st.pop("queue_ready_key", None)
        return []
    key = ",".join(q["id"] for q in ready) + "|" + ",".join(free)
    if st.get("queue_ready_key") == key:
        return []
    st["queue_ready_key"] = key
    return [f"QUEUE_READY panes={','.join(free)} queue={','.join(q['id'] for q in ready)}"]


def cmd_watch(args):
    if not CFG.slug:
        sys.exit("origin の URL から GitHub のリポジトリを特定できない")
    me = util.run(["gh", "api", "user", "-q", ".login"]).stdout.strip()
    if not me:
        # 空のまま進むと自分のコメントまでレビューとして数える
        sys.exit("gh api user でログイン名を取れない（gh auth status を確認）")
    s = store(args)
    if not args.once:
        supervisor.install_sigterm()
        with s.txn() as st:
            if args.reset:
                st["seen"] = []
            supervisor.register(st, [os.path.basename(__file__), " watch"])
    next_poll = next_health = 0.0
    errors = herdr_errors = 0
    while True:
        events = []
        if not args.once:
            try:
                live = herdr.live_sessions()
                herdr_errors = 0
            except herdr.Unreachable as e:
                # 届かない間は割り当ても劣化も判定しない。misses を数えると、動いている割り当てを GONE にする
                live = None
                herdr_errors += 1
                if herdr_errors == 1 or herdr_errors >= args.max_watch_errors:
                    print(herdr.unreachable_line(e, herdr_errors), flush=True)
                if args.exit_on_event and herdr_errors >= args.max_watch_errors:
                    return
            do_health = time.time() >= next_health
            if do_health:
                next_health = time.time() + args.health_interval
            with s.txn() as st:
                if not supervisor.beat(st):
                    return
                if live is not None:
                    events += check_assignments(st, live)
                    if do_health:
                        pmap = procs.claude_procs_by_pane()
                        events += check_health(st, live, pmap)
                        events += check_queue(st)
                        if check_version_enabled():
                            events += supervisor.self_outdated_events(
                                st, pmap, procs.installed_claude_version(), CFG.launch("session_manager")["cli"], live)
        if time.time() >= next_poll:
            next_poll = time.time() + max(args.interval, 60)
            items = poll(args, me)
            if items is None:
                errors += 1
                if args.exit_on_event and errors >= args.max_watch_errors:
                    return
            else:
                errors = 0
                with s.txn() as st:
                    events += [line for _, line in state.fresh_items(st, items)]
        for e in events:
            print(e, flush=True)
        if args.once or (events and args.exit_on_event):
            return
        time.sleep(args.slot_interval)


# ---------------------------------------------------------------- status / ask / queue / unassign

def cmd_status(args):
    st = store(args).load()
    try:
        live, herdr_error = herdr.live_sessions(), None
    except herdr.Unreachable as e:
        live, herdr_error = {}, str(e)
    if args.json:
        print(json.dumps({**st, "live": None if herdr_error else live, "herdr_error": herdr_error},
                         ensure_ascii=False, indent=1, default=str))
        return
    print(f"session-manager 状況（{local_hm(iso())}・{CFG.slug}）")
    print(supervisor.status_line(st, max(args.stale_sec, 120)))
    launched = [(sid, l) for sid, l in st["launched"].items() if sid in live]
    pmap = installed = None
    if check_version_enabled() and not herdr_error:
        pmap, installed = procs.claude_procs_by_pane(), procs.installed_claude_version()
        line = supervisor.self_version_line(pmap, installed, CFG.launch("session_manager")["cli"], live)
        if line:
            print(line)
    if herdr_error:
        print(f"⚠ Herdr に届かない（{herdr_error}）。ペインの状態は不明。sandbox の中なら外で実行し直す")
    print("割り当て:" + ("" if st["assignments"] else " なし"))
    for sid, a in st["assignments"].items():
        lv = live.get(sid) or {}
        pane = lv.get("pane_id") or a.get("pane")
        pr = f"#{a['pr']} " if a.get("pr") else ""
        sent = a.get("sent_at") or a.get("at")
        where = lv.get("agent_status") or ("不明" if herdr_error else "不在")
        print(f"  {a.get('name') or '-'} {pane} {where}  {pr}{a['kind']}  段階 {a.get('phase')}"
              f"  {local_hm(sent)}（{ago(sent)}）")
        print(f"     resume: claude --resume {sid}")
        if a.get("last_event"):
            print(f"     直近: {a['last_event']}")
        if lv:
            text, sugg = screen.input_box(pane)
            if text and not sugg:
                print(f"     入力欄（未送信）: {text[:80]}")
    print("起こしたセッション（劣化の監視対象・ペインで動いているもの）:"
          + (" 不明" if herdr_error else "" if launched else " なし"))
    for sid, l in launched:
        lv = live[sid]
        print(f"  {l.get('name')} {lv['pane_id']} {lv['agent_status']}  {l.get('title') or ''}  resume: {sid}")
        if st["degraded"].get(sid):
            print(f"     ⚠ 劣化: {st['degraded'][sid]}")
        gap = procs.version_gap(lv["pane_id"], pmap, installed)
        if gap:
            print(f"     ⚠ 古い版: {gap[0]} で動いている（インストール済みは {gap[1]}）。手の空いたときに立ち上げ直す")
    print("待ち行列:" + ("" if st["queue"] else " なし"))
    for q in st["queue"]:
        pr = f"#{q['pr']} " if q.get("pr") else ""
        print(f"  {q['id']} {pr}{q['kind']} {q.get('title') or ''}  理由 {q.get('reason')}（{ago(q.get('at') or q['since'])}）")
        if q.get("detail"):
            print(f"     {q['detail'][:120]}")
        print(f"     {q.get('cmd')}")
    print("あなた待ち:" + ("" if st["asks"] else " なし"))
    for line in state.ask_lines(st):
        print(line)
    if st["recent"]:
        print("最近終わった割り当て:")
        for a in st["recent"][:5]:
            pr = f"#{a['pr']} " if a.get("pr") else ""
            print(f"  {local_hm(a['ended_at'])} {a.get('name') or '-'} {pr}{a['kind']} → {a['outcome']}")


def cmd_ask(args):
    with store(args).txn() as st:
        if args.op == "add":
            if not args.text:
                sys.exit("ask add には質問の文が要る")
            print(state.ask_add(st, " ".join(args.text), pr=args.pr, where=args.pane, cmd=args.cmd))
            return
        if args.op == "done":
            state.ask_done(st, args.text)
        for line in state.ask_lines(st):
            print(line)


def cmd_queue(args):
    with store(args).txn() as st:
        if args.op == "rm":
            state.dequeue(st, ids=args.ids)
        for q in st["queue"]:
            print(f"{q['id']} {q['kind']} {'#' + str(q['pr']) if q.get('pr') else ''} {q.get('reason')}\n   {q.get('cmd')}")


def cmd_unassign(args):
    with store(args).txn() as st:
        sids = [s for s in st["assignments"] if s.startswith(args.session)]
        if len(sids) != 1:
            sys.exit(f"割り当て {args.session}: {len(sids)} 件一致")
        a = finish(st, sids[0], "unassigned")
    print(f"{a.get('name')} の割り当てを外した（{sids[0][:8]}）")


def cmd_import_state(args):
    """以前の版の state.json（割り当て・起こしたセッション・あなた待ち・既出）を取り込む。

    待ち行列とあなた待ちの cmd は manager やユーザーがそのまま実行するので取り込まない。以前の版の cmd は
    以前のスクリプトを指していて、切り替え後は使えないこともある。待ち行列は一覧を出し、manager が出し直す。"""
    try:
        old = state.read_own_json(os.path.join(args.src, state.STATE_FILE))
    except state.InsecureStateDir as e:
        util.die(f"取り込まない: {e}")
    hc = CFG.health("session_manager")
    with store(args).txn() as st:
        if st["assignments"] or st["launched"]:
            sys.exit("取り込み先に割り当てか起こしたセッションがある。取り込みは空の状態にだけ行う")
        for k in ("assignments", "launched", "asks", "next_ask", "seen", "degraded", "recent"):
            if k in old:
                st[k] = old[k]
        for l in st["launched"].values():
            l.setdefault("require_env", hc["require_env"])
            l.setdefault("check_mode", hc["check_mode"])
        for q in st["asks"]:
            q.setdefault("where", q.pop("pane", None))
            q["cmd"] = None
    print(f"取り込んだ: 割り当て {len(st['assignments'])}・起こしたセッション {len(st['launched'])}・"
          f"あなた待ち {len(st['asks'])}（cmd は除いた）・既出 {len(st['seen'])}")
    for q in old.get("queue", []):
        pr = f"#{q['pr']} " if q.get("pr") else ""
        print(f"取り込まなかった待ち行列: {pr}{q.get('kind')} {q.get('title') or ''}（理由 {q.get('reason')}）。"
              "必要なら restore / start を出し直す")
    print("前の watch は止まらないので、TaskStop で止めてから新しい watch を張る")


def main():
    global CFG
    ap = argparse.ArgumentParser(description="session-manager の CLI")
    ap.add_argument("--repo-dir", default=os.getcwd())
    ap.add_argument("--state-dir", help="既定は設定の state_dir/session-manager")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="解決した設定（起動コマンド・承認の案内・notes・state の置き場）を出す").set_defaults(func=cmd_info)

    f = sub.add_parser("find", help="再開候補のセッションを探す（JSON）。初回は数秒かかり、以降は transcript の追記分だけ読む")
    f.add_argument("--pr", type=int, help="ブランチ一致 > pr-link > 専用（PR 2 件以下）> 新しい順に並べる")
    f.add_argument("--key", help="課題キー（ABC-123）。数字で照合し、接頭辞の打ち間違いも拾う")
    f.add_argument("--query", help="タイトル・ブランチ・cwd・最後の依頼の部分一致")
    f.add_argument("--session", help="セッション ID の前方一致")
    f.add_argument("--not-live", action="store_true", help="Herdr のペインで動いていないものだけ")
    f.add_argument("--interrupted", action="store_true",
                   help="最後のターンが終わっていないもの（api_error:<種類> / user_interrupt / mid_turn / mid_tool）。"
                        "ユーザーが閉じたセッションはターンが終わっていて該当しない")
    f.add_argument("--limit", type=int, default=10)
    f.set_defaults(func=cmd_find)

    s = sub.add_parser("slots", help="このタブの、agent がいないペイン（JSON）。at_prompt: true かつ reserved: false のものだけ使える")
    s.add_argument("--tab")
    s.set_defaults(func=cmd_slots)

    rs = sub.add_parser("restore", help="空きペインでセッションを再開し、依頼を送ったところで結果を 1 行で返す")
    rs.add_argument("session_id", help="前方一致可")
    rs.add_argument("--pane", help="ペイン ID か auto（空きを選び、無ければ待ち行列に積む）。--dry-run 以外で必須")
    rs.add_argument("--name", help="herdr の agent 名。既定は pr-<N>（PR が 1 件のとき）か s-<ID 先頭 8 文字>")
    rs.add_argument("--request", choices=["review", "resume", "none"], default="none")
    rs.add_argument("--feedback", help="--request review のとき、watch の FEEDBACK 行をそのまま渡す")
    rs.add_argument("--extra", help="--request resume の依頼文の末尾に足すユーザーの指示")
    rs.add_argument("--busy-sec", type=int, default=600)
    rs.add_argument("--accept-warnings", action="store_true",
                    help="ユーザーが確認した警告（Desktop のセッション）を越えて進む。live・更新直後・ディレクトリ無しは越えない")
    rs.add_argument("--dry-run", action="store_true", help="コマンド・警告・依頼文を JSON で出すだけ")
    rs.add_argument("--start-timeout", type=int, help="秒。既定は設定の launch.start_timeout")
    rs.set_defaults(func=cmd_restore)

    stt = sub.add_parser("start", help="空きペインで新しいセッションを始め、最初の依頼を送ったところで結果を 1 行で返す")
    stt.add_argument("--pane", help="ペイン ID か auto。--dry-run 以外で必須")
    stt.add_argument("--title", required=True, help="Claude Code のセッション名（-n）。課題キーを先頭に")
    stt.add_argument("--key", help="課題キー。--prompt を省くと設定の requests.start で依頼文を作る")
    stt.add_argument("--name", help="herdr の agent 名（[a-z][a-z0-9_-]{0,31}）。既定は --key の小文字")
    stt.add_argument("--prompt", help="最初の依頼（1 行に詰める）")
    stt.add_argument("--cwd", help="起動ディレクトリ。既定はメインチェックアウト")
    stt.add_argument("--permission-mode", help="既定は設定の launch.permission_mode")
    stt.add_argument("--dry-run", action="store_true")
    stt.add_argument("--start-timeout", type=int, help="秒。既定は設定の launch.start_timeout")
    stt.set_defaults(func=cmd_start)

    w = sub.add_parser("watch", help="PR のレビュー・割り当て中のペイン・起こしたセッションの劣化・待ち行列を見張り、イベントを 1 行ずつ出す")
    w.add_argument("--interval", type=int, default=300, help="PR を問い合わせる間隔（秒）。60 未満にしない")
    w.add_argument("--slot-interval", type=int, default=20, help="割り当て中のペインを見る間隔（秒）")
    w.add_argument("--health-interval", type=int, default=60, help="劣化と待ち行列を見る間隔（秒）")
    w.add_argument("--reset", action="store_true", help="PR の既出記録を消す（manager の初回起動だけ）")
    w.add_argument("--exit-on-event", action="store_true")
    w.add_argument("--once", action="store_true", help="PR を 1 回だけ問い合わせて終わる（監視の登録はしない）")
    w.add_argument("--max-watch-errors", type=int, default=3)
    w.set_defaults(func=cmd_watch)

    sp = sub.add_parser("status", help="監視・割り当て・起こしたセッションの劣化・待ち行列・あなた待ちを 1 画面に出す")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--stale-sec", type=int, default=600, help="監視の最終確認がこれより古ければ止まっている可能性と出す")
    sp.set_defaults(func=cmd_status)

    a = sub.add_parser("ask", help="ユーザーへの質問を記録する。add は番号（1110-1）を出す")
    a.add_argument("op", choices=["add", "done", "list"])
    a.add_argument("text", nargs="*", help="add は質問の文、done は番号（複数可）")
    a.add_argument("--pr", type=int)
    a.add_argument("--pane", help="質問を出したペイン（manager 自身なら省略）")
    a.add_argument("--cmd", help="ユーザーに実行してほしいコマンド")
    a.set_defaults(func=cmd_ask)

    q = sub.add_parser("queue", help="待ち行列を見る・消す")
    q.add_argument("op", choices=["list", "rm"])
    q.add_argument("ids", nargs="*", help="rm する W 番号")
    q.set_defaults(func=cmd_queue)

    u = sub.add_parser("unassign", help="割り当てを手で外す（watch の SETTLED を待たない）")
    u.add_argument("session", help="セッション ID の前方一致")
    u.set_defaults(func=cmd_unassign)

    im = sub.add_parser("import-state", help="以前の版の状態ディレクトリから取り込む（切り替え時に 1 回）")
    im.add_argument("src")
    im.set_defaults(func=cmd_import_state)

    args = ap.parse_args()
    CFG = cfgmod.load(args.repo_dir)
    args.state_dir = args.state_dir or CFG.state_dir(MANAGER)
    if args.cmd in ("restore", "start") and not args.dry_run and not args.pane:
        ap.error(f"{args.cmd} には --pane が要る（ペイン ID か auto）")
    try:
        args.func(args)
    except herdr.Unreachable as e:
        util.die(herdr.unreachable_message(e))


if __name__ == "__main__":
    main()
