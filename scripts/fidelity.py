"""原文との忠実性検査（未翻訳コピー・漢語直置き候補）。

既存の機械ゲートは「形」（段落対応・字形・辞書残渣）を見るが、訳文が
**翻訳になっているか**は見ていない。ここでは原文と訳文の統計を比べ、
原文を写しただけの段落（カナが極端に少ない・原文の4-gramをそのまま含む）を
検出する。誤検知を避けるため、短い段落は判定しない。

使い方（commit / assemble から呼ぶ）:
    from fidelity import check_pair, check_block, cfg_from
    cfg = cfg_from(load_config())
    r = check_pair(src_text, dst_text, cfg)   # 1段落
    b = check_block(pairs, cfg)                # ブロック全体
"""
from __future__ import annotations

import re

# かな（ひらがな・カタカナ・長音）
KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u30fc]")
# 漢字（CJK統合漢字・拡張A）
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RUBY_RE = re.compile(r"\{([^{}|]*)\|[^{}|]*\}")

#: 既定の閾値。作品ごとに config の [qa] で上書きできる。
DEFAULTS = {
    "kana_min": 0.10,        # 段落のカナ比率の下限（原文20字以上の段落のみ）
    "ngram_max": 0.50,       # 原文4-gramの含有率の上限
    "min_src_chars": 20,     # 判定する原文段落の最小文字数
    "block_kana_min": 0.25,  # ブロック全体のカナ比率の下限
    "min_block_chars": 400,  # ブロック判定を行う最小文字数（小ブロックの誤検知を避ける）
    "min_dst_chars": 20,     # 段落判定を行う訳文の最小文字数
    "min_dst_ratio": 0.45,   # 訳文/原文の文字数比の下限（下回るなら判定しない）
    "ngram_n": 4,
}


def cfg_from(config: dict | None) -> dict:
    """config.toml の [qa] から閾値を読む（無い項目は既定値）。"""
    qa = (config or {}).get("qa") or {}
    cfg = dict(DEFAULTS)
    for key in ("kana_min", "ngram_max", "block_kana_min", "min_dst_ratio"):
        # 設定ファイルでは copy_ 接頭辞を付ける（copy_kana_min 等）。
        raw = qa.get(f"copy_{key}", qa.get(key))
        if raw is not None:
            try:
                cfg[key] = float(raw)
            except (TypeError, ValueError):
                pass
    for key in ("min_src_chars", "min_block_chars", "min_dst_chars", "ngram_n"):
        raw = qa.get(f"copy_{key}", qa.get(key))
        if raw is not None:
            try:
                cfg[key] = int(raw)
            except (TypeError, ValueError):
                pass
    return cfg


def strip_markup(text: str) -> str:
    """画像リンクと VFM ルビ記法を落として本文だけにする。"""
    t = _IMG_RE.sub("", text or "")
    t = _RUBY_RE.sub(lambda m: m.group(1), t)
    return t


def kana_ratio(text: str) -> float:
    """空白を除いた文字のうち、かなの割合。"""
    chars = [c for c in strip_markup(text) if not c.isspace()]
    if not chars:
        return 1.0
    return sum(1 for c in chars if KANA_RE.match(c)) / len(chars)


def _grams(text: str, n: int) -> set[str]:
    s = "".join(strip_markup(text).split())
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def containment(src: str, dst: str, n: int = 4) -> float:
    """訳文の n-gram のうち、原文にも存在する割合（0.0〜1.0）。"""
    a, b = _grams(src, n), _grams(dst, n)
    if not b:
        return 0.0
    return len(a & b) / len(b)


def check_pair(src: str, dst: str, cfg: dict | None = None) -> dict:
    """1段落の判定。

    戻り値: {"checked": bool, "kana": float, "contain": float,
             "reasons": [str], "flag": bool}
      reasons: "kana_low"（カナが極端に少ない）/ "copy"（原文の4-gramを多く含む）
    """
    c = cfg or DEFAULTS
    s = strip_markup(src)
    d = strip_markup(dst)
    out = {"checked": False, "kana": 0.0, "contain": 0.0, "reasons": [], "flag": False}
    if len("".join(s.split())) < c["min_src_chars"]:
        return out
    if not CJK_RE.search(s) or not d.strip():
        return out
    out["kana"] = kana_ratio(d)
    out["contain"] = containment(s, d, c["ngram_n"])
    # 極端に短い訳文はカナ比率も含有率も意味を持たない（内心の断片など）。
    # 原文と同程度の長さがある段落だけを「コピー疑い」の対象にする。
    src_n = len("".join(s.split()))
    dst_n = len("".join(d.split()))
    if dst_n < max(c["min_dst_chars"], c["min_dst_ratio"] * src_n):
        out["short"] = True
        return out
    out["checked"] = True
    if out["kana"] < c["kana_min"]:
        out["reasons"].append("kana_low")
    if out["contain"] > c["ngram_max"]:
        out["reasons"].append("copy")
    out["flag"] = bool(out["reasons"])
    return out


def check_block(pairs: list[tuple[str, str]], cfg: dict | None = None) -> dict:
    """ブロック全体の判定（段落単位の判定をすり抜けたコピーの受け皿）。"""
    c = cfg or DEFAULTS
    src_joined = "".join(strip_markup(s) for s, _d in pairs)
    dst_joined = "".join(strip_markup(d) for _s, d in pairs)
    n = len("".join(dst_joined.split()))
    if n < c["min_block_chars"] or len("".join(src_joined.split())) < c["min_block_chars"]:
        return {"checked": False, "kana": kana_ratio(dst_joined), "flag": False}
    ratio = kana_ratio(dst_joined)
    return {"checked": True, "kana": ratio, "flag": ratio < c["block_kana_min"]}


def describe(r: dict) -> str:
    """停止メッセージ用の短い説明。"""
    parts = []
    if "kana_low" in r.get("reasons", []):
        parts.append(f"カナ比率 {r['kana']:.2f}")
    if "copy" in r.get("reasons", []):
        parts.append(f"原文4-gramの含有率 {r['contain']:.2f}")
    return "・".join(parts) or "要確認"
