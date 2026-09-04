#!/usr/bin/env bash
# memory-tidy health check — サイズ / 空ファイル / 壊れリンク / PR 番号抽出・照合
#
# usage: bash check_health.sh <memory-dir> [--check-prs]
#   --check-prs: MEMORY.md 中の PR 番号を gh で実状態照合する。
#                gh のリポジトリ解決のため、プロジェクトリポジトリ内から実行するか GH_REPO を設定すること。
#
# 読み取り専用（何も変更しない）。

set -uo pipefail

DIR="."
CHECK_PRS=0
for arg in "$@"; do
  case "$arg" in
    --check-prs) CHECK_PRS=1 ;;
    *) DIR="$arg" ;;
  esac
done

ORIG_PWD="$PWD"
cd "$DIR" || { echo "ERROR: cannot cd to $DIR"; exit 1; }

echo "== MEMORY.md サイズ（行 / バイト） =="
if [ -f MEMORY.md ]; then
  wc -l -c MEMORY.md
  # 読込上限。プロジェクトの運用ルールに合わせて MEMORY_BYTE_LIMIT で上書きする
  LIMIT="${MEMORY_BYTE_LIMIT:-24985}"
  BYTES=$(wc -c < MEMORY.md | tr -d ' ')
  if [ "$BYTES" -gt "$LIMIT" ]; then
    echo "!! 読込上限 ${LIMIT}B を超過（${BYTES}B）— 末尾セクションが自動ロード時にドロップする"
  fi
  echo "-- 末尾セクション（超過時に最初に落ちる。消えて困るものが並んでいないか）--"
  grep -n '^## ' MEMORY.md | tail -3
else
  echo "MEMORY.md が見つからない"
fi

echo
echo "== .md ファイル行数（降順 上位20） =="
wc -l ./*.md 2>/dev/null | sort -rn | awk '$2 != "total"' | head -20

echo
echo "== 空ファイル（0 バイト .md） =="
EMPTY=$(find . -maxdepth 1 -name '*.md' -size 0)
if [ -n "$EMPTY" ]; then echo "$EMPTY"; else echo "(なし)"; fi

echo
echo "== 壊れリンク（](./file) 形式でファイル実体が無い） =="
BROKEN=0
for src in ./*.md; do
  while IFS= read -r target; do
    [ -z "$target" ] && continue
    if [ ! -e "$target" ]; then
      echo "BROKEN: $src -> $target"
      BROKEN=1
    fi
  done < <(grep -o '](\./[^)]*)' "$src" 2>/dev/null | sed 's/](\.\///; s/)$//' | sort -u)
done
[ "$BROKEN" -eq 0 ] && echo "(なし)"

echo
echo "== 未解決 wiki リンク（[[name]] で name.md が無い。※意図的な未作成マーカーの可能性もある） =="
BROKEN_WIKI=0
for src in ./*.md; do
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    if [ ! -f "$name.md" ]; then
      echo "MISSING: $src -> [[$name]]"
      BROKEN_WIKI=1
    fi
  done < <(grep -o '\[\[[^]|]*\]\]' "$src" 2>/dev/null | sed 's/\[\[//; s/\]\]//' | sort -u)
done
[ "$BROKEN_WIKI" -eq 0 ] && echo "(なし)"

echo
echo "== frontmatter / description 欠落（recall の応募資格が無い知見ファイル） =="
# task-*.md は進行中エントリから直接辿るので対象外
NOFM=0
for src in ./*.md; do
  base=$(basename "$src")
  case "$base" in MEMORY.md|KNOWLEDGE.md|task-*) continue ;; esac
  if ! head -1 "$src" | grep -q '^---$'; then
    echo "NO FRONTMATTER: $base"
    NOFM=1
  elif ! head -8 "$src" | grep -q '^description:'; then
    echo "NO DESCRIPTION: $base"
    NOFM=1
  fi
done
[ "$NOFM" -eq 0 ] && echo "(なし)"

echo
echo "== 被参照ゼロ（他のどの .md からも名前が出ない＝索引にも本文にも辿る道が無い） =="
ORPHAN=0
for src in ./*.md; do
  base=$(basename "$src")
  case "$base" in MEMORY.md|KNOWLEDGE.md) continue ;; esac
  name="${base%.md}"
  if ! grep -l -F -- "$name" ./*.md 2>/dev/null | grep -qv "^\./${base}$"; then
    echo "ORPHAN: $base"
    ORPHAN=1
  fi
done
[ "$ORPHAN" -eq 0 ] && echo "(なし)"

echo
echo "== MEMORY.md 内の PR 番号 =="
PRS=$(grep -oE 'PR #[0-9]+|#[0-9]{4,6}' MEMORY.md 2>/dev/null | grep -oE '[0-9]+' | sort -un)
if [ -z "$PRS" ]; then
  echo "(なし)"
else
  echo "$PRS" | tr '\n' ' '
  echo
  if [ "$CHECK_PRS" -eq 1 ]; then
    if command -v gh >/dev/null 2>&1; then
      echo "-- gh 照合（state / mergedAt。OPEN=進行中妥当 / MERGED=完了処理漏れ疑い）--"
      for pr in $PRS; do
        printf '#%s: ' "$pr"
        (cd "$ORIG_PWD" && gh pr view "$pr" --json state,mergedAt -q '.state + " " + (.mergedAt // "-")' 2>/dev/null) || echo "(取得失敗)"
      done
    else
      echo "gh が無いため照合スキップ（--check-prs 指定時のみ照合）"
    fi
  else
    echo "（--check-prs を付けると gh で実マージ状態を照合）"
  fi
fi
