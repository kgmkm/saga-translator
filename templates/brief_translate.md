# 翻訳タスク: {{work_id}} {{chapter}}/{{block}}

## あなたが読むファイル
1. コンテキスト: {{context_path}} — このファイルだけを読む。作品全体を読まない
2. 言語tips: references/lang_{{src}}2{{dst}}.md（存在すれば）

## 手順
1. `saga.py prepare <work> --chapter {{chapter}} --block {{block}}` を実行する（親が既に実行済みなら省略可）
2. コンテキストを読む
3. ドラフトを `output/_work/{{chapter}}_{{block}}_{{dst}}_numbered.md` に書く。規則:
   - 各段落の頭に [Pn] マーカー（原文の段落番号と1対1）
   - 段落番号は連番・重複禁止。結合・分割したら振り直す
   - 訳語・声は人物表・用語集に従う。新訳語は {{work}}/analysis/ の TOML に書かず、sidecar に書く（並列時）
4. `saga.py commit <work> --chapter {{chapter}} --block {{block}}` を実行する
5. fatal/confirm/残渣の指摘が出たら修正して再実行。confirm は内容確認済みなら `align.py ack` で裁定記録を残す

## 禁止事項
- 人物表・用語集の直接編集（並列時。単独時は翻訳より先に編集してよい）
- 他ブロックのドラフト・確定済み訳文の編集
- 全文の一括読み込み