#!/usr/bin/env python3
"""日本語新字体・旧字体 -> 簡体字 の変換表を opencc 辞書から合成して charmap/ja2zh.toml へ書き出す。

ja→zh 翻訳のターゲット検査（detect/ja2zh・normalize/ja2zh）が使う変換表を再生成する。
pip 版 opencc（opencc-python-reimplemented）は設定 JSON が欠けていたり .ocd2 を
参照したりすることがあるが、dictionary/ の .txt は同梱される。本スクリプトは
.txt を直接読むので設定は不要。opencc が必要なのは再生時だけ。

合成規則:
  - 変換元は GB2312 に無い字（= 日本語専用の字形・旧字体）に限る。中国語の常用字を
    変換元に含めると、正しい中国語の本文を壊す（誤爆）。
  - 旧字 -> 新字体（JPShinjitaiCharactersRev.txt）と 繁体 -> 簡体（TSCharacters.txt）を合成。
  - 1字に複数の対応が衝突する字（連=连/联 等）は自動修正から除外する（検出のみ側で扱う）。

再生成時は、現行の ja2zh.toml と diff を確認してから置き換えること
（表が変わると QA の挙動が変わる）。

使い方:
    python3 charmap/ja2zh.py [--out charmap/ja2zh.toml]
"""
from __future__ import annotations

import argparse
import pathlib

import opencc

HEADER = """\
# 日本語新字体・旧字体 -> 簡体字 の変換表（opencc 辞書から自動合成）。
#
# 再生成: scripts/charmap/ja2zh.py（opencc が必要なのは再生時だけ）
# 手で編集しない。変換元は GB2312 に無い字（= 日本語専用の字形）に限る。
# 1字に複数対応が衝突する字は自動修正から除外し、検出のみ側で扱う。
#
[ja2zh]
"""


def dictionary_dir() -> pathlib.Path:
    return pathlib.Path(opencc.__file__).resolve().parent / "dictionary"


def load_pairs(name: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in (dictionary_dir() / name).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 1 and len(parts[1]) == 1:
            pairs[parts[0]] = parts[1]
    return pairs


def gb2312_chars() -> set[str]:
    out: set[str] = set()
    for b1 in range(0xA1, 0xFA):
        for b2 in range(0xA1, 0xFF):
            try:
                ch = bytes([b1, b2]).decode("gb2312")
            except Exception:
                continue
            if len(ch) == 1:
                out.add(ch)
    return out


def build_table() -> tuple[dict[str, str], set[str]]:
    ts = load_pairs("TSCharacters.txt")
    rev = load_pairs("JPShinjitaiCharactersRev.txt")
    gb = gb2312_chars()

    table: dict[str, str] = {}
    conflict: set[str] = set()

    def add(src: str, dst: str) -> None:
        if src in table and table[src] != dst:
            conflict.add(src)
            return
        table[src] = dst

    for old, new in rev.items():
        if new in gb or new == old or old not in ts:
            continue
        add(new, ts[old])
    for c, s in ts.items():
        if c == s or c in gb or c in rev:
            continue
        add(c, s)
    for k in conflict:
        table.pop(k, None)
    return dict(sorted(table.items())), conflict


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(pathlib.Path(__file__).with_name("ja2zh.toml")))
    args = ap.parse_args()

    table, conflict = build_table()
    lines = [HEADER.rstrip("\n")]
    for ch, dst in table.items():
        lines.append(f"{toml_str(ch)} = {toml_str(dst)}")
    pathlib.Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print("entries:", len(table))
    print("conflict (excluded):", "".join(sorted(conflict)) or "(none)")
    print("written:", args.out)


if __name__ == "__main__":
    main()