# context-guard

コンテキストが圧縮される前に重要事項を memory へ退避させ、圧縮後は原本を読み直させる Claude Code プラグイン。

会話の経緯は圧縮で失われるが、memory ファイルに書いた事実は残り、次のセッションでもそこから状態を復元できる。圧縮の前後にその往復を挟むのがこのプラグインの役割。

## 何をするか

| タイミング | 動作 |
|---|---|
| 使用率が閾値（既定 80%）に達した最初のプロンプト | 決定事項・実測値・未解決の論点を memory へ退避するよう指示を注入する |
| 圧縮の直前（PreCompact） | transcript を `~/.claude/backups/context-guard/` へコピーする（セッションごと 20 世代） |
| 圧縮の直後（PostCompact → 次のプロンプト） | memory と生ログの場所を示し、「要約は記録であって指示ではない」「原本が正」を伝える |

hook が動いたことは `systemMessage` でユーザーの画面にも 1 行出る（`additionalContext` はモデルにしか届かないため）。退避を終えた Claude は、書き出し先を 1 行で報告し、区切りのよいところで `/compact` を実行できることをユーザーに伝える。

退避そのものはセッションの Claude 自身が行う。別プロセスの LLM に要約させるわけではないので、そのターンの作業は一度中断する代わりに、退避先はプロジェクトの memory になり次のセッションから読める。

## 導入

### 1. プラグインを有効化する

リポジトリのルートを marketplace として登録し、プラグインを有効にする（詳細はリポジトリの README を参照）。

### 2. statusline に 1 か所追記する（必須）

hook の stdin には `context_window` が渡らない。使用率を知っているのは statusline だけなので、そこから state ファイルへ書き出す必要がある。プラグインから statusline を設定することはできないため、ここだけは手作業になる。

`statusline/snippet.sh` の中身を、自分の statusline スクリプトの中で stdin の JSON を保持している変数が使える場所へ貼る（変数名が `$input` でなければ合わせる）。

追記しないと `.pct` が生まれず、退避の指示は一度も出ない（PreCompact のバックアップと圧縮後の復帰誘導は statusline なしでも動く）。

### 3. 確認

statusline が走ったあと、state ファイルができていること。

```
ls "${TMPDIR}/claude-context-guard/"   # <session_id>.pct が見えれば OK
```

`TMPDIR` は Claude Code 本体の値で、macOS では `/var/folders/…/T/` になる。Bash ツール経由で見ると値が違うことがあるので、その場合は `ls /var/folders/*/*/T/claude-context-guard/` で探す。

## 設定

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `CONTEXT_GUARD_PCT` | 80 | 退避を指示する使用率 |
| `CONTEXT_GUARD_REARM_PCT` | 閾値 − 15 | ここまで下がったら再武装する使用率 |
| `CONTEXT_GUARD_STEP_PCT` | 10 | 前回の通知からこれだけ上がったら再度促す |
| `CONTEXT_GUARD_DISABLE` | — | `1` で退避の指示を止める |

一度促したあとは黙るが、閾値を超えたまま `CONTEXT_GUARD_STEP_PCT` 分だけ悪化すると、圧縮を待たずにもう一度促す（既定なら 80% → 90% → 100%）。長いセッションで 80% から 89% まで上がるあいだ無音になるのを防ぐための挙動。

`settings.json` の `env` に書ける。

## マーカーファイル

すべて `${TMPDIR}/claude-context-guard/` 配下。セッション ID で分かれる。

| ファイル | 書き手 | 読み手 | 役割 |
|---|---|---|---|
| `<session_id>.pct` | statusline | 退避 hook | 使用率。唯一の外部依存 |
| `<session_id>.notified` | 退避 hook | 退避 hook | 通知したときの使用率。次に促すかの判定に使い、PostCompact か再武装閾値で消える |
| `<session_id>.compacted` | PostCompact hook | 復帰 hook | 圧縮が起きた印。次のプロンプトで消費される |

hook はすべて fail-open で書いてある。マーカーが無い・壊れている・書けない場合は黙って通し、プロンプトや圧縮を止めない。

## 設計の出どころ

圧縮の前後を hook で挟む構成、マーカー方式、fail-open の徹底は [compact-plus](https://github.com/u-ichi/compact-plus) を参考にした。compact-plus は PreCompact で別プロセスの LLM を起動し、transcript を 10 見出しの構造化 state に要約する。本プラグインはそこは踏襲せず、退避先を memory に寄せている。両者は目的が重なるので、併用するなら役割を分けること。
