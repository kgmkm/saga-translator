"""単一改行で区切られた TXT を、split_paras が段落として認識できる形に正規化する。

usage: python3 normalize_txt.py <input.txt> [-o output.txt]

単一改行のみの TXT を、全行間に空行を入れた `<元の名前>_norm.txt` として出力する。
空行が既に存在する（段落と空白行が区別されている）場合は何もしない。
入力ファイルは書き換えない。ingest 前に実行する。
"""
from __future__ import annotations

import argparse
from pathlib import Path


def normalize(src: Path, dst: Path | None) -> dict:
    raw = src.read_text(encoding="utf-8-sig")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # 末尾改行由来の空文字は段落境界とみなさない
    nonblank = [l for l in lines if l.strip()]
    already = any(not l.strip() for l in lines)
    if already:
        return {"skipped": True, "reason": "空行あり。既に段落分け済み"}
    out_lines = []
    for i, l in enumerate(lines):
        out_lines.append(l)
        if l.strip() and i < len(lines) - 1:
            out_lines.append("")
    if dst is None:
        dst = src.with_name(src.stem + "_norm" + src.suffix)
    dst.write_text("\n".join(out_lines), encoding="utf-8")
    return {"skipped": False, "out": str(dst), "lines": len(nonblank)}


def main() -> None:
    ap = argparse.ArgumentParser(description="単一改行TXTを段落認識可能な形に正規化")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()
    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"ファイルが見つかりません: {src}")
    r = normalize(src, Path(args.output) if args.output else None)
    if r["skipped"]:
        print(f"[normalize] スキップ: {src}（{r['reason']}）")
    else:
        print(f"[normalize] 出力: {r['out']}（行数={r['lines']}）")


if __name__ == "__main__":
    main()
