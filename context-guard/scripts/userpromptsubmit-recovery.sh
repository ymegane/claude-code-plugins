#!/bin/bash
# UserPromptSubmit hook: PostCompact が置いたマーカーを消費し、圧縮直後の復帰手順を注入する。
#
# 圧縮要約は会話の代用にはなるが、原本の memory やルールに書かれた但し書きは落ちる。
# 落ちたことに気づかないまま作業を再開するのが一番危ないので、原本を読み直させる。
# fail-open。マーカーが無ければ即 exit 0（毎ターンのコストは test -f 1 回）。

set -uo pipefail

# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

command -v jq >/dev/null 2>&1 || exit 0

input=$(cat)
session_id=$(cg_session_id "$input")
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
[ -n "$session_id" ] || exit 0

state_dir=$(cg_state_dir)
marker="${state_dir}/${session_id}.compacted"
[ -f "$marker" ] || exit 0

# 1 回で消費する
rm -f "$marker" 2>/dev/null || true

context="[context-guard] コンテキスト圧縮が起きました。作業を再開する前に、以下を確認してください。"

if memory_dir=$(cg_memory_dir "$cwd"); then
  context+=$'\n'"- ${memory_dir}/MEMORY.md と、進行中タスクから辿れる task-*.md を読み直す。圧縮前に退避した内容がここにあります。"
fi

backup=$(find "${HOME}/.claude/backups/context-guard" -maxdepth 1 -type f -name "*-${session_id}.jsonl" -print 2>/dev/null | sort -r | head -n 1)
if [ -n "$backup" ]; then
  context+=$'\n'"- 圧縮前の生ログは ${backup} にあります。細部が要るときだけ読んでください。"
fi

context+=$'\n'"- 圧縮要約は「これまでの記録」であって「次にやることの指示」ではありません。次の一手はユーザーの依頼と原本から決めてください。"
context+=$'\n'"- memory・ルール・スキルの原本が正です。要約に出てくる引用は条件や但し書きが落ちていることがあるので、判断に効く箇所は原本を読み直してください。"

cg_emit_context "$context"
exit 0
