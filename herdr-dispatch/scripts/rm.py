"""review-manager の CLI と常駐監視。サブコマンドは --help を参照。どれも作業ディレクトリのリポジトリを対象にする。

枠は label が review_manager.slots.label（既定 review-slot-{k}）のペイン。枠ごとに claude を 1 つ常駐させ、
レビューが終わるたびに /clear して次を渡す。
"""
import argparse
import json
import os
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hdlib import config as cfgmod  # noqa: E402
from hdlib import gh, herdr, launch, procs, screen, state, supervisor, util  # noqa: E402
from hdlib.util import ago, die, iso, local_hm, one_line  # noqa: E402
from sources import gh_review_requests as builtin_source  # noqa: E402

MANAGER = "review-manager"
CFG = None
VIEWER = None


def store(args):
    return state.Store(args.state_dir, {"kept": {}, "slot_seen": {}})


def rmcfg(*keys, default=None):
    return CFG.get("review_manager", *keys, default=default)


def slot_name(k):
    return util.fill(rmcfg("slots", "name"), k=k)


def slot_title(k):
    return util.fill(rmcfg("slots", "title"), k=k)


def viewer():
    global VIEWER
    VIEWER = VIEWER or gh.viewer_login()
    return VIEWER


def kinds():
    return rmcfg("kinds") or {}


def dependabot_kind():
    d = rmcfg("dependabot") or {}
    return d.get("kind") if d.get("enabled") else None


def kind_cfg(kind):
    k = kinds().get(kind)
    if k is None:
        die(f"kind {kind} が設定の review_manager.kinds に無い（あるもの: {', '.join(kinds())}）")
    return k


# ---- 枠 ----

def discover_slots():
    rx = cfgmod.label_regex(rmcfg("slots", "label"))
    slots = []
    for p in herdr.panes():
        m = rx.match(p.get("label") or "")
        if m:
            slots.append({"k": m.group(1), "pane_id": p["pane_id"], "agent": p.get("agent"),
                          "status": p.get("agent_status"), "session": herdr.session_of(p),
                          "tokens": p.get("tokens") or {}})
    return sorted(slots, key=lambda s: int(s["k"]))


def slot_by_k(k):
    return next((s for s in discover_slots() if s["k"] == str(k)), None)


def peek_slot(k):
    """待ちの途中や依頼を送った後に使う。そこで中断すると割り当ての記録が残らないので、届かないときは None。"""
    try:
        return slot_by_k(k)
    except herdr.Unreachable:
        return None


def slot_health(slot, pmap=None):
    if not slot["agent"]:
        return ["claude が動いていない（シェルだけ）"]
    lc, hc = CFG.launch("review_manager"), CFG.health("review_manager")
    return procs.health_reasons(slot["pane_id"], hc["require_env"],
                                lc["permission_mode"] if hc["check_mode"] else None, pmap)


def slot_version_gap(slot, pmap, installed):
    if not slot["agent"] or not CFG.health("review_manager").get("check_version", True):
        return None
    return procs.version_gap(slot["pane_id"], pmap, installed)


# ---- 対象の判定 ----

def list_targets(out_path):
    """(結果, エラー)。"""
    t = rmcfg("targets")
    if t.get("list_cmd"):
        r = util.run(CFG.cmd(t["list_cmd"], out=out_path, state_dir=CFG.state_dir(MANAGER)),
                     cwd=CFG.repo_root, timeout=600)
        if r.returncode != 0:
            return None, f"rc={r.returncode} {r.stderr.strip()[-200:]}"
        try:
            with open(out_path, encoding="utf-8") as f:
                return json.load(f), None
        except (OSError, json.JSONDecodeError):
            try:
                return json.loads(r.stdout), None
            except json.JSONDecodeError as e:
                return None, str(e)
    return builtin_source.list_targets(CFG.slug, viewer(), CFG.repo_root)


def untrusted_reason(t):
    """target の作者か会話の書き手に信頼できない人がいるとき、その説明。CODEOWNERS はフォークからの PR でも
    レビューを依頼し、公開リポジトリでは誰でもコメントできるので、外部の人が書いた本文・コード・コメントを
    review session の権限で読ませないための判定。ソースが author_association / participants を出さないときは
    その分を判定しない（その責任はソースにある）。"""
    assocs, authors = set(rmcfg("trusted_associations") or []), set(rmcfg("trusted_authors") or [])
    reasons = []
    assoc = t.get("author_association")
    if assoc is not None and assoc not in assocs and t.get("author") not in authors:
        reasons.append(f"作者 {t.get('author')}（{assoc}）")
    bad = [p["login"] for p in t.get("participants") or []
           if not (p.get("bot") or p.get("association") in assocs or p.get("login") in authors)]
    if bad:
        reasons.append(f"会話の書き手 {', '.join(bad)}")
    return "・".join(reasons) or None


def pr_target(n):
    """1 件の判定 target。失敗は None。"""
    t = rmcfg("targets")
    if t.get("pr_cmd"):
        r = util.run(CFG.cmd(t["pr_cmd"], n=n, state_dir=CFG.state_dir(MANAGER)), cwd=CFG.repo_root, timeout=180)
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
    else:
        d = builtin_source.pr_target(CFG.slug, n, viewer(), CFG.repo_root)
    return (d.get("targets") or [None])[0]


# ---- ロック（任意。定期ルーティンなど、同じ PR へ投稿する別の実行と排他する） ----

def lock_cfg():
    return rmcfg("lock")


def lock_info():
    lk = lock_cfg()
    if not lk:
        return None
    try:
        with open(os.path.expanduser(lk["path"]), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    started = util.parse_ts(d.get("started_at"))
    if not started:
        return None
    alive = util.parse_ts(d.get("refreshed_at")) or started
    left = lk.get("ttl_min", 240) - (util.now() - alive).total_seconds() / 60
    d.update(live=left > 0, minutes_left=int(left), minutes_since_alive=int((util.now() - alive).total_seconds() / 60))
    return d


def lock_run(op):
    argv = (lock_cfg() or {}).get(f"{op}_cmd")
    if not argv:
        return 1, f"lock.{op}_cmd が無い"
    r = util.run(CFG.cmd(argv, state_dir=CFG.state_dir(MANAGER)), cwd=CFG.repo_root, timeout=300)
    return r.returncode, (r.stderr.strip().splitlines()[-1:] or [r.stdout.strip()])[0]


def needs_lock(kind):
    return bool(lock_cfg()) and bool((kinds().get(kind) or {}).get("locking"))


def ensure_lock(st):
    """manager のロックを用意し started_at を返す。他者のロックなら None。"""
    lk = lock_info()
    mine = st.get("lock_started_at")
    if lk and lk["live"]:
        if mine and lk["started_at"] == mine:
            lock_run("refresh")
            return mine
        return None
    lock_run("acquire")
    lk = lock_info()
    if not lk or not lk["live"]:
        return None
    st["lock_started_at"] = lk["started_at"]
    return lk["started_at"]


# ---- 依頼文 ----

def build_prompt(n, kind, lock_started_at=None, report=None, head=None, extra=None):
    kc = kind_cfg(kind)
    values = dict(n=n, kind=kind, head=head or "-", lock_started_at=lock_started_at or "-",
                  report=report or "none", viewer=VIEWER or "-")
    cmd = util.fill(kc["command"], **values)
    body = one_line(util.fill(CFG.text(kc.get("template")) or "", **values))
    if extra:
        body = f"{body}。{one_line(extra)}" if body else one_line(extra)
    return f"{cmd} （review-manager からの依頼。{body}）" if body else cmd


# ---- status / check ----

def status_extra_lines():
    se = rmcfg("status_extra")
    if not se or not se.get("cmd"):
        return None, []
    r = util.run(CFG.cmd(se["cmd"]), cwd=CFG.repo_root, timeout=180)
    lines = (r.stderr + "\n" + r.stdout).splitlines()
    if se.get("line_regex"):
        rx = re.compile(se["line_regex"])
        lines = [ln for ln in lines if rx.search(ln)]
    lines = [ln for ln in lines if ln.strip()]
    return se.get("title") or "追加の状況", lines[:se.get("max_lines", 10)]


def cmd_info(args):
    print(json.dumps({
        "config": CFG.path, "config_found": os.path.exists(CFG.path), "repo": CFG.slug, "repo_configured": CFG.matched,
        "state_dir": args.state_dir, "notes": CFG.path_of(rmcfg("notes")),
        "launch": CFG.launch("review_manager"), "health": CFG.health("review_manager"),
        "slots": rmcfg("slots"), "targets": rmcfg("targets"), "lock": lock_cfg(),
        "kinds": {k: v.get("command") for k, v in kinds().items()}, "dependabot": rmcfg("dependabot"),
    }, ensure_ascii=False, indent=1))


def cmd_status(args):
    st = store(args).load()
    try:
        slots, herdr_error = discover_slots(), None
    except herdr.Unreachable as e:
        slots, herdr_error = [], str(e)
    lk = lock_info()
    pmap = procs.claude_procs_by_pane()
    installed = procs.installed_claude_version()
    rows = []
    for s in slots:
        text, sugg = screen.input_box(s["pane_id"]) if s["agent"] else ("", False)
        rows.append({**s, "assignment": st["assignments"].get(s["k"]), "kept": st["kept"].get(s["k"]),
                     "health": slot_health(s, pmap), "version_gap": slot_version_gap(s, pmap, installed),
                     "typed": "" if sugg else text})
    title, extra = status_extra_lines()
    if args.json:
        print(json.dumps({"slots": None if herdr_error else rows, "herdr_error": herdr_error, "lock": lk,
                          "queue": st["queue"], "asks": st["asks"], "supervisor": st["supervisor"], "extra": extra},
                         ensure_ascii=False, indent=1, default=str))
        return
    print(f"review-manager 状況（{local_hm(iso())}・{CFG.slug}）")
    print(supervisor.status_line(st))
    if lock_cfg():
        if lk:
            left = f"残り {lk['minutes_left'] // 60}h{lk['minutes_left'] % 60:02d}m" if lk["live"] else "期限切れ"
            print(f"ロック: {local_hm(lk['started_at'])} 取得（started_at {lk['started_at']}）・{left}・"
                  f"最終延長 {local_hm(lk.get('refreshed_at'))}")
        else:
            print("ロック: 無し")
    if herdr_error:
        print(f"枠: 不明。⚠ Herdr に届かない（{herdr_error}）。sandbox の中なら外で実行し直す")
        for k, a in st["assignments"].items():
            print(f"  {slot_name(k)} #{a['pr']} {a['kind']}  送信 {local_hm(a['sent_at'])}（{ago(a['sent_at'])}）"
                  f"  段階 {a.get('phase') or '-'}（state の記録）")
    else:
        print("枠:" + ("" if rows else " なし"))
    for s in rows:
        a = s["assignment"]
        line = f"  {slot_name(s['k'])} {s['pane_id']} {s['status'] or 'シェル'}"
        if a:
            line += f"  #{a['pr']} {a['kind']} head {a.get('head') or '-'}  送信 {local_hm(a['sent_at'])}（{ago(a['sent_at'])}）"
            if a.get("phase"):
                line += f"  段階 {a['phase']}"
        if s["kept"]:
            line += f"  残す（{s['kept']}）"
        elif not a:
            line += "  空き"
        print(line)
        print(f"     resume: claude --resume {s['session']}")
        if s["health"]:
            print(f"     ⚠ 劣化: {' / '.join(s['health'])}")
        if s["version_gap"]:
            print(f"     ⚠ 古い版: {s['version_gap'][0]} で動いている（インストール済みは {s['version_gap'][1]}）。"
                  f"空いたら rm.py restart {s['k']}")
        if s["typed"]:
            print(f"     入力欄（未送信）: {s['typed'][:80]}")
        if a and a.get("last_event"):
            print(f"     直近: {a['last_event']}")
    print("待ち行列: " + ("なし" if not st["queue"] else ", ".join(f"#{q['pr']}({q['kind']})" for q in st["queue"])))
    print("あなた待ち:" + ("" if st["asks"] else " なし"))
    for line in state.ask_lines(st):
        print(line)
    if extra:
        print(f"{title}:")
        for ln in extra:
            print(f"  {ln}")


def cmd_check(args):
    s = slot_by_k(args.k) or die(f"枠 {util.fill(rmcfg('slots', 'label'), k=args.k)} が無い")
    a = store(args).load()["assignments"].get(s["k"])
    info = herdr.agent_get(s["pane_id"]) or {}
    print(f"{slot_name(s['k'])} {s['pane_id']} status={s['status']} name={info.get('name')} session={s['session']}")
    if a:
        print(f"割り当て: #{a['pr']} {a['kind']} 送信 {local_hm(a['sent_at'])}（{ago(a['sent_at'])}）")
        if kinds().get(a["kind"], {}).get("check_target", True):
            t = pr_target(a["pr"])
            if t:
                print(f"--pr {a['pr']}: action={t['action']} head={(t.get('head_sha') or '')[:7]} {t.get('reason')}")
    health = slot_health(s)
    if health:
        print("⚠ 劣化: " + " / ".join(health))
    text, sugg = screen.input_box(s["pane_id"])
    if text:
        print(f"入力欄: {text[:100]}（{'入力候補・ユーザーの入力ではない' if sugg else 'ユーザーが入力した未送信の文字'}）")
    print("---- 最後の応答 ----")
    print(screen.last_reply(s["pane_id"], args.lines))


# ---- handoff ----

def dedicated_sessions(n):
    """枠以外で #N を見ているセッション。"""
    rx = cfgmod.label_regex(rmcfg("slots", "label"))
    pats = [util.fill(p, n=n) for p in rmcfg("dedicated_patterns", default=["#{n}"])]
    hits = []
    for p in herdr.panes():
        if rx.match(p.get("label") or "") or not p.get("agent"):
            continue
        blob = json.dumps(p.get("tokens") or {}, ensure_ascii=False) + " " + (p.get("terminal_title_stripped") or "")
        if any(pat in blob for pat in pats):
            hits.append(p["pane_id"])
    return hits


def ensure_name(s):
    want = slot_name(s["k"])
    if (herdr.agent_get(s["pane_id"]) or {}).get("name") != want:
        herdr.call("agent", "rename", s["pane_id"], want)
    return util.wait_until(lambda: (herdr.agent_get(want) or {}).get("agent_status") in ("idle", "done"), 60)


def clear_and_rename(s):
    """/clear して新しい session を待ち、名前を付け直す。旧 session を返す。"""
    old = s["session"]
    herdr.call("agent", "prompt", slot_name(s["k"]), "/clear")
    new = util.wait_until(lambda: (lambda x: x if x and x != old else None)((peek_slot(s["k"]) or {}).get("session")), 60)
    if not new:
        die(f"/clear 後に session が切り替わらない（{slot_name(s['k'])}）")
    ensure_name(s)
    return old


def dependabot_checks(n):
    """(待つべき理由か None, report の有無)。"""
    d = rmcfg("dependabot") or {}
    pr = gh.gh_json(["pr", "view", str(n), "--json", "statusCheckRollup,comments"], CFG.repo_root) or {}
    if d.get("ready_check"):
        for c in pr.get("statusCheckRollup", []):
            if c.get("name") == d["ready_check"] and c.get("status") not in (None, "COMPLETED"):
                return f"{d['ready_check']} が {c.get('status')}（レポート作成中）", None
    return None, "yes" if has_report(pr.get("comments", [])) else "none"


def has_report(comments):
    rep = (rmcfg("dependabot") or {}).get("report") or {}
    if not rep.get("marker"):
        return False
    return any((c.get("author") or {}).get("login") == rep.get("author", "github-actions")
               and rep["marker"] in (c.get("body") or "") for c in comments)


def cmd_handoff(args):
    n = args.pr
    s = slot_by_k(args.slot) or die(f"枠 {args.slot} が無い")
    st = store(args).load()
    k = s["k"]
    name = slot_name(k)
    if not s["agent"]:
        die(f"{name} は claude が動いていない。rm.py start {k} か restart {k}")
    if st["assignments"].get(k) and not args.force:
        die(f"{name} は #{st['assignments'][k]['pr']} を割り当て中（--force で上書き）")
    if st["kept"].get(k) and not args.force:
        die(f"{name} はユーザーの指定で残している（{st['kept'][k]}）")
    if s["status"] not in ("idle", "done"):
        die(f"{name} が {s['status']}。空くまで待つ")
    health = slot_health(s)
    if health:
        die(f"{name} が劣化: {' / '.join(health)}。rm.py restart {k}")
    for kk, a in st["assignments"].items():
        if a["pr"] == n and kk != k:
            die(f"#{n} は {slot_name(kk)} に割り当て中")
    hits = dedicated_sessions(n)
    if hits and not args.force:
        die(f"#{n} を見ている枠以外のセッションがある: {', '.join(hits)}")

    kind = args.kind
    head = None
    report = args.report
    if kind is None or kind_cfg(kind).get("check_target", True):
        t = pr_target(n)
        if not t:
            die(f"#{n} の判定に失敗")
        if t["action"] == "skip" and not args.force:
            die(f"#{n} は skip（{t.get('reason')}）")
        if t["action"] == "error":
            die(f"#{n} の判定がエラー（{t.get('reason')}）")
        why = untrusted_reason(t)
        if why and not args.allow_untrusted:
            die(f"#{n} にリポジトリの外部の人がいる（{why}）。渡すのはユーザーが明示したときだけ（--allow-untrusted）")
        head = (t.get("head_sha") or "")[:7]
        if kind is None:
            kind = "re-review" if t["action"] == "re-review" else "review"
    kc = kind_cfg(kind)
    if kc.get("require_review_request"):
        pr = gh.gh_json(["pr", "view", str(n), "--json", "reviewRequests,headRefOid"], CFG.repo_root) or {}
        logins = [x.get("login") for x in pr.get("reviewRequests", [])]
        if viewer() not in logins and not args.force:
            die(f"#{n} のレビュー依頼に {viewer()} がいない（外れた。author が修正の途中のことがある）")
        head = head or (pr.get("headRefOid") or "")[:7]
    if kind == dependabot_kind():
        wait, rep = dependabot_checks(n)
        if wait and not args.force:
            die(f"#{n} の {wait}")
        report = report or rep

    if args.dry_run:
        print(build_prompt(n, kind, st.get("lock_started_at") or "<取得する started_at>", report, head, args.note))
        return
    started_at = None
    if needs_lock(kind):
        with store(args).txn() as stx:
            started_at = ensure_lock(stx)
        if not started_at:
            die("ロックを取れない（別の実行が持っている）")
    viewer()
    prompt = build_prompt(n, kind, started_at, report, head, args.note)

    old = s["session"]
    if not args.no_clear:
        old = clear_and_rename(s)
    else:
        ensure_name(s)
    herdr.call("agent", "prompt", name, prompt)
    info = util.wait_until(lambda: (lambda a: a if a and a.get("agent_status") in ("working", "blocked") else None)(
        herdr.agent_get(name)), 30)
    with store(args).txn() as stx:
        stx["assignments"][k] = {
            "pr": n, "kind": kind, "head": head, "sent_at": iso(),
            "session": (peek_slot(k) or {}).get("session"),
            "seq": (info or {}).get("state_change_seq"), "phase": "working" if info else "不明",
        }
        state.dequeue(stx, f"pr:{n}")
    print(f"{name} に #{n}（{kind}）を渡した。{'作業開始を確認' if info else '作業開始を確認できない（rm.py check で確かめる。再送しない）'}")
    if not args.no_clear:
        print(f"前の会話: claude --resume {old}")
    if started_at:
        print(f"ロック started_at: {started_at}")


def cmd_tell(args):
    """割り当て中の枠へ manager から連絡を送り、終わるまで待って最後の応答を出す。

    この往復で起きた状態変化を watch が SLOT_SETTLED として拾わないよう、seq を既出にする。
    """
    k = str(args.k)
    name = slot_name(k)
    ok, _ = herdr.call("agent", "prompt", name, args.message, "--wait", "--timeout", str(args.timeout * 1000),
                       timeout=args.timeout + 30)
    info = herdr.agent_get(name) or {}
    with store(args).txn() as st:
        st["slot_seen"].setdefault(k, {})["reported_seq"] = info.get("state_change_seq")
    if not ok:
        print(f"待ちが終わらなかった（{info.get('agent_status')}）。rm.py check {k} で確かめる。再送しない")
    print(screen.last_reply(info.get("pane_id") or (slot_by_k(k) or {}).get("pane_id"), args.lines))


def cmd_unassign(args):
    with store(args).txn() as st:
        a = st["assignments"].pop(str(args.k), None)
        if a and args.drop_asks:
            st["asks"] = [q for q in st["asks"] if q.get("pr") != a["pr"]]
    print(f"{slot_name(args.k)} の割り当てを外した" + (f"（#{a['pr']}）" if a else "（割り当て無し）"))


def cmd_keep(args):
    with store(args).txn() as st:
        st["kept"][str(args.k)] = args.reason or "ユーザーの指定"
    print(f"{slot_name(args.k)} を残す")


def cmd_unkeep(args):
    with store(args).txn() as st:
        st["kept"].pop(str(args.k), None)
    print(f"{slot_name(args.k)} の残す指定を外した")


def cmd_queue(args):
    with store(args).txn() as st:
        if args.op == "add":
            if not args.pr:
                die("queue add には PR 番号が要る")
            if not any(q.get("pr") == args.pr for q in st["queue"]):
                state.enqueue(st, f"pr:{args.pr}", pr=args.pr, kind=args.kind, note=args.note)
        elif args.op == "rm":
            state.dequeue(st, f"pr:{args.pr}")
        for q in st["queue"]:
            print(f"#{q['pr']} {q['kind']} {local_hm(q['since'])} {q.get('note') or ''}")


def cmd_ask(args):
    with store(args).txn() as st:
        if args.op == "add":
            text = args.text or " ".join(args.ids)
            if not text:
                die("ask add には質問文が要る")
            print(state.ask_add(st, text, pr=args.pr, where=slot_name(args.slot) if args.slot else None, cmd=args.cmd))
            return
        if args.op == "done":
            state.ask_done(st, args.ids)
        for line in state.ask_lines(st):
            print(line)


def cmd_lock(args):
    if not lock_cfg():
        die("設定に review_manager.lock が無い")
    if args.op == "show":
        print(json.dumps(lock_info(), ensure_ascii=False))
    elif args.op == "acquire":
        with store(args).txn() as st:
            sa = ensure_lock(st)
        print(f"started_at {sa}" if sa else "取れない（別の実行が持っている）")
    elif args.op == "refresh":
        rc, note = lock_run("refresh")
        print(note)
        sys.exit(rc)
    elif args.op == "release":
        _, note = lock_run("release")
        with store(args).txn() as st:
            st.pop("lock_started_at", None)
        print(note)


# ---- start / restart ----

def launch_in_slot(s, resume, timeout):
    lc = CFG.launch("review_manager")
    sid = resume or str(uuid.uuid4())
    args = launch.claude_args(session_id=None if resume else sid, resume=resume, title=slot_title(s["k"]),
                              permission_mode=lc["permission_mode"])
    command = launch.build_command(lc, CFG.repo_root, args)
    print(f"起動: {command}" + (f"  （{lc['approval_hint']}）" if lc.get("approval_hint") else ""), flush=True)
    outcome, detail = launch.run_until_agent(s["pane_id"], command, timeout or lc["start_timeout"],
                                             CFG.get("shell", "prompt_regex"))
    if outcome == "pane_busy":
        die(f"{slot_name(s['k'])} の入力欄に文字が残っている（{one_line(detail)[-120:]}）。"
            "つながって実行されるので打ち込まない。ユーザーに入力欄を空けてもらう")
    if outcome == "start_failed":
        die(f"起動コマンドが終わってシェルへ戻った: {one_line(detail)[-200:]}")
    if outcome != "agent":
        die(f"claude を検出できない（{outcome}: {one_line(detail)[-120:]}）。承認待ちの可能性")
    got, agent = launch.wait_session(s["pane_id"], detail)
    if got is None:
        # 信頼確認ダイアログの間は ID が出ない。答えた後の命名は watch が行う
        print(f"BLOCKED stage=trust {slot_name(s['k'])}: セッション ID が出ない（{agent.get('agent_status')}）。"
              "画面を確かめてユーザーに答えてもらう")
        return
    if got != sid:
        print(f"MISMATCH expected={sid} got={got}")
    ok = ensure_name(s)
    s2 = slot_by_k(s["k"])
    health = slot_health(s2)
    print(f"{slot_name(s['k'])} 起動 session={s2['session']} {'idle' if ok else '未 idle'}"
          + (f" ⚠ {' / '.join(health)}" if health else " 健全"))


def cmd_restart(args):
    s = slot_by_k(args.k) or die(f"枠 {args.k} が無い")
    sid = s["session"]
    if s["agent"]:
        if s["status"] not in ("idle", "done") and not args.force:
            die(f"{slot_name(s['k'])} が {s['status']}（作業中の終了は --force）")
        herdr.call("agent", "send-keys", s["pane_id"], "esc")
        herdr.call("agent", "prompt", s["pane_id"], "/exit")
        def exited():
            # 届かないことを「claude がいない」と読むと、終わっていないペインで起動コマンドを打つ
            try:
                return not (slot_by_k(s["k"]) or {}).get("agent")
            except herdr.Unreachable:
                return False
        if not util.wait_until(exited, 60):
            die("/exit 後も claude が残っている")
    if not sid:
        die("resume する session id が分からない（rm.py start を使う）")
    launch_in_slot(s, sid, args.timeout)


def cmd_start(args):
    s = slot_by_k(args.k) or die(f"枠 {args.k} が無い")
    if s["agent"]:
        die(f"{slot_name(s['k'])} には claude が動いている（restart を使う）")
    launch_in_slot(s, None, args.timeout)


def cmd_sync(args):
    argv = rmcfg("sync_cmd")
    if not argv:
        print("設定に review_manager.sync_cmd が無い（何もしない）")
        return
    res = util.run(CFG.cmd(argv), cwd=CFG.repo_root, timeout=300)
    print("\n".join((res.stdout + res.stderr).strip().splitlines()[:12]))


# ---- watch（常駐監視） ----

def check_slots(st, do_health, pmap):
    events = []
    seen = st["slot_seen"]
    installed = procs.installed_claude_version() if do_health else None
    for s in discover_slots():
        k = s["k"]
        sseen = seen.setdefault(k, {})
        if not s["agent"]:
            reason = "claude が動いていない（シェルだけ）"
            if sseen.get("degraded") != f"{s['session']}:{reason}":
                sseen["degraded"] = f"{s['session']}:{reason}"
                events.append(f"SLOT_DEGRADED k={k} {reason}")
            continue
        info = herdr.agent_get(s["pane_id"]) or {}
        # /clear やセッションの復元で名前が外れる
        if info.get("name") != slot_name(k):
            herdr.call("agent", "rename", s["pane_id"], slot_name(k))
        if do_health:
            health = slot_health(s, pmap)
            key = f"{s['session']}:{' / '.join(health)}"
            if health and sseen.get("degraded") != key:
                sseen["degraded"] = key
                events.append(f"SLOT_DEGRADED k={k} {' / '.join(health)}")
            elif not health:
                sseen.pop("degraded", None)
            # 作業中に知らせても立ち上げ直せないので、空いた枠だけ知らせる
            gap = slot_version_gap(s, pmap, installed)
            free = (not st["assignments"].get(k) and k not in st["kept"]
                    and info.get("agent_status") in ("idle", "done"))
            vkey = f"{s['session']}:{gap[0]}->{gap[1]}" if gap else None
            if gap and free and sseen.get("outdated") != vkey:
                sseen["outdated"] = vkey
                events.append(f"SLOT_OUTDATED k={k} running={gap[0]} installed={gap[1]}")
            elif not gap:
                sseen.pop("outdated", None)

        a = st["assignments"].get(k)
        # 残す指定の枠はユーザーが直接やり取りしているので、往復のたびに起こさない
        if not a or k in st["kept"]:
            continue
        status = info.get("agent_status")
        seq = info.get("state_change_seq")
        if status == "working":
            a["phase"] = "working"
            continue
        if status not in ("idle", "done", "blocked") or seq is None:
            continue
        if seq == a.get("seq") or seq == sseen.get("reported_seq"):
            continue
        sseen["reported_seq"] = seq
        action = "-"
        if kinds().get(a["kind"], {}).get("check_target", True):
            action = (pr_target(a["pr"]) or {}).get("action", "?")
        a["phase"] = "完了" if action == "skip" else ("blocked" if status == "blocked" else "ユーザー待ちか停止")
        a["last_event"] = f"{local_hm(iso())} {status} action={action}"
        events.append(f"SLOT_SETTLED k={k} #{a['pr']} kind={a['kind']} status={status} action={action}")
    return events


def tend_lock(st):
    if not lock_cfg():
        return []
    needs = any(needs_lock(a["kind"]) for a in st["assignments"].values()) or any(
        needs_lock(q["kind"]) for q in st["queue"])
    lk = lock_info()
    mine = st.get("lock_started_at")
    ours = bool(lk and lk["live"] and mine and lk["started_at"] == mine)
    if needs:
        if not ours:
            if mine and st.get("lock_lost_reported") != mine:
                st["lock_lost_reported"] = mine
                return [f"LOCK_LOST started_at={mine}"]
            return []
        # 延長は refreshed_at だけを更新し started_at を変えない。依頼文で伝えた started_at が有効なまま残る
        if lk["minutes_since_alive"] >= lock_cfg().get("refresh_every_min", 60):
            lock_run("refresh")
        return []
    if ours:
        lock_run("release")
        st.pop("lock_started_at", None)
    return []


def poll_targets():
    """(既出判定のキー, 行) の一覧。取得の失敗は WATCH_ERROR を出して None。"""
    data, err = list_targets(os.path.join(CFG.state_dir(MANAGER), "targets.json"))
    if err:
        print(f"WATCH_ERROR {err}", flush=True)
        return None
    items = []
    for t in data.get("targets", []):
        if t.get("action") == "error":
            # 張り直した直後にも同じ行が出て即終了を繰り返すため、既出に入れて一度だけ出す
            items.append((f"error:{t.get('number')}:{t.get('reason')}", f"TARGET_ERROR #{t.get('number')} reason={util.quote_untrusted(t.get('reason'), 200)}"))
        elif t.get("action") in ("review", "re-review") and untrusted_reason(t):
            items.append((f"untrusted:{t['number']}@{t['head_sha']}",
                          f"UNTRUSTED_REVIEW_REQUEST #{t['number']} action={t['action']} head={t['head_sha'][:7]} "
                          f"author={t.get('author')} reason={util.quote_untrusted(untrusted_reason(t), 200)} "
                          f"title={util.quote_untrusted(t.get('title'))}"))
        elif t.get("action") in ("review", "re-review"):
            items.append((f"{t['number']}@{t['head_sha']}",
                          f"REVIEW_REQUEST #{t['number']} action={t['action']} head={t['head_sha'][:7]} "
                          f"author={t.get('author')} title={util.quote_untrusted(t.get('title'))}"))
    return items


def poll_dependabot():
    """dependabot の open PR を (PR, head) ごとに出す。レビュー済みかは渡す直前に判定するので、ここでは候補を出すだけ。"""
    d = rmcfg("dependabot") or {}
    prs = gh.gh_json(["pr", "list", "--author", d.get("author", "app/dependabot"), "--state", "open", "--limit", "50",
                      "--json", "number,headRefOid,title,comments"], CFG.repo_root)
    if prs is None:
        print("WATCH_ERROR dependabot の PR 一覧を取れない", flush=True)
        return None
    # 開いた直後の PR は report=none で届き、handoff はレポート待ちで止まる。head だけを既出の鍵にすると、
    # レポートが付いても manager が起こされず待ち行列が進まないので、レポートの有無も鍵に含める
    items = []
    for pr in prs:
        reported = has_report(pr.get("comments", []))
        items.append((f"dependabot:{pr['number']}@{pr['headRefOid']}" + (":report" if reported else ""),
                      f"DEPENDABOT #{pr['number']} head={pr['headRefOid'][:7]} "
                      f"report={'yes' if reported else 'none'} title={util.quote_untrusted(pr.get('title'))}"))
    return items


def cmd_watch(args):
    s = store(args)
    supervisor.install_sigterm()
    with s.txn() as st:
        if args.reset:
            st["seen"] = []
        supervisor.register(st, [os.path.basename(__file__), " watch"])
    next_poll = next_health = 0.0
    errors = herdr_errors = 0
    while True:
        events = []
        do_health = time.time() >= next_health
        if do_health:
            next_health = time.time() + args.health_interval
        pmap = procs.claude_procs_by_pane() if do_health else None
        unreachable = None
        with s.txn() as st:
            if not supervisor.beat(st):
                return
            try:
                events += check_slots(st, do_health, pmap)
                herdr_errors = 0
            except herdr.Unreachable as e:
                unreachable = e
            events += tend_lock(st)
        if unreachable:
            herdr_errors += 1
            if herdr_errors == 1 or herdr_errors >= args.max_watch_errors:
                print(herdr.unreachable_line(unreachable, herdr_errors), flush=True)
            if herdr_errors >= args.max_watch_errors:
                return
        if time.time() >= next_poll:
            next_poll = time.time() + max(args.interval, 60)
            items = poll_targets()
            if items is not None and dependabot_kind():
                d = poll_dependabot()
                items = None if d is None else items + d
            if items is None:
                errors += 1
                if errors >= args.max_watch_errors:
                    return
            else:
                errors = 0
                with s.txn() as st:
                    events += [line for _, line in state.fresh_items(st, items)]
        for e in events:
            print(e, flush=True)
        if events:
            return
        time.sleep(args.slot_interval)


def cmd_import_state(args):
    """以前の版の状態ディレクトリ（state.json と seen.json）を取り込む。あなた待ちの cmd はユーザーがそのまま
    実行するので取り込まない。待ち行列は PR 番号と kind だけを取り込み、渡すときに handoff が判定し直す。"""
    try:
        old = state.read_own_json(os.path.join(args.src, state.STATE_FILE))
    except state.InsecureStateDir as e:
        die(f"取り込まない: {e}")
    seen_path = os.path.join(os.path.expanduser(args.src), "seen.json")
    try:
        old_seen = sorted(state.read_own_json(seen_path)) if os.path.lexists(seen_path) else []
    except state.InsecureStateDir as e:
        die(f"取り込まない: {e}")
    except json.JSONDecodeError:
        old_seen = []
    with store(args).txn() as st:
        if st["assignments"]:
            die("取り込み先に割り当てがある。取り込みは空の状態にだけ行う")
        for k in ("assignments", "kept", "slot_seen", "lock_started_at", "lock_lost_reported", "next_ask"):
            if k in old:
                st[k] = old[k]
        st["asks"] = [{**q, "where": q.get("where") or (slot_name(q["slot"]) if q.get("slot") else None), "cmd": None}
                      for q in old.get("asks", [])]
        st["queue"] = []
        for q in old.get("queue", []):
            state.enqueue(st, f"pr:{int(q['pr'])}", pr=int(q["pr"]), kind=q["kind"], note=q.get("note"))
        st["seen"] = (st["seen"] + old_seen)[-state.SEEN_KEEP:]
    print(f"取り込んだ: 割り当て {len(st['assignments'])}・残す {len(st['kept'])}・待ち行列 {len(st['queue'])}・"
          f"あなた待ち {len(st['asks'])}（cmd は除いた）・既出 {len(st['seen'])}")
    print("前の監視は止まらないので、TaskStop で止めてから新しい watch を張る")


def main():
    global CFG
    ap = argparse.ArgumentParser(description="review-manager の CLI")
    ap.add_argument("--repo-dir", default=os.getcwd())
    ap.add_argument("--state-dir", help="既定は設定の state_dir/review-manager")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="解決した設定を出す").set_defaults(f=cmd_info)
    p = sub.add_parser("status", help="枠・割り当て・ロック・待ち行列・あなた待ちを 1 画面に出す")
    p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_status)
    p = sub.add_parser("check", help="枠 K の状態と最後の応答、判定を出す")
    p.add_argument("k")
    p.add_argument("--lines", type=int, default=40)
    p.set_defaults(f=cmd_check)
    p = sub.add_parser("handoff", help="確認 → ロック → /clear → 命名 → 依頼文の送信 → 割り当ての記録")
    p.add_argument("pr", type=int)
    p.add_argument("--slot", required=True)
    p.add_argument("--kind", help="review_manager.kinds のキー。省略時は判定の action から review / re-review を決める")
    p.add_argument("--report", choices=["yes", "none"], help="dependabot のレポートの有無（省略時は PR から判定）")
    p.add_argument("--note", help="依頼文の末尾に足す注記")
    p.add_argument("--no-clear", action="store_true")
    p.add_argument("--force", action="store_true", help="確認を飛ばす（ユーザーが明示したときだけ）")
    p.add_argument("--allow-untrusted", action="store_true",
                   help="リポジトリの外部の人の PR を渡す（ユーザーが明示したときだけ。--force では越えない）")
    p.add_argument("--dry-run", action="store_true", help="依頼文を出すだけで送らない")
    p.set_defaults(f=cmd_handoff)
    p = sub.add_parser("tell", help="manager からの連絡を送って応答を待つ（この往復は SLOT_SETTLED にしない）")
    p.add_argument("k")
    p.add_argument("message")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--lines", type=int, default=30)
    p.set_defaults(f=cmd_tell)
    p = sub.add_parser("unassign")
    p.add_argument("k")
    p.add_argument("--drop-asks", action="store_true")
    p.set_defaults(f=cmd_unassign)
    p = sub.add_parser("keep")
    p.add_argument("k")
    p.add_argument("reason", nargs="?")
    p.set_defaults(f=cmd_keep)
    p = sub.add_parser("unkeep")
    p.add_argument("k")
    p.set_defaults(f=cmd_unkeep)
    p = sub.add_parser("queue")
    p.add_argument("op", choices=["add", "rm", "list"])
    p.add_argument("pr", type=int, nargs="?")
    p.add_argument("--kind", default="review")
    p.add_argument("--note")
    p.set_defaults(f=cmd_queue)
    p = sub.add_parser("ask")
    p.add_argument("op", choices=["add", "done", "list"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--pr", type=int)
    p.add_argument("--slot")
    p.add_argument("--cmd")
    p.add_argument("--text")
    p.set_defaults(f=cmd_ask)
    p = sub.add_parser("lock")
    p.add_argument("op", choices=["show", "acquire", "refresh", "release"])
    p.set_defaults(f=cmd_lock)
    p = sub.add_parser("restart", help="/exit → --resume で立ち上げ直す（会話は継続）")
    p.add_argument("k")
    p.add_argument("--force", action="store_true")
    p.add_argument("--timeout", type=int)
    p.set_defaults(f=cmd_restart)
    p = sub.add_parser("start", help="シェルだけの枠で claude を新しく起動する")
    p.add_argument("k")
    p.add_argument("--timeout", type=int)
    p.set_defaults(f=cmd_start)
    sub.add_parser("sync", help="設定の sync_cmd を実行する").set_defaults(f=cmd_sync)
    p = sub.add_parser("watch", help="レビュー依頼・枠の状態・劣化・ロックを見張り、イベントを出したら終了する")
    p.add_argument("--interval", type=int, default=300, help="レビュー依頼のポーリング間隔（秒・60 以上）")
    p.add_argument("--slot-interval", type=int, default=20, help="枠の確認間隔（秒）")
    p.add_argument("--health-interval", type=int, default=60, help="劣化の確認間隔（秒）")
    p.add_argument("--max-watch-errors", type=int, default=3)
    p.add_argument("--reset", action="store_true", help="レビュー依頼の既出記録を消す（manager の初回起動だけ）")
    p.set_defaults(f=cmd_watch)
    p = sub.add_parser("import-state", help="以前の版の状態ディレクトリから取り込む（切り替え時に 1 回）")
    p.add_argument("src")
    p.set_defaults(f=cmd_import_state)
    args = ap.parse_args()
    CFG = cfgmod.load(args.repo_dir)
    args.state_dir = args.state_dir or CFG.state_dir(MANAGER)
    try:
        args.f(args)
    except herdr.Unreachable as e:
        die(herdr.unreachable_message(e))


if __name__ == "__main__":
    main()
