# データスキーマ

作業中と完了後で残るファイルが違う。構造化データは TOML（旧JSONも読む）。対訳（原文＋訳文の対照）は `--parallel`、または年齢正規化の記録（`analysis/age_policy_report-[ネタバレあり].md`）がある作品で出力する。

## 翻訳方向

- 原語は ingest が判定して `manifest.lang` に書く。訳文言語は `manifest.dst`（`ingest --dst`。無ければ原語から推定: zh→ja / ja→zh）
- 訳文ファイルの接尾辞・章ファイル名は訳文言語による（`_ja` / `_zh`）。機械的な判定は `_lib.direction` が持つ
- `titles.toml` は `title_ja` / `title_zh`・章見出し `ja` / `zh` を持ち、訳文言語の欄を優先する（もう一方で補う）
- 人物表・用語集の `*_ja` 欄には訳文言語の訳語を書く（`*_src` は原語キーのまま）。`name_ja` 等の欄名は方向によらず共通

## ディレクトリ

作業中:

```
<作品ID>/
├── manifest.toml
├── originals/source.md      # 完了後に削除
├── chapters/
│   └── chNNN.paras.toml     # 完了後に削除
├── chapters_meta.toml
├── analysis/
│   ├── characters-[ネタバレあり].toml
│   ├── terms.toml
│   ├── plot-[ネタバレあり].toml
│   └── titles.toml
├── translation_input/       # 完了後に削除
├── output/
│   ├── images/              # 画像置き場。訳文からは常に images/… で参照
│   ├── chNNN/bY_<dst>.md     # 完了後に章ファイルへ吸収して削除（訳文言語により _ja / _zh）
│   ├── chNNN/bY_parallel.md # 対訳の中間物。assemble が章・全文対訳を作る
│   ├── chNNN_<dst>.md        # 同上
│   ├── chNNN_parallel.md    # --parallel、または年齢正規化の記録がある作品で出力
│   ├── 全文_訳文.md
│   ├── 全文_対訳.md         # 同上。> [Pnnn] 付きで原文と訳文を対にする
│   ├── 目次.md
│   └── _work/               # 確定後に削除
└── logs/
```

完了後（assemble 既定）: 原文・段落・中間コンテキスト・ブロック訳は無い。対訳（`全文_対訳.md`・章対訳）は `--parallel`、または年齢正規化の記録がある作品でのみ残る。残るのは訳文・output/images・分析・manifest・目次。入力元ファイルは触らない。`--keep-source` で作業中のまま残せる。旧配置 `work/images/` があれば assemble が output/images/ へ移動する。

※ 旧 `.json` / 旧 `characters.toml` も読む。新規は TOML。JSONテンプレは置かない。

## TOML構造（analysis配下）

### characters-[ネタバレあり].toml

```toml
[[characters]]
name_src = "张三-李四"
name_ja = "チョウサン・リシ"
ruby = "ちょうさん・りし"
role = "最強剣士・伯爵令嬢"
first_person = "私(わたくし)"
second_person = "あなた"
speech_style = "丁寧な令嬢口調。語尾は「〜ですわ」「〜ますわね」「〜ですわよ」"
vocab = "漢語は使うが俗語は使わない。擬音は少ない"
catchphrase = "〜ですわね"
appearance = "銀髪ロングの美少女"
relationships = "王五のチームメイト"
address = [
  { target_src = "王五", target_ja = "王五", form = "王五さん" },
]
arcs = [
  { at_block = "ch001/b1", first_person = "私(わたくし)", speech_style = "丁寧な令嬢口調。語尾は「〜ですわ」「〜ますわね」", note = "登場時点" },
  { at_block = "ch001/b3", first_person = "あたし", speech_style = "崩れたタメ口。語尾は「〜だよ」「〜じゃん」", vocab = "短い単語が増える", note = "変化後。旧語尾を使わない" },
]
proposed = ""
```

- `name_ja` は訳語の固定表記。以後の翻訳・QA・置換の基準になる
- セリフの声は欄を分ける。`tone` に口調・一人称・呼び方を混ぜない（旧ファイルの `tone` は `speech_style` が空のときだけ読む）
  - `first_person` / `second_person`: 一人称と二人称の既定
  - `address`: 相手別の呼び方。翻訳時はこちらを `second_person` より優先
  - `speech_style`: 敬語/タメ口 と語尾の実例。形容詞だけで済ませない
  - `vocab`: 語彙の特徴（漢語過多・擬音・無口など）
  - `catchphrase`: 口癖。口調そのものとは別
- `arcs` は変化の記録。`at_block` 以降に効く。変わった欄だけ書く。精神状態は `note`、声の変化は `speech_style` / `first_person` 等。翻訳時は対象ブロックに対応する arc を使う
- `proposed` は既存訳語の変更候補。オーナー承認後に本採用し、`rename_terms.py` で伝播
- 通行人は `name_src` / `name_ja` / `role` だけでよい。セリフがある人物は声の欄を埋める

### terms.toml

```toml
[[terms]]
term_src = "心灵共感"
term_ja = "心霊共感"
ruby = "しんれいきょうかん"
memo = "技術名。初出は第1章"
```

### plot-[ネタバレあり].toml

```toml
[[entries]]
block = "ch001/b1"
text = "王五の悪夢と戴冠。黒ずくめの男の異変に気づく"
```

ファイル名に `[ネタバレあり]` を必ず含める。

### titles.toml（分析一周のあと。翻訳前）

ingest では決めない。プロットが一周した時点で1つ決める。assemble が目次・全文の見出しに使う。

```toml
title_src = "合成作品"
title_ja = "合成作品"
note = "原題の漢語を残した。ユーザーが後から直してよい"

[[chapters]]
id = "ch001"
src = "序章——合成作品"
ja = "序章 合成作品"
```

既にある titles.toml は上書きしない。

### chapters_meta.toml（split_chapters.py が生成）

```toml
[work]
title = "..."
author = "..."
lang = "zh"

[[chapters]]
id = "ch001"
title = "第1章"
n_paras = 40
status = "pending"

[[chapters.blocks]]
id = "b1"
paras = [1, 12]

[[chapters.blocks]]
id = "b2"
paras = [13, 40]
```

- `status` は `pending` / `emitted` / `done` の 3 値のみ
- `qa_translation --emit` が block の `status = "emitted"` と `emitted_at` を自動的に記録する。全ブロックが emitted なら章の `status = "done"` になる
- `record_meta.py` は `status` を変更しない。`translation` フィールドに model / prompt_version / tokens 情報を記録するだけ。中断しても status がズレない設計
- `build_context.py --mode analysis` を実行すると block の `analysis_done_at` が記録される（情報取得の開始記録で、分析結果の確定を意味しない）

### TOMLが壊れていたとき（_lib.TomlSyntaxError）

ユーザーが `analysis/` や `titles.toml` を手で編集するので、壊れ得る前提で作る。`load_toml` は構文エラーで、`load_chapters_meta` / `load_paras` / analysis 読み込みは構造チェックで `TomlSyntaxError` を出して停止する。

エージェントの対処:

1. エラーメッセージに出たパスを読む
2. 欄の意味を保ったままTOMLとして修復する（欠けたキーは空文字、配列は空。値を推測で埋めない）
3. 保存して同じコマンドを再実行。直らないならユーザーに何が壊れているか伝えて一度止まる

### tokens.jsonl

**未実装。** 仕様のみで、どのスクリプトも書き込まない。トークン・モデル情報は `record_meta.py` が chapters_meta の `translation` フィールドに記録する。モデル切替時の比較・品質回帰の診断が必要になったら record_meta.py の出力を加工する方向で検討する。

### 初期値テンプレート

新規作品開始時は `templates/` 以下の TOML をコピーして作る:

- `templates/characters.toml` → `analysis/characters-[ネタバレあり].toml`
- `templates/terms.toml` → `analysis/terms.toml`
- `templates/plot.toml` → `analysis/plot-[ネタバレあり].toml`
- `templates/titles.toml` → `analysis/titles.toml`（分析一周後に1つ作る。既にあれば上書きしない）

## 処理フローでの参照関係

- `build_context.py` は characters/terms/plot + 前章末尾 + 次章冒頭 + 対象ブロックを1つの md に束ねる
- `qa_translation.py` は段落整合（連番一致・段落数一致）と訳語残留（登場人物表の原語が訳文に残っていないか）を検証する
- `rename_terms.py` は旧訳語→新訳語の機械置換。影響箇所を一覧化し、確認のうえ適用する
- `assemble.py` は全ブロック確定後に章訳文・全文・目次を統合し、原文と中間物を消す。対訳（章対訳・`全文_対訳.md`）は `--parallel`、または年齢正規化の記録がある作品で出力する（`> [Pnnn]` 付き）

## 分析の記録（stage3 / P0-2 前半）

「分析済み」＝対象ブロックの原文ハッシュ `analysis_src_hash` に対し、(a) 検証を通った非空差分の反映済み（`analysis_committed_at`）、または (b) 「変更なし」の明示（`analysis_no_changes = true`）。

- `chapters_meta.toml` の各 block に記録: `analysis_committed_at`（従来どおり）/ `analysis_src_hash`（ブロック原文の sha256 先頭12桁。原文が変われば変わる）/ `analysis_no_changes`（(b) の場合のみ true）
- 分析 delta（analyze-commit の入力。`templates/brief_analyze.md` の形式が正）: トップレベル `block = "chNNN/bN"` 必須・chapters_meta に実在、`no_changes = true` は空差分の明示に必須。テーブルは **`[[characters_delta]]` / `[[terms_delta]]` / `[[plot_delta]]`**（`[[characters]]` 等の短縮形は無視されて空差分扱いになるので注意）。`[[characters_delta]]`・`[[terms_delta]]` は `match_key`/`match_value` 必須、`[[plot_delta]]` は `block`（delta と一致）/`text` 必須。違反はすべて拒否（exit 1・記録しない）
- `[[plot_delta]]` の `replace = true`（boolean）は同一 block の `text` を置換する（既定は追記）
- 保護キーの明示上書き（`--allow-protected-overwrite`。記述系キー memo / role / appearance / relationships / note のみで、`name_ja` / `term_ja` は対象外＝`rename_terms.py` の領分）と `replace = true` の plot 置換は、保存成功後に `_work/analysis_edits.jsonl` に1行ずつ追記する（`ts` / `match` / `key` / `old` / `new` / `via`。`via` は `protected-overwrite` か `plot-replace`。purge しない）
- 旧ブロック（`analysis_committed_at` のみ）は hash 未記録を許容

## 裁定記録 `_work/rulings.json`（stage3 / P2-3 前半・`scripts/rulings.py`）

裁定（ack・語彙の許容・誤検出の除外）を全キー一致で照合する記録。1件 = `{work_id, chapter, block, paragraph, rule_id, src_hash, draft_hash, dict_version, rationale, adjudicator, decided_at, disposition, status}`。`find_active_ruling` は **8キーすべて一致する active な記録だけ**を返す（1つでも違えば無効＝裁定の流用・陳腐化を防ぐ）。`revoke_ruling` も全キー一致で `status=revoked` に更新（履歴は残る）。`dict_version` の既定は `'none'`。

- known_terms（作品全体の文字列除去）は house_style.toml の `residue.known_terms` のまま。局所の誤検出裁定と混ぜない
- align ack の共通土台への移行は後続（stage4/11）

## 横断状態の台帳 `_work/ledger.toml`（review-1・`scripts/ledger.py`）

ブロックを跨ぐ消費状態（ルビ適用・用語初出・オノマトペ使用）をエージェントの文脈依存からファイルへ移した台帳。purge 対象外。連作では carry で引き継ぐ。

```toml
[ledger]
version = 1
updated_at = "2026-10-09T17:44:56+00:00"

[[ruby_applied]]          # 確定訳文の VFM {親|読み} 初出を commit 確定時に自動記録（term+reading がキー。2回目以降は追記しない）
term = "剣"
reading = "つるぎ"
block = "ch001/b1"
paragraph = 1

[[term_first_use]]        # terms.toml の term_ja が確定訳文に初めて現れた位置（既記録の語は追記しない）
term = "雪"
block = "ch001/b1"
paragraph = 2

[[ono_used]]              # 翻訳者が saga.py ledger-ono で申告（本体に表がないため自動抽出しない。form+block+para がキー）
form = "どきどき"
block = "ch001/b2"
paragraph = 3
context = "鼓動"
```

- 更新は commit 確定時の差分追記のみ（当該ブロックの確定訳文だけ走査）。冪等。再確定・revise で消えた記録の削除はしない（履歴として残す）。
- prepare（翻訳モード）は「消費済みルビ（再付与不可）・使用済みオノマトペ・用語の初出済み」の3節を注入する（上限つき省略表示。データなしでは節ごと非表示）。
- 既知の制限: `rename_terms.py` で親文字が変わると台帳の term が古くなる（実装対象外。台帳は履歴として残す）。

## 拡張語彙データの形式（stage7 / P0-3。spicy 側が中身を作る）

拡張スキル（saga-translator-spicy 等）が語彙マップを機械可読 TOML で提供する（`vocab.toml`。置き場所は拡張スキルの `references/`。本体は探索先リストから読む）:

```toml
version = "1"          # 辞書の版。裁定記録（rulings）の dict_version に接続する

[[vocab]]
src = "合成語A"            # 原語（原文字形＝簡体字のまま）
src_variants = ["合成語A"]  # 別字形・別表記（任意。両方で索引される）
dst = "合成訳A"    # 既定訳
dst_variants = ["合成訳A状"]  # 許容される別表記（任意。dst か dst_variants のどれか1つが該当段落/ブロックにあれば合格）
kind = "fixed"          # fixed | candidates | ja_ok
note = ""               # 任意の注記
```

- `fixed`: 機械で確定できる語。原文に出たら訳文は `dst` でなければならない。違反は commit 停止。
- `candidates`: 文脈で変わる語。訳文に原語相当の語が残れば commit 停止（通すのは本文修正か裁定記録）。
- `ja_ok`: 日本語としても正当な語（触手・改造など）。検査から除外。
- **許容別表記**: `dst_variants` は規定訳の許容別表記（任意）。規定訳の照合は `dst` と `dst_variants` の完全一致で行い、どれか1つが該当段落/ブロックにあれば合格。
- **優先順位**: 作品 terms（analysis/terms.toml）＞ house_style.toml ＞ 拡張 vocab.toml。同じ `src` は上位が勝つ。
- **原文字形索引**: `src` と `src_variants` の両方で引ける（spicy の見出しが日本語字形（合成語A）でも原文（合成語A）で当たる）。
