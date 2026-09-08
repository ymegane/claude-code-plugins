# japanese-writing

日本語の技術文書を書く、推敲するための文章規範。
常時適用する output style と、深く当てるための skill 2 本で構成する。

## 出典

skill 2 本は、k16shikano 氏が公開している gist をベースにしている。

- [japanese-tech-writing/SKILL](https://gist.github.com/k16shikano/fd287c3133457c4fd8f5601d34aa817d)
- [cognitive-rhythm-writing/SKILL.md](https://gist.github.com/k16shikano/eb2929f13ed19c97188393d297be8432)

いずれもライセンスは Unlicense（gist のコメントで著者が明記している）。

本文はほぼ原典のままで、変更点は次のとおり。

- `japanese-tech-writing`：冒頭に `cognitive-rhythm-writing` との併用指示を追加した。それ以外は原典のまま。
- `cognitive-rhythm-writing`：description だけを書き換え、適用場面を具体化して `japanese-tech-writing` との併用を明記した。本文は原典のまま。

output style（`output-styles/japanese-writing.md`）も、この 2 本から常時適用する中核を抜き出して再構成したもので、規範の中身は原典に帰属する。

## 3 つの層

| 層 | 実体 | 効くタイミング | 分量 |
|---|---|---|---|
| output style | `output-styles/japanese-writing.md` | セッション中つねに | 約 9 KB |
| skill（正確さ・論証） | `japanese-tech-writing` | 呼ばれたとき | 約 20 KB |
| skill（推進力） | `cognitive-rhythm-writing` | 呼ばれたとき | 約 17 KB |

output style はシステムプロンプトに載るので、文章が生成される前に効く。
チャット応答の地の文にも適用される。
skill は生成前に読ませる必要があり、章や記事のように腰を据えて書く場面で呼ぶ。

output style には `keep-coding-instructions: true` を設定してある。
Claude Code の組み込みのソフトウェアエンジニアリング指示を残したまま、文章規範を足す。
コードを書きながらドキュメントも書くセッションで使える。

## 有効化

`.claude/settings.local.json`（プロジェクト単位）か `~/.claude/settings.json`（ユーザー単位）に書く。

```json
{
  "outputStyle": "japanese-writing"
}
```

ターミナルでは `/config` の Output style から選ぶこともできる。
選択結果は `.claude/settings.local.json` に保存される。

デスクトップアプリでは `/config` がスタイルのピッカーを開かず Settings > Claude Code を開くため、設定ファイルを直接編集する。

**反映はセッション開始時**。
output style はシステムプロンプトの一部で、Claude Code がセッション開始時に一度だけ読む。
設定を変えたら `/clear` するか新しいセッションを開く。

### ピッカーに出ないとき

プラグイン提供の output style が名前で解決されない場合は、ユーザーレベルへシンボリックリンクを張る。

```bash
mkdir -p ~/.claude/output-styles
ln -sf ~/Developer/github/claude-code-plugins/japanese-writing/output-styles/japanese-writing.md ~/.claude/output-styles/
```

実体は 1 つのままなので、リポジトリ側を編集すれば両方に効く。

## 制約

**サブエージェントには効かない。**
サブエージェントは自身のシステムプロンプトで動く（fork のみ例外）。
レビュー系のサブエージェントが日本語で書く分には適用されないので、規範を効かせたければエージェント定義側に書く。

**`force-for-plugin` は使っていない。**
プラグイン有効中つねに適用してユーザーの `outputStyle` 設定を上書きする挙動になるため、Concise など他のスタイルを選べなくなる。
手動選択に留めてある。

## 規範の分担

output style には、違反が起きやすく判定も明快な中核を置いた。

- 整形（一文一行、ダッシュと中黒の禁止、太字の節度）
- 断定と推量の区別
- 論証の厳密さ（因果の機構、腑分け、例が主張を支えるか）
- LLM っぽい表現の禁止
- 冗長の排除
- 演出の抑制
- 駄文の判定（状況を更新するか、文書を更新するか）

skill 本体に残したのは、腰を据えて書くときにだけ要るもの。

- `japanese-tech-writing`：パラグラフライティング、読み手の負荷の管理、視点と語り、見出しの付け方、読者への誠実さ
- `cognitive-rhythm-writing`：文の拍、段落の密度波形、冒頭と節の入り方、緊張の管理、執筆後の点検手順

output style の末尾に、どちらをいつ読むかの導線を書いてある。
