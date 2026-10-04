"""対訳ファイル(*_parallel.md / 全文_対訳.md)の原文ブロックをバックアップから完全復元する。

対訳は 空行区切りで「原文段落ブロック（先頭行が '>'）→ 訳文ブロック」の交互構造。
原文ブロックは折り返し行（'>'で始まらない）を含むため、行単位のスキップでは守れない。
ここではブロック単位で判定し、原文ブロック全体をバックアップから戻す。

**照合の方針（#26）**: 写し戻しは行の位置ではなく、段落 ID（`[Pn]`）と
原文/訳文の役割で照合する。ブロック数・出現順・ID・役割のいずれかが食い違う
（一致が取れない・曖昧）場合は、復元せずに停止する（理由表示、exit 非ゼロ）。

使い方:
  python3 restore_parallel.py <work_dir> <backup_dir> [--apply]

  work_dir:   _saga/ 内部のディレクトリ（作品IDフォルダの親）
  backup_dir: バックアップディレクトリ（work_dir からの相対で作品IDが同じ構造）
  --apply:    実際にファイルを書き換える（省略時は dry-run 表示のみ）

終了コード: 0=成功（差分なし含む） / 1=停止（ノーバックアップ・照合不一致）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def read(p: Path) -> str:
    """改行コードを変換せずに読む（Python 3.12 でも動くよう open(newline="") を使う）。"""
    with open(p, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


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


# 段落 ID と役割をブロック先頭行から読む。原文は先頭が '>'、訳文は '>' 以外。
_ID_RE = re.compile(r"^\s*>?\s*\[P(\d+)\]")


def _block_meta(first_line: str) -> tuple[str | None, str | None]:
    """(段落ID, 役割) を返す。役割は 'src'(原文) / 'dst'(訳文)。"""
    s = first_line.lstrip()
    if not s:
        return None, None
    role = "src" if s.startswith(">") else "dst"
    m = _ID_RE.match(first_line)
    return (m.group(1) if m else None), role


def _aligned_blocks(cur: list[str], bak: list[str]) -> tuple[list, list, list[str]]:
    """現在/バックアップのブロックを位置で突き合わせ、(cur側, bak側, エラー理由) を返す。

    ブロック数・出現順・段落 ID・役割がすべて一致したときのみ対応とみなす。
    一致が取れない、または曖昧な場合はエラー理由を返す（復元しない）。
    """
    cur_items = []
    for s, e in blocks(cur):
        pid, role = _block_meta(cur[s])
        cur_items.append({"id": pid, "role": role, "lines": cur[s:e], "span": (s, e)})
    bak_items = []
    for s, e in blocks(bak):
        pid, role = _block_meta(bak[s])
        bak_items.append({"id": pid, "role": role, "lines": bak[s:e]})

    errors: list[str] = []
    if len(cur_items) != len(bak_items):
        errors.append(f"ブロック数が不一致: 現在 {len(cur_items)} / バックアップ {len(bak_items)}")
        return [], [], errors

    for k, (c, b) in enumerate(zip(cur_items, bak_items), 1):
        if c["id"] != b["id"]:
            errors.append(f"段落ID不一致 (ブロック{k}): 現在={c['id']} バックアップ={b['id']}")
            break
        if c["role"] != b["role"]:
            errors.append(f"役割不一致 (ブロック{k}, P{c['id']}): 現在={c['role']} バックアップ={b['role']}")
            break

    if errors:
        return [], [], errors
    return cur_items, bak_items, []


def restore_file(f: Path, work: Path, backup: Path, apply_: bool) -> tuple[int, int, str | None]:
    """1ファイルを復元する。(修正ファイル数, 修正行数, エラー理由) を返す。

    エラーが返ったファイルは書き換えない（誤復元しない）。
    """
    rel = f.relative_to(work)
    bf = backup / rel
    if not bf.is_file():
        return 0, 0, f"NO-BACKUP {rel}"
    cur = read(f).split("\n")
    bak = read(bf).split("\n")

    cur_items, bak_items, errors = _aligned_blocks(cur, bak)
    if errors:
        return 0, 0, f"STOP {rel}: {'; '.join(errors)}"

    n_fix = 0
    lines = list(cur)
    for c, b in zip(cur_items, bak_items):
        # 復元対象は原文ブロック（役割で判定。行位置では判定しない）
        if c["role"] != "src":
            continue
        s, e = c["span"]
        for k in range(e - s):
            if lines[s + k] != b["lines"][k]:
                lines[s + k] = b["lines"][k]
                n_fix += 1

    if n_fix:
        print(f"  restore {n_fix:3d}  {rel}")
        if apply_:
            with open(f, "w", encoding="utf-8", newline="") as fh:
                fh.write("\n".join(lines))
        return 1, n_fix, None
    return 0, 0, None


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
    stopped = 0
    for f in pars:
        n_files, n_lines, err = restore_file(f, work, backup, apply_)
        if err:
            stopped += 1
            print(err, file=sys.stderr)
            continue
        fixed_files += n_files
        fixed_lines += n_lines
    print(f"\nfiles={fixed_files} lines={fixed_lines}  (apply={apply_})")
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())