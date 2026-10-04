#!/usr/bin/env python3
"""訳文ドラフトの段落対応を「会話/非会話」と文字数比で点検する。

check_align.py が見るのは段落数と大まかな整合だが、原文の複数段落を1つに
まとめて別の段落を2つに割ると総数が合ってしまい、中身が1〜2段落ずれることがある。このツールは各段落で
「原文が会話なら訳も会話か」を突き合わせ、ずれを機械的に炙り出す。

**QA(--emit)より前に使う**。--emit は [Pn] マーカーを除去するため、確定後の
ファイルは点検できない。

使い方: python3 scripts/align_check.py <work_id> --block bN
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _lib import direction, target_suffix  # noqa: E402
from align import dialogue_head_exceptions  # noqa: E402

QUOTE_HEADS = ("「", "『", "（", "(", "“", "‘", '"', "'", "[", "［")  # 原文がASCII引用符・[]括り台詞の区間あり
# 構造タグ・擬音頭の例外（会話とみなさない条件）は言語 pack（DIALOGUE_HEAD_EXCEPTIONS）が正本。
# 構造タグで始まる段落は対話ではない（ルビ・挿絵タグ・章見出し）。
# align_check は原文と訳文を同じ規則で見るため、各テキストの言語の pack を引く
# （main では原文側・訳文側でそれぞれ渡す。既定は zh→ja に合わせて zh）。


def is_onomatopoeia_head(t: str, exceptions: dict | None = None) -> bool:
    """短い引用符付きの語（多くは擬音）で始まり、残りが地の文の段落か。

    条件（開き・閉じ文字・上限字数・本文の倍率）は言語 pack の例外定義が正本。
    """
    exc = exceptions if exceptions is not None else dialogue_head_exceptions("zh")
    s = t.lstrip()
    if not s.startswith(tuple(exc["ono_head_quotes"])):
        return False
    end = None
    for i, ch in enumerate(s[1:], start=1):
        if ch in exc["ono_head_closers"]:
            end = i
            break
    if end is None or end > exc["ono_head_max"]:
        return False
    rest = s[end + 1:]
    return len(rest) >= exc["ono_head_rest_factor"] * (end + 1)


def is_dialogue(t: str, exceptions: dict | None = None) -> bool:
    """段落が会話（引用符始まり）か。構造タグ・擬音頭は pack の例外で除外する。"""
    exc = exceptions if exceptions is not None else dialogue_head_exceptions("zh")
    s = t.lstrip()
    if s.startswith(tuple(exc["structure_prefixes"])):
        return False
    if is_onomatopoeia_head(s, exc):
        return False
    return s.startswith(QUOTE_HEADS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work_id")
    ap.add_argument("--block", required=True)
    ap.add_argument("--chapter", default="ch001")
    args = ap.parse_args()

    wd = pathlib.Path(args.work_id)
    src_lang, dst_lang = direction(wd)
    # 例外は各テキストの言語の pack を引く（原文側・訳文側で言語が違う）
    exc_src = dialogue_head_exceptions(src_lang)
    exc_dst = dialogue_head_exceptions(dst_lang)
    chapters = tomllib.loads((wd/"chapters_meta.toml").read_text(encoding="utf-8"))["chapters"]
    # --chapter に一致し、かつ対象ブロックを持つ章を優先。無ければ全章からブロックで探す
    # （前置き章+本編章のように章が複数ある作品の ch002 以降でも点検できる）
    ch = next((c for c in chapters
               if c.get("id") == args.chapter
               and any(b["id"] == args.block for b in c.get("blocks", []))), None)
    if ch is None:
        ch = next((c for c in chapters if any(b["id"] == args.block for b in c.get("blocks", []))), None)
    if ch is None:
        print(f"ブロックが見つかりません: {args.block}")
        return 1
    paras = tomllib.loads((wd/f"chapters/{ch['id']}.paras.toml").read_text(encoding="utf-8"))["paras"]
    pm = {p["n"]: p["text"] for p in paras}
    blk = [b for b in ch["blocks"] if b["id"] == args.block][0]
    a, z = blk["paras"]

    f = wd/"output"/"_work"/f"{ch['id']}_{args.block}{target_suffix(dst_lang)}_numbered.md"
    if not f.exists():
        print(f"[{args.block}] ドラフトが無い（--emit 済み？）: {f}")
        return 1
    t = f.read_text(encoding="utf-8")
    mine = {}
    for part in re.split(r"(?m)^(?=\[P\d+\])", t):
        m = re.match(r"\[P(\d+)\]\s*", part)
        if m:
            mine[int(m.group(1))] = part[m.end():].strip()

    ns = sorted(mine)

    # 接尾辞付きマーカー([P123b] 等)は行頭マーカーとして扱われず、ブロック内に
    # 埋もれて段落数が合ったまま中身がずれる。必ず検出して落とす。
    suffixed = [(m.group(1), m.group(2)) for m in re.finditer(r"(?m)^\[P(\d+)([^\]]*)\]", t) if m.group(2)]
    if suffixed:
        print(f"  **接尾辞付きマーカー: {['P'+a+b for a, b in suffixed]}")
    exp = list(range(a, z + 1))
    print(f"[{args.block}] 個数={len(ns)} 期待={len(exp)} 連番={ns == exp}")
    if ns != exp:
        print("  欠落:", sorted(set(exp) - set(ns)), "余剰:", sorted(set(ns) - set(exp)))

    bad = 0
    for n in exp:
        if n not in mine:
            continue
        src, m = pm.get(n, ""), mine[n]
        sd, md = is_dialogue(src, exc_src), is_dialogue(m, exc_dst)
        ratio = len(m) / max(len(src), 1)
        flag = ""
        if sd != md:
            flag += " 会話/非会話の不一致"
        if ratio < 0.25 or ratio > 3.0:
            flag += f" 文字数比{ratio:.2f}"
        if flag:
            bad += 1
            print(f"  P{n}{flag}")
            print(f"     原: {src[:46]}")
            print(f"     訳: {m[:46]}")
    print(f"  要確認 {bad} 件")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
