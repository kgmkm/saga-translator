# P0-2 棚卸しインベントリ

## 取り込み対象ツール（7ツール）

| ツール | 発見場所（ベースにした作品） | 元の行数 | 取り込み先パス | 主な変更点 |
|--------|------------------------------|----------|----------------|------------|
| verify_progress.py | `作品A/_saga/_tools/` (117行) + `作品B-8880001/_saga/_tools/` (98行) | 117 | `scripts/verify_progress.py` | 作品A版をベースに argparse 化。`_lib.py` の `load_chapters_meta`, `direction`, `target_suffix` を使用。JSON chapters_meta の後方互換対応 |
| check_src_keys.py | `作品A/_saga/_tools/` (91行) | 91 | `scripts/check_src_keys.py` | argparse 化。glob を Path.glob に置換。`_lib.py` の `load_toml` を使用。存在しないディレクトリで終了コード2を返す |
| safe_emit.py | `作品A/_saga/_tools/` (91行) / `作品B-8880001/_saga/_tools/` ／ `作品C-8880002/_saga/_tools/` — 同一内容 | 91 | `scripts/safe_emit.py` | SK のハードコードパスを `Path(__file__).parent` に置換。argparse は既に完備 |
| blk.py | `作品A/_saga/_tools/` (23行) | 23 | `scripts/blk.py` | argparse 化。`_lib.py` の `load_chapters_meta`, `load_paras` を使用。first char only のバグを修正 |
| ctx_body.py | `作品C-8880002/_saga/_tools/` (49行) | 49 | `scripts/ctx_body.py` | 変更ほぼ無し。argparse 完備。存在しないファイルで終了コード1を返すようにした |
| restore_parallel.py | `作品B-8880001/_saga/_tools/` (81行) | 81 | `scripts/restore_parallel.py` | 作品IDハードコードを argv から取るように完全書き換え。`work_dir`, `backup_dir` 引数化。作品IDフォルダ列挙を自動化 |
| check_layout.py | `作品D-8880003/_saga/_tools/` (172行) | — | **このタスクでは作成せず** | `fix_layout.py --check`（存在しない）への統合が必要。後続タスクで対応 |

## 取り込まなかったツール

以下は `_tools/` に存在するが作品固有または使い捨てのため、取り込まない：

| ツール | 発見場所 | 取り込まない理由 |
|--------|----------|------------------|
| fix_terms_v2.py | 多数の作品 | 作品固有の用語修正ワンオフスクリプト。単発の置換ルールがハードコードされている |
| scan_zh.py | 多数の作品 | 作品固有の中国語スキャン。各作品で異なるパターンを正規表現でベタ書き |
| fix_*.py（各種） | 多数の作品 | 各作品の状態に合わせた一回限りの修正スクリプト。作業が終われば捨てる前提 |

## 後続タスクでの扱い

- **check_layout.py** → `scripts/fix_layout.py` に `--check` モード追加。`fix_layout.py` の既存 `--fix` を check-only で呼べるようにする。P0-2 後半で対応予定。
- `_lib.py` のパス集約 → P0-3 で実施。このタスクでは触らない。

## 発見した注意点

1. **verify_progress.py** は作品A版と作品B版で異なる出力スタイル。作品A版は全作品スキャン、作品B版は単作品＋--chapter 指定。本体では作品A版をベースに argparse 化した（単作品も --work で指定可能）。
2. **blk.py** のオリジナルは chapters[0] しか見ない（単一章作品前提）。本体でも同じ制約だが、作品の chapters 配列が複数の場合は最初の章のみ対象と明記した。
3. **restore_parallel.py** のハードコードは同一作品の全サブIDだった。argv 化にあたり、作品IDディレクトリを自動列挙するように変更。
4. **safe_emit.py** は scripts/ のパスを `Path(__file__).parent` で動的解決するようにした。元の C:/Users/... 絶対パスは不要。