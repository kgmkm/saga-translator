# agent-delegation — 委譲運用

SKILL.md の翻訳パイプラインを子エージェント（サブエージェント）へ委譲するときの運用ルール。ハーネス非依存で、Hermes・DSH などの違いは呼び名と設定方法だけ（「ハーネス対応表」を参照）。参照される本体の版: 0.12.0。

実行環境（シェル・OS・文字コード・長文書き込み）ごとの注意は `references/env-*.md` に分離してある（例: `references/env-windows.md`）。

## ハーネス対応表

役割ごとの呼び名・設定方法はハーネスによって違う。自分の環境の列を読む。行（役割）・列（ハーネス）とも足して拡張する。

| 役割 | Hermes（例） | DSH（例） |
|------|--------------|-----------|
| 子エージェントの起動 | `delegate_task` | `subagent` |
| 委譲先のモデル・提供元の設定 | `delegation.provider` / `delegation.model`（`hermes config set` 経由） | ハーネスの設定方法に従う |
| 稼働中の子の確認・結果回収 | `delegate_task(action='list')` 等 | ハーネスの一覧・通知に従う |

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

委譲先のモデル・提供元の設定が古いと子が 429（レート制限）で全滅する。設定はハーネスの管理コマンド経由で行う（Hermes では `delegation.provider` / `delegation.model` を `hermes config set` で変更。config.yaml の直接編集は不可）。