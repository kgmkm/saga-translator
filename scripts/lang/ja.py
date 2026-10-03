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


def refine(text: str, result: dict) -> None:
    """日本語ソースは han_script を持たない。フック互換のため何もしない。"""
    return None
