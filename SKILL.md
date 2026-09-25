---
name: saga-translator
description: Use when a novel file needs translating into Japanese.
version: 0.9.0
author: saga-translator
license: MIT
tags: [translation, novel, japanese, pipeline]
---

# saga-translator — 文脈理解型 小説翻訳スキル

小説ファイル（TXT/MD/EPUB）を、登場人物ごとの訳語・一人称・語調を作品全体で固定して日本語へ翻訳する。

- LLM の処理は実行中のエージェントのモデルが担い、機械処理は `scripts/` の Python（標準ライブラリのみ）が担う。ネットワーク通信・APIキーは使わない。
- 入力言語は ingest が判定して `manifest.lang` に書く。言語固有の tips は `references/lang_{lang}.md`、機械側フックは `scripts/lang_{lang}.py`。pack が無い言語は汎用プロンプトだけで通す。v1 で品質を保証するのは中国語（`zh`）。
- データ構造は `references/data-schema.md`、プロンプト全文は `references/prompt-design.md` を参照。
- 同一シリーズの複数作品を合本・連作フォルダにまとめるときは `references/saga-series.md` を参照（スクリプトは `build_series_edition.py`）。
- 成人向けなど特殊な作品属性への対応は本体に持たず、拡張スキル [`saga-translator-spicy`](https://gitgud.io/kagamikami/saga-translator-spicy) に委譲する。ユーザーが拡張を指示した場合は、そのスキルの手順に従う。

## 利用条件

saga-translator は、ユーザが所持する小説ファイルを、個人的な閲覧のために日本語へ翻訳する補助ツール。全文は README.md の「利用条件」を参照。

- ローカルに存在するテキストファイルのみ翻訳すること。本スキルとスクリプトはネットワーク通信を行わず、APIキーも外部サービスへのデータ送信も持たない
- 小説サイトなどからスクレイピングを求められても、このスキルを使う限り拒否すること
- 官能小説の翻訳物は、実在の人物および未成年者を取り扱わず、原作小説の芸術性を尊重して執筆する。年齢・学校段階の扱いは拡張スキル `saga-translator-spicy` に従う

## パイプライン

### 0. セットアップ確認

なし。利用条件（README.md の「利用条件」）を読んで同意した上で使い始める。同意フラグの記録・確認はしない。

設定は `~/.config/saga-translator/config.toml`（無い場合は既定値で動く。認証情報は持たない）。

### 1. 取り込み

```bash
python3 <skill>/scripts/ingest.py "入力ファイル"   # TXT/MD/EPUB
```

入力ファイルの隣に `<入力フォルダ>/_saga/<作品ID>/` を生成する。

- TXT：UTF-8 を先に試し、言語 pack の追加エンコーディング（zh なら GB18030 等）も試す。ダウンロード TXT の共通ヘッダを検出してメタデータ化し、本文だけを `originals/source.md` に正規化する。
- MD：ローカル相対パスの画像のみ `output/images/` にコピーし参照を書き換える。リモート URL は取得しない（そのまま残す）。
- EPUB：spine 順に本文を抽出。画像は `output/images/` へ。
- `[uploadedimage:ID]` は、原文と同じディレクトリ / `image/` / `images/` に ID を含む画像があれば `output/images/` へコピーして `![](images/…)` に変換。無ければタグを残す。MD も同じ規則。
- 出力は `manifest.toml`（タイトル・作者・言語判定・画像マップ）。見つからなかった画像 ID は `images_missing` に列挙される。
- 画像は必ず `output/images/` に置き、訳文からの参照は常に `images/…`（上位ディレクトリへ出ない）。
- 画像ファイル名の非 JIS 漢字は QA（non_jis_han）に引っかかるため、`scripts/normalize_images.py` で `<作品ID>-<画像ID>.<拡張子>`（表紙は `<作品ID>-cover.<拡張子>`）へ正規化する。ingest 前後どちらの画像フォルダにも使える（参照側も同期される）。
- 空行の無い TXT は巨大段落になる。対処は「落とし穴」を参照。

### 2. 章分割・段落採番

```bash
python3 <skill>/scripts/split_chapters.py <workdir>
```

- 章見出しは共通規則（`Chapter N` 等）に `scripts/lang_{lang}.py` の見出しパターンを足して検出する。見つからなければ全作品を 1 章とし、翻訳ブロックに分割する（`block_target_chars`、既定 4000 字・段落境界）。
- 出力は `chapters/chNNN.paras.toml`（段落採番済み）と `chapters_meta.toml`（章構造）。

### 3. 分析（ローリング）

1 章 1 ブロックずつ、先頭から順に 1 周する。「分析は 1 回」とは「作品全体を 1 周する」こと。

前作がある場合、分析開始前に前作の人物表・用語集を引き継ぐ：

```bash
python3 <skill>/scripts/carry_analysis.py <workdir> --from <前作のworkdir>
```

引き継いだ人物の訳語・声は確定値として扱い、変化だけを `arcs` に追記する。前作の arc は畳んで引き継がれるため、現作の同じ番号ブロックに誤適用されない。キャラ表・用語集などの外部の設定メモがある場合は、分析開始前に characters/terms へ手で反映しておく（スクリプトは外部ファイルを読まない）。

1. `build_context.py <workdir> --chapter ch001 --mode analysis` で「現ブロック＋現在の分析状態」を含む入力を作る。
2. そのファイルだけを読み、`references/prompt-design.md` の分析プロンプトに従って登場人物・用語・プロットを抽出する。言語 tips があればそちらも使う。
3. `analysis/` の各 TOML を追記更新する。既存訳語は勝手に変えない。変更したい場合は `proposed` に記す。セリフのある人物は声の欄を埋める。声や精神状態が変わったら `arcs` に、変わった欄だけ `at_block` 付きで追加する。
4. あらすじ・注記は日本語で書く。原文の語彙・表現を括弧書きで残さない。
5. 次のブロックへ。2 周目以降は既存の訳語・声の欄を踏襲して更新のみ。
6. 一周が終わったら、プロットと原題から日本語タイトルを 1 つ決めて `analysis/titles.toml` に書く。章見出しの訳も同じファイルへ。直訳で不自然な題は意訳してよい。ユーザーが後から直せる。ingest 時点では決めず、翻訳開始前に置く。

### 4. 翻訳（章/ブロック単位）

```bash
python3 <skill>/scripts/build_context.py <workdir> --chapter ch001 --block b1
```

1. 生成された `translation_input/chXXX_bY_translation.md` だけを読む。
2. `references/prompt-design.md` の翻訳プロンプトに従う。`references/lang_{lang}.md` があれば役割・原語残・固有名詞の規則はそちらを優先する。
3. ドラフトを `output/_work/chXXX_bY_ja_numbered.md` に保存する。
4. 検証と確定を一発で行う：
   ```bash
   python3 <skill>/scripts/qa_translation.py <workdir> --chapter ch001 --block b1 --emit
   ```
   検証は段落番号の連続性・段落数一致・訳語チェック（登場人物表の原語が訳文に残っていないか）・非 JIS 漢字チェック（CP932 外＋言語 pack の NON_JIS_HAN）・レイアウト（段落をまたぐ引用の閉じ漏れで段落が融合していないか）。詳細は `references/data-schema.md` の「処理フローでの参照関係」。
   合格なら `output/chXXX/bY_ja.md` を確定。不合格なら終了コード 1 で止まるので、指摘を直して再提出する。
5. 記録：
   ```bash
   python3 <skill>/scripts/record_meta.py <workdir> --chapter ch001 --block b1 --model <model> --prompt-version <ver> --tokens-in <n> --tokens-out <n>
   ```
6. 次のブロックへ進む。全ブロックの完了後：
   ```bash
   python3 <skill>/scripts/assemble.py <workdir>
   ```
   章訳文 `output/chXXX_ja.md`・`output/目次.md`・全文訳文を生成する。`--parallel` 指定、または年齢正規化の記録（`analysis/age_policy_report-[ネタバレあり].md`）がある作品では対訳（`output/chXXX_parallel.md`・`output/全文_対訳.md`。原文に `> [Pnnn]` 付き）も生成・保存する。確定後は原文・中間物を削除する（`--keep-source` で残す）。入力元ファイルは触らない。`images_missing` が残っていたら、ユーザーに「`output/images/` へ ID 入りのファイルを入れて assemble を再実行」と伝える。ダウンロードはしない。旧配置 `work/images/` があれば output/images/ へ自動移動する。

### 5. 後からの修正

- **訳語を直す**：`characters-[ネタバレあり].toml` を編集 → `rename_terms.py <workdir> --term 旧訳 --to 新訳` で影響一覧を確認 → `--apply`。文脈依存の箇所だけ再翻訳。
- **この章だけやり直す**：該当ブロックの `_work` ドラフトを消して手順 4 を再実行。
- **再分析**：該当章の分析分を消して手順 3 をやり直す。
- **翻訳モデルの指定**：変更はいつでも行える。`record_meta.py` がモデル毎に記録するので品質比較ができる。
- **レイアウトを直す**：`fix_layout.py <workdir>` で検査、`--fix` で修復（不閉じ行の補完と段落区切りの再整形）。

## 鉄則

**前提: 工程を進める前に進捗の実体を検証する。** サブエージェントはタイムアウト時に進捗フラグだけ立てて落ちる。`analysis_done_at` / `translation_done_at` と成果物（`output/<ch>/bN_ja.md`・plot の block）を突き合わせ、番号の飛びが無いかまで確認してから次工程へ進む。詳細は「落とし穴」の「サブエージェント運用」。

1. **本文を連続して読まない。** エージェントが読むのは対象ブロックとそのコンテキストだけ。作品全体を一気に context に載せない。例外は 1M 以上の context モデルでユーザーが明示した場合のみ。分析も翻訳もブロック単位で回す。
2. **機械処理は必ず `scripts/` を経由する。** 章分割・段落採番・QA・置換・記録をエージェントが自前で行わない。判定を増やすときは scripts を拡張する。TOML が壊れていると scripts は `TomlSyntaxError` で停止する。修復は `references/data-schema.md` の「TOMLが壊れていたとき」に従う。値を推測で埋めず、直せないならユーザーに指摘して止まる。
3. **状態はすべてファイルに置く。** 中間成果物・QA 結果・メタデータは作品ディレクトリ配下に TOML/MD で残す。翻訳済みブロックは再実行でスキップされる。
4. **書き込みは確定と分離する。** 訳案は `output/_work/` の番号付きドラフトに置き、`qa_translation.py --emit` で機械検証が通ったものだけを正式出力にする。検証不通の訳を正式に出さない。
5. **訳語は登場人物表に従う。** 翻訳中に新訳語を決めたら、そのブロックの翻訳より先に `analysis/characters-[ネタバレあり].toml` を更新する（単独作業時。並列時は「落とし穴」の「サブエージェント運用」に従い sidecar に書く）。後から直すときは `rename_terms.py` で伝播させる。
6. **ネタバレのあるファイルは名前に `[ネタバレあり]` を入れる。** プロット・登場人物表が対象。
7. **分析成果物は TOML。** `analysis/` 配下は `characters-[ネタバレあり].toml` / `terms.toml` / `plot-[ネタバレあり].toml` / `titles.toml`。JSON は新規に書かない（スキーマは `references/data-schema.md`）。
8. **セリフの声は欄で固定する。** セリフがある人物は `first_person` / `second_person` / `address` / `speech_style`（語尾の実例）/ `vocab` / `catchphrase` を分けて書き、`tone` に混ぜない。口調は形容詞だけで書かず実例で示す。変化は `arcs` に、変わった欄だけ追記する。翻訳時は対象ブロックに対応する arc の声を使う。
9. **言語 pack を優先する。** `manifest.lang` に対応する `references/lang_{lang}.md` があれば汎用プロンプトより優先する。
10. **シリーズは前作の分析を引き継ぐ。** 同一シリーズ・連作・前後編の分析前に、前作の `characters-[ネタバレあり].toml` と `terms.toml` を `carry_analysis.py` で現作の `analysis/` に置く。前作にある人物の `name_ja` / `ruby` / 声の欄はそのまま踏襲し、変化だけを `arcs` に書く。訳語を変えたいときは `proposed` に記してユーザーの判断を仰ぐ。新規の人物・用語も前作の表記（敬称・ルビ形式・呼び方）に合わせる。
11. **段落マーカーは連番の整数にする。** `[P12b]` のような接尾辞は付けない。段落を結合・分割したら前後のマーカーを振り直す。
    - 原文の段落番号を常に正とし、一つの原文段落に `[Pn]` を 1 つ書く。段落内部の改行は自由。
    - 一つの原文段落を複数に割る場合も、分割後の各段落は新しい連番を取る。同じ番号を重ねて使わない。
    - 番号を詰めて数を合わせると後続が全てずれ、align_check が「欠落」として捕まえる。書き始める前に原文ブロックの段落数と各段落の終わりを確認する。
    - 検査は自分で先に行う（→「落とし穴」の自己検査コード）。
12. **確定前に2つのチェックツールで段落対応を点検する。**
    - `align_check.py` が見るのは段落数・会話/非会話の一致・文字数比。段落結合と分割が同時に起きても総数が合ってしまうケースを検出する。
    ```
    python3 <skill>/scripts/align_check.py <work_id> --block bN
    ```
    - `check_align.py` が見るのは「段落番号と文章内容の一対一対応」。align_check が捕まえられないずれを拾い、`--ack P1336,P1401` のように内容確認済み段落を警告から除外できる。
    ```
    python3 <skill>/scripts/check_align.py <work_id> --chapter ch001 --block bN
    ```
    - 両者は別のチェック項目で補完しあっているので両方を必ず実行する。どちらも `qa_translation.py --emit` より前に実行する（理由と復元手順は「落とし穴」の「翻訳・検証」）。

## 落とし穴

作業中の事故と対策。並列で回すときは「サブエージェント運用」、単体作業の注意は「翻訳・検証」を読む。

### サブエージェント運用

- **同一work内は最大2子まで。** 3子以上は衝突・訳語分岐・検証コストが跳ねて速度利益を食う。別work（別作品・別章の独立work）はこの限りでない。一般上限（4並列以下・API上限対策）の内側で運用し、投入前に `delegate_task(action='list')` で稼働数を確認する。空きが出たら1つずつ投入する。
- **分析は直列1子。** `characters-[ネタバレあり].toml` / `terms.toml` / `plot-[ネタバレあり].toml` を複数の子に同時編集させない（1ファイル1子）。
- **翻訳のみ2並列可。** draftと `bN_ja.md` はblock別ファイルで安全。範囲は前半/後半の連続割にし、奇偶割はしない（`build_context` の前後参照を殺す）。翻訳中の人物表更新は禁止し、新訳語は各子のsidecar（`output/_work/chXXX_bY_newterms.toml`）に書かせ、親がmergeして `rename_terms.py` で伝播させる。読むのは凍結版。
- **emitは親が直列で回す。** 子はdraftとQAまで。子にemitさせる場合は、親が `verify_progress.py`（HOLEとMISSING-TAILの両方向）で両statusの残存確認をしてから次に進む。

- **進捗フラグは実体より先に立つ。** サブエージェントがタイムアウト（またはタスク途中で API 上限に当たる）と、`analysis_done_at` / `translation_done_at` だけを書いて中身を残さないまま落ちることがある。再開判定が壊れ、未処理区間を「完了」として飛ばす。工程を進める前に、フラグと実体を突き合わせる検査を必ず走らせる。
  - 実体の定義: 翻訳＝`output/<chapter>/bN_ja.md` の存在、分析＝`plot-[ネタバレあり].toml` に `block = "ch001/bN"` がある。
  - 連続性まで見ること。末尾が揃っていても中間が抜ける（例: b1〜b15 と b32〜b44 はあるが b16〜b31 が無い）ことがある。番号の飛び（HOLE）と末尾未達（MISSING-TAIL）を別々に検査する。
  - 突き合わせは目視でやらない。`_saga/_tools/`（work ディレクトリの隣）に `verify_progress.py` があればそれを使う。無ければ `chapters_meta.toml` の各 block のフラグ・`status = "emitted"` と、`output/<chapter>/bN_ja.md` の一覧・plot の block 一覧を読み合わせ、フラグのみ／実体のみの両方向を列挙する。
  - 不整合を見つけたら、フラグ側を消して実体に合わせ、欠落区間だけを再実行する。
- **タイムアウト対策。** 子エージェントに「5 ブロックごとに emit まで完了させ、区切りで中間報告する」と指示し、成果を小まめに確定させる。1 回の委譲で 1 章の全ブロックを渡すと、途中で落ちたときの損失が大きい。
- **委譲のモデル／プロバイダが生きているか確認する。** 上位の `delegation.provider` / `delegation.model` が古いと、子は起動直後に 429（残高・上限）で全滅する。子は動いているのに全タスクが失敗するときは、まず委譲の設定を見る。
- **委譲設定は config.yaml を直接編集できない**（セキュリティ保護）。`hermes config set delegation.provider <p>` / `hermes config set delegation.model <m>` を使う。
- **同一 TOML を複数の子に同時編集させない。** ブロック範囲を分けても、`plot-[ネタバレあり].toml` / `terms.toml` は共有ファイルなので、複数の子が「read→追記→write」を並行すると後から書いた子が相手の追加分を消す（並行追記で相手の追加分が消えた実例が複数回ある）。対策は次のいずれか。
  - 1 ファイル 1 子にする（例: 子A は `b1〜bN` の plot だけ、子B は `bN+1〜bM` の terms だけ、のようにファイル所有権で分ける）。
  - どうしても同一ファイルを共有するなら、子に「書き込み直前に必ず最新を read し、自分の追加分だけを末尾に足す（fresh-read→append→atomic write）」を明示し、完了後に親が両方の追加分が残っているかを突き合わせて検証する。
  - 親が検証する対象: 各ブロックの本数、`block = "chNNN/bN"` の重複・欠落、terms 件数が減っていないか。
- **原文キー（`*_src`）を一括置換で壊さない。** 訳語を一括置換するとき、対象行から `term_src` / `name_src` / `title_src` / `target_src` を除外する。原文キーは簡体字のままが正で、日本語新字体に置換すると原文照合が壊れる（一括置換で原文キーがまとめて壊れた実例がある）。置換後の検査は2点。
  - 同じ訳語に対して原文キーが2種類できていないか（エントリの重複）。
  - 原文キーに日本語の新字体・正字が混じっていないか（原文が簡体字なら誤り）。照合は「陽光」「聖耀」「戦姫」のような2字語以上の形で行い、単字（結・据・陽 等）では拾わない（日本語の正字を大量に誤検出する）。`*_ja` を持つエントリだけを対象にする（`title_src` だけのエントリまで見ると誤検出に埋もれる）。
  - `_saga/_tools/` に `check_src_keys.py` があればそのまま使える。無ければ上記2点を grep と読み合わせで確認する。
- **子が「失敗」と報告しても実体を疑う。** 出力スキーマ不適合やタイムアウトで「failed」になっても、ファイルは部分的に書かれていることが多い。まず上記の突き合わせ（`verify_progress.py`）と各 TOML の中身を読み、フラグと実体の両方を突き合わせてから再投入範囲を決める。反対に「完了」報告でも `analysis_done_at` だけ立って中身が無いことがある。
- **analysis TOML は CRLF 改行。** `analysis/*.toml` は CRLF、`plot-[ネタバレあり].toml` は LF（スクリプト生成物）。複数子が同一 TOML を追記するとき、`open(..., newline="")` で読むと `\r` が残り `^name_src = "..."$` の `re.M` マッチが全件外れる（`$` の直前が `\r`になるため）。マッチ用は `read().replace("\r\n","\n")` で正規化した本文を使い、書き戻し時に元の改行コードへ戻す。`at_block` 等の重複ガードがこのせいで無効化され、二重追記が起きる。
- **`analysis_done_at` は成果物ではない。** これは `build_context.py --mode analysis` 実行時に立つ「取得開始記録」にすぎない（`references/data-schema.md`）。真の完了判定は `plot-[ネタバレあり].toml` に `block = "chNNN/bN"` が存在すること。フラグだけを見て『完了』と判断すると未処理区間を飛ばす。

### 翻訳・検証

- **`qa_translation.py --emit` は `output/_work/` のドラフトを削除する。** jp_fix / align_check / check_align は emit より前に走らせる。順序を誤ってドラフトを失ったら `output/<ch>/bN_parallel.md`（原文＋訳文の対）から `[Pn]` 付きドラフトを復元できる。emit 後の訳語一括置換や誤検知対応はこの復元→再 emit で行う。
- **段落をまたぐ台詞の `」` 閉じ漏れは emit で段落の区切りを消す。** 中国語原作は台詞が複数段落にまたがるとき各段落頭に `“` を繰り返し置き、最後の段落で一度だけ `”` を閉じる。これをそのまま `「` に写すと閉じの無い行が生まれ、`layout_lines` が次に閉じるまでの段落を空行なしで連結する（90 行超が 1 ブロックに潰れた実例がある）。訳出時は各段落の末尾に `」` を補う。ドラフト段階では `qa_translation.py` の `merged_paragraphs` 検査が段落融合を検出する（2 段落は警告・3 段落以上は不合格）。確定済みの作品に見つけた場合は `scripts/fix_layout.py <workdir> --fix` で修復する（不閉じ行に `」` を補い、段落区切りを再整形。対訳の原文側は触らない）。
- **日本語フィールドに原語が残る。** 分析 TOML の `note` / `memo` / `arcs` / `text` / `appearance` や訳文本文に、原文の簡体字や中国語語彙（例: 缆・涩・师・铃、星辰・搏動・紅暈・翕動・醇厚・凄絶、`凛老师`・`青涩`・`腼腆`）をそのまま残す事故が多発する。carry で次作へ伝播するので、見つけたら全作品を遡って直す。
  - `*_src` / `target_src` / `term_src` / `title_src` は原文キーなので原字のままが正しい。触らない。
  - 検出は日本語フィールドだけを対象にし、原文字段を除外する。既存のファイルを一括走査する道具を用意して、工程の区切りごとに回す。
  - `凄絶な悲鳴`（中国語の「凄厉」）のように、日本語で成立するが不自然な語は、後続語（悲鳴・絶叫・哀鳴）で判定して `凄まじい悲鳴` に直す。`凄絶` 単体の機械置換はしない。
  - 段落ごと未訳のまま（中国語の語彙を漢字のまま並べただけの）行が紛れることがある。語の置換ではなく原文から訳し直す。
- **ドラフト検査コード**（鉄則11）。ツールを呼ぶ前に自分で走らせる。鉤括弧の均衡は `qa_translation.py` の `merged_paragraphs` 検査が自動で見る：
  ```python
  import re
  ns = [int(x) for x in re.findall(r"^\[P(\d+)\]", open(F).read(), re.M)]
  assert ns == list(range(ns[0], ns[-1] + 1)), "マーカーが連番でない"
  ```
- **align の誤検知**：訳文の非会話段落が `「` で始まると会話と誤判定される。原文が非会話なら、行頭の鉤括弧を避ける語順に直す（例: 「プレートに“調教室C”と書かれた」→「プレートには“調教室C”の文字があった」）。
- **巨大な合体段落**：原文が 1 段落に数百〜数千字を入れていることがある。読点の位置で推測分割せず、原文の段落数とマーカー番号を正とする。
- **空行の無い TXT**：行が単一改行でしか区切られていない TXT では split_paras が巨大段落を作る。`normalize_txt.py` で全行間に空行を入れた `_norm.txt` を生成してから ingest をかけ、`n_paras` が行数と一致するか検証する。原入力は触らない。
  ```
  python3 <skill>/scripts/normalize_txt.py 入力.txt   # 入力_norm.txt が生成される
  ```

## よくある指示への応答

| 指示 | 動作 |
|---|---|
| 「この小説を翻訳して」 | 0→1→2→3→4→5 を順に。進捗はブロック単位で報告 |
| 「第N章だけ」 | 該当章のブロックのみ翻訳 |
| 「この章やり直して」 | 該当ブロックを消して再翻訳 |
| 「○○の訳語を△△にして」 | rename_terms で伝播 |
| 「登場人物表見せて」 | analysis/characters-[ネタバレあり].toml を要約表示 |
| 「続きを翻訳して」 | 未完了ブロックを検出して続行 |
| 「対訳で出力して」 | `assemble.py --parallel` で対訳を生成（年齢正規化の記録がある作品は自動） |
| 「画像が無い」 | `output/images/` に ID を含む画像を置くよう案内。assemble 再実行でタグを埋め込む |
| 「成人向け作品を訳したい」 | 本体では対応しない。ユーザーが拡張スキル（`saga-translator-spicy`）を指示した場合は、そのスキルに従う |
| 「全体を一気に訳して」 | 1M 以上の context モデルなら例外として許可。ただし章単位を推奨 |

## 出力スタイル（既定）

- 文体は常体（切替可）。ラノベ読者に自然な日本語。翻訳調・説明過多・評価語の連発を避ける。
- ルビは人名・固有名詞の初出のみ `{訳語|よみ}`（VFM）。設定で always/none に切替可。
- 感覚表現（擬音・身体反応）は直訳せず、日本語として実感のある表現に置き換える。
- 不確実な箇所は推測で埋めず、`[要確認: …]` を本文に残さない。作業後にユーザーへ確認を取る。
- 段落は原文と 1 対 1。結合・分割はマーカー振り直しを伴う場合のみ（鉄則11・12）。
- レイアウトは全行の間に空行を 1 つ入れる（改行 2 回）。セリフ行の前後にも、地の文の 1 行ごとにも空行を入れる。画像行・見出し行も同じ。確定出力（`qa_translation.py --emit`）が機械的に整形する。引用符が閉じていない間の行（歌詞などの複数行ブロック）は単一改行で継続する。
- 台詞の括弧は `“ ”` を使わず鉤括弧 `「」` で統一する。原語の引用符は訳出時に置換する。
