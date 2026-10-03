"""指定ブロックの原文段落を表示する。

chapters/ch<N>.paras.toml から指定ブロック（chapters_meta の paras 範囲）を
読んで `n|text` 形式で出力する。

使い方:
  python3 blk.py <work_dir> <bN>
  例: python3 blk.py ./_saga b5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import load_chapters_meta, load_paras  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="ブロックの原文段落を表示")
    ap.add_argument("work", type=Path, help="作品ディレクトリ（_saga/ 内部または親）")
    ap.add_argument("block", help="ブロックID（例: b5）")
    args = ap.parse_args()

    work = args.work
    if (work / "_saga").is_dir():
        work = work / "_saga"

    meta = load_chapters_meta(work)
    ch = meta["chapters"][0]
    try:
        blkdef = next(b for b in ch["blocks"] if b["id"] == args.block)
    except StopIteration:
        print(f"ブロック {args.block} が見つかりません", file=sys.stderr)
        return 1

    p0, p1 = blkdef["paras"]
    paras = load_paras(work, ch["id"])
    for p in paras:
        if p0 <= p["n"] <= p1:
            print(f"{p['n']}|{p['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())