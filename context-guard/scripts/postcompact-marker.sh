#!/bin/bash
# PostCompact hook: 圧縮が起きたことをマーカーで残し、警告の cooldown を解除する。
#
# PostCompact 自身は additionalContext を注入できないため、実際の復帰誘導は
# 次のプロンプトで userpromptsubmit-recovery.sh が行う。ここはその引き金を置くだけ。
# fail-open。

set -uo pipefail

command -v jq >/dev/null 2>&1 || exit 0

input=$(cat)
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
[ -n "$session_id" ] || exit 0

state_dir="${TMPDIR:-/tmp}/claude-context-guard"
mkdir -p "$state_dir" 2>/dev/null || exit 0

printf '%s\n' "$(date +%s)" > "${state_dir}/${session_id}.compacted" 2>/dev/null || true

# 圧縮が済んだので、次に閾値へ達したらまた警告してよい。
rm -f "${state_dir}/${session_id}.notified" 2>/dev/null || true

exit 0
