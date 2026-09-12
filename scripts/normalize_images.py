"""画像ファイル名を `<作品ID>-<画像ID>.<ext>` に正規化し、参照側も同期する。

背景:
- QAの non_jis_han チェックは訳文の段落テキスト(画像参照行を含む)に掛かるため、
  `images/第一章：理性之银辉-升华的月光【上半】.png` のような名前は不合格になる。
- ただし**ファイル名を変えただけでは駄目**。`originals/source.md` と
  `chapters/*.paras.toml` が旧名を指したままだと参照が全部壊れる(#5 25816307 で実際に発生)。
- また「非JIS漢字があるときだけ改名する」判定は、前段の変換で既にJIS漢字化
  されている場合に**対象0件で素通り**し、題名付きの名前が残ってしまう。

なので判定は「非JIS漢字の有無」ではなく「**目標形と違うか**」で行い、
改名後は参照を3箇所(source.md / ch*.paras.toml / manifest.toml の images_copied)で書き換える。

    python3 normalize_images.py <作品ディレクトリ|画像フォルダ> [--apply]

--apply なしはドライラン。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _lib import find_non_jis_han  # noqa: E402

# 目標形（すでに正規化済み）: <work>-<imgid>.<ext> / <work>-cover.<ext>
DONE = re.compile(r"^(?P<work>\d{3,})-(?:(?P<img>\d{3,})|(?P<cover>cover))\.(?P<ext>png|jpg|jpeg|webp|gif)$", re.I)
# 題名付き: <work>-<imgid>-<title>.<ext> / <work>-<title>.<ext>
IMG = re.compile(r"^(?P<work>\d{3,})-(?:(?P<img>\d{3,})-)?(?P<title>.+)\.(?P<ext>png|jpg|jpeg|webp|gif)$", re.I)


def target_name(name: str) -> str | None:
    """正規化後の名前。すでに目標形なら None。"""
    if DONE.match(name):
        return None
    m = IMG.match(name)
    if not m:
        return None
    work, img, ext = m.group("work"), m.group("img"), m.group("ext").lower()
    return f"{work}-{img}.{ext}" if img else f"{work}-cover.{ext}"


# 参照を持ちうるファイル（workディレクトリ相対）
REF_FILES = ("originals/source.md", "chapters/ch001.paras.toml")


def find_workdir(arg: Path) -> tuple[Path, Path]:
    """(workdir, images_dir) を返す。arg が画像フォルダ直指定でも動く。"""
    if (arg / "output" / "images").is_dir():
        return arg, arg / "output" / "images"
    return arg.parent, arg


def rewrite_refs(workdir: Path, mapping: dict[str, str]) -> list[str]:
    """参照を新名へ置換。更新したファイルの一覧を返す。"""
    changed: list[str] = []
    # 旧名の揺れ（簡体字のまま参照している場合）も拾えるよう、両方向で照合する
    for rel in REF_FILES:
        p = workdir / rel
        if not p.exists():
            continue
        text = old = p.read_text(encoding="utf-8")
        for oldname, newname in mapping.items():
            # 実ファイル名そのもの
            text = text.replace(f"images/{oldname}", f"images/{newname}")
            # 題名部分の漢字だけ簡体字/繁体字だった参照を、末尾の画像IDで照合して置換
            m = re.match(r"^(\d{3,})-(\d{3,})-", oldname)
            if m:
                prefix = f"images/{m.group(1)}-{m.group(2)}-"
                text = re.sub(
                    re.escape(prefix) + r"[^)\s]+?\.(png|jpg|jpeg|webp|gif)",
                    f"images/{newname}",
                    text,
                    flags=re.I,
                )
        if text != old:
            p.write_text(text, encoding="utf-8")
            changed.append(rel)
    # manifest の images_copied も追従させる
    mp = workdir / "manifest.toml"
    if mp.exists():
        mt = mo = mp.read_text(encoding="utf-8")
        for oldname, newname in mapping.items():
            mt = mt.replace(f"\\\\{oldname}", f"\\\\{newname}").replace(f"/{oldname}", f"/{newname}")
        if mt != mo:
            mp.write_text(mt, encoding="utf-8")
            changed.append("manifest.toml")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="作品ディレクトリ（output/images を含む）または画像フォルダ")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    workdir, folder = find_workdir(Path(args.path))
    if not folder.is_dir():
        print(f"[normalize] 画像フォルダが見つからない: {folder}")
        return

    mapping: dict[str, str] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        new = target_name(p.name)
        if new and new != p.name:
            mapping[p.name] = new

    for oldname, newname in mapping.items():
        flag = ""
        if find_non_jis_han(oldname):
            flag = " [非JIS漢字]"
        print(f"{oldname}\n  -> {newname}{flag}")

    if args.apply:
        renamed: dict[str, str] = {}
        for oldname, newname in mapping.items():
            src, dst = folder / oldname, folder / newname
            if dst.exists():
                print(f"  !! 既存のためスキップ: {newname}")
                continue
            src.rename(dst)
            renamed[oldname] = newname
        changed = rewrite_refs(workdir, renamed)
        print(f"[normalize] 改名 {len(renamed)} 件 / 参照更新: {', '.join(changed) or 'なし'}")

    print(f"[normalize] 対象 {len(mapping)} 件 {'適用' if args.apply else 'ドライラン'}")

    if args.apply:
        left = [p.name for p in sorted(folder.iterdir()) if p.is_file() and find_non_jis_han(p.name)]
        print(f"[normalize] 残存する非JIS漢字を含むファイル: {len(left)}")
        for n in left:
            print(f"  ! {n}")


if __name__ == "__main__":
    main()
