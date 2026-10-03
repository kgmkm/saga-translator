# 分析タスク: {{work_id}} {{chapter}}/{{block}}

## あなたが読むファイル
1. `saga.py prepare <work> --chapter {{chapter}} --block {{block}} --mode analysis` で生成されたコンテキスト — このファイルだけを読む。作品全体を読まない
2. 言語tips: `references/lang_{{src}}2{{dst}}.md`（存在すれば）

## 手順
1. `saga.py prepare <work> --chapter {{chapter}} --block {{block}} --mode analysis` を実行する（親が既に実行済みなら省略可）
2. コンテキストを読む
3. 既存の人物表・用語集・プロットを分析し、新しい登場人物・用語・プロットエントリを抽出する
4. `_work/{{chapter}}_{{block}}_delta.toml` に差分を書く。形式:
   ```toml
   block = "{{chapter}}/{{block}}"

   [[characters_delta]]
   match_key = "name_src"
   match_value = "{{character_name_src}}"
   [characters_delta.updates]
   name_ja = "{{character_name_ja}}"
   role = "{{character_role}}"
   first_person = "{{first_person}}"
   speech_style = "{{speech_style}}"
   # arcs がある場合:
   [[characters_delta.updates.arcs]]
   at_block = "{{chapter}}/{{block}}"
   speech_style = "{{new_speech_style}}"

   [[terms_delta]]
   match_key = "term_src"
   match_value = "{{term_src}}"
   [terms_delta.updates]
   term_ja = "{{term_ja}}"

   [[plot_delta]]
   block = "{{chapter}}/{{block}}"
   text = "{{plot_text}}"
   ```
5. `saga.py analyze-commit <work> --delta _work/{{chapter}}_{{block}}_delta.toml` を実行する

## ルール
- 既存の訳語（name_ja / term_ja）は変えない。変更したい場合は `proposed` に記す
- 新規キャラ・用語は `proposed` を使わず直接 `updates` に書く
- arcs は既存キャラの声の変化だけを書く（変化のない欄は省略）
- 差分ファイルは `_work/` に置き、他の分析と衝突しない

## 差分TOMLの正確な形式
`saga.py analyze-commit --help` と `docs/phase5-task-p1-4.md` を参照して正確に記述すること。特に:
- `block` は最上位キー
- `characters_delta` / `terms_delta` / `plot_delta` は配列
- 既存エントリの特定は `match_key` + `match_value` で行う
- 既存の保護キー（name_ja / term_ja / role / first_person / second_person / speech_style / vocab / catchphrase / ruby）を変更する差分は `proposed` に退避される

## 禁止事項
- 人物表・用語集の直接編集
- 既存の分析ファイル（analysis/*.toml）の直接編集
- 他ブロックの分析結果の改変
- 全文の一括読み込み