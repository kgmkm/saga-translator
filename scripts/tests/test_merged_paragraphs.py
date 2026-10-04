"""段落融合（[Pn] 境界の消滅）と、最後の段落の閉じ漏れ・引用閉じ規則の回帰テスト。

- [Pn] の境界を1つでも消す融合は fatal（2段落の融合も通過させない）。同一 [Pn] 内の
  複数行（歌・詩・折り返し）は境界を消さないため許す。
- 最後の段落の閉じ漏れ（文末で引用が閉じていない）は別の検査（unclosed_quote）。
- prepare が出すコンテキストの冒頭に引用閉じ規則が入る（P0-1）。
合成データのみ。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

# 仕様で固定された文言（改変禁止）。build_context.QUOTE_RULE と一致すること。
QUOTE_RULE = "【段落の引用の閉じ方】段落をまたぐ台詞でも、その段落の終わりで 」 を閉じる。次の段落では 「 を開き直す。引用を開いたまま段落を終えないこと。"
QUOTE_RULE_EXAMPLE = "（例: 会話が2段落に続くとき、1段落目の終わりに 」 だけを置いて閉じる）"


def _make_work(base: Path) -> Path:
    """run_qa / build_context 用の最小 work（ch001/b1 = 段落1〜4、zh→ja）。"""
    work = base / "work"
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "output").mkdir()
    paras = [{"n": n, "text": f"原文{n}。"} for n in (1, 2, 3, 4)]
    (work / "chapters" / "ch001.paras.json").write_text(
        json.dumps({"chapter": "ch001", "title": None, "paras": paras}, ensure_ascii=False),
        encoding="utf-8")
    meta = {"chapters": [{"id": "ch001", "title": None,
                          "blocks": [{"id": "b1", "paras": [1, 4]}]}]}
    (work / "chapters_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (work / "analysis" / "characters.json").write_text(
        json.dumps({"characters": []}, ensure_ascii=False), encoding="utf-8")
    return work


class TestMergedParagraphs(unittest.TestCase):
    """[Pn] の境界を消す融合と、最後の段落の閉じ漏れ。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = _make_work(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_qa(self, draft_text: str) -> dict:
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text(draft_text, encoding="utf-8")
        report, _ = run_qa(self.work, "ch001", "b1", draft)
        return report

    def test_two_paragraph_merge_is_fatal(self):
        """①2段落の融合（境界消滅）も fatal。"""
        report = self._run_qa(
            "[P001] 「台詞が開いたまま\n\n"
            "[P002] 段落末で閉じる」\n\n"
            "[P003] 訳三。\n\n"
            "[P004] 訳四。")
        check = report["checks"]["merged_paragraphs"]
        self.assertFalse(check["pass"])
        self.assertEqual(len(check["found"]), 1)
        self.assertEqual(check["found"][0]["first"], 1)
        self.assertEqual(check["found"][0]["last"], 2)
        self.assertEqual(check["found"][0]["count"], 2)
        self.assertFalse(report["pass"])

    def test_three_paragraph_merge_is_fatal(self):
        """②3段落以上の融合は従来どおり fatal。"""
        report = self._run_qa(
            "[P001] 「台詞が開いたまま\n\n"
            "[P002] 段落が続き、\n\n"
            "[P003] さらに続く」\n\n"
            "[P004] 訳四。")
        check = report["checks"]["merged_paragraphs"]
        self.assertFalse(check["pass"])
        self.assertEqual(check["found"][0]["first"], 1)
        self.assertEqual(check["found"][0]["last"], 3)
        self.assertEqual(check["found"][0]["count"], 3)
        self.assertFalse(report["pass"])

    def test_multiline_song_within_paragraph_passes(self):
        """③同一 [Pn] 内の複数行の台詞（歌・詩）は境界を消さないので PASS。"""
        report = self._run_qa(
            "[P001] 「うた いち\nうた に\nうた さん」\n\n"
            "[P002] 訳二。\n\n"
            "[P003] 訳三。\n\n"
            "[P004] 訳四。")
        self.assertTrue(report["checks"]["merged_paragraphs"]["pass"])
        self.assertTrue(report["checks"]["unclosed_quote"]["pass"])
        self.assertTrue(report["pass"], report["checks"])

    def test_last_paragraph_unclosed_quote_detected(self):
        """④最後の段落の閉じ漏れ（文末で引用が閉じていない）を検出。"""
        report = self._run_qa(
            "[P001] 「こんにちは」\n\n"
            "[P002] 訳二。\n\n"
            "[P003] 訳三。\n\n"
            "[P004] 「まだ閉じていない")
        self.assertTrue(report["checks"]["merged_paragraphs"]["pass"])  # 融合ではない
        check = report["checks"]["unclosed_quote"]
        self.assertFalse(check["pass"])
        self.assertEqual(check["found"][0]["para"], 4)
        self.assertEqual(check["found"][0]["open"], 1)
        self.assertEqual(check["found"][0]["close"], 0)
        self.assertFalse(report["pass"])

    def test_dialogue_closed_in_each_paragraph_passes(self):
        """⑤正常な連続台詞（各段落の終わりで閉じる）は PASS。"""
        report = self._run_qa(
            "[P001] 「今日はここまで。\nまた明日」\n\n"
            "[P002] 「わかった」\n\n"
            "[P003] 訳三。\n\n"
            "[P004] 「またね」")
        self.assertTrue(report["checks"]["merged_paragraphs"]["pass"])
        self.assertTrue(report["checks"]["unclosed_quote"]["pass"])
        self.assertTrue(report["pass"], report["checks"])


class TestContextQuoteRule(unittest.TestCase):
    """prepare が出すコンテキストの冒頭に引用閉じ規則が入る（P0-1）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = _make_work(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rule_verbatim_and_at_head_of_context(self):
        from build_context import QUOTE_RULE as RULE_IN_CODE
        from build_context import QUOTE_RULE_EXAMPLE as EXAMPLE_IN_CODE
        from build_context import _stage_impl
        # 仕様の文言そのまま（改変禁止）
        self.assertEqual(RULE_IN_CODE, QUOTE_RULE)
        self.assertEqual(EXAMPLE_IN_CODE, QUOTE_RULE_EXAMPLE)
        _stage_impl(self.work, "ch001", "b1", "translation")
        ctx = (self.work / "translation_input" / "ch001_b1_translation.md").read_text(
            encoding="utf-8")
        head = ctx.split("## ", 1)[0]  # 最初のセクションより前＝冒頭
        self.assertIn(QUOTE_RULE, head)
        self.assertIn(QUOTE_RULE_EXAMPLE, head)

    def test_rule_in_analysis_context(self):
        from build_context import _stage_impl
        _stage_impl(self.work, "ch001", "b1", "analysis")
        ctx = (self.work / "translation_input" / "ch001_b1_analysis.md").read_text(
            encoding="utf-8")
        self.assertIn(QUOTE_RULE, ctx.split("## ", 1)[0])
