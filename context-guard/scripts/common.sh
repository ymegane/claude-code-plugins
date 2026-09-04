#!/bin/bash
# context-guard の各 hook が共有するヘルパ。
#
# 全 hook は fail-open で書く。マーカーが無い・壊れている・書けないときは黙って exit 0 し、
# ユーザーのプロンプトや圧縮を止めない。hook の失敗で作業が止まるほうが害が大きい。

# セッションごとの状態を置くディレクトリ。
# 書き手は statusline（使用率）と各 hook（マーカー）で、両者とも Claude Code 本体の子プロセス。
# Bash ツール経由の TMPDIR とは値が違うことがある（本体は macOS 既定の /var/folders/... を使う）。
cg_state_dir() {
  printf '%s' "${TMPDIR:-/tmp}/claude-context-guard"
}

# hook の stdin JSON から session_id を取り出す。取れなければ空文字。
cg_session_id() {
  printf '%s' "$1" | jq -r '.session_id // empty' 2>/dev/null
}

# additionalContext を UserPromptSubmit の出力形式で標準出力へ書く。
# 第 2 引数を渡すと systemMessage としてユーザーの画面にも 1 行出す。
# additionalContext はモデルにしか届かないので、hook が動いた事実を人が知るにはこちらが要る。
cg_emit_context() {
  if [ -n "${2:-}" ]; then
    jq -n --arg ctx "$1" --arg msg "$2" '{
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: $ctx
      },
      systemMessage: $msg
    }'
  else
    jq -n --arg ctx "$1" '{
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: $ctx
      }
    }'
  fi
}

# cwd に対応する auto memory ディレクトリ。実在するときだけパスを返す
# （存在しないパスを指示に載せない）。Claude Code の命名は cwd の / と . を - に置換したもの。
cg_memory_dir() {
  local cwd="$1" encoded dir
  [ -n "$cwd" ] || return 1
  encoded=${cwd//\//-}
  encoded=${encoded//./-}
  dir="${HOME}/.claude/projects/${encoded}/memory"
  [ -d "$dir" ] || return 1
  printf '%s' "$dir"
}
