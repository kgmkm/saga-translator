---
name: saga-translator
description: Use when a novel file needs translating into Japanese or Simplified Chinese.
version: 0.16.0
author: saga-translator
license: MIT
tags: [translation, novel, japanese, pipeline]
---

# saga-translator — 文脈理解型 小説翻訳スキル

小説ファイル（TXT/MD/EPUB）を登場人物ごとの訳語・声を固定し日本語/簡体中国語へ翻訳する（zh→ja / ja→zh）。

- LLM の処理はエージェントのモデル、機械処理は `scripts/` が担う。標準ライブラリのみで動作し、opencc があると字形変換が自動化される（任意依存。無い環境でも手当て表で動き、テストはスキップを明示する）。
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
| references/agent-delegation.md | — | 委譲運用ルール（ハーネス非依存） |
| references/series-translation.md | — | 連作翻訳フロー（パイロット先行・chain-carry 量産） |
| references/env-windows.md | — | Windows での実行時の注意（PowerShell 節を含む） |
| references/lang_<src>2<dst>.md | — | 言語固有 tips（実在時のみ） |
| templates/brief_analyze.md | — | 分析委譲ブリーフ |
| templates/brief_translate.md | — | 翻訳委譲ブリーフ |
| opencc (opencc-python-reimplemented) | 任意 | 字形変換の自動化。無くても動作 |
| saga-translator-spicy | 外部 | 成人向け拡張（ユーザー指示時のみ） |

## 利用条件

saga-translator はユーザが所持する小説ファイルを個人的な閲覧のために翻訳する補助ツール。全文は README.md の「利用条件」を参照。

- ローカルに存在するテキストファイルのみ翻訳すること。ネットワーク通信・APIキーは持たない
- 小説サイトなどからのスクレイピングは拒否すること
- 官能小説の翻訳物は実在の人物および未成年者を取り扱わず、原作の芸術性を尊重する。年齢・学校段階の扱いは拡張スキル `saga-translator-spicy` に従う

## 実行環境

- コマンド例は `python3` のまま。`python3` が無い環境（Windows の一部・DSH 同梱ランタイム等）では `references/env-windows.md` に従い、実体（`python` / `py -3` 等）を先に確定する。
- 成功判定は exit コードだけでなく出力本文（例: `[commit] align: PASS`）で行う。PowerShell では python の stderr 出力により、成功でも exit 非0 に見えることがある（`references/env-windows.md`）。
- 実行環境ごとの注意（git-bash / PowerShell / goose・長文書き込み）は `references/env-windows.md`。
- 委譲（サブエージェント）の運用は `references/agent-delegation.md`（ハーネス対応表を含む）。
- **ファイルの置き場所の正本は `scripts/paths.py`**（どのパスが purge され、どこに何を置くか）。`python3 scripts/paths.py` で一覧、`--md` で木。md に構造を書き足さない。

## パイプライン（通常フロー）

### 1. 取り込み

```
python3 scripts/ingest.py "入力ファイル"   # --dst ja / --dst zh
```

`<入力フォルダ>/_saga/<作品ID>/` 生成。TXT/MD/EPUB 対応。manifest にスキル版・コミットを記録する。

### 1.5. 画像の正規化

```
python3 scripts/normalize_images.py <workdir> --apply
```

画像ファイル名を `<作品ID>-<画像ID>` に改名し、参照を一括更新する（改名と更新は一体）。

### 2. 章分割・段落採番

```
python3 scripts/split_chapters.py <workdir>
```

`chapters/chNNN.paras.toml` + `chapters_meta.toml`。
本文が2回繰り返されている（挿絵版＋無挿絵版の連結など）と**何も書かずに停止**する（重複率10%以上）。`--dedupe` で後半を削除、`--accept-duplicate` で承知のうえ続行（記録し、status が毎回警告する）。数字だけの行は既定で節番号として扱い章にしない（他に章見出しが無いときだけ章。`--numeric-headings chapter|section` で変更）。

### 2.5. 初期訳語ロック

```
python3 scripts/lock_prep.py <workdir>
```

`_work/lock_prep.md` を読み、主要人物・用語を決める（命名方針は `_house/house_style.toml` に記録する。`python3 scripts/house_style.py init <workdir>` で雛形作成。直下の `house_style.toml` は**読まれない**ので init で移す）。候補は2〜6字の全部分文字列なので、等頻度の断片（長い名前の一部）・機能語の端・普通名詞は自動で落とす。作品固有のジャンル語が残る場合は `_house/house_style.toml` の `[naming] stopwords = ["語", …]` に足す。

### 3. 分析（ローリング）

前作がある場合:

```
python3 scripts/carry_analysis.py <workdir> --from <前作のworkdir>
```

連作2作目以降の運用（パイロット先行・量産・レビュー二段階）は `references/series-translation.md` に従う。

以下を章/ブロック単位で先頭から1周:

```
python3 scripts/saga.py prepare <workdir> --mode analysis
# LLM を読ませて分析
python3 scripts/saga.py analyze-commit <workdir> --delta <差分TOML>
```

- 分析の前の点検: `python3 scripts/saga.py lint <workdir>`（分析 TOML の日本語フィールド残渣を事前一覧。読み取り専用。差分 TOML は `--delta` で追加検査）。差分の未知キー・必須キーは `python3 scripts/check_src_keys.py <workdir> --delta _work/chNNN_bN_delta.toml` でも事前検査できる（work 相対可）
- 記述系キー（memo / role / appearance / relationships / note）の明示上書き: `python3 scripts/saga.py analyze-commit … --allow-protected-overwrite`（`_work/analysis_edits.jsonl` に監査記録。name_ja / term_ja は対象外＝`rename_terms.py` の領分）

1周し終わったら `analysis/titles.toml` を作って章題と作品題を確定する（`title_src`/`title_ja` と `[[chapters]]` の `id`/`src`/`ja`）。assemble はこれが無いと止まる（原題のまま出す場合は `--allow-src-title`）。

### 4. 翻訳

```
python3 scripts/saga.py prepare <workdir> --chapter chNNN [--block bN]
# コンテキスト: <workdir>/translation_input/{ch}_{b}_translation.md（このファイルだけを LLM に読ませる）
# LLM がドラフトを output/_work/{ch}_{b}_ja_numbered.md に（ja→zh は _zh）
python3 scripts/saga.py commit <workdir> --chapter chNNN --block bN
```

- commit は字形整形→段落対応(align)→QA→残渣検査→確定を一括。失敗理由だけ返す。
- 原文を写しただけの訳文（未翻訳コピー）は commit で止まる（段落のカナ比率・原文4-gramの含有率。緩めるなら config `[qa] copy_check = "warn"/"off"`）。段落内の空行は1つの改行に正規化する（確定後の出力で段落対応が崩れるため）。
- 擬音が中国語のまま残った箇所（プ嗤・噗嗤 等）は**警告のみ**で commit は止めない（訳し分けは文脈依存で、止めると語の置換で本文が壊れる）。抑制は config `[qa] ono_check = "off"`。
- 文字数比の要確認は `python3 scripts/align.py ack` で裁定記録を残して通す。
- コンテキストは既定で compact（対象に必要な分だけの人物表・用語集）。全件を出すには `python3 scripts/saga.py prepare … --full`（分析モードは full 固定）。
- 確定時に `_work/ledger.toml` へ台帳を追記する（ルビ適用・用語初出は自動、使用済みオノマトペは `python3 scripts/saga.py ledger-ono <workdir> --form <表記> --block chNNN/bM [--para N]` で申告）。prepare は「消費済みルビ（再付与不可）・使用済みオノマトペ・用語の初出済み」を注入するので、消費済みの再使用は避ける。台帳は purge 対象外で、連作では carry で引き継ぐ。
- 語彙 candidates の裁定記録（`vocab:<原語>`）に CLI はない。一時 .py ファイルで `rulings.add_ruling` を呼ぶ（8キー全一致。原文・ドラフトを変えたら無効になり再記録が必要）。ハッシュは手計算せず `align` / `rulings` の関数で求める:
  ```python
  import sys; sys.path.insert(0, "scripts")
  from pathlib import Path
  from rulings import add_ruling, text_hash
  from align import config_version, draft_hash
  from _lib import load_paras, block_paras, load_chapters_meta, block_meta, direction, target_suffix  # 原文段落の取得
  work = Path("<workdir>"); chapter = "ch001"; block = "b1"; para = 443; src = "穴口"
  bmeta = block_meta(load_chapters_meta(work), f"{chapter}/{block}")
  paras = {int(p["n"]): str(p.get("text", "")) for p in block_paras(load_paras(work, chapter), bmeta)}
  suf = target_suffix(direction(work)[1])
  draft_p = work / "output" / "_work" / f"{chapter}_{block}{suf}_numbered.md"
  add_ruling(work, work_id=work.name, chapter=chapter, block=block, paragraph=para,
             rule_id=f"vocab:{src}", src_hash=text_hash(paras[para]),
             draft_hash=draft_hash(draft_p), dict_version=config_version(work),
             rationale="<なぜこの訳でよいか>", adjudicator="translator",
             disposition="allow", status="active")
  ```
  `dict_version` 省略時は `'none'` になる（拡張辞書なし時と一致すれば有効。配線はされている）。
- 声の確定: 分析差分に次を入れて `analyze-commit`（声の記述が1つ以上ないと拒否される）。status の「声の状態(未確認)」はこれで消える:
  ```toml
  [[characters_delta]]
  match_key = "name_src"
  match_value = "<原語名>"
  voice_status = "confirmed"
  ```
- 異表記候補の別人棄却: 分析差分に `dismiss_variant = "<相手の原語名>"`（対象キャラの match 指定と併せて。任意で `dismiss_reason`）を入れて `analyze-commit`。該当ペアは status の異表記候補から落ち、再検出しない。監査は `_work/analysis_edits.jsonl`（`via="variant-dismiss"`）。連作では carry で引き継ぐ。
- 判断記録: 曖昧原文の判断理由は本文に書かず `_work/notes.md` へ追記する（任意・追記式。機械は読まない。purge されず、再開・検収時に親が読む）。

### 5. 進捗確認

```
python3 scripts/saga.py status <workdir>
```

フラグと実体の突き合わせ。次にやることも出す。proposed 滞留（訳語変更の候補）も表示する。一覧・承認・却下は `python3 scripts/saga.py proposed list|approve|reject <workdir> --name <訳語> --key <欄>`（承認後は `rename_terms.py` で訳文へ反映）。

### 6. 検収

```
python3 scripts/saga.py review <workdir>
```

機械指摘＋リスク箇所＋抜き取りのリストを `_work/review_waveN.md` に出力。**assemble は検収をしない**（機械検査を通っただけで品質は保証されない。status は検収の記録が無ければ「検収は未実施」と表示する）。

標準手順:

1. `_work/review_waveN.md` を検証者（人間または別エージェント）が全件照合する
2. 指摘の修正を '旧<TAB>新' の一覧（fixes.txt）にまとめる
3. `python3 scripts/rename_terms.py <workdir> --pairs-file fixes.txt --apply` で一括適用する
4. `python3 scripts/saga.py status <workdir>` で再 assemble の要否を確認する
5. assemble（手順 7）を実行する
6. `python3 scripts/saga.py review <workdir> --wave N --done --note "何を見たか"` で検収の完了を記録する（status が「検収済み: wave N」と表示する。改名が検収より新しければ「再検収が必要」と出る）

### 7. 全ブロック確定後

```
python3 scripts/assemble.py <workdir>
```

assemble の入力は**確定済みのブロック**に限る（commit を経ない出力は渡さない）。全文（全文_訳文.md）と対訳（章対訳・全文_対訳.md）は既定で出力する（抑制は `--no-full` / `--no-parallel`）。完了時に成果物のバイト数と「残る中間物」の一覧を出す。空の組立て（確定済みブロックなし）は exit 1。原文を写しただけの訳文（未翻訳コピー）が残っていれば止まる（承知で通すなら `--allow-quality "理由"`。manifest に記録）。改訂予定なら `--keep-source` 推奨（originals・translation_input・output/_work も残す）。assemble 後の改訂手順（chapters・ドラフトの復旧）は `references/troubleshooting.md` の「assemble 後の改訂路」を参照。

## 鉄則（仕組みで担保できないもののみ）

1. **本文を連続して読まない。** エージェントが読むのは対象ブロックとそのコンテキストだけ。例外は 1M context モデルでユーザー明示時のみ。
2. **機械処理は scripts/ 経由。** TOML が壊れていれば指摘して止まる。値を推測で埋めない。
3. **段落マーカーは連番の整数。** `[P12b]` のような接尾辞は付けない。結合・分割時は前後を振り直す。原文の段落番号を正とし、1段落に `[Pn]` を1つ。
4. **訳語は登場人物表に従う。** 並列翻訳中の新訳語は sidecar へ。親が merge して `rename_terms.py` で伝播。
5. **ネタバレのあるファイルは `[ネタバレあり]` を名前に含める。** プロット・登場人物表が対象。
6. **言語 pack を優先。** `references/lang_<src>2<dst>.md` があれば汎用より優先。
7. **スキルの正本はリポジトリ。** 修正はリポジトリのブランチで行い、実行環境へは `python3 scripts/tools/sync_skill.py --dest <配布先>` で配布する（`--check` で差分検査）。翻訳作業中のエージェントはスキルのファイルを直接修正しない。修正が要るときはバグ報告を出して止まる。作業ディレクトリの manifest には作成時のスキル版とコミットが記録され、版が変わると prepare が警告する。

## 後からの修正

- 訳語修正: `characters-[ネタバレあり].toml` 編集 → `python3 scripts/rename_terms.py <workdir> --term 旧訳 --to 新訳` → `--apply`（複数ペアは `--term`/`--to` の繰り返し・`--pairs-file fixes.txt`（'旧<TAB>新'）・`--pairs-json`。長い旧訳から自動処理される）
- 改名の監査は `_work/renames.jsonl` が正（commit のスナップショットと不一致になってよい）
- 章のやり直し: 該当ブロックの _work ドラフトを削除 → 手順 4 再実行
- 再分析: 該当章の分析を削除 → 手順 3 再実行
- モデル変更: 常時可能。`record_meta.py` が品質記録
- レイアウト修正: `python3 scripts/fix_layout.py <workdir>` → `--fix`

## よくある指示への応答

| 指示 | 動作 |
|------|------|
| 「この小説を翻訳して」 | 1→2→2.5→3→4→5→6→7 を順に。進捗はブロック単位で報告 |
| 「第N章だけ」 | 該当章のブロックのみ翻訳 |
| 「この章やり直して」 | 該当ブロックを消して再翻訳 |
| 「○○の訳語を△△にして」 | rename_terms で伝播 |
| 「登場人物表見せて」 | analysis/characters-[ネタバレあり].toml を要約表示 |
| 「続きを翻訳して」 | 未完了ブロックを検出して続行 |
| 「対訳で出力して」 | `python3 scripts/assemble.py`（対訳は既定。不要なら `--no-parallel`） |
| 「画像が無い」 | output/images/ へ ID 入り画像を置くよう案内。assemble 再実行 |
| 「成人向け作品を訳したい」 | 本体非対応。拡張スキル `saga-translator-spicy` があればそれに従う |
| 「全体を一気に訳して」 | 1M 以上の context モデルなら例外許可。ただし章単位を推奨 |
| 「連作を訳して」 | 第1話で型決め→残りは carry 量産。手順は `references/series-translation.md` |

## 出力スタイル（既定）

- 文体は訳文言語の常体。翻訳調・説明過多・評価語の連発を避ける。
- ルビは人名・固有名詞の初出のみ `{訳語|よみ}`（VFM）。設定 `[translation] ruby`（first / always / none）は翻訳コンテキスト冒頭に「ルビ方針」として注入される。ja→zh では使わない。旧形式 `親{読み}` は台帳・review の初出検査に載らない（commit 時に警告）。コードネームにはルビを振らない。（言語別の詳細: `references/lang_<src>2<dst>.md`「ジャンル別命名慣行」）
- コードネーム（代号: 魔法少女・変身名など）はジャンル慣行に従う（魔法少女ものはカタカナ英語）。人名は字面＋初出ルビ。判定は `house_style.toml` の `[naming_policy] codename`（katakana / sinitic / keep）または `[[rules]] kind="naming"` で登録する。
- 感覚表現は直訳せず訳文言語で実感のある表現に置き換える。
- 不確実な箇所は推測で埋めず、`[要確認: …]` を本文に残さない。作業後に確認を取る。
- 段落は原文と 1 対 1。結合・分割はマーカー振り直しを伴う場合のみ。
- レイアウトは全行の間に空行を 1 つ（改行 2 回）。セリフ行の前後も同様。確定出力（`python3 scripts/saga.py commit`）が機械的に整形する。
- 台詞の括弧は訳文言語による（zh→ja は `「」` / ja→zh は `" "`）。原語の引用符は訳出時に置換。
