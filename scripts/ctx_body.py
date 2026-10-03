"""build_context.py が生成した translation_input ファイルから必要な部分だけを表示する。

翻訳エージェントが 1 ブロックごとに 60KB 全文を読み直すのを避けるための補助。

  --section body  : 「## 対象本文」以降だけ（既定）
  --section head  : 対象本文より前の全ヘッダ（登場人物表・用語集・あらすじ等）
  --section arcs  : 「## 声の変化」〜「## 用語集」の前まで（声の変化だけ）
  --chars N       : 表示文字数の上限（超過分は "...(省略)" と表示）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

CUTS = {
    "body": ("## 対象本文", None),
    "head": (None, "## 対象本文"),
    "arcs": ("## 声の変化", "## 用語集"),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="コンテキストファイルの一部を表示")
    ap.add_argument("context_file", type=Path, help="build_context.py の出力ファイル")
    ap.add_argument("--section", choices=sorted(CUTS), default="body",
                    help="表示するセクション")
    ap.add_argument("--chars", type=int, default=0,
                    help="表示文字数の上限（0=無制限）")
    args = ap.parse_args()

    p = args.context_file
    if not p.is_file():
        print(f"ファイルが見つかりません: {p}", file=sys.stderr)
        return 1
    text = p.read_text(encoding="utf-8")
    start_key, end_key = CUTS[args.section]
    i = text.find(start_key) if start_key else 0
    if i < 0:
        print(f"(section {args.section} が見つからない)", file=sys.stderr)
        return 1
    j = text.find(end_key, i) if end_key else -1
    seg = text[i:j] if j > 0 else text[i:]
    seg = seg.strip()
    if args.chars and len(seg) > args.chars:
        seg = seg[: args.chars] + "\n...(省略)"
    print(seg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())