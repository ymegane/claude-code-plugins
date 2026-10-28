"""review-manager の組み込みの対象ソース。自分にレビュー依頼が来ている open PR を gh で集め、PR ごとに判定する。

出力は review_manager.targets の list_cmd / pr_cmd と同じ形:
  {"viewer": "...", "targets": [{"number": N, "title": "...", "author": "...", "author_association": "MEMBER",
                                 "participants": [{"login": "...", "association": "MEMBER", "bot": false}],
                                 "action": "review|re-review|skip|error", "reason": "...", "head_sha": "..."}]}

author_association（GitHub の作者とリポジトリの関係）と participants（レビュー・コメントの書き手）を出すと、
review-manager は設定の trusted_associations にも trusted_authors にも当たらない人が作者か書き手にいる PR を
自動では渡さない。bot（GitHub App）はリポジトリの管理者が入れたものとして信頼する。出さないソースでは、この判定を行わない。

判定は自分の review（pending と submit 済み）の commit と現在の head の比較だけで決める。
review-requested だけを見るので、submit 済みで依頼が外れた PR は、author が依頼し直すまで拾わない。

  python3 gh_review_requests.py [--output PATH]   依頼が来ている PR を全て判定する
  python3 gh_review_requests.py --pr N            1 件だけ判定する（依頼の有無は問わない）
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
SEARCH = "is:open is:pr archived:false"
# hdlib.util.TEXT と同じ。単体の CLI としても動くよう hdlib に頼らない
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def gh(args, cwd=None):
    r = subprocess.run(["gh", *args], capture_output=True, **TEXT, cwd=cwd)
    if r.returncode != 0:
        return None, (r.stderr.strip() or f"rc={r.returncode}")[-200:]
    try:
        return json.loads(r.stdout or "null"), None
    except json.JSONDecodeError as e:
        return None, str(e)


def gh_list(path, cwd=None):
    """REST の一覧を全ページ取る。--paginate はページごとの配列を連結して出し 1 つの JSON にならないので、要素を 1 行ずつ出させる。"""
    r = subprocess.run(["gh", "api", path, "--paginate", "--jq", ".[]"], capture_output=True, **TEXT, cwd=cwd)
    if r.returncode != 0:
        return None, (r.stderr.strip() or f"rc={r.returncode}")[-200:]
    try:
        return [json.loads(line) for line in r.stdout.splitlines() if line.strip()], None
    except json.JSONDecodeError as e:
        return None, str(e)


def participants(items):
    """レビュー・コメントの書き手の一覧。review session は PR の会話も読むので、review-manager が書き手の信頼を判定する。"""
    seen = {}
    for it in items:
        u = it.get("user") or {}
        if u.get("login"):
            seen[u["login"]] = {"login": u["login"], "association": it.get("author_association"),
                                "bot": u.get("type") == "Bot"}
    return sorted(seen.values(), key=lambda p: p["login"])


def short(sha):
    return (sha or "")[:7] or "unknown"


def decide(detail, my_reviews, viewer):
    if detail.get("isDraft"):
        return "skip", "draft PR"
    if (detail.get("author") or {}).get("login") == viewer:
        return "skip", "自分が author"
    head = detail.get("headRefOid")
    if not my_reviews:
        return "review", "自分のレビュー履歴なし（初回）"
    # REST の返却順に依存しないよう、review id が最大のものを最新とみなす
    pendings = [r for r in my_reviews if r.get("state") == "PENDING"]
    if pendings:
        pending = max(pendings, key=lambda r: r.get("id") or 0)
        if pending.get("commit_id") == head:
            return "skip", f"pending review が最新 head({short(head)}) と同一コミット"
        return "re-review", f"pending review({short(pending.get('commit_id'))}) 作成後に head が {short(head)} へ進んだ"
    submitted = [r for r in my_reviews if r.get("submitted_at")]
    if not submitted:
        return "review", "自分のレビュー状態を判定できず（安全側で再レビュー）"
    last = max(submitted, key=lambda r: r["submitted_at"])
    if last.get("commit_id") != head:
        return "re-review", f"前回レビュー({short(last.get('commit_id'))})以降に head が {short(head)} へ進んだ"
    return "skip", f"前回レビュー以降 head 不変({short(head)})"


def judge(slug, number, viewer, cwd=None):
    # gh pr view の --json は作者とリポジトリの関係（author_association）を出せないので REST で取る
    pr, err = gh(["api", f"repos/{slug}/pulls/{number}"], cwd)
    entry = {"number": number}
    if err:
        return {**entry, "action": "error", "reason": f"pr 取得失敗: {err}"}
    detail = {"isDraft": pr.get("draft"), "author": {"login": (pr.get("user") or {}).get("login")},
              "headRefOid": (pr.get("head") or {}).get("sha")}
    entry.update(title=pr.get("title"), url=pr.get("html_url"), author=detail["author"]["login"],
                 author_association=pr.get("author_association"), head_sha=detail["headRefOid"])
    reviews, err = gh_list(f"repos/{slug}/pulls/{number}/reviews", cwd)
    if err:
        return {**entry, "action": "error", "reason": f"reviews 取得失敗: {err}"}
    talk = list(reviews)
    for path in (f"repos/{slug}/issues/{number}/comments", f"repos/{slug}/pulls/{number}/comments"):
        items, err = gh_list(path, cwd)
        if err:
            return {**entry, "action": "error", "reason": f"コメント取得失敗: {err}"}
        talk += items
    entry["participants"] = participants(talk)
    # REST の reviews は他人の PENDING を返さないが、自分の PENDING は返す
    mine = [{"id": r.get("id"), "state": r.get("state"), "commit_id": r.get("commit_id"),
             "submitted_at": r.get("submitted_at")}
            for r in reviews or [] if (r.get("user") or {}).get("login") == viewer]
    action, reason = decide(detail, mine, viewer)
    return {**entry, "action": action, "reason": reason}


def viewer_login():
    r = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, **TEXT)
    return r.stdout.strip() or None


def list_targets(slug, viewer, cwd=None, limit=50):
    """(結果, エラー)。検索そのものの失敗はエラーを返す。個々の PR の失敗は action=error の行になる。"""
    q = f"{SEARCH} repo:{slug} review-requested:{viewer}"
    prs, err = gh(["search", "prs", *shlex.split(q), "--limit", str(limit), "--json", "number"], cwd)
    if err:
        return None, err
    return {"viewer": viewer, "targets": [judge(slug, p["number"], viewer, cwd) for p in prs or []]}, None


def pr_target(slug, number, viewer, cwd=None):
    return {"viewer": viewer, "targets": [judge(slug, number, viewer, cwd)]}


def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hdlib import config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int)
    ap.add_argument("--output")
    args = ap.parse_args()
    root = config.git_toplevel(os.getcwd())
    slug = config.repo_slug_of(root)
    viewer = viewer_login()
    if not slug or not viewer:
        sys.exit("リポジトリ（origin）か gh のログイン名を特定できない")
    if args.pr:
        data = pr_target(slug, args.pr, viewer, root)
    else:
        data, err = list_targets(slug, viewer, root)
        if err:
            sys.exit(f"検索に失敗: {err}")
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
