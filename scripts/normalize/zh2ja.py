#!/usr/bin/env python3
"""訳文ドラフトの非JIS漢字(簡体字の書き残し)を一括修正する。

qa_translation.py の non_jis_han 検査と同じ基準(_lib.find_non_jis_han)で検出し、
1) 複合語の手当て表(MANUAL_COMPOUND)
2) opencc s2t→t2jp による自動変換(結果がJIS収載の1文字のときだけ)
3) 単字の手当て表(MANUAL_CHAR)
の順に適用する。opencc が使えない環境でも 1)+3) だけで動く。

**QAを走らせる前に使う**のが目的。毎ブロック数件の簡体字混入で QA が落ち、
手作業で直す往復が発生するのを防ぐ。

使い方:
    python3 scripts/normalize/zh2ja.py <draft.md> [...]        # その場で修正
    python3 scripts/normalize/zh2ja.py --check <draft.md>      # 検出のみ
使い方(work_id指定・_work と確定済みの両方を見る):
    python3 scripts/normalize/zh2ja.py --all <work_id>         # <work_id>/output/_work と output/ch001
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _lib import find_non_jis_han  # noqa: E402


# 複合語の手当て(先に当てる)。簡体字の熟語がそのまま残ったときの定型。
MANUAL_COMPOUND = [
    ("嗓音", "声"),
    ("强行", "強行"),
    ("陡然", "急に"),
    ("骤然", "急に"),
    ("磅礴", "雄渾"),
    ("汹涌", "激しく"),
    ("噙着", "浮かべ"),
    ("博弈", "駆け引き"),
    ("尾骶骨", "尾骨"),
    ("滚烫", "煮え滾る"),
    ("滚热", "煮えたぎる"),
    ("粗糙", "粗い"),
    ("抽搐", "身を捩る"),
    ("灭顶", "滅頂"),
    ("荒诞", "荒唐"),
]

# 単字の手当て(opencc で解決できない字の受け皿)
MANUAL_CHAR = {
    "嗓": "声",
    "骤": "急",
    "陡": "急",
    "弈": "駆",
    "撑": "張",
    "撐": "張",
    "韌": "靭",
    "坝": "堰",
    "壩": "堰",
    "汹": "逆",
    "洶": "逆",
    "盏": "基",
    "盞": "基",
    "礴": "渾",
    "虬": "龍",
    "虯": "龍",
    "沉": "沈",
    "烫": "熱",
    "烬": "燼",
    "炽": "熾",
    "绽": "咲",
    "刁": "えぐ",
    "滟": "艶",
    "啃": "噛",
    "喻": "喩",
    "朵": "輪",
    "俏": "茶目っ気",
    "嗡": "轟",
    "肏": "犯",
    "缕": "一筋",
    "滩": "水溜まり",
}


def is_jis(ch: str) -> bool:
    try:
        ch.encode("cp932")
    except UnicodeEncodeError:
        return False
    return True


def _load_opencc():
    try:
        from opencc import OpenCC

        return OpenCC("s2t_custom"), OpenCC("t2jp_custom")
    except Exception as exc:  # pragma: no cover - 環境依存
        print(f"[注意] opencc を使えないため手当て表のみで処理します: {exc}")
        return None, None


def build_map(chars: list[str], s2t, t2jp) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    for ch in chars:
        if ch in MANUAL_CHAR:
            mapping[ch] = MANUAL_CHAR[ch]
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
    found = dict(find_non_jis_han(text))
    if not found:
        return text, {}, []

    out = text
    applied: dict[str, int] = {}
    for pat, rep in MANUAL_COMPOUND:
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


def fix_file(path: pathlib.Path, s2t, t2jp, check: bool) -> tuple[int, int]:
    """(未解決数, 修正数) を返す。"""
    text = path.read_text(encoding="utf-8")
    before = dict(find_non_jis_han(text))
    if not before:
        return 0, 0
    new, applied, unresolved = fix_text(text, s2t, t2jp)
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
    ap.add_argument("--check", action="store_true", help="検出のみ")
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
    for path in targets:
        n_bad, n_fix = fix_file(path, s2t, t2jp, args.check)
        bad += n_bad
        fixed += n_fix
    print(f"\n修正 {fixed} 件 / 未解決 {bad} 件")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
