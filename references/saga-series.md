# 連作版ビルダー（saga-series）

saga-translator で訳した同一シリーズの複数作品を、連作として読みやすい1フォルダにまとめる。
`_saga/<作品ID>/output/` の確定出力（全文_訳文.md・images/）だけを読む。source は `--repair` 指定時のみ書き換える。

## いつ使うか

- 「outputを1つのフォルダにまとめて」「連作として読みやすく」「合本にして」「sagatest にコピーして」
- シリーズの続きを訳し終えて、既訳分と並べた読書用フォルダが欲しいとき

## 使い方

```bash
SK=saga-translator/scripts
python3 "$SK/build_series_edition.py" <root> --check          # 点検（書込みなし）
python3 "$SK/build_series_edition.py" <root> --dest <dst> \
  --note "原作：…／シリーズ名…" --url "https://…" --footer "翻訳: saga-translator（…）"
```

- `<root>` はシリーズ親ディレクトリ（`_saga/` を含む）または `_saga/` 自体。どちらでも解決する。
- 既存 dest を更新するときは `--force`（dest を消して再ビルドする）。
- `--works id1,id2` で対象作品を明示できる。

### 出力

| ファイル | 内容 |
|---|---|
| `目次.md` | シリーズ情報・単話リンク・未翻訳一覧 |
| `合本.md` | 全話結合 |
| `NN_<訳題>.md` | 通し番号付き単話 |
| `images/` | 全話の画像を統合 |

- 収録順：`_saga/進捗.md` の表の順（＝pixiv 投稿順）→ 無ければ ID 昇順。
- 訳題の解決順：`analysis/titles.toml` → 全文の見出し → 進捗表の訳題 → 原題（要確認）。

## 鉄則

1. ビルド前に `--check` を走らせる。訳題未確定・画像参照欠落・非JIS漢字を先に出す。要対応が出たら `--repair` で補修してからビルドする。
2. `--repair` の対象は最小限にとどめる。titles.toml 欠落と、全文・目次の見出し欠落だけを補修する。訳題が原題しか解決できない作品には titles.toml を作らない（勝手に訳題を発明しない）。補修後は再実行する。
3. 検証はスキップしない。`verify: OK` は「各話が source と内容一致・画像参照全解決・目次リンク全解決・setext 危険なし」の意味。`--no-verify` は緊急時のみ。
4. 画像名の衝突で停止したら、勝手にリネームせずユーザーに聞く。saga-translator は `<作品ID>-<画像ID>.<ext>` 形式なので通常は衝突しない。
5. `--force` は既存 dest を消す。深いパス以外は拒否するが、dest の指定は慎重に。
6. 非JIS漢字の報告は情報として扱う。シリーズ名・未訳原題（意図的な中文表記）は正常。単話本文に出たら source を直して再ビルドする（`rename_terms` か該当ブロック再翻訳）。

## スクリプトが対策済み障害

手作業で合本を作ると必ず踏む地雷と、それへの対策を記す。

- `---` の setext 事故：結合部の `---` の直前が非空行だと、markdown はそれを setext 見出しとして描画する（URL行が巨大見出しになる）。`normalize_body` が `---` の前後に空行を確保し、`verify_build` が `setext_hazards` で再発を検出する。
- 見出し欠落：`analysis/titles.toml` を作らず assemble した作品は、全文の先頭が `<!-- ch001 -->` / `# ch001` のまま見出しが無い。単話ファイルには訳題見出しを補い、`--repair` で source 側も直す。
- 内部足場：全文には `<!-- ch001 -->` と `# ch001` が残る。読書用コピーでは除去し、空行の連続も 1 つに圧縮する（本文は変えない）。
- 改行コード：読書用コピーは CRLF に統一（`--eol lf` で変更可）。検証が孤立改行を検出する。

## テスト

```bash
python3 -m unittest discover -s <skill>/scripts/tests -v
```
自作フィクスチャのみ（実作品は入れない）。
