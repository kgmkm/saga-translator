"""日本語ソース向けの機械側フック。LLM 向け tips は references/lang_ja2zh.md。

翻訳方向の判定は manifest の lang/dst を見る（_lib.direction）。
"""
from __future__ import annotations

LANG = "ja"

# ingest が UTF-8 の次に試す（日本語ダウンロードTXT想定）
EXTRA_ENCODINGS = ("cp932", "shift_jis")

# 章見出し。アンカーは _lib.compile_chapter_re が付ける。
# ・pixiv テキストの [chapter:…]（サブ見出し [chapter:一] を含む）を章として扱う
# ・Markdown/素テキストの和文章題（第一章 …, プロローグ 等）にも対応
# トップレベルの章だけを章にする。pixiv のサブ見出し [chapter:一] は章にしない
# （章内の見出し段落として訳される）。
CHAPTER_ALTS = (
    r"\[chapter:(?:プロローグ|エピローグ)[^\]]*\]"
    r"|\[chapter:第[^\]]*章[^\]]*\]"
    r"|#+\s*第\s*[0-9０-９一二三四五六七八九十百千]+\s*章.*"
    r"|第\s*[0-9０-９一二三四五六七八九十百千]+\s*章.*"
    r"|序章.*|終章.*|間章.*|幕間.*"
)

# ── 会話頭判定の例外（align.py / align_check.py が参照。定義の正本はここ）──
# 構造タグは pixiv 記法で ja/zh 共通（ルビ・挿絵タグ・章見出し・改ページ）。
# ja 原文の会話頭は「…」だが、訳文側（zh）のドラフト検査でも同じ一覧を使う。
DIALOGUE_HEAD_EXCEPTIONS = {
    "structure_prefixes": ("[[rb:", "[pixivimage:", "[chapter:", "[newpage]", "[uploadedimage:"),
    # 短い引用（多くは擬音）＋地の文の段落を対話と誤判定しない条件:
    # 引用が ono_head_max 字以内に ono_head_closers で閉じ、
    # 閉じた後の本文が引用の ono_head_rest_factor 倍以上あること。
    "ono_head_max": 8,
    "ono_head_closers": "”」』\"",
    "ono_head_rest_factor": 2,
    # 擬音頭判定で「引用の開き」とみなす文字（align_check の粗い判定が使う）。
    "ono_head_quotes": ("“", "[", "［", "「", "『", "（", "(", "‘", '"', "'"),
}


def refine(text: str, result: dict) -> None:
    """日本語ソースは han_script を持たない。フック互換のため何もしない。"""
    return None
