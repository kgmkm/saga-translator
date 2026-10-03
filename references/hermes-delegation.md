# hermes-delegation — 委譲運用

SKILL.md の翻訳パイプラインで Hermes の `delegate_task` を使う際の運用ルール。参照される本体の版: 0.11.0。

## 並列の上限

- 同一 work 内は最大2子まで。3子以上は衝突・訳語分岐・検証コストが跳ねる。
- 別 work（別作品・別章の独立 work）はこの限りでない。
- 分析は直列1子（`characters-[ネタバレあり].toml` / `terms.toml` / `plot-[ネタバレあり].toml` を複数の子に同時編集させない）。
- 翻訳のみ2並列可。範囲は前半/後半の連続割にし、奇偶割はしない（build_context の前後参照を殺す）。
- commit は親が直列で行う（saga.py commit）。

## ブリーフ

`templates/brief_translate.md` / `templates/brief_analyze.md` を使う。

## 子に渡す範囲

連続割（前半/後半ブロック）。1 回の委譲で 1 章の全ブロックを渡さない。

## 翻訳中の新訳語

翻訳中の人物表更新は禁止。新訳語は各子の sidecar（`_work/chXXX_bY_newterms.toml`）に書かせ、親が merge して `rename_terms.py` で伝播させる。子が読む人物表は凍結版。

## 委譲設定の確認

`delegation.provider` / `delegation.model` が古いと子が 429 で全滅する。`hermes config set` 経由で設定（config.yaml 直接編集不可）。