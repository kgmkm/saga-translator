"""擬音の警告（ono.py）と age_policy の status 表示のテスト。

合成フィクスチャのみ使用。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import ono  # noqa: E402
import saga  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path) -> Path:
    """run_qa と status が読める最小 work（zh→ja）。"""
    work = base / "_saga" / "99999"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters" / "ch001.paras.json",
           json.dumps({"chapter": "ch001", "title": None,
                       "paras": [{"n": 1, "text": "原文一。"},
                                 {"n": 2, "text": "原文二。"}]}, ensure_ascii=False))
    _write(work / "chapters_meta.json",
           json.dumps({"chapters": [{"id": "ch001", "title": None,
                                     "blocks": [{"id": "b1", "paras": [1, 2]}]}]},
                      ensure_ascii=False))
    _write(work / "analysis" / "characters.json",
           json.dumps({"characters": [{"name_src": "张三", "name_ja": "チョウサン"}]},
                      ensure_ascii=False))
    (work / "output").mkdir(parents=True, exist_ok=True)
    return work


class TestFindOno(unittest.TestCase):
    def test_detects_chinese_onomatopoeia(self):
        self.assertEqual(ono.find_ono("雨が噗嗤と降る"), ["噗嗤"])

    def test_detects_mixed_katakana_form(self):
        # プ嗤 のような混成（カナ＋漢字1字）も拾う
        self.assertEqual(ono.find_ono("彼女はプ嗤と笑った"), ["プ嗤"])

    def test_japanese_kanji_is_not_flagged(self):
        # 嘶く・嗤う・滋養 は日本語の漢字（誤検出しない）
        self.assertEqual(ono.find_ono("馬が嘶いた"), [])
        self.assertEqual(ono.find_ono("彼は嗤った"), [])
        self.assertEqual(ono.find_ono("滋養強壮"), [])

    def test_nested_forms_are_not_double_reported(self):
        self.assertEqual(ono.find_ono("噗嗤"), ["噗嗤"])

    def test_clean_japanese_is_empty(self):
        self.assertEqual(ono.find_ono("静かな夜だった。彼女は笑った。"), [])

    def test_both_positions_are_reported(self):
        # 独立した位置に出ていれば短い形も拾う
        self.assertEqual(ono.find_ono("噗嗤とプ嗤った"), ["噗嗤", "プ嗤"])


class TestCheckParas(unittest.TestCase):
    def test_returns_para_and_forms(self):
        found = ono.check_paras([(1, "ok"), (2, "噗嗤")])
        self.assertEqual(found, [{"para": 2, "forms": ["噗嗤"]}])

    def test_enabled_default_and_off(self):
        self.assertTrue(ono.enabled({}))
        self.assertFalse(ono.enabled({"qa": {"ono_check": "off"}}))

    def test_describe(self):
        found = [{"para": 3, "forms": ["噗嗤", "嗤"]}]
        self.assertIn("P3", ono.describe(found))
        self.assertIn("噗嗤", ono.describe(found))


class TestQaIntegration(unittest.TestCase):
    """QA では警告のみ（pass は True のまま）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _run(self, text: str):
        from qa_translation import run_qa
        draft = self.td / "draft.md"
        _write(draft, text)
        return run_qa(self.work, "ch001", "b1", draft)

    def test_residue_is_warning_only(self):
        report, _ = self._run("[P001] 彼女はプ嗤と笑った。\n\n[P002] 静かだった。\n")
        entry = report["checks"]["onomatopoeia"]
        self.assertTrue(entry["pass"])
        self.assertEqual(entry["severity"], "warn")
        self.assertEqual(entry["found"][0]["para"], 1)
        self.assertTrue(report["pass"], report["checks"])

    def test_clean_draft_has_no_findings(self):
        report, _ = self._run("[P001] 彼女は笑った。\n\n[P002] 静かだった。\n")
        self.assertEqual(report["checks"]["onomatopoeia"]["found"], [])


class TestAgePolicyStatus(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _rule(self, status: str = "locked") -> str:
        return ('[[rules]]\nid = "age_keep_author_setting"\nkind = "sonota"\n'
                f'status = "{status}"\n')

    def test_locked_rule_means_settled(self):
        _write(self.work / "_house" / "house_style.toml", self._rule())
        s = saga._age_policy_summary(self.work)
        self.assertIn("裁定済み", s)
        self.assertIn("age_keep_author_setting", s)
        self.assertNotIn("誤配置", s)

    def test_draft_rule_is_not_settled(self):
        _write(self.work / "_house" / "house_style.toml", self._rule("draft"))
        self.assertNotIn("裁定済み", saga._age_policy_summary(self.work))

    def test_unsettled_count_from_toml(self):
        _write(self.work / "analysis" / saga.AGE_POLICY_REPORT_TOML,
               '[[mentions]]\nruling = ""\n\n[[mentions]]\nruling = "keep"\n')
        self.assertEqual(saga._age_policy_summary(self.work), "未裁定 1件")

    def test_zero_mentions(self):
        _write(self.work / "analysis" / saga.AGE_POLICY_REPORT_TOML, "mentions = []\n")
        self.assertIn("検出 0件", saga._age_policy_summary(self.work))

    def test_md_only_is_unknown_not_zero(self):
        _write(self.work / "analysis" / saga.AGE_POLICY_REPORT_MD, "# report\n")
        s = saga._age_policy_summary(self.work)
        self.assertIn("件数不明", s)
        self.assertNotIn("0件", s)
        self.assertIsNotNone(saga._age_policy_warning(self.work))

    def test_nothing_scanned(self):
        self.assertIn("未実施", saga._age_policy_summary(self.work))

    def test_misplaced_house_style_still_found(self):
        # 作品直下の house_style.toml でも裁定を取りこぼさない（警告は init が案内）
        _write(self.work / "house_style.toml", self._rule())
        s = saga._age_policy_summary(self.work)
        self.assertIn("裁定済み", s)
        self.assertIn("誤配置", s)


if __name__ == "__main__":
    unittest.main()
