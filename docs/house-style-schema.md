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
```

## 非互換変更のルール

- `version`（トップレベル）をインクリメントする条件：
  - 既存の `kind` の値セットを変更・削除したとき
  - `scope` / `status` の値セットを変更したとき
  - 必須属性を追加・削除したとき
- エントリの追加・削除だけでは version を上げない