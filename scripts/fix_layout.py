#!/usr/bin/env python3
"""訳文レイアウトの検査と修復: 閉じない鉤括弧で潰れた段落区切りを直す。

qa_translation.py --emit は layout_lines で「「」が閉じるまで単一改行で連結」
する。中国語原作の段落またぎ台詞をそのまま「 に写すと閉じの無い行が生まれ、
そこから次の閉じ——多くの場合ブロック末尾——までの段落の区切りが消える。
ドラフト段階では qa_translation.py の merged_paragraphs 検査が検出する。
確定済みの作品では本スクリプトで検査し、--fix で復旧する。

検査(空行区切りのブロック単位):
- 不閉じ行(「だけの行) → 事故の疑い。--fix の対象
- 閉じ過多行(」だけの行で、ブロック全体が閉じ過多) → 閉じの重複・欠落。報告のみ
- 引用の収支が途中で 0 に戻るのに後続が続くブロック → 段落融合の疑い。
  旧形式(本スキル以外が書いた/古い版が書いた)ファイルにも同形があるため報告のみ

修復(--fix): 不閉じ行の末尾に 」 を補い、そのブロックだけを空行区切りに
再整形する。対訳(> [Pnnn] 付き)は原文側ブロックをそのまま残し、訳文側だけ
を直す。修復後に再検査し、残った課題を表示する。

使い方:
    python3 scripts/fix_layout.py <workdir>          # 検査のみ(要確認があれば終了コード1)
    python3 scripts/fix_layout.py <workdir> --fix    # 検出したファイルを修復
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import atomic_write_text, utf8  # noqa: E402
from qa_translation import layout_lines  # noqa: E402

QOPEN, QCLOSE = "「", "」"

# (glob, 対訳か) の順に処理する
TARGETS = (
    ("ch*/b*_ja.md", False),
    ("ch*_ja.md", False),
    ("全文_訳文.md", False),
    ("ch*/b*_parallel.md", True),
    ("ch*_parallel.md", True),
    ("全文_対訳.md", True),
)


def read_normalized(path: Path) -> tuple[str, str]:
    """(改行を LF に正規化した本文, 書き戻し用の改行) を返す。"""
    raw = path.read_text(encoding="utf-8")
    crlf = raw.count("\r\n")
    eol = "\r\n" if crlf and crlf >= raw.count("\n") - crlf else "\n"
    return raw.replace("\r\n", "\n").replace("\r", "\n"), eol


def split_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if ln.strip():
            cur.append(ln)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def is_original_block(block: list[str]) -> bool:
    """対訳の原文側ブロックか(> [Pnnn] 行・タブ継続行・足場行)。"""
    first = block[0]
    if first.startswith("> [P") or first.startswith("\t"):
        return True
    return all(l.startswith("#") or l.startswith("<!--") for l in block)


def block_issues(block: list[str]) -> list[tuple[str, str]]:
    """ブロック内の問題 [(種別, 行)] を返す。種別: 不閉じ/閉じ過多/融合疑い。"""
    if not any(QOPEN in l or QCLOSE in l for l in block):
        return []
    opens = sum(l.count(QOPEN) for l in block)
    closes = sum(l.count(QCLOSE) for l in block)
    if opens > closes:
        return [("不閉じ", l) for l in block if l.count(QOPEN) > l.count(QCLOSE)]
    if closes > opens:
        return [("閉じ過多", l) for l in block if l.count(QCLOSE) > l.count(QOPEN)]
    run = 0
    for k, ln in enumerate(block):
        run += ln.count(QOPEN) - ln.count(QCLOSE)
        if run == 0 and k < len(block) - 1:
            return [("融合疑い", block[k + 1])]
    return []


def analyze(text: str, *, parallel: bool = False) -> list[tuple[str, str]]:
    """[(種別, 行の抜粋)] を返す。"""
    issues: list[tuple[str, str]] = []
    for block in split_blocks(text.split("\n")):
        if parallel and is_original_block(block):
            continue
        if all(l.startswith("#") or l.startswith("<!--") for l in block):
            continue
        for kind, ln in block_issues(block):
            issues.append((kind, ln.strip()[:70]))
    return issues


def fix_blocks(blocks: list[list[str]], *, parallel: bool) -> tuple[list[str], int]:
    """不閉じ行を補完して再整形した結果(ブロック文字列の列)と補完数を返す。"""
    out: list[str] = []
    appended = 0
    for block in blocks:
        if parallel and is_original_block(block):
            out.append("\n".join(block))
            continue
        if not any(k == "不閉じ" for k, _ in block_issues(block)):
            out.append("\n".join(block))
            continue
        fixed: list[str] = []
        for ln in block:
            o, c = ln.count(QOPEN), ln.count(QCLOSE)
            if o > c:
                ln = ln + QCLOSE * (o - c)
                appended += o - c
            fixed.append(ln)
        out.extend(layout_lines("\n".join(fixed)).split("\n\n"))
    return out, appended


def process(path: Path, *, parallel: bool, fix: bool) -> int:
    """残った課題数を返す。"""
    text, eol = read_normalized(path)
    issues = analyze(text, parallel=parallel)
    if not issues:
        print(f"[OK] {path.name}")
        return 0
    print(f"[NG] {path.name}: {len(issues)} 件")
    for kind, sample in issues[:8]:
        print(f"    [{kind}] {sample}")
    if not fix:
        return len(issues)
    fixed_blocks, appended = fix_blocks(split_blocks(text.split("\n")), parallel=parallel)
    fixed = "\n\n".join(fixed_blocks) + "\n"
    if fixed != text:
        atomic_write_text(path, fixed.replace("\n", eol))
    remaining = analyze(fixed, parallel=parallel)
    print(f"[fix] {path.name}: 補完 {appended} 箇所 / 残り {len(remaining)} 件")
    for kind, sample in remaining[:8]:
        print(f"    [{kind}] {sample}")
    return len(remaining)


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="訳文レイアウトの検査・修復(閉じない鉤括弧の連結事故)")
    ap.add_argument("work", help="work ディレクトリ(_saga/<作品ID>)")
    ap.add_argument("--fix", action="store_true", help="不閉じ行を補完して再整形する")
    args = ap.parse_args()

    out = Path(args.work) / "output"
    if not out.is_dir():
        raise SystemExit(f"output がありません: {out}")

    total = 0
    n_files = 0
    for pat, par in TARGETS:
        for path in sorted(out.glob(pat)):
            n_files += 1
            total += process(path, parallel=par, fix=args.fix)
    if total == 0:
        print(f"layout: 全てOK（{n_files} ファイル）")
    else:
        print(f"layout: {total} 件（要確認）")
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
