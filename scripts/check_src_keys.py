"""analysis TOML の「原文キー」が日本語化されていないか検査する。

背景: 分析サブエージェントが term_src / name_src を日本語表記へ書き換えてしまい、
同じ語のエントリが二重化した（例: term_src="阳光环" と "光A環" が併存）。
原文キーは原文のままが正しい（訳語は term_ja / name_ja 側に書く）。

検出方法:
  1. 原文キーの重複（同じ term_ja に対して複数の term_src がある）
  2. 原文キーに、日本語新字体だけに現れる字が含まれる（原文が簡体字なら誤り）
     例: 光A環（陽・環は日本語）／阳光环（简体字）が正しい

使い方:
    python3 check_src_keys.py <work_dir> [...] [--fix]
    --fix は「重複エントリのうち、原文キーが日本語化されている側」を削除する。
    削除の判断がつかない場合は report のみ。
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import load_characters, load_terms, load_toml  # noqa: E402

# 日本語新字体にだけ現れる字（簡体字側は別字）。原文キーにこれが入っていたら怪しい。
JA_ONLY = set("陽環嵐聖輝戦姫説実発現見開間問東車馬鳥魚門楼涙鈴師缆専")  # 据・結は日本語でも使うため除外

# 日本語化の疑いが特に強い語（過去に実害が出たもの）
KNOWN_BAD = {"光A環": "阳光环", "日冕嵐": "日冕风暴"}


def entries_of(path: Path):
    d = load_toml(path)
    for key in ("terms", "characters"):
        if key in d:
            return key, d[key]
    return None, []


def main() -> int:
    ap = argparse.ArgumentParser(description="analysis TOML 原文キー検査")
    ap.add_argument("works", nargs="+", metavar="WORK_DIR",
                    help="作品ディレクトリ（_saga/ 内部またはその親）")
    ap.add_argument("--fix", action="store_true",
                    help="重複エントリのうち日本語化側を削除する（報告のみで実際の削除はしない）")
    args = ap.parse_args()

    total = 0
    for w in args.works:
        work = Path(w)
        if not work.is_dir():
            print(f"ディレクトリが見つかりません: {work}", file=sys.stderr)
            return 2
        for f in sorted(work.glob("analysis/*.toml")):
            key, items = entries_of(f)
            if not items:
                continue
            src_key = "term_src" if key == "terms" else "name_src"
            ja_key = "term_ja" if key == "terms" else "name_ja"

            # 1) 同じ訳語に対して複数の原文キー（重複エントリ）
            by_ja: dict[str, list[int]] = {}
            for i, x in enumerate(items):
                ja = x.get(ja_key)
                if ja:
                    by_ja.setdefault(ja, []).append(i)
            for ja, idxs in by_ja.items():
                if len(idxs) > 1:
                    rel = f.relative_to(work) if f.is_relative_to(work) else f
                    print(f"===== {rel}  重複: {ja} ({len(idxs)}件)")
                    for i in idxs:
                        print(f"   - {src_key} = \"{items[i].get(src_key)}\"")
                    total += len(idxs) - 1
                    if args.fix:
                        # 日本語化されている側（JA_ONLY を含む src）を削除
                        drop = [i for i in idxs if any(c in JA_ONLY for c in str(items[i].get(src_key, "")))]
                        if drop and len(drop) < len(idxs):
                            print(f"   -> 削除候補: {[items[i].get(src_key) for i in drop]}")
                            print("   ※ --fix はこのツールでは実行しません。手動で該当エントリを削除してください。")

            # 2) 既知の誤り
            for i, x in enumerate(items):
                s = str(x.get(src_key, ""))
                for bad, good in KNOWN_BAD.items():
                    if s == bad:
                        rel = f.relative_to(work) if f.is_relative_to(work) else f
                        print(f"===== {rel}  原文キーが日本語化: {src_key}=\"{bad}\" → \"{good}\" が正")
                        total += 1

            # 3) 原文キーに日本語新字体だけの字が混入していないか（一括置換事故の検出）
            for i, x in enumerate(items):
                s = str(x.get(src_key, ""))
                bad = [c for c in s if c in JA_ONLY]
                if bad:
                    rel = f.relative_to(work) if f.is_relative_to(work) else f
                    print(f"===== {rel}  原文キーに日本語字: {src_key}=\"{s}\"  字={bad}")
                    total += 1
    print(f"\n合計 {total} 件の疑い")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())