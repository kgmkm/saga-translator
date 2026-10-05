"""画像ファイル名を `<作品ID>-<画像ID>.<ext>` に正規化し、参照側も一体で更新する。

背景:
- QAの non_jis_han チェックは訳文の段落テキスト(画像参照行を含む)に掛かるため、
  `images/第1章：題名付きの名前.png` のような名前は不合格になりやすい。
- ただし**ファイル名を変えただけでは駄目**。原文・確定出力・対訳・translation_input 等が
  旧名を指したままだと参照が全部壊れる(レビュー #5)。改名と参照更新は一体で行う。
- 改名対象の判定は「非JIS漢字の有無」ではなく「**目標形と違うか**」で行う。題名付きの名前は
  字種に依らず対象にし、前段の変換でJIS漢字化されていても題名が残らないようにする。
  純 ASCII の名前(画像IDそのもの等)は、ingest/relink の ID 照合を壊さないため対象にしない。
- 名前の衝突(既に同名ファイルがある)は skip せず、一意な接尾辞(`-2`, `-3`, …)で解決する。
- 中断に備えて実行前スナップショット(改名前後の対応表)を `_work/normalize_images.json` に
  残す。再実行は冪等(2回目は何もしない exit 0)で、中断時は残りの改名と参照更新を完走する。
- 命名規則・判定・採番の実装は _lib.py が正本（ingest と共用。分岐実装を作らない）。
  このスクリプトは既存作品の救済（改名と参照更新の一体実行）を担う。

    python3 normalize_images.py <作品ディレクトリ|画像フォルダ> [--apply|--dry-run]

--apply なし(または --dry-run)はドライラン。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    IMAGE_EXT, ImageNamePlanner, allocate_image_name, atomic_write_text,
    find_non_jis_han, find_unnormalized_images, work_id_of,
)

SNAPSHOT_REL = "_work/normalize_images.json"

# 参照を持ちうる場所（workdir 相対）: 原文・全章の段落・translation_input・output の md/toml/txt
REF_ROOTS = ("originals", "chapters", "translation_input", "output")
REF_SUFFIXES = (".md", ".toml", ".txt")


def find_workdir(arg: Path) -> tuple[Path, Path]:
    """(workdir, images_dir) を返す。arg が画像フォルダ直指定でも動く。"""
    if (arg / "output" / "images").is_dir():
        return arg, arg / "output" / "images"
    if (arg / "output").is_dir() or (arg / "manifest.toml").exists() or (arg / "chapters_meta.toml").exists():
        # 作品ディレクトリだが画像が無い（呼び出し側で「見つからない」を表示）
        return arg, arg / "output" / "images"
    return arg.parent, arg


def _snapshot_path(workdir: Path) -> Path | None:
    """スナップショットの置き場。作品ディレクトリと判定できるときだけ `_work/` に置く。"""
    if ((workdir / "manifest.toml").exists() or (workdir / "manifest.json").exists()
            or (workdir / "chapters_meta.toml").exists() or (workdir / "chapters_meta.json").exists()
            or (workdir / "output").is_dir()):
        return workdir / "_work" / "normalize_images.json"
    return None


def _load_snapshot(path: Path) -> dict[str, str]:
    """実行前スナップショット（改名前後の対応表）を読む。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        renames = data.get("renames") or {}
        if not isinstance(renames, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in renames.items()):
            raise ValueError("renames の形式が不正")
        return dict(renames)
    except (OSError, ValueError) as e:
        raise SystemExit(
            f"[normalize] スナップショットが読めません: {path}（{e}）。"
            "中身を確認するか、削除して再実行してください"
        )


def _save_snapshot(path: Path, renames: dict[str, str]) -> None:
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "renames": renames,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _replace_refs(text: str, mapping: dict[str, str], known: set[str]) -> str:
    """参照を新名へ置換する。known は実在名・改名先（誤爆防止に使う）。

    1) 実ファイル名そのものの参照（`/旧名`・エスケープ済みパスの `\\旧名`）を置換する。
    2) 旧名の揺れ（字形違いのまま参照している場合）を、末尾の画像IDで照合して拾う。
       known に一致する名前は他のファイルのものなので触らない。
    """
    for old, new in mapping.items():
        text = text.replace(f"/{old}", f"/{new}")
        text = text.replace(f"\\{old}", f"\\{new}")
    for old, new in mapping.items():
        m = re.match(r"^(\d{3,})-(\d{3,})-", old)
        if not m:
            continue
        prefix = f"images/{m.group(1)}-{m.group(2)}-"
        pat = re.compile(re.escape(prefix) + rf"[^)\s]+?\.(?:{IMAGE_EXT})", re.I)

        def _sub(mm: re.Match, new: str = new) -> str:
            name = mm.group(0)[len("images/"):]
            if name in known:
                return mm.group(0)
            return f"images/{new}"

        text = pat.sub(_sub, text)
    return text


def _iter_ref_files(workdir: Path):
    """参照を含みうるファイル（原文・全章の段落・translation_input・output・manifest）。"""
    for root in REF_ROOTS:
        base = workdir / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in REF_SUFFIXES:
                continue
            if root == "output" and "images" in p.relative_to(base).parts:
                continue  # 画像の実体は対象外
            yield p
    for fname in ("manifest.toml", "manifest.json"):
        p = workdir / fname
        if p.is_file():
            yield p


def rewrite_refs(workdir: Path, mapping: dict[str, str], known: set[str], *, write: bool) -> list[str]:
    """参照を新名へ置換する。write=False は変更予定の列挙のみ。更新したファイル一覧を返す。"""
    changed: list[str] = []
    if not mapping:
        return changed
    for p in _iter_ref_files(workdir):
        text = p.read_text(encoding="utf-8")
        new = _replace_refs(text, mapping, known)
        if new != text:
            changed.append(str(p.relative_to(workdir)).replace("\\", "/"))
            if write:
                atomic_write_text(p, new)
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description="画像名の正規化（改名と参照更新を一体で）")
    ap.add_argument("path", help="作品ディレクトリ（output/images を含む）または画像フォルダ")
    ap.add_argument("--apply", action="store_true", help="実際に改名と参照更新を行う（既定はドライラン）")
    ap.add_argument("--dry-run", action="store_true", help="変更せず対象の表示だけ行う（既定の動作）")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("[normalize] --apply と --dry-run は同時に指定できません")

    workdir, folder = find_workdir(Path(args.path))
    if not folder.is_dir():
        print(f"[normalize] 画像フォルダが見つからない: {folder}")
        return

    entries = {p.name: p.is_file() for p in folder.iterdir()}
    names = set(entries)  # ファイルもディレクトリも含む（衝突回避に使う）
    files = {n for n, is_file in entries.items() if is_file}
    work_id = work_id_of(workdir)
    snap_path = _snapshot_path(workdir)
    prev = _load_snapshot(snap_path) if snap_path else {}

    # 改名の計画（決定的: 名前のソート順に一意な改名先を割り当てる）。
    # 採番・衝突解決は _lib.py の正本（ImageNamePlanner）を使う（ingest と共通）。
    planner = ImageNamePlanner(work_id, names)
    completed = {old: new for old, new in prev.items() if old not in names and new in names}
    plan: list[tuple[str, str]] = []
    for name in sorted(files):
        target = planner.target(name)
        if target is None:
            continue
        plan.append((name, target))

    for old, new in plan:
        flag = " [非JIS漢字]" if find_non_jis_han(old) else ""
        print(f"{old}\n  -> {new}{flag}")

    if not args.apply:
        preview = rewrite_refs(workdir, {**completed, **dict(plan)},
                               names | set(plan) | set(prev.values()), write=False)
        print(f"[normalize] 参照更新予定: {', '.join(preview) or 'なし'}")
        print(f"[normalize] 対象 {len(plan)} 件 ドライラン")
        return

    # 適用: 実行前スナップショット → 改名 → 参照更新（改名と更新を一体で行い、中断時は再実行で完走）
    plan_map = dict(plan)
    merged = dict(prev)
    merged.update(plan_map)
    snapshot_written = False
    if snap_path and merged and merged != prev:
        _save_snapshot(snap_path, merged)
        snapshot_written = True

    renamed: dict[str, str] = {}
    finals: dict[str, str] = {}
    for old, target in plan:
        src, dst = folder / old, folder / target
        if not src.exists():
            finals[old] = target  # 既に改名済み（中断からの復旧）: 参照更新だけ残っている
            continue
        if dst.exists():
            # 計画後に外部で同名が現れた場合の保険（通常は計画時に -2 等で解決済み）
            target = allocate_image_name(target, {p.name for p in folder.iterdir()}, set())
            dst = folder / target
        src.rename(dst)
        renamed[old] = target
        finals[old] = target
    if snap_path and finals and finals != plan_map:
        merged.update(finals)
        _save_snapshot(snap_path, merged)
        snapshot_written = True

    ref_map = {**completed, **finals}
    known = names | set(ref_map.values())
    changed = rewrite_refs(workdir, ref_map, known, write=True)
    print(f"[normalize] 改名 {len(renamed)} 件 / 参照更新: {', '.join(changed) or 'なし'}")
    if snapshot_written:
        print(f"[normalize] スナップショット: {SNAPSHOT_REL}")
    print(f"[normalize] 対象 {len(plan)} 件 適用")

    left = find_unnormalized_images(folder)
    print(f"[normalize] 未正規化のまま残ったファイル: {len(left)}")
    for n in left:
        print(f"  ! {n}")


if __name__ == "__main__":
    main()
