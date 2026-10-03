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
- 引用の収支が途中で 0 に戻るのに後続が続くブロック → 段落区切りが潰れた疑い。
  旧形式のファイルに多い。--fix の対象(空行区切りへ再整形して分割する)

修復(--fix):
- 不閉じ行の末尾に 」 を補い、そのブロックを空行区切りに再整形する
- 収支が途中で 0 に戻るブロック(段落区切りが潰れたもの)も再整形して分割する
- 対訳では原文側ブロックのタブ欠け行に \t を付ける
対訳は原文側ブロックをそのまま残し、訳文側だけを直す。
修復後に再検査し、残った課題を表示する。

使い方:
    python3 scripts/fix_layout.py <workdir>          # 検査のみ(要確認があれば終了コード1)
    python3 scripts/fix_layout.py <workdir> --fix    # 検出したファイルを修復
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import atomic_write_text, direction, target_quotes, target_suffix, utf8  # noqa: E402
from qa_translation import layout_lines  # noqa: E402

QOPEN, QCLOSE = "「", "」"  # 既定（zh→ja）。訳文言語ごとの値は _lib.target_quotes



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


def block_issues(block: list[str], *, parallel: bool = False, qopen: str = QOPEN, qclose: str = QCLOSE) -> list[tuple[str, str]]:
    """ブロック内の問題 [(種別, 行)] を返す。種別: 不閉じ/閉じ過多/融合疑い。"""
    if not any(qopen in l or qclose in l for l in block):
        return []
    opens = sum(l.count(qopen) for l in block)
    closes = sum(l.count(qclose) for l in block)
    if opens > closes:
        return [("不閉じ", l) for l in block if l.count(qopen) > l.count(qclose)]
    if closes > opens:
        return [("閉じ過多", l) for l in block if l.count(qclose) > l.count(qopen)]
    if parallel:
        # 対訳の訳文側は複数行の段落(段落内改行)を持ちうる。
        # 段落融合の疑いは訳文単体(<dst>.md / 全文_訳文.md)で検出する。
        return []
    run = 0
    for k, ln in enumerate(block):
        run += ln.count(qopen) - ln.count(qclose)
        if run == 0 and k < len(block) - 1:
            return [("融合疑い", block[k + 1])]
    return []


def tab_issues(lines: list[str]) -> list[str]:
    """対訳の原文側ブロックで \\t の無い継続行を返す(旧形式の正規化対象)。"""
    bad: list[str] = []
    in_block = False
    for l in lines:
        if l.startswith("> [P"):
            in_block = True
            continue
        if in_block:
            if not l.strip():
                in_block = False
            elif not l.startswith("\t"):
                bad.append(l)
    return bad


def analyze(text: str, *, parallel: bool = False, qopen: str = QOPEN, qclose: str = QCLOSE) -> list[tuple[str, str]]:
    """[(種別, 行の抜粋)] を返す。"""
    issues: list[tuple[str, str]] = []
    lines = text.split("\n")
    for block in split_blocks(lines):
        if parallel and is_original_block(block):
            continue
        if all(l.startswith("#") or l.startswith("<!--") for l in block):
            continue
        for kind, ln in block_issues(block, parallel=parallel, qopen=qopen, qclose=qclose):
            issues.append((kind, ln.strip()[:70]))
    if parallel:
        for ln in tab_issues(lines):
            issues.append(("原文タブ欠け", ln.strip()[:70]))
    return issues


def fix_tabs(lines: list[str]) -> tuple[list[str], int]:
    """対訳の原文側ブロックのタブ欠け行に \\t を付ける。"""
    out: list[str] = []
    in_block = False
    n = 0
    for l in lines:
        if l.startswith("> [P"):
            in_block = True
            out.append(l)
            continue
        if in_block:
            if not l.strip():
                in_block = False
            elif not l.startswith("\t"):
                l = "\t" + l
                n += 1
        out.append(l)
    return out, n


def fix_blocks(blocks: list[list[str]], *, parallel: bool = False, qopen: str = QOPEN, qclose: str = QCLOSE) -> tuple[list[str], int]:
    """不閉じ行の補完と、段落区切りが潰れたブロックの分割を行った結果を返す。"""
    out: list[str] = []
    appended = 0
    for block in blocks:
        if parallel and is_original_block(block):
            out.append("\n".join(block))
            continue
        kinds = [k for k, _ in block_issues(block, parallel=parallel, qopen=qopen, qclose=qclose)]
        if not kinds:
            out.append("\n".join(block))
            continue
        fixed: list[str] = []
        if "不閉じ" in kinds:
            for ln in block:
                o, c = ln.count(qopen), ln.count(qclose)
                if o > c:
                    ln = ln + qclose * (o - c)
                    appended += o - c
                fixed.append(ln)
        else:
            # 収支が途中で 0 に戻る(段落区切りが潰れた)ブロックは、
            # 空行区切りへ再整形すると区切りが復元される
            fixed = list(block)
        out.extend(layout_lines("\n".join(fixed), qopen, qclose).split("\n\n"))
    return out, appended


def process(path: Path, *, parallel: bool, fix: bool, qopen: str = QOPEN, qclose: str = QCLOSE) -> int:
    """残った課題数を返す。"""
    text, eol = read_normalized(path)
    issues = analyze(text, parallel=parallel, qopen=qopen, qclose=qclose)
    if not issues:
        print(f"[OK] {path.name}")
        return 0
    print(f"[NG] {path.name}: {len(issues)} 件")
    for kind, sample in issues[:8]:
        print(f"    [{kind}] {sample}")
    if not fix:
        return len(issues)
    lines = text.split("\n")
    tabs = 0
    if parallel:
        lines, tabs = fix_tabs(lines)
    fixed_blocks, appended = fix_blocks(split_blocks(lines), parallel=parallel, qopen=qopen, qclose=qclose)
    fixed = "\n\n".join(fixed_blocks) + "\n"
    if fixed != text:
        atomic_write_text(path, fixed.replace("\n", eol))
    remaining = analyze(fixed, parallel=parallel, qopen=qopen, qclose=qclose)
    print(f"[fix] {path.name}: 補完 {appended} 箇所 / タブ {tabs} 行 / 残り {len(remaining)} 件")
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
        # 旧形式: 出力が work ディレクトリ直下に置かれている場合
        if list(Path(args.work).glob("*.md")):
            out = Path(args.work)
        else:
            raise SystemExit(f"output がありません: {out}")

    _, dst = direction(Path(args.work))
    suf = target_suffix(dst)
    qopen, qclose = target_quotes(dst)
    # (glob, 対訳か)。訳文ファイルの接尾辞は方向による（_ja / _zh）
    targets = (
        (f"ch*/b*{suf}.md", False),
        (f"ch*{suf}.md", False),
        ("全文_訳文.md", False),
        ("ch*/b*_parallel.md", True),
        ("ch*_parallel.md", True),
        ("全文_対訳.md", True),
    )
    total = 0
    n_files = 0
    for pat, par in targets:
        for path in sorted(out.glob(pat)):
            n_files += 1
            total += process(path, parallel=par, fix=args.fix, qopen=qopen, qclose=qclose)
    if total == 0:
        print(f"layout: 全てOK（{n_files} ファイル）")
    else:
        print(f"layout: {total} 件（要確認）")
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
