# house_style.toml スキーマ定義

## ファイル置き場

```
<saga_root>/_house/house_style.toml
```

現行レイアウトでは `<入力フォルダ>/_saga/_house/house_style.toml`。

新レイアウト（P3-3 `migrate_layout.py` 対応後）は `<入力フォルダ>/_翻訳版/_共通設定/house_style.toml`。

## トップレベル属性

| キー | 型 | 必須 | 説明 |
|------|-----|------|------|
| `version` | 整数 | yes | スキーマ版。新規作成時は1。既存と互換性がない変更のたびに+1 |
| `created_at` | 文字列 | yes | ISO 8601 UTC（例: `2026-10-03T12:00:00Z`） |
| `updated_at` | 文字列 | yes | 同上。エントリ追加時・更新時に書き換える |
| `rules` | 配列 of テーブル | yes | ハウススタイルルールの配列 |
| `naming_policy` | テーブル | no | 命名方針（下記 `[naming_policy]` 節を参照） |

## `[[rules]]` エントリ

| キー | 型 | 必須 | 説明 |
|------|-----|------|------|
| `id` | 文字列 | yes | snake_case の一意識別子。重複禁止 |
| `kind` | 文字列 | yes | `naming` / `ruby` / `keishou` / `jishou` / `ninshou` / `kigou` / `kugirisen` / `goi` / `sonota` のいずれか |
| `scope` | 文字列 | **yes** | `work` / `series` / `character` / `global` のいずれか |
| `target` | 文字列 | 条件付き | scope に応じた識別子（作品ID、シリーズID、人物IDなど）。`global` の場合は省略可 |
| `pattern` | 文字列 | yes | 対象となる文字列・パターン |
| `replacement` | 文字列 | no | 推奨される置換先（`preference` との二者択一） |
| `preference` | 文字列 | no | 推奨される表記（`replacement` との二者択一） |
| `rationale` | 文字列 | no | 根拠（作品と段落、または「過去作の表記に合わせる」等） |
| `status` | 文字列 | yes | `candidate` / `approved` / `locked` のいずれか |
| `version` | 整数 | yes | このエントリの版。新規作成時は1 |
| `evidence` | 文字列の配列 | no | 参考になる段落番号やファイル位置 |

### `kind` の値と意味

| 値 | 意味 |
|----|------|
| `naming` | 命名規則（人名・地名・固有名詞の訳し方） |
| `ruby` | ルビの形式（`{訳語|よみ}` の使用有無・パターン） |
| `keishou` | 敬称（さん・様・殿・先生 等の対応） |
| `jishou` | 自称の対応（私・俺・僕 等） |
| `ninshou` | 二人称の対応（あなた・君・お前 等） |
| `kigou` | 記号（約物の使い分け） |
| `kugirisen` | 区切り線（`---` `***` 等の処理） |
| `goi` | 語彙（頻出語・仕様外で決めた訳語） |
| `sonota` | その他 |

### `scope` と `target` の関係

| scope | target の意味 | target の要否 |
|-------|--------------|---------------|
| `work` | 作品ID | 必須 |
| `series` | シリーズID | 必須 |
| `character` | 人物ID（`name_src` または `name_ja`） | 必須 |
| `global` | シリーズ横断／全作品共通 | 省略可 |

### `status` の意味

| 値 | 意味 |
|----|------|
| `candidate` | 自動収集された候補。自動採用されない |
| `approved` | ユーザーまたはレビューで承認済み |
| `locked` | 確定。変更には改めてレビューが必要 |

## `[naming_policy]`（命名方針・任意）

固有名（人名など）の扱い方の決定を記録する。`lock_prep` が「固有名の扱い（命名方針）」の一覧を出力し、扱いが混在していれば「要確認」として提示する（混在は機械には誤りと判定させない。作者が意図して付けた名前もありうる）。決めた方針をここに記録する。

未訳のブロックが残っている作品では、次のブロックを確定する前に方針を決める（`saga.py status` が「命名方針: 未決定」を表示する）。続編では `carry_analysis` が前作の `[naming_policy]` を引き継ぎ、「前作の方針: …（このまま進めるか確認）」として表示する。

| キー | 型 | 必須 | 説明 |
|------|-----|------|------|
| `style` | 文字列 | yes | `transliterate`（音訳・カタカナ）/ `sinitic`（漢語のまま）/ `mixed`（混在。意図的な併用）のいずれか |
| `note` | 文字列 | no | 理由・言語圏の系統（例: 洋風世界観のため名も姓も音訳、言語圏を揃える） |
| `scope` | 文字列 | yes | `work` / `series` のいずれか。人名の裁定は両作品に同時適用される前提のため `series` を推奨 |
| `decided_at` | 文字列 | yes | 決定日時。ISO 8601 UTC（例: `2026-10-05T12:00:00Z`） |

`[naming_policy]` はテーブル（`[[rules]]` ではない）。lock_prep の「要確認」は確認の候補であり、方針が記録されれば表示されなくなる。方針の記録は `house_style.py validate` の必須キー（`rules` 等）には影響しない（`[naming_policy]` は任意）。

## 設計の原則

1. **自動採用しない**: `house_style.py build` は `status: candidate` の候補を列挙するだけ。既存の `house_style.toml` に書き込まない。
2. **scope 必須**: すべてのルールに scope を必須とすることで、「1作品の改名が全作品に波及する」事故を防ぐ。
3. **replacement と preference の二者択一**: `replacement` は置換先文字列、`preference` は表記の推奨（どちらか一方のみ使う）。

## ファイル例

```toml
version = 1
created_at = "2026-10-03T12:00:00Z"
updated_at = "2026-10-03T12:00:00Z"

[[rules]]
id = "ruby_format_vfm"
kind = "ruby"
scope = "global"
pattern = "ルビ"
replacement = "{訳語|よみ}"
rationale = "VFM形式のルビで統一する"
status = "approved"
version = 1
evidence = ["ch001/b1", "ch001/b3"]

[naming_policy]
style = "transliterate"
note = "洋風世界観のため名も姓も音訳。言語圏の系統を揃える"
scope = "series"
decided_at = "2026-10-05T12:00:00Z"
```

## 非互換変更のルール

- `version`（トップレベル）をインクリメントする条件：
  - 既存の `kind` の値セットを変更・削除したとき
  - `scope` / `status` の値セットを変更したとき
  - 必須属性を追加・削除したとき
- エントリの追加・削除だけでは version を上げない