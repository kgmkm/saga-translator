"""fix_layout の回帰テスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import fix_layout  # noqa: E402


class TestAnalyze(unittest.TestCase):
    def test_unclosed_is_detected(self):
        # 閉じ漏れの行が後続を連結している(収支が開いたままブロックが終わる)
        text = "「？！\n地の文。\n\n後続。\n"
        issues = fix_layout.analyze(text)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "不閉じ")

    def test_song_block_is_not_flagged(self):
        # 1ブロック内で開いて閉じる(歌・複数行台詞)は正常
        text = "「うた いち\nうた に\nうた さん」\n\n地の文。\n"
        self.assertEqual(fix_layout.analyze(text), [])

    def test_close_only_balanced_block_is_not_flagged(self):
        # 継続行の閉じ(「あ / い」)は正常
        text = "「あ\nい」\n\n地の文。\n"
        self.assertEqual(fix_layout.analyze(text), [])

    def test_excess_close_is_detected(self):
        text = "…かい？」\n"
        issues = fix_layout.analyze(text)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "閉じ過多")

    def test_glued_balanced_block_is_reported(self):
        # 収支が途中で 0 に戻るのに後続が続く = 融合疑い(修復対象外)
        text = "「あ\nい」\n「う\nえ」\n"
        issues = fix_layout.analyze(text)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "融合疑い")

    def test_parallel_original_block_is_skipped(self):
        text = "> [P001] 「あ\n\tい\n\n「訳文」\n"
        self.assertEqual(fix_layout.analyze(text, parallel=True), [])

    def test_parallel_multiline_ja_block_is_not_glued(self):
        # 対訳の訳文側は複数行(段落内改行)を持ちうる。融合疑いは出さない
        text = "> [P001] 原文\n\n「司令？」\n神田優の声が沈黙を破った。\n「あなたは……」\n"
        self.assertEqual(fix_layout.analyze(text, parallel=True), [])

    def test_parallel_missing_tab_is_detected(self):
        # 原文側ブロックの継続行に \t が無い(旧形式) → 原文タブ欠け
        text = "> [P001] 原文一行\n原文二行\n原文三行\n\n訳文。\n"
        issues = fix_layout.analyze(text, parallel=True)
        kinds = [k for k, _ in issues]
        self.assertEqual(kinds.count("原文タブ欠け"), 2)


class TestFix(unittest.TestCase):
    def test_fix_appends_close_and_separates(self):
        text = "「？！\n地の文。\n"
        blocks, appended = fix_layout.fix_blocks(fix_layout.split_blocks(text.split("\n")), parallel=False)
        self.assertEqual(appended, 1)
        fixed = "\n\n".join(blocks) + "\n"
        self.assertIn("「？！」\n\n地の文。", fixed)
        self.assertEqual(fix_layout.analyze(fixed), [])

    def test_fix_ignores_balanced_blocks(self):
        text = "「うた\nつづき」\n"
        blocks, appended = fix_layout.fix_blocks(fix_layout.split_blocks(text.split("\n")), parallel=False)
        self.assertEqual(appended, 0)
        self.assertEqual(blocks, ["「うた\nつづき」"])

    def test_fix_splits_glued_blocks(self):
        # 収支が途中で 0 に戻るブロックは空行区切りへ再整形して分割する
        text = "「あ\nい」\nう。\n"
        blocks, appended = fix_layout.fix_blocks(fix_layout.split_blocks(text.split("\n")), parallel=False)
        self.assertEqual(appended, 0)
        fixed = "\n\n".join(blocks) + "\n"
        self.assertIn("「あ\nい」\n\nう。", fixed)
        self.assertEqual(fix_layout.analyze(fixed), [])

    def test_fix_parallel_keeps_original(self):
        text = "> [P001] “原\n\t文。”\n\n「あ\n続き。\n"
        blocks, appended = fix_layout.fix_blocks(fix_layout.split_blocks(text.split("\n")), parallel=True)
        fixed = "\n\n".join(blocks) + "\n"
        self.assertEqual(appended, 1)
        self.assertIn("> [P001] “原\n\t文。”", fixed)
        self.assertIn("「あ」\n\n続き。", fixed)
        self.assertEqual(fix_layout.analyze(fixed, parallel=True), [])

    def test_fix_tabs_adds_tab_to_source_continuation(self):
        text = "> [P001] 原文一行\n原文二行\n\n訳文。\n"
        lines, n = fix_layout.fix_tabs(text.split("\n"))
        self.assertEqual(n, 1)
        fixed = "\n".join(lines)
        self.assertIn("> [P001] 原文一行\n\t原文二行", fixed)
        self.assertEqual(fix_layout.analyze(fixed, parallel=True), [])

    def test_fix_tabs_does_not_touch_ja_side(self):
        text = "> [P001] 原文\n\n訳文一行\n訳文二行\n"
        lines, n = fix_layout.fix_tabs(text.split("\n"))
        self.assertEqual(n, 0)
        self.assertEqual(lines, text.split("\n"))


class TestCli(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "output").mkdir()
        self.shutil = shutil

    def tearDown(self):
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra: str) -> int:
        import sys as _sys
        from unittest import mock
        with mock.patch.object(_sys, "argv", ["fix_layout", str(self.tmp), *extra]):
            with self.assertRaises(SystemExit) as cm:
                fix_layout.main()
        return cm.exception.code

    def test_check_reports_and_fix_repairs(self):
        p = self.tmp / "output" / "ch001_ja.md"
        p.write_text("「？！\n地の文。\n\n後続。\n", encoding="utf-8")
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._run("--fix"), 0)
        got = p.read_text(encoding="utf-8")
        self.assertIn("「？！」", got)
        self.assertIn("\n\n地の文。", got)

    def test_parallel_tab_fix(self):
        p = self.tmp / "output" / "ch001_parallel.md"
        p.write_text("> [P001] 原文一行\n原文二行\n\n訳文。\n", encoding="utf-8")
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._run("--fix"), 0)
        got = p.read_text(encoding="utf-8")
        self.assertIn("> [P001] 原文一行\n\t原文二行", got)

    def test_old_layout_no_output_dir(self):
        # 旧形式: 出力が work 直下にある場合でも動く
        (self.tmp / "output").rmdir()
        (self.tmp / "ch001_ja.md").write_text("問題なし。\n", encoding="utf-8")
        self.assertEqual(self._run(), 0)

    def test_clean_exit_zero(self):
        (self.tmp / "output" / "ch001_ja.md").write_text("問題なし。\n", encoding="utf-8")
        self.assertEqual(self._run(), 0)


if __name__ == "__main__":
    unittest.main()
