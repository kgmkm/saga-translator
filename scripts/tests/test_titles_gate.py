"""titles ゲート（assemble）と chapter_title 契約のテスト（レビュー #2 / P0-2）。

titles.toml（analysis/titles.toml）が無い/空のとき assemble は停止し、
--allow-src-title の経路だけが原題のまま完走する（未確定の章題は目次に「題未確定」）。
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

# 合成データ（架空の題のみ）
SRC_TITLE = "第一章　序"          # 原題の章見出し
JA_TITLE = "第一章　始まり"       # 訳題の章見出し
WORK_TITLE = "合成作品"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path) -> Path:
    """全ブロック確定済み・titles.toml 無しの合成 work（assemble 直前）。"""
    work = base / "work"
    _write(work / "manifest.toml", f'title = "{WORK_TITLE}"\nlang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", f"""\
[work]
title = "{WORK_TITLE}"

[[chapters]]
id = "ch001"
title = "{SRC_TITLE}"
status = "done"

[[chapters.blocks]]
id = "b1"
paras = [1, 2]
status = "emitted"
""")
    _write(work / "output" / "ch001" / "b1_ja.md", "訳一。\n\n訳二。\n")
    _write(work / "output" / "ch001" / "b1_parallel.md",
           "> [P001] 原一。\n\n訳一。\n\n> [P002] 原二。\n\n訳二。\n")
    return work


def _write_titles(work: Path, *, with_chapter: bool = True) -> None:
    """titles.toml を置く（with_chapter=False なら章題の欄が無い＝章題は未確定のまま）。"""
    body = f'title_src = "{WORK_TITLE}"\ntitle_ja = "{WORK_TITLE}（改題）"\n'
    if with_chapter:
        body += f'\n[[chapters]]\nid = "ch001"\nsrc = "{SRC_TITLE}"\nja = "{JA_TITLE}"\n'
    _write(work / "analysis" / "titles.toml", body)


def _assemble(work: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(SCRIPTS / "assemble.py"), str(work), "--keep-full", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _toc(work: Path) -> str:
    return (work / "output" / "目次.md").read_text(encoding="utf-8")


def _toc_line(work: Path) -> str:
    """目次の ch001 行。"""
    return next(l for l in _toc(work).splitlines() if "ch001_ja.md" in l)


class TestTitlesGate(unittest.TestCase):
    """assemble の titles ゲート（CLI サブプロセス）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_missing_titles_stops_with_reason(self):
        """①titles.toml 無しで assemble は exit 非ゼロ（理由を表示し、出力しない）。"""
        work = _make_work(self.td)
        p = _assemble(work)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout + p.stderr
        self.assertIn("titles.toml", out)
        self.assertIn("--allow-src-title", out)
        # 停止は出力を書く前（黙って原題で出さない）
        self.assertFalse((work / "output" / "目次.md").exists())

    def test_allow_src_title_completes_with_undetermined_mark(self):
        """②--allow-src-title で完走し、目次に「題未確定」を明示する。"""
        work = _make_work(self.td)
        p = _assemble(work, "--allow-src-title")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((work / "output" / "全文_訳文.md").exists())
        line = _toc_line(work)
        self.assertIn(SRC_TITLE, line)      # 原題のまま出力
        self.assertIn("題未確定", line)      # 未確定であることの明示
        full = (work / "output" / "全文_訳文.md").read_text(encoding="utf-8")
        self.assertIn(f"# {SRC_TITLE}", full)

    def test_titles_toml_used_when_present(self):
        """③titles.toml ありは通常完走し、訳題が採用される。"""
        work = _make_work(self.td)
        _write_titles(work)
        p = _assemble(work)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        line = _toc_line(work)
        self.assertIn(JA_TITLE, line)
        self.assertNotIn("題未確定", line)
        self.assertIn(f"# {WORK_TITLE}（改題）", _toc(work))
        full = (work / "output" / "全文_訳文.md").read_text(encoding="utf-8")
        self.assertIn(f"# {JA_TITLE}", full)  # 章見出しに訳題が採用される

    def test_empty_titles_toml_is_undetermined(self):
        """④空の titles.toml も未確定扱い（停止し、回避時は目次に題未確定）。"""
        work = _make_work(self.td)
        _write(work / "analysis" / "titles.toml", "# 章題はまだ確定していない\n")
        p = _assemble(work)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        p = _assemble(work, "--allow-src-title")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("題未確定", _toc_line(work))

    def test_partial_titles_toml_stops_and_marks_on_bypass(self):
        """章題の欄が無い titles.toml では通常経路を止め、回避時は題未確定を明示する。"""
        work = _make_work(self.td)
        _write_titles(work, with_chapter=False)
        p = _assemble(work)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ch001", p.stdout + p.stderr)
        p = _assemble(work, "--allow-src-title")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("題未確定", _toc_line(work))

    def test_allow_src_title_parallel_line_keeps_both_marks(self):
        """対訳つき目次行でも「題未確定」が落ちない（--parallel との併用）。"""
        work = _make_work(self.td)
        p = _assemble(work, "--allow-src-title", "--parallel")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        line = _toc_line(work)
        self.assertIn("ch001_parallel.md", line)
        self.assertIn("題未確定", line)


class TestChapterTitleContract(unittest.TestCase):
    """chapter_title は訳題が無いとき None（未確定）を返し、原題へ黙って落ちない。"""

    def test_resolved_uses_dst_then_other(self):
        from _lib import chapter_title
        self.assertEqual(chapter_title({"ja": "序章", "zh": "序幕"}, "ja"), "序章")
        self.assertEqual(chapter_title({"zh": "序幕"}, "ja"), "序幕")  # もう一方で補う

    def test_unresolved_returns_none(self):
        from _lib import chapter_title
        self.assertIsNone(chapter_title({}, "ja"))
        self.assertIsNone(chapter_title({"src": "序章"}, "ja"))


if __name__ == "__main__":
    unittest.main()
