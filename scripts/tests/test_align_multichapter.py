"""複数章ワーク（前置き章+本編章）での align_check / check_align と
normalize_txt --each-line の検査。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


class TestNormalizeEachLine(unittest.TestCase):
    def test_each_line_rewrites_mixed_breaks(self):
        import normalize_txt
        td = Path(tempfile.mkdtemp())
        src = td / "in.txt"
        # 空行と単一改行が混在（--each-line の対象）
        src.write_text("見出し\n\n本文A。\n本文B。\n\n本文C。\n", encoding="utf-8")
        r = normalize_txt.normalize(src, None, each_line=True)
        self.assertFalse(r["skipped"])
        self.assertEqual(r["lines"], 4)
        self.assertEqual(
            Path(r["out"]).read_text(encoding="utf-8"),
            "見出し\n\n本文A。\n\n本文B。\n\n本文C。\n",
        )

    def test_each_line_default_off_skips(self):
        import normalize_txt
        td = Path(tempfile.mkdtemp())
        src = td / "in.txt"
        src.write_text("見出し\n\n本文A。\n", encoding="utf-8")
        r = normalize_txt.normalize(src, None)
        self.assertTrue(r["skipped"])


class TestAlignMultiChapter(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"
        (self.wd / "chapters").mkdir(parents=True)
        (self.wd / "output" / "_work").mkdir(parents=True)
        (self.wd / "chapters_meta.toml").write_text(
            '[work]\n'
            'work_id = "w1"\n\n'
            '[[chapters]]\n'
            'id = "ch001"\n'
            'title = ""\n'
            'n_paras = 1\n'
            'n_chars = 4\n'
            'blocks = [{ id = "b1", paras = [1, 1], n_chars = 4, scene_break_cut = false }]\n'
            'status = "pending"\n\n'
            '[[chapters]]\n'
            'id = "ch002"\n'
            'title = "第二章"\n'
            'n_paras = 3\n'
            'n_chars = 34\n'
            'blocks = [{ id = "b1", paras = [1, 3], n_chars = 34, scene_break_cut = false }]\n'
            'status = "pending"\n',
            encoding="utf-8", newline="\n",
        )
        (self.wd / "chapters" / "ch001.paras.toml").write_text(
            'chapter = "ch001"\n\ntitle = ""\n\n[[paras]]\nn = 1\ntext = "前置き"\n',
            encoding="utf-8", newline="\n",
        )
        (self.wd / "chapters" / "ch002.paras.toml").write_text(
            'chapter = "ch002"\n\ntitle = "第二章"\n\n'
            '[[paras]]\nn = 1\ntext = "“おはよう、いい朝だね”"\n\n'
            '[[paras]]\nn = 2\ntext = "[テスト台詞です]"\n\n'
            '[[paras]]\nn = 3\ntext = "今日はいい天気だと彼女は思った。"\n',
            encoding="utf-8", newline="\n",
        )
        (self.wd / "output" / "_work" / "ch002_b1_ja_numbered.md").write_text(
            "[P1] 「おはよう、いい朝だね」\n\n"
            "[P2] 「テスト台詞です」\n\n"
            "[P3] 今日はいい天気だ、と彼女は思った。\n",
            encoding="utf-8", newline="\n",
        )

    def run_script(self, name, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_align_check_finds_block_in_second_chapter(self):
        r = self.run_script("align_check.py", str(self.wd),
                            "--chapter", "ch002", "--block", "b1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("要確認 0 件", r.stdout)

    def test_check_align_finds_block_in_second_chapter(self):
        r = self.run_script("check_align.py", str(self.wd),
                            "--chapter", "ch002", "--block", "b1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_check_align_detects_broken_dialogue(self):
        # 原文は [] 台詞なのに訳が地の文 → 警告（exit 1）
        p = self.wd / "output" / "_work" / "ch002_b1_ja_numbered.md"
        p.write_text(
            "[P1] 「おはよう、いい朝だね」\n\n"
            "[P2] テスト台詞だと言った。\n\n"
            "[P3] 今日はいい天気だ、と彼女は思った。\n",
            encoding="utf-8", newline="\n",
        )
        r = self.run_script("check_align.py", str(self.wd),
                            "--chapter", "ch002", "--block", "b1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("原文は会話だが訳が会話になっていない", r.stdout)


if __name__ == "__main__":
    unittest.main()
