#!/bin/bash
# Stop hook: 応答が終わった時点でコンテキスト使用率を見て、閾値を超えていたら
# 圧縮で失われると困る事項を memory へ退避させてから停止させる。
#
# UserPromptSubmit ではなく Stop で促す理由: 依頼の直前に割り込むと、退避が終わるまで
# ユーザーの依頼が後回しになり、そのぶん圧縮も先送りになる。作業が一段落した Stop なら、
# 退避を終えた直後にそのまま /compact へ移れる。
#
# 会話の継続は decision: "block" ではなく additionalContext で行う。ループ保護
# （stop_hook_active と 8 回連続の上限）は同じだが、transcript には hook エラーではなく
# Stop hook feedback として出る。退避の指示は設計どおりの動作であって異常ではない。
#
# 使用率の取得経路: hook の stdin には context_window が渡らないため、毎ターン走る statusline が
# 書き出す `<session_id>.pct` を読む（組み込み方は context-guard/README.md）。
# state が無いときは何もしない。推測で発火させない。
#
# 一度促したら黙る。cooldown マーカー `<session_id>.notified` に通知時の使用率を記録し、
# 圧縮が走ったら PostCompact hook が消して次の警告を許す。
# 圧縮を経ずに使用率が下がった場合の保険として、再武装閾値でも消す。
# さらに、閾値を超えたまま step 分だけ悪化したら、圧縮を待たずにもう一度促す
# （80% で促してから 89% まで上がるような長いセッションが無音になるのを防ぐ）。
#
# ユーザーが Ctrl+C で中断したターンでは Stop 自体が発火しない。その回は促さず、次に
# 応答が終わったときに拾う。
#
# 環境変数:
#   CONTEXT_GUARD_PCT       発火する使用率（既定 80）
#   CONTEXT_GUARD_REARM_PCT ここまで下がったら再武装する使用率（既定 = 発火閾値 - 15）
#   CONTEXT_GUARD_STEP_PCT  前回の通知からこれだけ上がったら再度促す（既定 10）
#   CONTEXT_GUARD_DISABLE   1 なら何もしない

set -uo pipefail

# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

input=$(cat)

[ "${CONTEXT_GUARD_DISABLE:-0}" = "1" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

# この hook が既に会話を継続させている。ここで再度促すと止まらなくなる。
[ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ] && exit 0

# バックグラウンド作業の完了待ちで止まっているだけなら、まだ区切りではない。
bg_count=$(printf '%s' "$input" | jq -r '(.background_tasks // []) | length' 2>/dev/null)
[ "${bg_count:-0}" = "0" ] || exit 0

threshold=${CONTEXT_GUARD_PCT:-80}
rearm=${CONTEXT_GUARD_REARM_PCT:-$(( threshold - 15 ))}

session_id=$(cg_session_id "$input")
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
[ -n "$session_id" ] || exit 0

state_dir=$(cg_state_dir)
pct_file="${state_dir}/${session_id}.pct"
notified_file="${state_dir}/${session_id}.notified"

[ -f "$pct_file" ] || exit 0
pct_raw=$(cat "$pct_file" 2>/dev/null)
[ -n "$pct_raw" ] || exit 0

# 使用率は小数で来る（例 78.4）ので整数へ丸めて比較する
pct=$(printf '%.0f' "$pct_raw" 2>/dev/null) || exit 0

if [ "$pct" -lt "$rearm" ]; then
  rm -f "$notified_file" 2>/dev/null
  exit 0
fi

[ "$pct" -ge "$threshold" ] || exit 0

# 通知済みでも、そこから step 分だけ悪化していれば promote し直す。
step=${CONTEXT_GUARD_STEP_PCT:-10}
last_notified=""
if [ -f "$notified_file" ]; then
  last_notified=$(cat "$notified_file" 2>/dev/null)
  # 使用率を記録する前の形式（空ファイル）は、閾値ちょうどで通知済みとみなす
  case "$last_notified" in '' | *[!0-9]*) last_notified=$threshold ;; esac
  [ "$pct" -ge "$(( last_notified + step ))" ] || exit 0
fi

if memory_dir=$(cg_memory_dir "$cwd"); then
  memory_hint="退避先は ${memory_dir}/ 配下（該当する task-*.md があれば更新、無ければ新規作成し、MEMORY.md の「進行中タスク」から 1 行で辿れるようにする）。"
else
  memory_hint="退避先はこのプロジェクトのメモリディレクトリ配下（運用ルールは MEMORY.md に従う）。"
fi

mkdir -p "$state_dir" 2>/dev/null && printf '%s\n' "$pct" > "$notified_file" 2>/dev/null

if [ -n "$last_notified" ]; then
  headline="[context-guard] コンテキスト使用率が ${pct}% まで上がりました（前回 ${last_notified}% で退避を促しています）。自動圧縮が近づいています。"
else
  headline="[context-guard] コンテキスト使用率が ${pct}%（閾値 ${threshold}%）に達しました。自動圧縮が近づいています。"
fi

cg_emit_context "$(cat <<EOF
${headline}

作業が一段落したこのタイミングで、圧縮で失われると困る事項を memory へ退避してください。${memory_hint}

書き出す対象は、このセッションで新たに確定したもの（要約すれば復元できる話ではなく、失うと調べ直しになるもの）に限ります。

- 決定事項と、その理由
- 実測値・検証結果（コマンドの出力、再現手順、環境固有の値）
- 未解決の論点、次にやること、途中で保留した作業
- 参照した Jira / PR / ファイルパスなどの座標

退避が済んだら、何をどのファイルへ書いたかを 1 行で述べ、いま \`/compact\` を実行できることをユーザーに伝えてください（圧縮の直前には transcript が自動でバックアップされます）。
退避の報告だけで終えてください。直前のターンで完了した作業の続きを、ここから新しく始めないでください。
すでに退避済みで新しく書くことが無ければ、その旨を 1 行述べて終えて構いません。
EOF
)" "context-guard: ctx ${pct}% — 退避を指示しました（完了後 /compact 可）" "Stop"
exit 0
