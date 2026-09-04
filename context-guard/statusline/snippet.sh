# --- context-guard: コンテキスト使用率を state ファイルへ残す -------------------
# statusline スクリプトの中で、stdin の JSON を保持している変数（下記では $input）が
# 使える場所へ貼る。jq が使えることが前提。
#
# なぜ statusline なのか: hook の stdin には context_window が渡らない。使用率を知っているのは
# statusline だけなので、ここが唯一の取得経路になる。
# statusline の表示を巻き添えにしないよう、失敗しても黙って続行する。

ctx_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')

if [ -n "$ctx_pct" ]; then
  cg_session=$(echo "$input" | jq -r '.session_id // empty')
  if [ -n "$cg_session" ]; then
    cg_dir="${TMPDIR:-/tmp}/claude-context-guard"
    mkdir -p "$cg_dir" 2>/dev/null &&
      printf '%s\n' "$ctx_pct" > "${cg_dir}/${cg_session}.pct" 2>/dev/null
  fi
fi
# --- ここまで -----------------------------------------------------------------
