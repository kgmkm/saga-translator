# troubleshooting — 落とし穴と対処

SKILL.md の通常フローに含まれない障害対応。参照される本体の版: 0.11.0。

## タイムアウト対策

saga.py commit がブロック単位で確定するようになったので「こまめに commit」が基本。翻訳委譲の際はブロック単位の commit を推奨。

## 委譲設定の確認

- 子が 429（残高・上限）で全滅する → `delegation.provider` / `delegation.model` を確認。`hermes config set delegation.provider <p>` / `hermes config set delegation.model <m>` で設定（config.yaml 直接編集不可）。
- 稼働数確認: `delegate_task(action='list')`

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

## 空行の無い TXT の対処

```
python3 scripts/normalize_txt.py <input.txt>   # 入力_norm.txt を生成
```
生成後、`n_paras` が行数と一致するか検証してから ingest をかける。

## 段落をまたぐ台詞の閉じ漏れ

中国語原作の複数段落 `"` を `「` に写すと閉じの無い行が生まれる。訳出時に各段落末尾に `」` を補う。確定済みの作品は `scripts/fix_layout.py <workdir> --fix` で修復。

## align の誤検知

訳文の非会話段落が訳文言語の会話頭で始まると誤判定される。行頭の引用符を避ける語順に直す（例: 「プレートに"調教室C"と書かれた」→「プレートには"調教室C"の文字があった」）。

## 原文キーの照合ルール

照合は「陽光」「聖耀」のような2字語以上で行い、単字（結・据・陽 等）では拾わない。`*_ja` を持つエントリだけを対象に。

## emit 済みドラフトの復元

commit 後は `_work/done/` にアーカイブされている。`output/<ch>/bN_parallel.md`（対訳）から `[Pn]` 付きドラフトを復元できる。

## 分析 TOML の改行コード

手編集時は改行コードを元のまま維持。`analysis/*.toml` は CRLF、`plot-[ネタバレあり].toml` は LF。