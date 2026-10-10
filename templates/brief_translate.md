# 翻訳タスク: {{work_id}} {{chapter}}/{{block}}

## あなたが読むファイル
1. コンテキスト: {{context_path}} — このファイルだけを読む。作品全体を読まない
2. 言語tips: references/lang_{{src}}2{{dst}}.md（存在すれば）

## 手順
1. `python3 scripts/saga.py prepare <work> --chapter {{chapter}} --block {{block}}` を実行する（親が既に実行済みなら省略可）
2. コンテキストを読む
3. ドラフトを `output/_work/{{chapter}}_{{block}}_{{dst}}_numbered.md` に書く。規則:
   - 各段落の頭に [Pn] マーカー（原文の段落番号と1対1）
   - 段落番号は連番・重複禁止。結合・分割したら振り直す
   - 訳語・声は人物表・用語集に従う。新訳語は {{work}}/analysis/ の TOML に書かず、sidecar に書く（並列時）
4. `python3 scripts/saga.py commit <work> --chapter {{chapter}} --block {{block}}` を実行する
5. fatal/confirm/残渣の指摘が出たら修正して再実行。confirm は内容確認済みなら `python3 scripts/align.py ack` で裁定記録を残す

## 共有ロックの衝突（exit 3）
`saga.py` の各コマンドは作品単位の共有ロックを使う。並列で動かすと衝突は必ず起きる。
- **exit 3**（`…（終了コード 3）` と表示される）はロック取得失敗。**10秒待って同じコマンドを再実行する（最大5回）**
- 5回失敗したら親エージェント／ユーザーに報告する。それ以外のエラーは内容を直してから再実行する（リトライしない）

## 禁止事項
- 人物表・用語集の直接編集（並列時。単独時は翻訳より先に編集してよい）
- 他ブロックのドラフト・確定済み訳文の編集
- 全文の一括読み込み