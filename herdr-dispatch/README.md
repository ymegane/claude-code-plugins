# herdr-dispatch

Herdr（コーディングエージェント向けのターミナルマルチプレクサ）のペインで動く Claude Code セッションへ仕事を振り分ける manager の skill 2 本。manager 役のセッションは振り分けだけを行い、レビューや修正の作業はペインに常駐させた別のセッションが行う。

| skill | 役割 |
|---|---|
| `review-manager` | 自分に来たレビュー依頼を監視し、枠のペイン（review session）へ `/clear` してから依頼を渡す。dependabot の PR も設定で扱える |
| `session-manager` | 自分の open PR に届いた指摘を監視し、その PR を作ったセッションを空きペインで復元して対応方針の提示を依頼する。中断したセッションの再開と、新しい課題のセッションの起動も行う |

どちらも状態を state.json に持つ。manager の会話が圧縮されても、manager を再起動しても、`status` で割り当て・待ち行列・ユーザーへの質問を取り戻せる。監視は Bash のバックグラウンド実行で張り、イベントを 1 行でも出したら終了する。manager は終了の通知で起こされ、行を処理して張り直す。

## 動作要件

- Herdr の中（`HERDR_ENV=1`）で manager を起動する
- `sm.py` と `rm.py` は Claude Code の sandbox の外で実行する。sandbox の中では herdr の socket が拒否され、ペインの状態を取れない。そのときスクリプトは「Herdr に届かない」と出して止まり、動いているセッションが無いものとしては扱わない
- `python3`（3.9 以上、標準ライブラリだけ）、`gh`（認証済み）、`git`
- 劣化の検知で `ps eww` を使う。macOS でだけ確認している

## 導入

```bash
claude plugin install herdr-dispatch@ymegane-plugins
```

設定ファイルが無くても既定値で動く。既定では review-manager は `/code-review {n}` を依頼し、自分にレビュー依頼が来ている PR を組み込みの判定で拾う。起動コマンドは素の `claude`。

## 設定

置き場所は `~/.config/herdr-dispatch/config.json`（環境変数 `HERDR_DISPATCH_CONFIG` で変えられる）。リポジトリの外に置くので、社内の起動コマンドや依頼文を公開リポジトリに含めずに済む。書き方は [examples/config.example.json](./examples/config.example.json) を参照。

値は builtin の既定値、`defaults`、`repos["owner/name"]` の順に深くマージする。`owner/name` は作業ディレクトリの git の origin から決まる。manager ごとの `launch` / `health` は、リポジトリの `launch` / `health` の上に重なる。文字列の値を `@` で始めると、設定ファイルのディレクトリからの相対パスのファイルの中身を使う（長い依頼文をファイルに分けるため）。

`rm.py info` / `sm.py info` で、解決した設定を確認できる。

### 起動コマンド（`launch`）

| キー | 既定 | 意味 |
|---|---|---|
| `cli` | `claude` | ペインのシェルに打ち込むコマンド。シェルのエイリアスや関数も書ける |
| `template` | `cd {cwd} && {cli} {args}` | 起動コマンドの形。`{args}` に `--resume` / `--session-id` / `-n` / `--permission-mode` が入る |
| `permission_mode` | `auto` | 新しく起動するセッションの permission mode |
| `start_timeout` | `1800` | agent の出現を待つ秒数。`cli` が承認を待つ間も含む |
| `approval_hint` | なし | 起動前にユーザーへ伝える文。設定すると manager が PushNotification で伝える |

起動は `herdr pane run` でペインの対話シェルへコマンドを打ち込む。そのため、認証情報を環境に足してから claude を起動するラッパーを `cli` に書ける。Herdr の再起動やセッションの復元は素の `claude --resume` で起こすので、ラッパーが足した環境は消える。`health.require_env` にその環境変数の名前を書くと、消えたセッションを劣化として知らせる。

### 依頼文

review-manager の依頼文は `review_manager.kinds.<kind>` の `command` と `template` から作る。使えるプレースホルダは `{n}`（PR 番号）、`{head}`、`{kind}`、`{lock_started_at}`、`{report}`、`{viewer}`。`template` の本文は 1 行に詰めてから `（review-manager からの依頼。…）` の形で `command` の後ろに付く。

| kind のキー | 意味 |
|---|---|
| `command` | 依頼のスラッシュコマンド |
| `template` | 依頼文の本文（任意） |
| `locking` | `review_manager.lock` があるとき、渡す前にロックを取るか |
| `require_review_request` | 依頼に自分が残っていることを渡す条件にするか |
| `check_target` | 渡す前に PR の判定を見て、`skip` なら渡さないか |

session-manager の依頼文は `session_manager.requests` の `review` / `resume` / `start`。`review` では `{pr}` `{since}` `{events}` `{by}` `{kinds}` `{where}`、`resume` では `{last_prompt}` `{state}` `{where}`、`start` では `{key}` を使える。

### レビューの監視（`session_manager.feedback`）

session-manager の watch は、自分の open PR に届いたレビューとコメントを見て、紐付くセッションを復元し、指摘の評価を依頼する。復元したセッションは指摘の本文を読むので、本文に指示を仕込める他者のコメントでは復元しない。

| キー | 既定 | 意味 |
|---|---|---|
| `trusted_associations` | `["OWNER", "MEMBER", "COLLABORATOR"]` | 復元のきっかけにする作者の、リポジトリとの関係（GitHub の `authorAssociation`） |
| `trusted_authors` | `[]` | 上記以外で復元のきっかけにする作者のログイン名 |
| `coderabbit_login` | `coderabbitai` | CodeRabbit のログイン名。新しい指摘がある review だけを数え、作者の関係は問わない |
| `ignored_authors` | bot 数種 | 復元のきっかけに数えない作者（`untrusted=` の数には入る） |

これ以外の作者の書き込みは `UNTRUSTED_FEEDBACK` として報告だけする。bot（GitHub App）はリポジトリの管理者が入れたものとして信頼する。復元したセッションは PR の会話を gh でまとめて読むので、信頼できる作者のレビューで復元するときでも、PR に外部の人の書き込みがあれば（`FEEDBACK` 行の `untrusted=` が 1 以上。review の state や本文の有無、`ignored_authors` を問わず全件で数える。レビューかコメントが 50 件を超えて数えきれないときは `unknown` で、同じ扱い）`default` mode で起動し、操作のたびにユーザーの承認を求めさせる。外部の書き込みが無くても、前回の permission mode が `bypassPermissions` なら引き継がず、`launch.permission_mode` で起動する。

### 対象の判定（`review_manager.targets`）

既定の `builtin` は、`review-requested:<自分>` の open PR を集め、自分の review（pending と submit 済み）の commit と現在の head を比べて `review` / `re-review` / `skip` を決める。独自の判定を使うときは、`list_cmd`（`{out}` に JSON を書く）と `pr_cmd`（`{n}` の 1 件を標準出力に JSON で出す）を設定する。JSON の形は [scripts/sources/gh_review_requests.py](./scripts/sources/gh_review_requests.py) の docstring にある。

target に `author_association`（GitHub の作者とリポジトリの関係）と `participants`（レビュー・コメントの書き手）があると、`review_manager.trusted_associations`（既定 `["OWNER", "MEMBER", "COLLABORATOR"]`）にも `trusted_authors` にも当たらない人が作者か書き手にいる PR は `UNTRUSTED_REVIEW_REQUEST` として報告だけになり、`handoff` は `--allow-untrusted` が無ければ渡さない。bot（GitHub App）はリポジトリの管理者が入れたものとして信頼する。CODEOWNERS はフォークからの PR でもレビューを依頼し、公開リポジトリでは誰でもコメントできるので、外部の人の書いたものが review session に届きうる。`builtin` はこの値を出す。独自のソースで出さないときは、その分の判定を行わない。

### ロック（`review_manager.lock`、任意）

同じ PR へ投稿する別の実行（定期実行のレビューなど）と排他するときに設定する。`path` のファイルは `started_at` と `refreshed_at`（ISO 8601）を持つ JSON で、`acquire_cmd` / `refresh_cmd` / `release_cmd` がそれを作り、更新し、消す。manager は割り当て中に `refresh_every_min` ごとに延長し、割り当てが無くなったら解放する。延長では `started_at` を変えないので、依頼文で伝えた `{lock_started_at}` は有効なまま残る。

### リポジトリ固有の運用（`notes`）

`review_manager.notes` / `session_manager.notes` にファイルを指定すると、manager は起動時にそれを読み、skill の手順より優先して従う。リポジトリ固有の手順（seed の実行方法、承認の運用など）はここに書く。

## 状態の置き場所

`/tmp/claude-<uid>/herdr-dispatch/<manager>/`（環境変数 `HERDR_DISPATCH_STATE_DIR` か設定の `state_dir` で変えられる）。待ち行列には manager が実行する再実行コマンドが入るので、スクリプトはこのディレクトリを 0700 で作り、自分の所有でないとき、symlink のとき、親を他のユーザーが書き換えられるときは止まる。ローカルスキルなど以前のバージョンの状態ディレクトリから切り替えるときは、前の監視を止めてから `rm.py import-state <旧ディレクトリ>` / `sm.py import-state <旧ディレクトリ>` で取り込む。取り込むのは、自分の所有で他のユーザーが書けないディレクトリとファイルだけ。あなた待ちの `cmd` と session-manager の待ち行列は取り込まない（session-manager の待ち行列は一覧を出すので、必要なら出し直す）。

## テスト

```bash
python3 -m unittest discover -s herdr-dispatch/tests
```
