"""中国語の擬音が訳文にそのまま残っていないかの検査。**警告専用（停止しない）**。

理由: 擬音の訳し分けは文脈依存で、機械的に正解を決められない。ここで fatal に
すると弱いモデルは「止められないため」に語を不自然に置換し、本文を壊す実害が出る。
この検査は pass を常に True にし、警告（段落と形）だけを返す。

用法（qa_translation から）:
    from ono import check_paras, enabled
    if enabled(load_config()):
        checks["onomatopoeia"] = {"pass": True, "severity": "warn",
                                  "found": check_paras(items)}
"""
from __future__ import annotations

#: 中国語の擬音の代表形（2字以上）。訳文にこれが出たら「訳し残し」の疑いが強い。
#: 語彙の正本は拡張スキルの V7-onoma.md。ここは検出用の最小集合だけを持つ。
ONO_FORMS: tuple[str, ...] = (
    "噗嗤", "噗呲", "噗叽", "噗咿", "噗通", "噗噜", "噗噗",
    "咕啾", "咕叽", "咕噜", "咕嘟", "咕唧",
    "啪叽", "啪啪", "啪嗒", "啪嚓",
    "咔哒", "咔嚓", "咔咔",
    "簌簌", "汩汩", "滴答", "嗡嗡", "嘎吱", "嘎嘣", "哗啦", "淅沥",
    # 機械置換の残骸（カナ＋漢字・漢字の重複）。単字では拾わない形も明示する。
    "プ嗤", "プ叽", "滋滋",
)

#: 単字でも「日本語の本文には現れない」字。
#: 嗤（嗤う）・嘶（嘶く）・滋（滋養）は日本語の漢字なので単字では拾わない
#: （「プ嗤」「滋滋」のような残骸の形だけ ONO_FORMS に入れる）。
ONO_CHARS: tuple[str, ...] = ("噗", "咕", "啾", "啵", "啪", "咔")

#: 長い形から先に見て、短い形の重複報告を避ける
_ALL_FORMS: tuple[str, ...] = tuple(sorted(ONO_FORMS + ONO_CHARS, key=len, reverse=True))


def find_ono(text: str) -> list[str]:
    """text に残っている擬音形（初出順・重複なし）。

    「噗嗤」と「噗」のように入れ子になる形は、長い形が覆う範囲を短い形で
    二重に報告しない（別の位置に独立して出ていれば両方拾う）。
    """
    hits: list[tuple[int, str]] = []
    for f in _ALL_FORMS:
        start = 0
        while True:
            i = text.find(f, start)
            if i < 0:
                break
            hits.append((i, f))
            start = i + 1
    hits.sort(key=lambda t: (t[0], -len(t[1])))
    out: list[str] = []
    covered: list[tuple[int, int]] = []
    for i, form in hits:
        if any(i >= s and i + len(form) <= e for s, e in covered):
            continue
        covered.append((i, i + len(form)))
        out.append(form)
    return out


def check_paras(items: list[tuple[int, str]]) -> list[dict]:
    """段落リスト（[Pn, 本文]）から擬音の残りを拾う。空なら異常なし。"""
    found: list[dict] = []
    for n, text in items:
        forms = find_ono(text or "")
        if forms:
            found.append({"para": n, "forms": forms})
    return found


def enabled(config: dict | None) -> bool:
    """`[qa] ono_check`（既定 warn）。off のときだけ無効。"""
    qa = (config or {}).get("qa") or {}
    return str(qa.get("ono_check", "warn")).lower() != "off"


def describe(found: list[dict]) -> str:
    """警告1行用の要約（段落と形）。"""
    parts = []
    for f in found[:10]:
        parts.append(f"P{f['para']} {'/'.join(f['forms'])}")
    more = "" if len(found) <= 10 else f" ほか{len(found) - 10}段落"
    return "、".join(parts) + more
