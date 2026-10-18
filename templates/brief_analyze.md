# 分析タスク: {{work_id}} {{chapter}}/{{block}}

## あなたが読むファイル
1. `python3 scripts/saga.py prepare <work> --chapter {{chapter}} --block {{block}} --mode analysis` で生成されたコンテキスト — このファイルだけを読む。作品全体を読まない
2. 言語tips: `references/lang_{{src}}2{{dst}}.md`（存在すれば）

## 手順
1. `python3 scripts/saga.py prepare <work> --chapter {{chapter}} --block {{block}} --mode analysis` を実行する（親が既に実行済みなら省略可）
2. コンテキストを読む
3. 既存の人物表・用語集・プロットを分析し、新しい登場人物・用語・プロットエントリを抽出する
4. `_work/{{chapter}}_{{block}}_delta.toml` に差分を書く。形式:
   ```toml
   block = "{{chapter}}/{{block}}"

   # 新規の人物は全欄を書く。既存の人物は「変える欄だけ」を書き、
   # 値が変わらない欄は書かない（同じ値の再送は無視される）
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
5. 差分を書いたら、書式を先に検査する（未知キー・必須キー欠けを analyze-commit の前に検出）:
   `python3 scripts/check_src_keys.py <work> --delta _work/{{chapter}}_{{block}}_delta.toml`
   → `[delta-check] OK` を確認してから次へ
6. `python3 scripts/saga.py analyze-commit <work> --delta <work>/_work/{{chapter}}_{{block}}_delta.toml` を実行する（`--delta` は絶対パスで渡す。work 相対は analyze-commit では解決されないため `<work>/` から書く）

## ルール
- 既存の訳語（name_ja / term_ja）は変えない。変更したい場合は `proposed` に記す
- 既存エントリには、変える欄だけを書く（同じ値の再送は無視され、差分が空になる）
- 新規キャラ・用語は `proposed` を使わず直接 `updates` に書く
- arcs は既存キャラの声の変化だけを書く（変化のない欄は省略）
- 差分ファイルは `_work/` に置き、他の分析と衝突しない
- 異表記（variant）は「**同じ指示対象が、原文中で実際に別表記されている**」場合のみ登録する（例: 王五⇔小王、张三⇔三儿）。**普通の語・一般名詞は字形が近いだけでは登録しない**（月光⇔月咏・声音のような語を variant にすると用語集・人物表が汚れる）。近い表記の別人物だった場合は登録せず、親に報告する（棄却は親が `dismiss_variant` で行う）

## 共有ロックの衝突（exit 3）
`saga.py` の各コマンドは作品単位の共有ロックを使う。並列で動かすと衝突は必ず起きる。
- **exit 3**（`…（終了コード 3）` と表示される）はロック取得失敗。**10秒待って同じコマンドを再実行する（最大5回）**
- 5回失敗したら親エージェント／ユーザーに報告する。それ以外のエラーは内容を直してから再実行する（リトライしない）

## 差分TOMLの正確な形式
`python3 scripts/saga.py analyze-commit --help` と `references/data-schema.md`（「分析の記録」節）を参照して正確に記述すること。特に:
- `block` は最上位キー
- `characters_delta` / `terms_delta` / `plot_delta` は配列
- 既存エントリの特定は `match_key` + `match_value` で行う
- 既存の保護キー（name_ja / term_ja / role / first_person / second_person / speech_style / vocab / catchphrase / ruby）を変更する差分は `proposed` に退避される

## 禁止事項
- 人物表・用語集の直接編集
- 既存の分析ファイル（analysis/*.toml）の直接編集
- 他ブロックの分析結果の改変
- 全文の一括読み込み