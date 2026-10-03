"""簡体字訳ドラフトの「日本語字形の残り」を一括修正する（ja→zh 方向）。

detect/ja2zh.JA2ZH の対応表（charmap/ja2zh.toml + 手書き表）で、日本語の
新字体・日本語専用字形を簡体字へ置換する。仮名は表で直せないため、検出して
報告する（文脈ごと訳し直す）。

**P1-1 分類**: 全エントリが glyph（字形変換）であることを確認済み。
意味置換（meaning）は存在しないため候補化の必要なし。

**QA(qa_translation.py --emit)を走らせる前に使う**のが目的。毎ブロック数件の
字形混入で QA が落ち、手作業で直す往復が発生するのを防ぐ。

使い方:
    python3 scripts/normalize/ja2zh.py <draft.md> [...]        # その場で修正
    python3 scripts/normalize/ja2zh.py --check <draft.md>      # 検出のみ
    python3 scripts/normalize/ja2zh.py --all <work_id>         # output/_work と output/chNNN を全部
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from detect.ja2zh import JA2ZH, find_kana, find_non_gbk_han  # noqa: E402


def fix_text(text: str) -> tuple[str, dict[str, int]]:
    applied: dict[str, int] = {}
    for ja, zh in JA2ZH.items():
        n = text.count(ja)
        if n:
            applied[ja] = n
            text = text.replace(ja, zh)
    return text, applied


def shape_glyph(text: str) -> tuple[str, list[dict]]:
    """commit から呼ばれる字形整形（glyph のみ）。zh2ja 側と同じ形状の API。

    ja→zh 方向に意味置換（meaning）は存在しないため、fix_text の全適用が
    そのまま glyph 整形に相当する。applied は {item, replacement, count} の列。
    """
    new_text, applied = fix_text(text)
    applied_list = [{"item": ja, "replacement": zh, "count": n}
                    for ja, n in applied.items() for zh in [JA2ZH[ja]]]
    return new_text, applied_list


def fix_file(path: pathlib.Path, check: bool) -> tuple[int, int, int]:
    """(未解決の字形数, 仮名数, 修正数) を返す。"""
    text = path.read_text(encoding="utf-8")
    new, applied = fix_text(text)
    remaining = [c for c, _ in find_non_gbk_han(new)]
    kana = [c for c, _ in find_kana(new)]
    print(f"== {path.name}: 修正{sum(applied.values())}件 / 残{len(remaining)}種 / 仮名{len(kana)}種")
    if applied:
        print("   適用: " + ", ".join(f"{k}->{v}" for k, v in applied.items()))
    if remaining:
        print(f"   **要手当て(字形): {''.join(remaining)}")
    if kana:
        print(f"   **要手当て(仮名): {''.join(kana)}")
    if not check and new != text:
        path.write_text(new, encoding="utf-8")
        print("   書き換え済み")
    return len(remaining), len(kana), sum(applied.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--check", action="store_true", help="検出のみ")
    ap.add_argument("--all", metavar="WORK_ID", help="<work_id>/output/_work と output/ch* を全部")
    args = ap.parse_args()

    targets: list[pathlib.Path] = []
    if args.all:
        base = pathlib.Path(args.all) / "output"
        files = list((base / "_work").glob("ch*_b*_zh_numbered.md"))
        for d in sorted(base.glob("ch*")):
            if d.is_dir():
                files += list(d.glob("b*_zh.md"))
        targets = sorted(files, key=lambda p: p.name)
    targets += [pathlib.Path(p) for p in args.paths]
    if not targets:
        ap.error("ファイルか --all <work_id> が必要")

    bad = kana = fixed = 0
    for path in targets:
        n_bad, n_kana, n_fix = fix_file(path, args.check)
        bad += n_bad
        kana += n_kana
        fixed += n_fix
    print(f"\n修正 {fixed} 件 / 字形の残り {bad} 種 / 仮名 {kana} 種")
    return 1 if (bad or kana) else 0


if __name__ == "__main__":
    raise SystemExit(main())
