# claude-code-plugins

個人用の Claude Code プラグイン置き場。リポジトリのルートがそのまま marketplace になっている。

| プラグイン | 中身 | 概要 |
|---|---|---|
| [context-guard](./context-guard/) | hooks | コンテキスト圧縮に備えて重要事項を memory へ退避させ、transcript を退避し、圧縮後は原本を読み直させる |
| [memory-tidy](./memory-tidy/) | skill | auto memory（MEMORY.md ＋ トピック/タスクファイル群）を整理・圧縮する |
| [japanese-writing](./japanese-writing/) | output style + skills | 日本語技術文書の文章規範。常時適用の output style と、正確さ・論証／推進力の skill 2 本 |
| [fable-advisor](./fable-advisor/) | skill + agent | 設計判断のセカンドオピニオン、難航したデバッグの深掘り診断を助言専任のサブエージェントに相談する |
| [herdr-dispatch](./herdr-dispatch/) | skills | Herdr のペインに常駐させたセッションへ、受けたレビュー依頼・自分の PR への指摘対応・中断したセッションの再開を振り分ける |

japanese-writing の文章規範は [k16shikano](https://gist.github.com/k16shikano) 氏が公開している gist（Unlicense）をベースにしている。詳細は [japanese-writing/README.md](./japanese-writing/README.md) を参照。
context-guard の設計は [compact-plus](https://github.com/u-ichi/compact-plus) を参考にした。

## 導入

```bash
claude plugin marketplace add ymegane/claude-code-plugins
claude plugin install context-guard@ymegane-plugins
```

入れられるのは `context-guard` / `memory-tidy` / `japanese-writing` / `fable-advisor` / `herdr-dispatch` の 5 つ。要るものだけ入れる。

`~/.claude/settings.json` に直接書いてもよい。

```json
{
  "extraKnownMarketplaces": {
    "ymegane-plugins": {
      "source": {
        "source": "github",
        "repo": "ymegane/claude-code-plugins"
      }
    }
  },
  "enabledPlugins": {
    "context-guard@ymegane-plugins": true,
    "memory-tidy@ymegane-plugins": true
  }
}
```

### 別途要る設定

- **context-guard**: statusline への追記が要る。追記しないと退避の指示は一度も出ない（hook の stdin にはコンテキスト使用率が渡らないため）。[context-guard/README.md](./context-guard/README.md)
- **japanese-writing**: output style は `outputStyle` の設定で有効にする。skill 2 本は設定なしで使える。[japanese-writing/README.md](./japanese-writing/README.md)
- **herdr-dispatch**: 起動コマンドや依頼文をリポジトリごとに変えるときは `~/.config/herdr-dispatch/config.json` を書く。無ければ既定値で動く。[herdr-dispatch/README.md](./herdr-dispatch/README.md)

memory-tidy と fable-advisor は、インストールすればそのまま使える。

### 動作要件

- context-guard の hook は `bash` と `jq` を使う。`jq` が無い環境では hook は黙って何もしない（fail-open で書いてある）
- memory-tidy の health check スクリプトは `bash`。`--check-prs` を付けるときは `gh` も要る
- japanese-writing と fable-advisor に追加の依存はない
- herdr-dispatch は Herdr の中で動かす。`python3`（3.9 以上）・`gh`・`git` を使う
- 開発と動作確認は macOS でのみ行っている。同梱の bash は 3.2 なので、それで動く書き方にしてある。Linux でも動くはずだが確認はしていない

## 開発

手元で編集しながら使うなら、クローンしたローカルパスを `directory` ソースで登録する。

```json
{
  "extraKnownMarketplaces": {
    "ymegane-plugins": {
      "source": {
        "source": "directory",
        "path": "/path/to/claude-code-plugins"
      }
    }
  }
}
```

`directory` ソースは `~/.claude/plugins/known_marketplaces.json` の `installLocation` がそのディレクトリ自身を指す（`github` ソースのようにキャッシュへクローンされない）。編集はそのまま実体に効き、反映は Claude Code の再起動時。

変更前の検証:

```bash
claude plugin validate ./context-guard
claude plugin validate ./memory-tidy
claude plugin validate ./japanese-writing
claude plugin validate ./fable-advisor
claude plugin validate ./herdr-dispatch
claude plugin validate .
```
