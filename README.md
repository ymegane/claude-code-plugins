# claude-code-plugins

個人用の Claude Code プラグイン置き場。リポジトリのルートがそのまま marketplace になっている。

| プラグイン | 中身 | 概要 |
|---|---|---|
| [context-guard](./context-guard/) | hooks | コンテキスト圧縮に備えて重要事項を memory へ退避させ、transcript を退避し、圧縮後は原本を読み直させる |
| [memory-tidy](./memory-tidy/) | skill | auto memory（MEMORY.md ＋ トピック/タスクファイル群）を整理・圧縮する |
| [japanese-writing](./japanese-writing/) | output style + skills | 日本語技術文書の文章規範。常時適用の output style と、正確さ・論証／推進力の skill 2 本 |
| [fable-advisor](./fable-advisor/) | skill + agent | 設計判断のセカンドオピニオン、難航したデバッグの深掘り診断を助言専任のサブエージェントに相談する |

## 導入

GitHub から入れる。private リポジトリなので、`gh auth login` 済みか SSH 鍵が通っていることが前提。

```bash
claude plugin marketplace add ymegane/claude-code-plugins
claude plugin install context-guard@ymegane-plugins
claude plugin install memory-tidy@ymegane-plugins
```

`~/.claude/settings.json` に直接書くこともできる。

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

context-guard は statusline への追記が別途要る。[context-guard/README.md](./context-guard/README.md) を参照。
japanese-writing の output style は `outputStyle` の設定が別途要る。[japanese-writing/README.md](./japanese-writing/README.md) を参照。

## 開発

自分で編集しながら使うなら、`github` ソースではなくクローンしたローカルパスを `directory` ソースで登録する。

```json
{
  "extraKnownMarketplaces": {
    "ymegane-plugins": {
      "source": {
        "source": "directory",
        "path": "/Users/<user>/Developer/github/claude-code-plugins"
      }
    }
  }
}
```

`directory` ソースは `~/.claude/plugins/known_marketplaces.json` の `installLocation` がこのリポジトリ自身を指す（`github` ソースのようにキャッシュへクローンされない）。編集はそのまま実体に効き、反映は Claude Code の再起動時。

変更前の検証:

```bash
claude plugin validate ./context-guard
claude plugin validate ./memory-tidy
```
