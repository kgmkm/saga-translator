#!/usr/bin/env python3
"""ドラフトと原文の段落対応を検査する。

使い方:
  python3 check_align.py <work_id> --chapter ch001 --block b4

検査内容:
  1. 段落マーカーの欠落・重複・過不足
  2. 発話段落（原文が鉤括弧で始まる）と訳文の対応
  3. 原文と訳文の文字数比が極端に外れた段落の警告
     （段落の結合・分割・丸ごと欠落を検出する）

日本語化で原文より長くなる段落は文字数比が 2.6 を超えることがあり、
内容的に正しいのに警告が出る。中身を目で確認して問題なしと判断した段落は
`--ack P1336,P1401` のように列挙すると警告から外せる（検査記録が残るので
黙って無視するより安全）。
"""
import argparse
import pathlib
import re
import sys
import tomllib

SKILL_ROOT = pathlib.Path(__file__).resolve().parent


def load_source(workdir: pathlib.Path, chapter: str):
    meta = tomllib.loads(
        (workdir / "chapters_meta.toml").read_text(encoding="utf-8")
    )["chapters"][0]
    paras = tomllib.loads(
        (workdir / f"chapters/{chapter}.paras.toml").read_text(encoding="utf-8")
    )["paras"]
    return meta, {p["n"]: p["text"] for p in paras}


def load_draft(path: pathlib.Path):
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks = {}
    order = []
    for chunk in re.split(r"(?m)^(?=\[P\d+\] )", text):
        m = re.match(r"\[P(\d+)\] ?(.*)", chunk, re.S)
        if m:
            n = int(m.group(1))
            blocks[n] = m.group(2).strip()
            order.append(n)
    return blocks, order


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work_id")
    ap.add_argument("--chapter", default="ch001")
    ap.add_argument("--block", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--ack", default="", help="内容確認済みとして警告から外す段落番号（例 P1336,P1401）")
    args = ap.parse_args()
    acked = {int(x.strip().lstrip("Pp")) for x in args.ack.split(",") if x.strip()}

    workdir = pathlib.Path(args.root) / args.work_id
    meta, para_map = load_source(workdir, args.chapter)
    blk = next(b for b in meta["blocks"] if b["id"] == args.block)
    a, z = blk["paras"]
    expected = list(range(a, z + 1))

    draft_path = workdir / "output/_work" / f"{args.chapter}_{args.block}_ja_numbered.md"
    if not draft_path.exists():
        print(f"ドラフトなし: {draft_path}")
        return 1
    draft, order = load_draft(draft_path)

    problems = []
    if order != expected:
        miss = [n for n in expected if n not in draft]
        extra = [n for n in order if n not in expected]
        problems.append(f"段落番号の不一致 期待{len(expected)}件/実際{len(order)}件 欠落={miss} 余剰={extra}")

    for n in expected:
        if n not in draft:
            continue
        if n in acked:
            continue
        src = para_map[n].strip()
        ja = draft[n]
        # 原文が会話で訳が会話でない場合のみ警告する。
        # 訳側だけ「」で始まるのは、原文が鉤括弧を文中に置く場合の正当な語順変更。
        if src.startswith("“") and not ja.startswith("「"):
            problems.append(f"P{n:03d} 原文は会話だが訳が会話になっていない")
        # 短い段落は日本語化で伸縮が大きいので、長さ比の検査は25字以上に限る
        ratio = len(ja) / max(len(src), 1)
        if len(src) >= 25 and (ratio < 0.45 or ratio > 2.6):
            problems.append(f"P{n:03d} 文字数比 {ratio:.2f}（原文{len(src)}→訳{len(ja)}）")

    if problems:
        print(f"[{args.block}] 要確認 {len(problems)}件")
        for p in problems:
            print("  -", p)
        return 1
    print(f"[{args.block}] 段落対応OK（{len(expected)}段落）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
