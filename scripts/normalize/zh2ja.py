#!/usr/bin/env python3
"""訳文ドラフトの非JIS漢字(簡体字の書き残し)を一括修正する。

qa_translation.py の non_jis_han 検査と同じ基準(_lib.find_non_jis_han)で検出し、
1) 複合語の手当て表(MANUAL_COMPOUND)  -- glyph のみ自動適用
2) opencc s2t->t2jp による自動変換(結果がJIS収載の1文字のときだけ) -- 常に glyph
3) 単字の手当て表(MANUAL_CHAR)  -- glyph のみ自動適用
の順に適用する。opencc が使えない環境でも 1)+3) だけで動く。

**重要 (P1-1 改修)**: 意味置換 (kind="meaning") は候補化された。
自動適用（既定モード）は字形変換 (kind="glyph") のみ。
--apply-meaning で従来の全適用が可能。

**QAを走らせる前に使う**のが目的。毎ブロック数件の簡体字混入で QA が落ち、
手作業で直す往復が発生するのを防ぐ。

使い方:
    python3 scripts/normalize/zh2ja.py <draft.md> [...]           # glyph のみ適用
    python3 scripts/normalize/zh2ja.py --report <draft.md>       # 検出のみ（glyph/meaning 分類表示）
    python3 scripts/normalize/zh2ja.py --check <draft.md>        # --report と同じ
    python3 scripts/normalize/zh2ja.py --apply-meaning <draft.md># meaning も含めて全適用
使い方(work_id指定・_work と確定済みの両方を見る):
    python3 scripts/normalize/zh2ja.py --all <work_id>           # <work_id>/output/_work と output/ch001
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _lib import find_non_jis_han  # noqa: E402


# 複合語の手当て(先に当てる)。簡体字の熟語がそのまま残ったときの定型。
# 各エントリ: (src, dst, kind)
MANUAL_COMPOUND: list[tuple[str, str, str]] = [
    ("嗓音", "声", "meaning"),      # 翻訳: 嗓音→声
    ("强行", "強行", "glyph"),      # 强→強 の字形
    ("陡然", "急に", "meaning"),    # 字数変化、語の置換
    ("骤然", "急に", "meaning"),    # 字数変化、語の置換
    ("磅礴", "雄渾", "meaning"),    # 翻訳的置換
    ("汹涌", "激しく", "meaning"),  # 翻訳的置換
    ("噙着", "浮かべ", "meaning"),  # 翻訳的置換
    ("博弈", "駆け引き", "meaning"),# 翻訳的置換
    ("尾骶骨", "尾骨", "meaning"),  # 翻訳的置換
    ("滚烫", "煮え滾る", "meaning"),# 翻訳的置換
    ("滚热", "煮えたぎる", "meaning"),# 翻訳的置換
    ("粗糙", "粗い", "meaning"),    # 翻訳的置換
    ("抽搐", "身を捩る", "meaning"),# 翻訳的置換
    ("灭顶", "滅頂", "glyph"),      # 灭→滅 の字形
    ("荒诞", "荒唐", "meaning"),    # 翻訳的置換
]

# 単字の手当て(opencc で解決できない字の受け皿)
# 各エントリ: (src, dst, kind)
MANUAL_CHAR: list[tuple[str, str, str]] = [
    ("嗓", "声", "meaning"),       # 嗓音→声 の要素
    ("骤", "急", "meaning"),       # 骤然→急 の要素
    ("陡", "急", "meaning"),       # 陡然→急 の要素
    ("弈", "駆", "meaning"),       # 博弈→駆け引き の要素
    ("撑", "張", "meaning"),       # 文脈依存の訳語
    ("撐", "張", "meaning"),       # 同上
    ("韌", "靭", "glyph"),         # 異体字→JIS字形
    ("坝", "堰", "meaning"),       # 意味置換（壩 ではなく 堰 を選択）
    ("壩", "堰", "meaning"),       # 同上
    ("汹", "逆", "meaning"),       # 汹涌→激しく の要素
    ("洶", "逆", "meaning"),       # 同上
    ("盏", "基", "meaning"),       # 量詞の置換
    ("盞", "基", "meaning"),       # 同上
    ("礴", "渾", "meaning"),       # 磅礴→雄渾 の要素
    ("虬", "龍", "glyph"),         # 古字→JIS字形
    ("虯", "龍", "glyph"),         # 同上
    ("沉", "沈", "glyph"),         # 異体字→JIS字形
    ("烫", "熱", "meaning"),       # 滚烫→煮え滾る の要素
    ("烬", "燼", "glyph"),         # 簡体字→JIS字形
    ("炽", "熾", "glyph"),         # 簡体字→JIS字形
    ("绽", "咲", "meaning"),       # 文脈依存の訳語（绽放→咲く）
    ("刁", "えぐ", "meaning"),     # 翻訳的置換
    ("滟", "艶", "meaning"),       # 翻訳的置換（滟→艶）
    ("啃", "噛", "meaning"),       # 翻訳的置換
    ("喻", "喩", "glyph"),         # 異体字→JIS字形
    ("朵", "輪", "meaning"),       # 量詞の置換
    ("俏", "茶目っ気", "meaning"), # 翻訳的置換
    ("嗡", "轟", "meaning"),       # 翻訳的置換（擬音語）
    ("肏", "犯", "meaning"),       # 翻訳的置換
    ("缕", "一筋", "meaning"),     # 翻訳的置換
    ("滩", "水溜まり", "meaning"), # 翻訳的置換
    ("张", "張", "glyph"),         # 簡体字→JIS字形（一般的な姓・動詞）
]


def is_jis(ch: str) -> bool:
    try:
        ch.encode("cp932")
    except UnicodeEncodeError:
        return False
    return True


_OPENCC_NOTICE_SHOWN = False


def opencc_available() -> bool:
    """字形変換に使う opencc のカスタム設定が揃っているか。

    パッケージが入っていても `s2t_custom` / `t2jp_custom` の設定が無い環境が
    あるため、import だけでなく設定のロードまで確認する。テストのスキップ判定と
    縮退通知の両方でこの関数を使う。
    """
    try:
        from opencc import OpenCC

        OpenCC("s2t_custom")
        OpenCC("t2jp_custom")
        return True
    except Exception:
        return False


def _load_opencc():
    global _OPENCC_NOTICE_SHOWN
    try:
        from opencc import OpenCC

        return OpenCC("s2t_custom"), OpenCC("t2jp_custom")
    except Exception as exc:  # pragma: no cover - 環境依存
        if not _OPENCC_NOTICE_SHOWN:
            _OPENCC_NOTICE_SHOWN = True
            print(f"[注意] opencc の字形変換を使えないため手当て表のみで処理します: {exc}")
        return None, None


_BUILT_GLYPH_MAP: dict[str, str] | None = None
_BUILT_MEANING_MAP: dict[str, str] | None = None


def _build_char_maps() -> tuple[dict[str, str], dict[str, str]]:
    """MANUAL_CHAR から glyph 用と meaning 用の単字マップを構築（キャッシュ付き）。

    Returns: (glyph_map, meaning_map) — 各 dict[str, str]。
    """
    global _BUILT_GLYPH_MAP, _BUILT_MEANING_MAP
    if _BUILT_GLYPH_MAP is not None and _BUILT_MEANING_MAP is not None:
        return _BUILT_GLYPH_MAP, _BUILT_MEANING_MAP
    glyph: dict[str, str] = {}
    meaning: dict[str, str] = {}
    for src, dst, kind in MANUAL_CHAR:
        if kind == "glyph":
            glyph[src] = dst
        else:
            meaning[src] = dst
    _BUILT_GLYPH_MAP = glyph
    _BUILT_MEANING_MAP = meaning
    return glyph, meaning


def _get_glyph_compounds() -> list[tuple[str, str]]:
    """MANUAL_COMPOUND から kind='glyph' のエントリのみ抽出。"""
    return [(s, d) for s, d, k in MANUAL_COMPOUND if k == "glyph"]


def _get_meaning_compounds() -> list[tuple[str, str]]:
    """MANUAL_COMPOUND から kind='meaning' のエントリのみ抽出。"""
    return [(s, d) for s, d, k in MANUAL_COMPOUND if k == "meaning"]


# ---------------------------------------------------------------------------
# 後方互換フラグ（SAGA_NORMALIZE_LEGACY=1 で従来の全適用動作）
# ---------------------------------------------------------------------------

def _is_legacy() -> bool:
    import os
    return os.environ.get("SAGA_NORMALIZE_LEGACY", "").lower().strip() in ("1", "true", "yes")


_LEGACY = _is_legacy()


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def _pick_sentinels(text: str) -> tuple[str, str]:
    """画像リンクの退避に使う開き・閉じの2文字を選ぶ（#25）。

    どちらも PUA (U+E000–U+F8FF) から選び、対象テキストに含まれない
    ことを実行時に確認する。本文に同文字があると、退避したリンクと
    本文を取り違えるため。候補が無ければ推測で使わず例外で止める。
    """
    pool = [chr(c) for c in range(0xE000, 0xF8FF + 1) if chr(c) not in text]
    if len(pool) < 2:
        raise ValueError("画像退避の sentinel に使える PUA 文字が本文中に存在しません")
    return pool[0], pool[1]


def shape_glyph(text: str) -> tuple[str, list[dict]]:
    """glyph 分類のみ適用し、(変換後テキスト, 適用履歴) を返す。

    適用履歴の各要素: {"item": str, "replacement": str, "count": int}

    画像リンク ![alt](path) のパスは実ファイル名(=原語表記)と一致させる必要が
    あるため、整形の対象から退避し、変換後に復元する。
    """
    s2t, t2jp = _load_opencc()
    out = text
    applied: list[dict] = []

    # 0) 画像リンクを退避（パス内の簡体字を字形整形から守る）
    img_pat = re.compile(r"!\[[^\]]*\]\([^)]*\)")
    imgs = img_pat.findall(out)
    if imgs:
        # 開きと閉じに別の文字を使い、本文に含まれないことを確認する（#25）。
        SENT_OPEN, SENT_CLOSE = _pick_sentinels(out)
        out = img_pat.sub(lambda m: SENT_OPEN + str(imgs.index(m.group(0))) + SENT_CLOSE, out)

    # 1) glyph 複合語
    for pat, rep in _get_glyph_compounds():
        if pat in out:
            n = out.count(pat)
            applied.append({"item": pat, "replacement": rep, "count": n})
            out = out.replace(pat, rep)

    # 2) opencc による字形変換（常に glyph 扱い）
    remaining = [c for c, _ in find_non_jis_han(out)]
    mapping, _ = build_map(remaining, s2t, t2jp)
    for ch, rep in mapping.items():
        if ch in out:
            n = out.count(ch)
            applied.append({"item": ch, "replacement": rep, "count": n})
            out = out.replace(ch, rep)

    # 3) glyph 単字 — opencc で解消できなかった非JIS字を手当て表で
    glyph_map, _ = _build_char_maps()
    remaining2 = [c for c, _ in find_non_jis_han(out)]
    for ch in remaining2:
        if ch in glyph_map:
            n = out.count(ch)
            applied.append({"item": ch, "replacement": glyph_map[ch], "count": n})
            out = out.replace(ch, glyph_map[ch])

    # 4) 画像リンクを復元
    if imgs:
        for i, im in enumerate(imgs):
            out = out.replace(SENT_OPEN + str(i) + SENT_CLOSE, im)

    return out, applied


def meaning_candidates(text: str) -> list[dict]:
    """meaning 分類を検出（適用しない）。

    戻り値: [{"value": str, "replacement": str, "context": str, "line": int}]
    value: 検出された文字列
    replacement: 置換先
    context: 出現位置の前後 ~10 文字（改行は空白化）
    line: 行番号（1-indexed）
    """
    candidates: list[dict] = []

    # 複合語 meaning
    for pat, rep in _get_meaning_compounds():
        idx = text.find(pat)
        if idx >= 0:
            start = max(0, idx - 10)
            end = min(len(text), idx + len(pat) + 10)
            ctx = text[start:end].replace("\n", " ")
            candidates.append({
                "value": pat,
                "replacement": rep,
                "context": ctx,
                "line": text[:idx].count("\n") + 1,
            })

    # 単字 meaning（複合語で未カバーのものだけ検出）
    _, meaning_map = _build_char_maps()
    seen_vals = {c["value"] for c in candidates}
    for ch, rep in meaning_map.items():
        if ch not in text:
            continue
        # 既にこの字を含む複合語が検出済みならスキップ
        already = any(ch in v for v in seen_vals)
        if already:
            continue
        idx = text.find(ch)
        start = max(0, idx - 10)
        end = min(len(text), idx + 1 + 10)
        ctx = text[start:end].replace("\n", " ")
        candidates.append({
            "value": ch,
            "replacement": rep,
            "context": ctx,
            "line": text[:idx].count("\n") + 1,
        })

    return candidates


def build_map(chars: list[str], s2t, t2jp) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    glyph_char_map, meaning_char_map = _build_char_maps()
    for ch in chars:
        if _LEGACY:
            # 後方互換: glyph + meaning 両方の手当て表を見る
            if ch in glyph_char_map:
                mapping[ch] = glyph_char_map[ch]
                continue
            if ch in meaning_char_map:
                mapping[ch] = meaning_char_map[ch]
                continue
        else:
            # 新動作: glyph のみ自動適用
            if ch in glyph_char_map:
                mapping[ch] = glyph_char_map[ch]
                continue
        done = False
        if s2t and t2jp:
            conv = t2jp.convert(s2t.convert(ch))
            if len(conv) == 1 and conv != ch and is_jis(conv):
                mapping[ch] = conv
                done = True
        if not done:
            unresolved.append(ch)
    return mapping, unresolved


def fix_text(text: str, s2t, t2jp) -> tuple[str, dict[str, int], list[str]]:
    """後方互換用——既定では glyph のみ、SAGA_NORMALIZE_LEGACY=1 で全適用。

    新しいコードは shape_glyph() / meaning_candidates() を使ってください。
    """
    found = dict(find_non_jis_han(text))
    if not found:
        return text, {}, []

    out = text
    applied: dict[str, int] = {}
    for pat, rep, kind in MANUAL_COMPOUND:
        if _LEGACY or kind == "glyph":
            if pat in out:
                applied[pat] = out.count(pat)
                out = out.replace(pat, rep)

    remaining = [c for c, _ in find_non_jis_han(out)]
    mapping, _ = build_map(remaining, s2t, t2jp)
    for ch, rep in mapping.items():
        if ch in out:
            applied[ch] = out.count(ch)
            out = out.replace(ch, rep)

    unresolved = [c for c, _ in find_non_jis_han(out)]
    return out, applied, unresolved


def fix_file(path: pathlib.Path, s2t, t2jp, check: bool, apply_meaning: bool = False) -> tuple[int, int]:
    """(未解決数, 修正数) を返す。"""
    text = path.read_text(encoding="utf-8")
    before = dict(find_non_jis_han(text))
    if not before:
        return 0, 0

    if apply_meaning:
        # 全適用モード（従来動作に近い但し kind を参照しない = 全部適用）
        new = text
        applied: dict[str, int] = {}
        for pat, rep, _ in MANUAL_COMPOUND:
            if pat in new:
                applied[pat] = new.count(pat)
                new = new.replace(pat, rep)
        remaining = [c for c, _ in find_non_jis_han(new)]
        mapping, unresolved = build_map(remaining, s2t, t2jp)
        for ch, rep in mapping.items():
            if ch in new:
                applied[ch] = new.count(ch)
                new = new.replace(ch, rep)
        # meaning 単字も全て
        _, meaning_map = _build_char_maps()
        remaining2 = [c for c, _ in find_non_jis_han(new)]
        for ch in remaining2:
            if ch in meaning_map:
                if ch in new:
                    applied[ch] = new.count(ch)
                    new = new.replace(ch, meaning_map[ch])
        unresolved = [c for c, _ in find_non_jis_han(new)]
    elif _LEGACY:
        new, applied, unresolved = fix_text(text, s2t, t2jp)
    else:
        # 既定: glyph のみ適用
        new, applied_list = shape_glyph(text)
        applied = {a["item"]: a["count"] for a in applied_list}
        unresolved = [c for c, _ in find_non_jis_han(new)]

    print(f"== {path.name}: {len(before)}種 -> 残{len(unresolved)}種")
    print(f"   検出: {''.join(before.keys())}")
    if applied:
        print("   適用: " + ", ".join(f"{k}->{v}" for k, v in applied.items()))
    if unresolved:
        print(f"   **未解決: {''.join(unresolved)}")
    if not check and new != text:
        path.write_text(new, encoding="utf-8")
        print("   書き換え済み")
    return len(unresolved), sum(applied.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--check", action="store_true", help="検出のみ（--report と同じ）")
    ap.add_argument("--report", action="store_true", help="検出のみ。glyph/meaning 分類表示")
    ap.add_argument("--apply-meaning", action="store_true", help="meaning も含めて全適用（手動運用向け）")
    ap.add_argument("--all", metavar="WORK_ID", help="<work_id>/output/_work と output/ch001 を全部")
    args = ap.parse_args()

    targets: list[pathlib.Path] = []
    if args.all:
        base = pathlib.Path(args.all) / "output"
        files = list((base / "_work").glob("ch001_b*_ja_numbered.md")) + list((base / "ch001").glob("b*_ja.md"))
        targets = sorted(files, key=lambda p: int(re.search(r"_?b(\d+)", p.name).group(1)))
    targets += [pathlib.Path(p) for p in args.paths]
    if not targets:
        ap.error("ファイルか --all <work_id> が必要")

    s2t, t2jp = _load_opencc()
    bad = fixed = 0

    do_report = args.report or args.check

    for path in targets:
            if do_report:
                text = path.read_text(encoding="utf-8")
                g_applied_list = shape_glyph(text)[1]
                meaning = meaning_candidates(text)
                remaining = [c for c, _ in find_non_jis_han(shape_glyph(text)[0])]
                print(f"== {path.name}: glyph_applied={len(g_applied_list)} "
                      f"meaning={len(meaning)} unresolved={len(remaining)}")
                glyph_items = ", ".join(f"{a['item']}->{a['replacement']}" for a in g_applied_list) if g_applied_list else "(なし)"
                print("   glyph: " + glyph_items)
                if meaning:
                    print("   meaning: " + ", ".join(f"{m['value']}->{m['replacement']}" for m in meaning))
                if remaining:
                    print(f"   未解決: {''.join(remaining)}")
                bad += len(remaining)
            else:
                n_bad, n_fix = fix_file(path, s2t, t2jp, check=False, apply_meaning=args.apply_meaning)
                bad += n_bad
                fixed += n_fix

    if not do_report:
        print(f"\n修正 {fixed} 件 / 未解決 {bad} 件")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
