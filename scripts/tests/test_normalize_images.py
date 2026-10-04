"""normalize_images の改名・参照更新（多章・衝突・冪等・中断復旧・dry-run）のテスト。

合成データのみ使用し、作品固有情報は含めない。CLI はサブプロセスで実行する。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
NORMALIZE = SCRIPTS / "normalize_images.py"

WORK_ID = "w123"
IMG_TITLED = "10000000-10000001-原語題名.png"   # 題名付き → 10000000-10000001.png
IMG_BARE = "原語画像A.png"                      # 作品IDなし → w123-001.png
IMG_COVER = "10000000-封面.png"                 # 表紙語つき → 10000000-cover.png
IMG_SAFE = "20956216.png"                       # 純ASCII（ID照合用）→ 対象外

REF_SITES = (
    "originals/source.md",
    "chapters/ch001.paras.toml",
    "chapters/ch002.paras.toml",
    "translation_input/ch001_b1_translation.md",
    "output/ch001/b1_ja.md",
    "output/ch002/b1_ja.md",
    "output/ch001/b1_parallel.md",
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(NORMALIZE), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _make_work(base: Path) -> Path:
    """多章の参照（原文・章段落・translation_input・確定出力・対訳・manifest）を持つ合成 work。"""
    work = base / "work"
    img = work / "output" / "images"
    img.mkdir(parents=True)
    for name in (IMG_TITLED, IMG_BARE, IMG_COVER, IMG_SAFE):
        (img / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    ref_titled = f"![図](images/{IMG_TITLED})"
    ref_bare = f"![図](images/{IMG_BARE})"
    _write(work / "manifest.toml",
           f'work_id = "{WORK_ID}"\nlang = "zh"\ndst = "ja"\n'
           f'images_copied = ["C:\\\\x\\\\output\\\\images\\\\{IMG_TITLED}"]\n')
    _write(work / "originals" / "source.md", f"原段落。\n\n{ref_titled}\n\n{ref_bare}\n")
    _write(work / "chapters" / "ch001.paras.toml",
           'chapter = "ch001"\n\n[[paras]]\nn = 1\ntext = "原段落。"\n\n'
           f'[[paras]]\nn = 2\ntext = "{ref_titled}"\n\n[[paras]]\nn = 3\ntext = "{ref_bare}"\n')
    _write(work / "chapters" / "ch002.paras.toml",
           f'chapter = "ch002"\n\n[[paras]]\nn = 1\ntext = "{ref_bare}"\n')
    _write(work / "translation_input" / "ch001_b1_translation.md", f"# 文脈\n\n{ref_titled}\n")
    _write(work / "output" / "ch001" / "b1_ja.md", f"訳文。\n\n{ref_titled}\n")
    _write(work / "output" / "ch002" / "b1_ja.md", f"訳文。\n\n{ref_bare}\n")
    _write(work / "output" / "ch001" / "b1_parallel.md",
           f"> [P001] 原段落。\n\n訳文。\n\n> [P002] {ref_titled}\n\n{ref_titled}\n")
    return work


def _tree(work: Path) -> dict[str, str]:
    """work 配下の全ファイルの内容ハッシュ（冪等検査用）。"""
    return {str(p.relative_to(work)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(work.rglob("*")) if p.is_file()}


class _WorkFixture(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.work = _make_work(self.td)


class TestNormalizeImagesDryRun(_WorkFixture):
    """dry-run（既定・--dry-run）は何も変更しない。"""

    def test_dry_run_lists_plan_without_changes(self):
        before = _tree(self.work)
        p = _run(str(self.work))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("10000000-10000001.png", p.stdout)
        self.assertIn("ドライラン", p.stdout)
        self.assertTrue((self.work / "output" / "images" / IMG_TITLED).exists())
        self.assertEqual(_tree(self.work), before, "dry-run がファイルを変更した")
        self.assertFalse((self.work / "_work").exists(), "dry-run がスナップショットを作った")

    def test_explicit_dry_run_flag(self):
        p = _run(str(self.work), "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ドライラン", p.stdout)

    def test_apply_and_dry_run_conflict(self):
        p = _run(str(self.work), "--apply", "--dry-run")
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)


class TestNormalizeImagesApply(_WorkFixture):
    """--apply: 改名と参照更新の一体実行。"""

    def test_renames_and_updates_all_refs_multichapter(self):
        img = self.work / "output" / "images"
        p = _run(str(self.work), "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("改名 3 件", p.stdout)
        names = sorted(x.name for x in img.iterdir())
        self.assertEqual(names, ["10000000-10000001.png", "10000000-cover.png",
                                 "20956216.png", "w123-001.png"])
        # 全参照箇所（原文・全章の段落・translation_input・確定出力・対訳）が一括で更新される
        for rel in REF_SITES:
            body = (self.work / rel).read_text(encoding="utf-8")
            self.assertNotIn(IMG_TITLED, body, f"{rel}: 旧名が残っている")
            self.assertNotIn(IMG_BARE, body, f"{rel}: 旧名が残っている")
        src = (self.work / "originals" / "source.md").read_text(encoding="utf-8")
        self.assertIn("images/10000000-10000001.png", src)
        self.assertIn("images/w123-001.png", src)
        # manifest の images_copied（エスケープ済みパス）も追従する
        man = (self.work / "manifest.toml").read_text(encoding="utf-8")
        self.assertNotIn(IMG_TITLED, man)
        self.assertIn("images\\\\10000000-10000001.png", man)
        # 実行前スナップショット（改名前後の対応表）が残る
        snap = json.loads((self.work / "_work" / "normalize_images.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["renames"], {IMG_TITLED: "10000000-10000001.png",
                                           IMG_BARE: "w123-001.png",
                                           IMG_COVER: "10000000-cover.png"})
        self.assertIn("未正規化のまま残ったファイル: 0", p.stdout)

    def test_second_run_is_noop(self):
        p1 = _run(str(self.work), "--apply")
        self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)
        before = _tree(self.work)
        p2 = _run(str(self.work), "--apply")
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertIn("改名 0 件", p2.stdout)
        self.assertEqual(_tree(self.work), before, "2回目の実行が何かを変更した")

    def test_collision_resolves_with_unique_suffix(self):
        """目標名が既存ファイルに埋まっていても skip せず -2 を割り当てる。"""
        img = self.work / "output" / "images"
        (img / "10000000-10000001.png").write_bytes(b"existing")
        p = _run(str(self.work), "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("10000000-10000001-2.png", p.stdout)
        self.assertEqual((img / "10000000-10000001.png").read_bytes(), b"existing")
        self.assertTrue((img / "10000000-10000001-2.png").exists())
        body = (self.work / "output" / "ch001" / "b1_ja.md").read_text(encoding="utf-8")
        self.assertIn("images/10000000-10000001-2.png", body)
        # 2回目は冪等
        before = _tree(self.work)
        p2 = _run(str(self.work), "--apply")
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertIn("改名 0 件", p2.stdout)
        self.assertEqual(_tree(self.work), before)

    def test_two_siblings_resolve_in_sorted_order(self):
        """同じ目標名を争う2ファイルはソート順に本体と -2 へ解決し、参照も別々に追従する。"""
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        work = td / "work"
        img = work / "output" / "images"
        img.mkdir(parents=True)
        a = "10000000-10000001-題A.png"
        b = "10000000-10000001-題B.png"
        (img / a).write_bytes(b"a")
        (img / b).write_bytes(b"b")
        _write(work / "manifest.toml", f'work_id = "{WORK_ID}"\n')
        _write(work / "output" / "ch001" / "b1_ja.md", f"![a](images/{a})\n")
        _write(work / "output" / "ch002" / "b1_ja.md", f"![b](images/{b})\n")
        p = _run(str(work), "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((img / "10000000-10000001.png").read_bytes(), b"a")
        self.assertEqual((img / "10000000-10000001-2.png").read_bytes(), b"b")
        body1 = (work / "output" / "ch001" / "b1_ja.md").read_text(encoding="utf-8")
        body2 = (work / "output" / "ch002" / "b1_ja.md").read_text(encoding="utf-8")
        self.assertIn("images/10000000-10000001.png", body1)
        self.assertIn("images/10000000-10000001-2.png", body2)


class TestNormalizeImagesRecovery(_WorkFixture):
    """中断からの復旧: スナップショットを残し、再実行が冪等に完走する。"""

    def _seed_snapshot(self, mapping: dict[str, str]) -> None:
        _write(self.work / "_work" / "normalize_images.json",
               json.dumps({"version": 1, "renames": mapping}, ensure_ascii=False))

    def test_resume_when_rename_done_refs_pending(self):
        """改名だけ済み・参照更新が未了の状態から、参照更新を完走する。"""
        img = self.work / "output" / "images"
        (img / IMG_TITLED).rename(img / "10000000-10000001.png")
        self._seed_snapshot({IMG_TITLED: "10000000-10000001.png"})
        p = _run(str(self.work), "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        src = (self.work / "originals" / "source.md").read_text(encoding="utf-8")
        self.assertNotIn(IMG_TITLED, src)
        self.assertIn("images/10000000-10000001.png", src)
        # 完走後の再実行は何もしない
        before = _tree(self.work)
        p2 = _run(str(self.work), "--apply")
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertIn("改名 0 件", p2.stdout)
        self.assertEqual(_tree(self.work), before)

    def test_resume_when_rename_pending(self):
        """スナップショットだけ書かれた状態（改名前）から、改名と参照更新を完走する。"""
        img = self.work / "output" / "images"
        self._seed_snapshot({IMG_TITLED: "10000000-10000001.png"})
        p = _run(str(self.work), "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((img / "10000000-10000001.png").exists())
        self.assertFalse((img / IMG_TITLED).exists())
        src = (self.work / "originals" / "source.md").read_text(encoding="utf-8")
        self.assertIn("images/10000000-10000001.png", src)
        self.assertNotIn(IMG_TITLED, src)


if __name__ == "__main__":
    unittest.main()
