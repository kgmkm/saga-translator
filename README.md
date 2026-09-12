# saga-translator

文脈理解型の小説日訳スキル。小説ファイル(TXT/MD/EPUB)を、登場人物の訳語・一人称・語調を**作品全体で固定**したまま日本語へ翻訳する。言語固有のtipsは `references/lang_{lang}.md` と `scripts/lang_{lang}.py`。v1 の品質保証は中国語。

Hermes Agentで開発された汎用AIエージェント用のスキル(SKILL.md + Python scripts)として動作する。LLM処理はすべてエージェントのモデルが担い、このスキル自体はAPIキー・外部通信を持たない。

## 特長

- **訳語固定** — `analysis/characters-[ネタバレあり].toml` に訳語・一人称・口調(arcsによる変化)を登録。機械QAが原語の残留を検出する
- **ブロック翻訳** — 長編をコンテキストに合わせて分割し、前ブロック訳文・あらすじ・用語集を文脈として投入
- **段落1対1** — 段落連番の機械検証(QA)を通過した訳文のみ正式出力。対訳ファイルは出さない
- **ルビ記法** — `{血脈の契約|けつみゃくのけいやく}` 形式(VFM互換)
- **標準ライブラリのみ** — 依存ゼロ。置けば動く

## インストール(例: Hermes Agentの場合)

```bash
git clone https://github.com/kgmkm/saga-translator.git "$HERMES_HOME/skills/saga-translator"
```

または `hermes skills install` / zip展開でも可。

## 利用条件

saga-translator は、**あなたが所持する小説ファイルを、個人的な閲覧のために日本語へ翻訳する補助ツール**です。利用にあたり、次を確認してください。

- **翻訳文の転載・公開・配布・投稿は、著作権者の許諾なく行ってはなりません。** 出力の利用は私的利用の範囲に留めてください。
- 出力はあなたの端末内に留まります。本スキルに共有・クラウド同期の機能はありません。
- 本スキルとスクリプトはネットワーク通信を行いません。APIキーも、外部サービスへのデータ送信も持ちません。翻訳・分析のLLM処理は、あなたが利用中のAIエージェント契約の範囲内で実行されます。
- 原著の権利処理と翻訳の適法性の判断は、あなたの利用形態と地域の法令に従ってください。本スキルはこれを代行しません。

同意フラグの記録・確認の仕組みはありません。この節を読んで同意した上で使い始めてください。

## 使い方(パイプライン)

1. `ingest.py` — 原文TXT/MD/EPUBを正規化し、作品ディレクトリ `_saga/<id>/` を作る
2. `split_chapters.py` — 章分割 + ブロック分割(コンテキストサイズに合わせる)
3. analysis — `characters-[ネタバレあり].toml` / `terms.toml` / `plot-[ネタバレあり].toml` を書く。一周後に `titles.toml`
4. `build_context.py` — ブロックごとの翻訳コンテキスト生成
5. 翻訳(エージェントが本文を訳す) → `qa_translation.py --emit` — 機械QA合格で正式出力
6. `record_meta.py` — 版記録(model / prompt_version)
7. `assemble.py` — 全文訳文・目次を統合。完了後に原文を削除

詳細は `SKILL.md` と `references/prompt-design.md` を参照。

## テスト

```bash
python scripts/tests/test_saga.py
```

## ライセンス

MIT
