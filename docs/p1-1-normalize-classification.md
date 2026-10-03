# P1-1 normalize 分類表 — glyph / meaning 判定

## 分類ルール（親決定・固定）

| 分類 | 定義 | 自動適用 |
|------|------|----------|
| **glyph**（字形整形） | 同じ語を日本語の文字形に直しただけ。ソースの各字がターゲットの対応位置の字の簡体字・異体字に対応する。文字数は変わらない。 | 既定モードで自動適用 |
| **meaning**（意味置換） | 翻訳・言い換え・語の入れ替え。文字数が変わる、語が別の語になる、訳語を選んでいる。 | 候補のみ返す（`--apply-meaning` で適用可） |

判定の目安: ソースの各字がターゲットの対応位置の字の簡体字・異体字に対応する（文字形の変換だけ）なら glyph。文字数が変わる、語が別の語になる、訳語を選んでいるなら meaning。

opencc 経由の変換は常に glyph 扱い（同じ字の日本字形化）。

## MANUAL_COMPOUND 分類

| src | dst | kind | 判定根拠 |
|-----|-----|------|----------|
| 强行 | 強行 | glyph | 强→強 の字形変換のみ |
| 灭顶 | 滅頂 | glyph | 灭→滅 の字形変換のみ |
| 嗓音 | 声 | meaning | 翻訳（別の語に置換） |
| 陡然 | 急に | meaning | 字数変化＋別の語 |
| 骤然 | 急に | meaning | 字数変化＋別の語 |
| 磅礴 | 雄渾 | meaning | 翻訳的置換 |
| 汹涌 | 激しく | meaning | 翻訳的置換 |
| 噙着 | 浮かべ | meaning | 翻訳的置換 |
| 博弈 | 駆け引き | meaning | 翻訳的置換 |
| 尾骶骨 | 尾骨 | meaning | 翻訳的置換（字数変化） |
| 滚烫 | 煮え滾る | meaning | 翻訳的置換 |
| 滚热 | 煮えたぎる | meaning | 翻訳的置換 |
| 粗糙 | 粗い | meaning | 翻訳的置換 |
| 抽搐 | 身を捩る | meaning | 翻訳的置換 |
| 荒诞 | 荒唐 | meaning | 翻訳的置換 |

### 小計

glyph: 2 件 (强行, 灭顶)
meaning: 13 件

## MANUAL_CHAR 分類

| src | dst | kind | 判定根拠 |
|-----|-----|------|----------|
| 韌 | 靭 | glyph | 異体字→JIS字形。同じ字の字形差 |
| 虬 | 龍 | glyph | 古字→JIS正字 |
| 虯 | 龍 | glyph | 同上 |
| 沉 | 沈 | glyph | 異体字→JIS字形 |
| 烬 | 燼 | glyph | 簡体字→JIS字形 |
| 炽 | 熾 | glyph | 簡体字→JIS字形 |
| 喻 | 喩 | glyph | 異体字→JIS字形 |
| 嗓 | 声 | meaning | 嗓音→声 の構成要素。翻訳的 |
| 骤 | 急 | meaning | 骤然→急 の構成要素 |
| 陡 | 急 | meaning | 陡然→急 の構成要素 |
| 弈 | 駆 | meaning | 博弈→駆け引き の構成要素 |
| 撑 | 張 | meaning | 文脈依存の訳語（撑≠張） |
| 撐 | 張 | meaning | 同上 |
| 坝 | 堰 | meaning | 意味置換（壩 ではなく 堰 を選択） |
| 壩 | 堰 | meaning | 同上 |
| 汹 | 逆 | meaning | 汹涌→激しく の構成要素 |
| 洶 | 逆 | meaning | 同上 |
| 盏 | 基 | meaning | 量詞の置換（盏≠基） |
| 盞 | 基 | meaning | 同上 |
| 礴 | 渾 | meaning | 磅礴→雄渾 の構成要素 |
| 烫 | 熱 | meaning | 滚烫→煮え滾る の構成要素 |
| 绽 | 咲 | meaning | 文脈依存（绽放→咲く） |
| 刁 | えぐ | meaning | 翻訳的置換 |
| 滟 | 艶 | meaning | 翻訳的置換 |
| 啃 | 噛 | meaning | 翻訳的置換 |
| 朵 | 輪 | meaning | 量詞の置換 |
| 俏 | 茶目っ気 | meaning | 翻訳的置換 |
| 嗡 | 轟 | meaning | 擬音語の翻訳的置換 |
| 肏 | 犯 | meaning | 翻訳的置換 |
| 缕 | 一筋 | meaning | 翻訳的置換 |
| 滩 | 水溜まり | meaning | 翻訳的置換 |

### 小計

glyph: 7 件 (韌, 虬, 虯, 沉, 烬, 炽, 喻)
meaning: 24 件

## 全体集計

| 区分 | glyph | meaning | 計 |
|------|-------|---------|----|
| MANUAL_COMPOUND | 2 | 13 | 15 |
| MANUAL_CHAR | 7 | 24 | 31 |
| opencc s2t→t2jp | 動的（常に glyph） | - | - |
| **合計** | **9 + opencc** | **37** | **46 + opencc** |

## ja2zh.py の状況

全エントリが字形変換（glyph）のみ。意味置換（meaning）は存在しないため候補化の必要なし。docstring に対称確認済みのコメントを追加。

## meaning 分類の異議申し立て

meaning への異議はユーザー裁定で変更できる。変更時は以下を両方更新すること:

1. `scripts/normalize/zh2ja.py` の該当エントリの kind
2. `scripts/tests/test_normalize_candidate.py` の該当アサーション