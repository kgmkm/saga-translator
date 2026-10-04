# -*- coding: utf-8 -*-
"""合本の題の正本（titles.toml）と不一致停止・--use-titles のテスト（レビュー #21 / P0-2）。

build_series_edition は、titles.toml の題（正本）と全文の H1（派生物）が食い違うと停止し、
--use-titles で titles.toml を正本として完走、採用した決定を出力側 manifest に記録する。
一致している場合と titles.toml 無しの旧作品は従来どおり完走する（記録なし）。
CLI はサブプロセスで実行する。合成データのみ使用し、作品固有情報は含めない。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
PYTHON = sys.executable


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(saga: Path, wid: str, *, title_ja=None, h1=None) -> Path:
    """合成 work。titles.toml は title_ja 指定時のみ、全文の H1 は h1 指定時のみ置く。"""
    w = saga / wid
    src = "原作" + wid
    _write(w / "manifest.toml", f'work_id = "{wid}"\ntitle = "{src}"\n')
    _write(w / "chapters_meta.toml", f'[work]\nwork_id = "{wid}"\ntitle = "{src}"\n')
    if title_ja is not None:
        _write(w / "analysis" / "titles.toml", f'title_src = "{src}"\ntitle_ja = "{title_ja}"\n')
    lines = []
    if h1:
        lines += [f"# {h1}", ""]
    lines += [f"本文。{wid}", ""]
    _write(w / "output" / "全文_訳文.md", "\n".join(lines))
    return w


def _make_saga(root: Path, works) -> Path:
    """works: [(wid, title_ja or None, h1 or None), ...] から合成 _saga を作る。"""
    saga = root / "_saga"
    saga.mkdir(parents=True)
    for wid, title_ja, h1 in works:
        _make_work(saga, wid, title_ja=title_ja, h1=h1)
    return saga


def _build(root: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(SCRIPTS / "build_series_edition.py"), str(root),
         "--dest", str(out), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


class TestSeriesTitleMismatch(unittest.TestCase):
    """titles.toml と全文の H1 の照合（停止・--use-titles・記録）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

    def test_mismatch_stops_and_lists_both_titles(self):
        """①titles あり＋古い H1 → 停止し、両方の題を一覧表示（書き込み前）。"""
        _make_saga(self.td, [("11111111", "新題", "旧題"), ("22222222", "新題2", "旧題2")])
        out = self.td / "out" / "S"
        p = _build(self.td, out)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        text = p.stdout + p.stderr
        self.assertIn("11111111: titles.toml「新題」 / 全文の H1「旧題」", text)
        self.assertIn("22222222: titles.toml「新題2」 / 全文の H1「旧題2」", text)
        self.assertIn("--use-titles", text)
        self.assertFalse(out.exists())  # 停止は出力を書く前

    def test_mismatch_stop_keeps_existing_dest_intact(self):
        """①続き: 停止は出力先の削除より先（--force でも既存出力を壊さない）。"""
        _make_saga(self.td, [("11111111", "新題", "旧題")])
        out = self.td / "out" / "S"
        out.mkdir(parents=True)
        (out / "keep.txt").write_text("x", encoding="utf-8")
        p = _build(self.td, out, "--force")
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((out / "keep.txt").exists())

    def test_use_titles_completes_and_records(self):
        """②--use-titles → titles 正本で完走し、採用（対象作品・日時）を manifest に記録。"""
        _make_saga(self.td, [("11111111", "新題", "旧題"), ("22222222", "同じ題", "同じ題")])
        out = self.td / "out" / "S"
        p = _build(self.td, out, "--use-titles")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # 単話ファイル名・H1・目次・合本は titles.toml の題（H1 で上書きしない）
        self.assertTrue((out / "01_新題.md").exists())
        part = (out / "01_新題.md").read_text(encoding="utf-8")
        self.assertTrue(part.startswith("# 新題\n"), part[:40])
        g = (out / "合本.md").read_text(encoding="utf-8")
        self.assertIn("# 新題", g)
        self.assertNotIn("# 旧題", g)
        toc = (out / "目次.md").read_text(encoding="utf-8")
        self.assertIn("新題", toc)
        # manifest: series_title_resolved と対象作品・日時（一致していた作品は含まない）
        man = (out / "manifest.toml").read_text(encoding="utf-8")
        self.assertIn('series_title_resolved = "titles"', man)
        self.assertIn("11111111", man)
        self.assertNotIn("22222222", man)
        self.assertRegex(man, r'series_title_resolved_at = "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
        # 派生物（全文）が古いままなので再生成の案内を出す
        self.assertIn("assemble", p.stdout + p.stderr)

    def test_match_completes_without_record(self):
        """③一致 → 通常完走（--use-titles でも記録しない）。"""
        _make_saga(self.td, [("11111111", "同じ題", "同じ題")])
        out = self.td / "out" / "S"
        p = _build(self.td, out)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((out / "manifest.toml").exists())
        p = _build(self.td, out, "--use-titles", "--force")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((out / "manifest.toml").exists())

    def test_legacy_work_without_titles_uses_heading(self):
        """④titles 無しの旧作品 → 従来挙動（全文の H1 の題で完走、記録なし）。"""
        _make_saga(self.td, [("11111111", None, "旧作の題")])
        out = self.td / "out" / "S"
        p = _build(self.td, out)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((out / "01_旧作の題.md").exists())
        self.assertFalse((out / "manifest.toml").exists())

    def test_legacy_work_without_heading_falls_back(self):
        """④続き: titles 無し・H1 無し → 原題扱いのまま完走（停止しない）。"""
        _make_saga(self.td, [("11111111", None, None)])
        out = self.td / "out" / "S"
        p = _build(self.td, out)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((out / "01_原作11111111.md").exists())
        self.assertFalse((out / "manifest.toml").exists())


class TestReplaceHeadingUnit(unittest.TestCase):
    """build_part_body の見出し差替（先頭 H1 のみ・本文は不変）。"""

    def test_replace_heading_swaps_first_line_only(self):
        import build_series_edition as bse
        body, heading, prepended = bse.build_part_body(
            "# 旧題\n\n本文。\n", "新題", replace_heading=True)
        self.assertEqual(body, "# 新題\n\n本文。\n")
        self.assertEqual(heading, "旧題")
        self.assertFalse(prepended)
        body2, _, _ = bse.build_part_body("# 旧題\n\n本文。\n", "新題")
        self.assertEqual(body2, "# 旧題\n\n本文。\n")  # 既定では差し替えない

    def test_replace_heading_skips_when_matching(self):
        import build_series_edition as bse
        body, _, _ = bse.build_part_body("# 同じ題\n\n本文。\n", "同じ題", replace_heading=True)
        self.assertEqual(body, "# 同じ題\n\n本文。\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
