# troubleshooting — 落とし穴と対処

SKILL.md の通常フローに含まれない障害対応。参照される本体の版: 0.12.0。

## タイムアウト対策

saga.py commit がブロック単位で確定するようになったので「こまめに commit」が基本。翻訳委譲の際はブロック単位の commit を推奨。

## 委譲設定の確認

- 子が 429（残高・上限）で全滅する → `delegation.provider` / `delegation.model` を確認。`hermes config set delegation.provider <p>` / `hermes config set delegation.model <m>` で設定（config.yaml 直接編集不可）。
- 稼働数確認: `delegate_task(action='list')`

## ロック衝突（exit 3）

`saga.py` の各コマンドは作品単位の共有ロック（`<work>/_work/.lock`）を取る。取れない場合は **exit 3**（`…（終了コード 3）` の表示）で止まる。並列委譲の子エージェントでは必ず起きる。

- **10秒待って同じコマンドを再実行する（最大5回）**。exit 3 は「失敗」ではなく「順番待ち」なので、内容を直したりせずそのまま再実行する。
- 長く走る処理（analyze-commit 等）と衝突した場合は5回で解消しないこともある。その場合は親（またはユーザー）に報告する。
- stale ロック（10分超）は所有者の死亡が確認できたときだけ回収される（別ホスト・権限不足では回収されない）。

## 同一 TOML の同時編集

analyze-commit の排他ロックで機械的に担保。手動で編集する場合は以下を守る:

- 最新を read → 自分の追加分だけを末尾に足す（fresh-read→append→atomic write）
- 完了後に親が両方の追加分が残っているかを検証（ブロック本数・重複・欠落・terms 件数）

## 原文キー（*_src）保護

rename_terms 等の一括置換で `term_src` / `name_src` / `title_src` / `target_src` を壊さない。原文キーは原語のままが正。

置換後の検査:
1. 同一訳語に原文キーが2種類できていないか（エントリ重複）
2. 原文キーに訳文言語の字形が混じっていないか

`check_src_keys.py` が scripts/ にあるので使うこと。

## 子の「失敗」報告への対処

「failed」でもファイルは部分的に書かれていることが多い。`verify_progress.py`（scripts/ に実在）を必ず使い、フラグと実体の両方を突き合わせてから再投入範囲を決める。

## 失敗した commit を調べる

commit が止まったら、まずスナップショットと stdout を見る:

- `output/_work/commit/<ts>_<ch>_<b>/draft.md` — 検査開始時のドラフト（字形整形を適用済み。失敗した実物）
- `output/_work/commit/<ts>_<ch>_<b>/report.json` — 検査の内訳（`stages.align` / `stages.qa` / `stages.residue`。どの検査で落ちたかが残る）
- stdout の `[commit] エラー:` / `[commit] QA FAIL:` / `[commit] 中止。…` を確認する（PowerShell の exit 表示に依存しない。`references/env-windows.md`）

よくある失敗:

- `[commit] エラー: ドラフトがありません: …output/_work/chNNN_bM_ja_numbered.md` — 置き場は **`output/_work/`**（`_work/` ではない）
- `[commit] … 確定済みです。改訂するには --revise を付けて再実行してください` — 改訂は `--revise`。**新しいドラフトが必須**（`output/_work/done/` のアーカイブを編集しても通らない。`output/_work/` に置き直す）
- `QA FAIL: leftover_source_names` — 原語（または `aliases`）が訳文に残っている。`aliases` に入れた日本語名が原因のことがある → 次の節
- exit 3（`…（終了コード 3）`）— ロック衝突 → 上の「ロック衝突（exit 3）」節

## aliases で QA が落ちたとき（leftover_source_names）

`aliases` は**原文の別表記**の欄で、QA（`leftover_source_names`）は `aliases` の各形も「訳文に原語が残っていないか」の照合対象にする。ここに**訳文に残す日本語名**を入れると、訳文に正しく現れていても残留と誤判定され commit が止まる。

1. `aliases` から訳文用の日本語名を外す
2. 訳文にそのまま残す名前は、**独立エントリ**として `name_src` と `name_ja` を同じ表記にして登録する（QA は `name_src == name_ja` のエントリを照合しない）
3. 普通の語は `analysis/terms.toml` に `term_src` / `term_ja` で登録し、`aliases` と二重管理しない

定義は `references/data-schema.md`（characters 節）を参照。

## 失敗コミット後の draft 再読込

commit は検査前に字形整形を draft へ in-place 適用する（`[commit] 字形整形: N件適用 …` と出る）。失敗コミットの後で draft を編集するときは、**必ず再読込してから**編集する（適用前の読み取り内容での編集は競合する）。

## 空行の無い TXT の対処

```
python3 scripts/normalize_txt.py <input.txt>   # 入力_norm.txt を生成
```
生成後、`n_paras` が行数と一致するか検証してから ingest をかける。

## 段落をまたぐ台詞の閉じ漏れ

中国語原作の複数段落 `"` を `「` に写すと閉じの無い行が生まれる。訳出時に各段落末尾に `」` を補う。確定済みの作品は `scripts/fix_layout.py <workdir> --fix` で修復。

## align の誤検知

訳文の非会話段落が訳文言語の会話頭で始まると誤判定される。行頭の引用符を避ける語順に直す（例: 「プレートに"施設C"と書かれた」→「プレートには"施設C"の文字があった」）。

## 原文キーの照合ルール

照合は「光A」「光B」のような2字語以上で行い、単字（結・据・陽 等）では拾わない。`*_ja` を持つエントリだけを対象に。

## emit 済みドラフトの復元

commit 後、ドラフトは `output/_work/done/` にアーカイブされている（purge 後も残る）。`output/<ch>/bN_parallel.md`（対訳）から `[Pn]` 付きドラフトを復元できるのは、対訳を残した作品（既定で残る。`--no-parallel` で assemble した作品・旧版では無い）。復元の手順は下の「assemble 後の改訂路」。

## assemble 後の改訂路（chapters・ドラフトの復旧）

assemble が完了すると、原文・`translation_input`・`output/_work`（`done` と `commit` を除く）は削除される。`chapters/*.paras.toml` を残す版ではそのまま使える（assemble 済みで chapters が無い場合は再生成する）。`status` の案内（「改訂するには対象ブロックを commit --revise で確定し直して再 assemble」）を実行するには:

1. **chapters を確認する**: `chapters/chNNN.paras.toml` が無ければ、元の入力ファイル（purge は触らない）から再生成する（下のスニペット）
2. **chapters_meta の記録を守る**: `split_chapters.py` を再実行した場合は analysis_* / translation の記録が作り直され消える。**実行前に `chapters_meta.toml` を退避**し、block の記録を書き戻す（記録が無いと分析ゲート・改名 stale 判定が狂う）
3. **ドラフトを戻す**: `output/_work/done/chNNN_bM_ja_numbered.md` を `output/_work/` へコピーする（これが「新しいドラフト」）
4. **再確定して再 assemble**: `python3 scripts/saga.py commit <workdir> --chapter chNNN --block bM`（確定出力が残っている作品は `--revise`。消えていれば通常の commit）→ `python3 scripts/assemble.py <workdir>`

段落再生成のスニペット（実証済み）:

```python
import sys; sys.path.insert(0, "scripts")
from pathlib import Path
from _lib import parse_novel_header, split_paras, save_structured
work = Path("<workdir>"); src_file = Path("<元の入力ファイル>")
meta, body = parse_novel_header(src_file.read_text(encoding="utf-8"))  # 戻り値は (meta|None, body)
paras = split_paras(body)  # 空行区切り
save_structured(work / "chapters" / "ch001.paras",
                {"paras": [{"n": i + 1, "text": t} for i, t in enumerate(paras)]})
# 検証: 段落数・本文が manifest の n_paras / n_chars と一致するか確認する
```

- 再 assemble で `[assemble] 確定済みブロックなし` と出るのは、確定済みブロックの出力（`output/<ch>/<b>_ja.md`）が無く、`output/_work/` にドラフトも無いため。手順 3 のドラフト復旧 → 再確定を経てから再実行する。`output/_work/done/` も空なら原文からやり直しになる
- 予防: 改訂の予定がある作品は assemble を `--keep-source` で実行し、中間物を残す（この復旧作業が不要になる）

## 分析 TOML の改行コード

手編集時は改行コードを元のまま維持。`analysis/*.toml` は CRLF、`plot-[ネタバレあり].toml` は LF。