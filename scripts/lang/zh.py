"""中国語ソース向けの機械側フック。LLM向けtipsは references/lang_zh.md。"""
from __future__ import annotations

LANG = "zh"

# ingest が UTF-8 の次に試す
EXTRA_ENCODINGS = ("gb18030", "big5")

# 章見出し断片。アンカーは _lib.compile_chapter_re が付ける
CHAPTER_ALTS = (
    r"第\s*[0-9０-９一二三四五六七八九十百千万零〇两]+\s*[章節节回卷部話话](?!课|時|时)"
    r"|序章|序[章節节]|楔子|终章|終章|尾声|結尾声|后记|後記|番外"
)

_SIMPLE_MARKERS = "们这说学对头车东长门见后发经过还动进会来开种样点为问时没让边务记读览术张应变两风"
_TRAD_MARKERS = "們這說學對頭車東長門見後發經過還動進會來開種樣點為問時沒讓邊務記讀覽術張應變兩風"

# JIS X 0208 には収録されているが、日本語で使わない・読めない漢字。
# find_non_jis_han の CP932 判定では拾えないため、ここで補う。
# 実作品で引っかかった字を種として育てる。運用規則は references/lang_zh.md。
# 固有の作品名・人名は残さない（例: 筱）。
NON_JIS_HAN = "筱"


def refine(text: str, result: dict) -> None:
    """guess_lang の結果に簡繁を足す。result をその場で更新。"""
    s = sum(1 for c in text if c in _SIMPLE_MARKERS)
    t = sum(1 for c in text if c in _TRAD_MARKERS)
    result["han_script"] = "simplified" if s >= t else "traditional"
