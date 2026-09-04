#!/bin/bash
# PreCompact hook: 圧縮前の transcript を永続領域へコピーする。
#
# 圧縮後に「あの調査の生ログを見たい」となったとき、圧縮済みの会話からは戻せない。
# 退避指示（userpromptsubmit-context-guard.sh）が間に合わなかった場合の最後の保険。
#
# 保存先は ~/.claude/backups/context-guard/<epoch>-<session_id>.jsonl。
# セッションごとに新しい 20 世代を残す。
# fail-open: バックアップに失敗しても圧縮は止めない。

set -uo pipefail

command -v jq >/dev/null 2>&1 || exit 0

input=$(cat)
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
transcript_path=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)

[ -n "$session_id" ] || exit 0
[ -n "$transcript_path" ] || exit 0
[ -f "$transcript_path" ] || exit 0

backup_dir="${HOME}/.claude/backups/context-guard"
mkdir -p "$backup_dir" 2>/dev/null || exit 0

dest="${backup_dir}/$(date +%s)-${session_id}.jsonl"
cp "$transcript_path" "$dest" 2>/dev/null || exit 0

# 古い世代を落とす。mapfile は bash 4 以降の builtin で macOS 同梱の bash は 3.2 なので使わない。
find "$backup_dir" -maxdepth 1 -type f -name "*-${session_id}.jsonl" -print 2>/dev/null |
  sort -r |
  tail -n +21 |
  while IFS= read -r old; do
    rm -f "$old" 2>/dev/null || true
  done

exit 0
