"""lock_prep の N-gram ノイズ抑制（最長一致・機能語の端・普通名詞・作品別 stopwords）。

合成フィクスチャのみ使用。作品固有の語は使わない。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import lock_prep  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


class TestStopword(unittest.TestCase):
    def test_edge_function_char(self):
        # 機能語の字が端に来る N-gram（人的・彻底 等）は落とす
        self.assertTrue(lock_prep._is_stopword("人的"))
        self.assertTrue(lock_prep._is_stopword("彻底"))

    def test_common_noun(self):
        self.assertTrue(lock_prep._is_stopword("男人"))
        self.assertTrue(lock_prep._is_stopword("身体"))
        self.assertTrue(lock_prep._is_stopword("这样"))

    def test_name_like_passes(self):
        # 名前の字（甲乙丙）は落とさない
        self.assertFalse(lock_prep._is_stopword("甲乙丙"))
        self.assertFalse(lock_prep._is_stopword("甲乙"))


class TestSuppressNested(unittest.TestCase):
    def test_equal_frequency_keeps_longest(self):
        words = ["甲乙丙丁", "甲乙丙", "乙丙丁", "甲乙", "乙丙"]
        counter = Counter({w: 5 for w in words})
        self.assertEqual(lock_prep._suppress_nested(words, counter), ["甲乙丙丁"])

    def test_shorter_with_more_hits_is_kept(self):
        # 姓だけの呼び方が多いときは残す
        words = ["甲乙丙丁", "甲乙"]
        counter = Counter({"甲乙丙丁": 5, "甲乙": 9})
        self.assertEqual(sorted(lock_prep._suppress_nested(words, counter)),
                         ["甲乙", "甲乙丙丁"])

    def test_unrelated_words_untouched(self):
        words = ["甲乙", "丙丁"]
        counter = Counter({"甲乙": 5, "丙丁": 5})
        self.assertEqual(lock_prep._suppress_nested(words, counter), ["甲乙", "丙丁"])


class TestExtraStopwords(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def _work(self) -> Path:
        work = self.td / "_saga" / "99999"
        _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
        return work

    def test_reads_from_house_naming(self):
        work = self._work()
        _write(work / "_house" / "house_style.toml",
               '[naming]\nstopwords = ["甲種", "乙種"]\n')
        self.assertEqual(lock_prep._load_extra_stopwords(work), {"甲種", "乙種"})

    def test_missing_house_style_is_empty(self):
        self.assertEqual(lock_prep._load_extra_stopwords(self._work()), set())

    def test_gather_excludes_extra_stopwords(self):
        work = self._work()
        # 全段落で「甲種」が5回出る（普通なら候補に入る）
        paras = [("ch001", 1, "甲種が来た。甲種は笑った。"),
                 ("ch001", 2, "甲種と乙種が来た。甲種は座った。")]
        plain = [r["word"] for r in lock_prep._gather_proper_nouns(paras, "zh", 50)]
        self.assertIn("甲種", plain)
        filtered = [r["word"] for r in lock_prep._gather_proper_nouns(
            paras, "zh", 50, extra_stopwords={"甲種"})]
        self.assertNotIn("甲種", filtered)


class TestGatherIntegration(unittest.TestCase):
    def test_nested_fragments_collapse(self):
        # 「甲乙丙丁」だけが頻出する本文 → 断片（甲乙丙・乙丙丁 等）は出ない
        text = "甲乙丙丁は笑った。甲乙丙丁は座った。甲乙丙丁は立った。"
        paras = [("ch001", i, text) for i in range(1, 4)]
        words = [r["word"] for r in lock_prep._gather_proper_nouns(paras, "zh", 20)]
        self.assertIn("甲乙丙丁", words)
        for frag in ("甲乙丙", "乙丙丁", "丙丁"):
            self.assertNotIn(frag, words)


if __name__ == "__main__":
    unittest.main()
