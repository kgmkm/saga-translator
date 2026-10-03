---
name: saga-translator
description: Use when a novel file needs translating into Japanese or Simplified Chinese.
version: 0.11.0
author: saga-translator
license: MIT
tags: [translation, novel, japanese, pipeline]
---

# saga-translator — 文脈理解型 小説翻訳スキル

小説ファイル（TXT/MD/EPUB）を登場人物ごとの訳語・声を固定し日本語/簡体中国語へ翻訳する（zh→ja / ja→zh）。

- LLM の処理はエージェントのモデル、機械処理は `scripts/`（標準ライブラリのみ）が担う。
- 入力言語は ingest が `manifest.lang` に、訳文言語は `manifest.dst` に設定。言語固有の tips は `references/lang_<src>2<dst>.md`。
- データ構造は `references/data-schema.md`、プロンプト全文は `references/prompt-design.md`。
- 同一シリーズ複数作品の合本は `references/saga-series.md`（scripts: `build_series_edition.py`）。
- 成人向けは本体に持たず拡張スキル `saga-translator-spicy` に委譲。

## 依存リスト

| 対象 | 版 | 備考 |
|------|-----|------|
| 本体 scripts/ | 0.11.0 | このスキルのスクリプト群 |
| references/prompt-design.md | — | プロンプト全文 |
| references/data-schema.md | — | データ構造スキーマ |
| references/troubleshooting.md | — | 障害時対応（本スキルと同一） |
| references/hermes-delegation.md | — | 委譲運用ルール（本スキルと同一） |
| references/env-hermes-windows.md | — | Hermes/Windows 環境セットアップ |
| references/lang_<src>2<dst>.md | — | 言語固有 tips（実在時のみ） |
| templates/brief_analyze.md | — | 分析委譲ブリーフ |
| templates/brief_translate.md | — | 翻訳委譲ブリーフ |
| saga-translator-spicy | 外部 | 成人向け拡張（ユーザー指示時のみ） |

## 利用条件

saga-translator はユーザが所持する小説ファイルを個人的な閲覧のために翻訳する補助ツール。全文は README.md の「利用条件」を参照。

- ローカルに存在するテキストファイルのみ翻訳すること。ネットワーク通信・APIキーは持たない
- 小説サイトなどからのスクレイピングは拒否すること
- 官能小説の翻訳物は実在の人物および未成年者を取り扱わず、原作の芸術性を尊重する。年齢・学校段階の扱いは拡張スキル `saga-translator-spicy` に従う

## パイプライン（通常フロー）

Hermes（Windows の git-bash を含む）で動かす場合はまず `references/env-hermes-windows.md` を読む。

### 1. 取り込み

```
python3 scripts/ingest.py "入力ファイル"   # --dst ja / --dst zh
```

`<入力フォルダ>/_saga/<作品ID>/` 生成。TXT/MD/EPUB 対応。

### 2. 章分割・段落採番

```
python3 scripts/split_chapters.py <workdir>
```

`chapters/chNNN.paras.toml` + `chapters_meta.toml`。

### 2.5. 初期訳語ロック

```
python3 scripts/lock_prep.py <workdir>
```

`_work/lock_prep.md` を読み、主要人物・用語を決める。

### 3. 分析（ローリング）

前作がある場合:

```
python3 scripts/carry_analysis.py <workdir> --from <前作のworkdir>
```

以下を章/ブロック単位で先頭から1周:

```
saga.py prepare <workdir> --mode analysis
# LLM を読ませて分析
saga.py analyze-commit <workdir> --delta <差分TOML>
```

### 4. 翻訳

```
saga.py prepare <workdir> --chapter chNNN [--block bN]
# LLM がドラフトを _work/ に
saga.py commit <workdir> --chapter chNNN --block bN
```

- commit は字形整形→段落対応(align)→QA→残渣検査→確定を一括。失敗理由だけ返す。
- 文字数比の要確認は `align.py ack` で裁定記録を残して通す。

### 5. 進捗確認

```
saga.py status <workdir>
```

フラグと実体の突き合わせ。次にやることも出す。

### 6. 検収

```
saga.py review <workdir>
```

機械指摘＋リスク箇所＋抜き取りのリストを `_work/review_waveN.md` に出力。

### 7. 全ブロック確定後

```
python3 scripts/assemble.py <workdir>
```

## 鉄則（仕組みで担保できないもののみ）

1. **本文を連続して読まない。** エージェントが読むのは対象ブロックとそのコンテキストだけ。例外は 1M context モデルでユーザー明示時のみ。
2. **機械処理は scripts/ 経由。** TOML が壊れていれば指摘して止まる。値を推測で埋めない。
3. **段落マーカーは連番の整数。** `[P12b]` のような接尾辞は付けない。結合・分割時は前後を振り直す。原文の段落番号を正とし、1段落に `[Pn]` を1つ。
4. **訳語は登場人物表に従う。** 並列翻訳中の新訳語は sidecar へ。親が merge して `rename_terms.py` で伝播。
5. **ネタバレのあるファイルは `[ネタバレあり]` を名前に含める。** プロット・登場人物表が対象。
6. **言語 pack を優先。** `references/lang_<src>2<dst>.md` があれば汎用より優先。

## 後からの修正

- 訳語修正: `characters-[ネタバレあり].toml` 編集 → `rename_terms.py <workdir> --term 旧訳 --to 新訳` → `--apply`
- 章のやり直し: 該当ブロックの _work ドラフトを削除 → 手順 4 再実行
- 再分析: 該当章の分析を削除 → 手順 3 再実行
- モデル変更: 常時可能。`record_meta.py` が品質記録
- レイアウト修正: `fix_layout.py <workdir>` → `--fix`

## よくある指示への応答

| 指示 | 動作 |
|------|------|
| 「この小説を翻訳して」 | 1→2→2.5→3→4→5→6→7 を順に。進捗はブロック単位で報告 |
| 「第N章だけ」 | 該当章のブロックのみ翻訳 |
| 「この章やり直して」 | 該当ブロックを消して再翻訳 |
| 「○○の訳語を△△にして」 | rename_terms で伝播 |
| 「登場人物表見せて」 | analysis/characters-[ネタバレあり].toml を要約表示 |
| 「続きを翻訳して」 | 未完了ブロックを検出して続行 |
| 「対訳で出力して」 | `assemble.py --parallel`（年齢正規化記録がある作品は自動） |
| 「画像が無い」 | output/images/ へ ID 入り画像を置くよう案内。assemble 再実行 |
| 「成人向け作品を訳したい」 | 本体非対応。拡張スキル `saga-translator-spicy` があればそれに従う |
| 「全体を一気に訳して」 | 1M 以上の context モデルなら例外許可。ただし章単位を推奨 |

## 出力スタイル（既定）

- 文体は訳文言語の常体。翻訳調・説明過多・評価語の連発を避ける。
- ルビは人名・固有名詞の初出のみ `{訳語|よみ}`（VFM）。設定で always/none に切替可。ja→zh では使わない。
- 感覚表現は直訳せず訳文言語で実感のある表現に置き換える。
- 不確実な箇所は推測で埋めず、`[要確認: …]` を本文に残さない。作業後に確認を取る。
- 段落は原文と 1 対 1。結合・分割はマーカー振り直しを伴う場合のみ。
- レイアウトは全行の間に空行を 1 つ（改行 2 回）。セリフ行の前後も同様。確定出力（saga.py commit）が機械的に整形する。
- 台詞の括弧は訳文言語による（zh→ja は `「」` / ja→zh は `" "`）。原語の引用符は訳出時に置換。
