"""章見出しの数字行の扱いと、本文の二重取り込みの検出（split）のテスト。

合成データのみ使用し、作品固有情報は含めない。
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _body(i: int) -> str:
    """判定対象になる長さ（20字以上）の合成中国語段落。"""
    return f"这是第{i}段的正文内容，用来构成足够长度的段落。" + "字" * 20


class TestNumericHeadings(unittest.TestCase):
    def test_auto_ignores_numbers_when_real_headings_exist(self):
        paras = ["第一章 始まり", "本文一", "1", "本文二", "第二章 続き", "本文三"]
        chs = _lib.find_chapters(paras, lang="zh", numeric="auto")
        self.assertEqual([t for t, _s, _e in chs], ["第一章 始まり", "第二章 続き"])

    def test_section_treats_numbers_as_body(self):
        paras = ["第一章 始まり", "本文一", "1", "本文二"]
        chs = _lib.find_chapters(paras, lang="zh", numeric="section")
        self.assertEqual([t for t, _s, _e in chs], ["第一章 始まり"])

    def test_chapter_restores_old_behaviour(self):
        paras = ["第一章 始まり", "本文一", "1", "本文二"]
        chs = _lib.find_chapters(paras, lang="zh", numeric="chapter")
        self.assertEqual([t for t, _s, _e in chs], ["第一章 始まり", "1"])

    def test_auto_uses_numbers_when_no_real_heading(self):
        paras = ["1", "本文一", "2", "本文二"]
        chs = _lib.find_chapters(paras, lang="zh", numeric="auto")
        self.assertEqual([t for t, _s, _e in chs], ["1", "2"])


class TestDuplicateDetection(unittest.TestCase):
    def _doubled(self, n_per_half: int = 12) -> list[str]:
        """本文全体を2回繰り返した段落列（章題も反復する）。"""
        half = ["第一章 始まり"] + [_body(i) for i in range(n_per_half)]
        return half + half

    def test_detects_wholesale_duplicate_and_cut_point(self):
        paras = self._doubled()
        chs = _lib.find_chapters(paras, lang="zh", numeric="auto")
        dup = _lib.detect_duplicate_work(paras, chs, min_paras=4)
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["cut_chapter"], 1)
        self.assertEqual(dup["cut_para"], chs[1][1])

    def test_no_duplicate_for_original_work(self):
        paras = ["第一章 始まり"] + [_body(i) for i in range(24)]
        chs = _lib.find_chapters(paras, lang="zh", numeric="auto")
        dup = _lib.detect_duplicate_work(paras, chs, min_paras=4)
        self.assertFalse(dup["duplicate"])
        self.assertIsNone(dup["cut_para"])

    def test_repeated_content_without_repeated_titles_is_reported_without_cut(self):
        paras = ["第一章 始まり"] + [_body(i) for i in range(12)]
        paras += ["第二章 別の章"] + [_body(i) for i in range(12)]
        chs = _lib.find_chapters(paras, lang="zh", numeric="auto")
        dup = _lib.detect_duplicate_work(paras, chs, min_paras=4)
        self.assertTrue(dup["duplicate"])
        self.assertIsNone(dup["cut_para"])


def _make_work(base: Path, source: str) -> Path:
    work = base / "work"
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\n')
    _write(work / "originals" / "source.md", source)
    return work


def _run_split(work: Path, *extra: str) -> tuple[int, str]:
    import split_chapters
    buf = io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["split_chapters", str(work), *extra]):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                split_chapters.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


class TestSplitDuplicateGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)
        half = ["第一章 始まり"] + [_body(i) for i in range(24)]
        self.source = "\n\n".join(half + half) + "\n"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stops_by_default_without_writing(self):
        work = _make_work(self.base, self.source)
        code, out = _run_split(work)
        self.assertEqual(code, 1)
        self.assertIn("2回繰り返されています", out)
        self.assertIn("--dedupe", out)
        self.assertFalse((work / "chapters").exists())

    def test_dedupe_keeps_first_half(self):
        work = _make_work(self.base, self.source)
        code, out = _run_split(work, "--dedupe")
        self.assertEqual(code, 0, out)
        meta = _lib.load_chapters_meta(work)
        self.assertEqual(len(meta["chapters"]), 1)
        self.assertTrue(meta["work"]["duplicate_check"]["deduped"])
        # ratio は検出時の値（後半を消した後ではなく、元の重複率）を記録する
        self.assertGreaterEqual(meta["work"]["duplicate_check"]["ratio"], 0.10)

    def test_accept_duplicate_records_decision(self):
        work = _make_work(self.base, self.source)
        code, out = _run_split(work, "--accept-duplicate")
        self.assertEqual(code, 0, out)
        meta = _lib.load_chapters_meta(work)
        self.assertEqual(len(meta["chapters"]), 2)
        self.assertTrue(meta["work"]["duplicate_check"]["accepted"])

    def test_numeric_heading_option_is_applied(self):
        work = _make_work(self.base,
                          "第一章 始まり\n\n" + _body(1) + "\n\n1\n\n" + _body(2) + "\n")
        code, out = _run_split(work, "--numeric-headings", "chapter")
        self.assertEqual(code, 0, out)
        meta = _lib.load_chapters_meta(work)
        self.assertEqual([c["title"] for c in meta["chapters"]], ["第一章 始まり", "1"])


if __name__ == "__main__":
    unittest.main()
