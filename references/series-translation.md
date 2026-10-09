# 連作翻訳フロー（series-translation）

同一作者・同一世界の複数作品を、訳語・声を固定したまま訳す。合本化は `references/saga-series.md`（出力段階）。ここは翻訳時の状態管理だけ。

## 既定: パイロット先行

第1話で型決め→残りは carry 量産。全話一括スタートは既定にしない。

## 手順

### 0. 機械処理の一括

ingest（2作目以降は `--series-from <前作workdir>`）→ split → `age_policy --check`（spicy）→ normalize_images をループで回す。LLM はまだ読まない。スキル更新中は ingest しない（manifest に旧版が刻まれる）。

### 1. 第1話で型決め

lock_prep → 分析一周 → titles → 翻訳 → review_wave → fixes → rename 伝播。人物表・用語・文体・spicy 語彙をここで固定する。

### 2. 残りは carry 量産

```
python3 scripts/carry_analysis.py <現作> --from <前作>
```

以降の分析は引き継ぎ値を確定扱いし、変化だけ arcs に追記する。新規人物・用語が無いブロックは `no_changes = true` で明示する。分析は直列1（人物表の同時編集禁止）。翻訳は別 work 扱いで並列可（同一 work 内は最大2子）。

### 3. レビュー二段階

第1〜2話だけフルレビュー。残りは `saga.py status` で滞留（proposed）だけ捌き、横断の不整合は最後の `build_series_edition --check` で検出する。話数分の fixes.txt を回さない。

### 4. 合本は最後

各話 titles は最小限で回し、訳題統一と合本（目次・通し番号・合本.md）は全話確定後に一括。`--check` → `--repair` → build。

## 鉄則

1. 前作の arcs は持ち込まない。`carry_analysis.py` が終了時点に畳み、新規 `at_block: carry` を起点にする。作品を跨ぐ arcs は書かない。
2. 単一キャラファイルにまとめない。各話の人物表・分析・翻訳・QA は workdir 単位で行う。
3. ledger は carry で引き継ぐ（ルビ再付与・オノマトペ重複の防止）。purge しない。
4. spicy の年齢・学校段階方針はパイロット話で確定させる。2話目以降の覆しは全話作り直しになる。
5. `ingest --series-from` / `--standalone` の決定を manifest に残す。後から連作だったと気づいたら carry で繋ぎ直せる。
