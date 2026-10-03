"""対訳ファイル(*_parallel.md / 全文_対訳.md)の原文ブロックをバックアップから完全復元する。

対訳は 空行区切りで「原文段落ブロック（先頭行が '>'）→ 訳文ブロック」の交互構造。
原文ブロックは折り返し行（'>'で始まらない）を含むため、行単位のスキップでは守れない。
ここではブロック単位で判定し、原文ブロック全体をバックアップから戻す。

使い方:
  python3 restore_parallel.py <work_dir> <backup_dir> [--apply]

  work_dir:   _saga/ 内部のディレクトリ（作品IDフォルダの親）
  backup_dir: バックアップディレクトリ（work_dir からの相対で作品IDが同じ構造）
  --apply:    実際にファイルを書き換える（省略時は dry-run 表示のみ）

終了コード: 0
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", newline="")


def blocks(lines: list[str]) -> list[tuple[int, int]]:
    out = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == "":
            i += 1
            continue
        j = i
        while j < n and lines[j].strip() != "":
            j += 1
        out.append((i, j))
        i = j
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="対訳ファイルの原文ブロック復元")
    ap.add_argument("work_dir", type=Path, help="作品ディレクトリ（_saga/ 内部）")
    ap.add_argument("backup_dir", type=Path,
                    help="バックアップディレクトリ（作品IDフォルダを含む）")
    ap.add_argument("--apply", action="store_true",
                    help="実際にファイルを書き換える（省略時は dry-run）")
    args = ap.parse_args()

    work = args.work_dir
    backup = args.backup_dir
    apply_ = args.apply

    if not backup.is_dir():
        print(f"バックアップディレクトリが見つかりません: {backup}", file=sys.stderr)
        return 1
    if not work.is_dir():
        print(f"作業ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 作品IDサブディレクトリを列挙
    ids = sorted(d.name for d in work.iterdir() if d.is_dir() and d.name.isdigit())
    if not ids:
        print(f"作品IDサブディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    pars: list[Path] = []
    for i in ids:
        pars.extend(work.glob(f"{i}/output/**/*_parallel.md"))
        pars.extend(work.glob(f"{i}/output/**/*全文_対訳.md", case_sensitive=False))
    if not pars:
        # 入れ子なし版も試す（work 直下が作品）
        par = work / "output"
        if par.is_dir():
            pars = sorted(par.glob("ch*_parallel.md")) + sorted(par.glob("全文_対訳.md", case_sensitive=False))

    pars = sorted(set(p.resolve() for p in pars))
    if not pars:
        print("対訳ファイルが見つかりません", file=sys.stderr)
        return 1

    fixed_files = 0
    fixed_lines = 0
    for f in pars:
        rel = f.relative_to(work)
        bf = (backup / rel)
        if not bf.is_file():
            print("NO-BACKUP", rel)
            continue
        cur = read(f).split("\n")
        bak = read(bf).split("\n")
        if len(cur) != len(bak):
            print("LENDIFF", rel, len(cur), len(bak))
            continue
        n_fix = 0
        for s, e in blocks(cur):
            if cur[s].startswith(">"):  # 原文ブロック → 丸ごと復元
                for k in range(s, e):
                    if cur[k] != bak[k]:
                        cur[k] = bak[k]
                        n_fix += 1
        if n_fix:
            fixed_files += 1
            fixed_lines += n_fix
            print(f"  restore {n_fix:3d}  {rel}")
            if apply_:
                f.write_text("\n".join(cur), encoding="utf-8", newline="")
    print(f"\nfiles={fixed_files} lines={fixed_lines}  (apply={apply_})")
    return 0


if __name__ == "__main__":
    sys.exit(main())