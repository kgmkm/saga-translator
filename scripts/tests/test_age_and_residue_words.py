"""phase11-taskU23 の回帰テスト: age_policy の文型4分類・report TOML・残渣の語単位判定。

- age: 4分類（age_diff / past_time / other_person / direct）の検出と全件残存
  （分類は確認の負担を減らすためのもので、対象外にするものではない）
- report: analysis/age_policy_report-[ネタバレあり].toml への記録（context_hash 付き）
  と、TOML からの md 生成
- 残渣: 語単位の判定（忧郁 検出 / 馥郁 非検出）と、単字ヒットの正当語除外
- 既存の残渣検出契約（safe_fix 付きでも検出は返る＝確定停止の材料）の維持

フィクスチャは合成データのみ（実作品の語彙を載せない）。
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from detect.zh2ja import (  # noqa: E402
    AGE_CLASSES,
    AgeFinding,
    ResidueConfig,
    build_age_policy_records,
    find_age_mentions,
    find_zh_vocab,
    render_age_policy_md,
    write_age_policy_report,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


class TestAgeClassification(unittest.TestCase):
    """① 4分類の検出と全件残存 ② past_time でも対象外にされない"""

    def test_four_classes_detected_and_kept(self):
        text = "她大我八岁。他八岁时搬到了城里。妹妹今年五岁。我今年十岁。"
        findings = find_age_mentions(text)
        # 分類がどれでも検出リストから外さない（全件残存）
        self.assertEqual(len(findings), 4)
        self.assertEqual([f.age_class for f in findings],
                         ["age_diff", "past_time", "other_person", "direct"])

    def test_all_detections_kept_in_mixed_text(self):
        text = "大她八岁、八岁时、妹妹五岁、我十岁。"
        findings = find_age_mentions(text)
        self.assertEqual(len(findings), 4)
        self.assertEqual({f.age_class for f in findings}, set(AGE_CLASSES))

    def test_age_diff_patterns(self):
        for text in ("大她八岁", "他比我大八岁", "弟弟小我八岁"):
            with self.subTest(text=text):
                findings = find_age_mentions(text)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].matched, "八岁")
                self.assertEqual(findings[0].age_class, "age_diff")

    def test_past_time_not_excluded(self):
        text = "八岁那年，她搬走了。等到十岁时，又回来了。"
        findings = find_age_mentions(text)
        # 過去の時点でも対象外にしない（確認対象に残す）
        self.assertEqual([f.matched for f in findings], ["八岁", "十岁"])
        self.assertEqual([f.age_class for f in findings],
                         ["past_time", "past_time"])

    def test_other_person_and_direct(self):
        findings = find_age_mentions("妹妹今年五岁。我今年十岁。")
        self.assertEqual([f.age_class for f in findings],
                         ["other_person", "direct"])

    def test_range_form_kept(self):
        # 範囲・不確定表現も検出に残す（誤変換の判断は人間側）
        findings = find_age_mentions("十七八岁的样子。")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].matched, "十七八岁")

    def test_line_numbers(self):
        findings = find_age_mentions("一行目。\n八岁时の話。")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 2)

    def test_age_finding_dataclass(self):
        f = AgeFinding(matched="八岁", age_class="direct",
                       context="…八岁…", line=3)
        self.assertEqual(f.matched, "八岁")
        self.assertEqual(f.age_class, "direct")
        self.assertEqual(f.context, "…八岁…")
        self.assertEqual(f.line, 3)


class TestAgePolicyReport(unittest.TestCase):
    """③ report TOML の記録（context_hash 付き）と md 生成"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_age_report_"))
        self.work = self.tmp / "work_0001"
        _write(self.work / "chapters_meta.toml",
               "[work]\n"
               'title = "テスト作品"\n'
               'lang = "zh"\n'
               "\n"
               "[[chapters]]\n"
               'id = "ch001"\n'
               'title = "第1章"\n'
               "n_paras = 2\n"
               "\n"
               "[[chapters.blocks]]\n"
               'id = "b1"\n'
               "paras = [1, 2]\n")
        _write(self.work / "chapters" / "ch001.paras.toml",
               "[[paras]]\n"
               "n = 1\n"
               'text = "她大我八岁。"\n'
               "\n"
               "[[paras]]\n"
               "n = 2\n"
               'text = "八岁那年，她搬走了。"\n')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _report_paths(self):
        adir = self.work / "analysis"
        return (adir / "age_policy_report-[ネタバレあり].toml",
                adir / "age_policy_report-[ネタバレあり].md")

    def test_report_toml_and_md(self):
        result = write_age_policy_report(self.work)
        self.assertTrue(result["written"])
        self.assertEqual(result["count"], 2)
        toml_path, md_path = self._report_paths()
        self.assertTrue(toml_path.is_file())
        self.assertTrue(md_path.is_file())

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        mentions = data["mentions"]
        self.assertEqual(len(mentions), 2)
        rec = mentions[0]
        self.assertEqual(
            set(rec),
            {"block", "paragraph", "matched", "class", "context_hash",
             "ruling", "rationale", "source_line"})
        self.assertEqual(rec["block"], "ch001/b1")
        self.assertEqual(rec["paragraph"], 1)
        self.assertEqual(rec["matched"], "八岁")
        self.assertEqual(rec["class"], "age_diff")
        self.assertEqual(rec["ruling"], "")       # 未裁定は空のまま
        self.assertEqual(rec["rationale"], "")
        self.assertEqual(rec["source_line"], "她大我八岁。")
        # context_hash は前後文の sha256 先頭12桁（rulings と同じ形式）
        expected = hashlib.sha256(
            "她大我八岁。".encode("utf-8")).hexdigest()[:12]
        self.assertEqual(rec["context_hash"], expected)
        self.assertRegex(rec["context_hash"], r"^[0-9a-f]{12}$")
        # past_time も記録から外さない
        self.assertEqual(mentions[1]["class"], "past_time")

        # md は TOML からの生成物（同じ内容から再生成できる）
        md_text = md_path.read_text(encoding="utf-8")
        self.assertEqual(md_text, render_age_policy_md(data))
        self.assertIn("age_diff", md_text)
        self.assertIn("past_time", md_text)
        self.assertIn("ch001/b1 P001", md_text)
        self.assertIn("生成物", md_text)

    def test_report_regenerated_idempotent(self):
        write_age_policy_report(self.work)
        toml_path, md_path = self._report_paths()
        first = (toml_path.read_text(encoding="utf-8"),
                 md_path.read_text(encoding="utf-8"))
        write_age_policy_report(self.work)
        second = (toml_path.read_text(encoding="utf-8"),
                  md_path.read_text(encoding="utf-8"))
        self.assertEqual(first, second)

    def test_report_not_written_when_no_mentions(self):
        work2 = self.tmp / "work_0002"
        _write(work2 / "chapters" / "ch001.paras.toml",
               "[[paras]]\n"
               "n = 1\n"
               'text = "彼女は静かに笑った。"\n')
        result = write_age_policy_report(work2)
        self.assertEqual(result["count"], 0)
        self.assertFalse(result["written"])
        self.assertFalse((work2 / "analysis").exists())

    def test_records_from_source_md_fallback(self):
        work3 = self.tmp / "work_0003"
        _write(work3 / "chapters_meta.toml",
               "[[chapters]]\n"
               'id = "ch001"\n'
               "n_paras = 2\n"
               "\n"
               "[[chapters.blocks]]\n"
               'id = "b1"\n'
               "paras = [1, 2]\n")
        _write(work3 / "originals" / "source.md",
               "她大我八岁。\n\n八岁那年，她搬走了。\n")
        records = build_age_policy_records(work3)
        self.assertEqual([r["class"] for r in records],
                         ["age_diff", "past_time"])
        self.assertEqual(records[0]["block"], "ch001/b1")
        self.assertEqual([r["paragraph"] for r in records], [1, 2])


class TestResidueWordLevel(unittest.TestCase):
    """④ 忧郁 検出/馥郁 非検出 ⑤ 単字ヒットが正当語で除外される ⑥ 契約維持"""

    def test_simp_word_detected(self):
        findings = find_zh_vocab("彼女は忧郁だった。")
        self.assertEqual(len(findings), 1)
        hit = findings[0]
        self.assertEqual(hit.rule, "simp_word")
        self.assertEqual(hit.value, "忧郁")
        self.assertEqual(hit.safe_fix, "憂鬱")
        # 単字（忧・郁）の重複検出はしない
        self.assertFalse([f for f in findings if f.rule == "simp"])

    def test_ja_ok_word_not_detected(self):
        self.assertEqual(find_zh_vocab("馥郁たる香りが漂う。"), [])

    def test_single_hit_excluded_by_legit_word(self):
        # 馥郁 の中の 郁 は語単位の除外で単字ヒットにならない
        self.assertEqual(find_zh_vocab("馥郁の香り。"), [])
        # 語の外にある 郁 は単字で検出される（非常用字）
        hits = [f for f in find_zh_vocab("名前は郁だ。") if f.rule == "simp"]
        self.assertEqual([f.value for f in hits], ["郁"])

    def test_config_extension_words(self):
        # 呼び出し側の追加語（合成語）でも語単位の判定が働く
        cfg = ResidueConfig(simp_words={"郁忧": "憂鬱"})
        findings = find_zh_vocab("郁忧の日々。", config=cfg)
        self.assertEqual([(f.rule, f.value) for f in findings],
                         [("simp_word", "郁忧")])
        cfg2 = ResidueConfig(ja_ok_words={"郁忧"})
        self.assertEqual(find_zh_vocab("郁忧の日々。", config=cfg2), [])

    def test_jp_common_single_char_not_detected(self):
        # 常用漢字（日本語で使う字）は単字では検出しない（非常用字だけを検出する）
        cfg = ResidueConfig(simp={"念": "念"})
        self.assertEqual(
            [f for f in find_zh_vocab("念を押す。", config=cfg)
             if f.rule == "simp"], [])
        cfg2 = ResidueConfig(simp={"筱": "笹"})
        hits = [f for f in find_zh_vocab("筱を名に持つ。", config=cfg2)
                if f.rule == "simp"]
        self.assertEqual([f.value for f in hits], ["筱"])

    def test_existing_contract_simp_single_chars(self):
        # 既存の単字検出は維持される（safe_fix 付きでも検出として返る＝
        # commit は候補でも停止する材料になる。契約維持）
        findings = find_zh_vocab("缆を巻き取る。涩い味。")
        hits = {f.value: f for f in findings if f.rule == "simp"}
        self.assertIn("缆", hits)
        self.assertIn("涩", hits)
        self.assertTrue(hits["缆"].safe_fix)
        self.assertTrue(hits["涩"].safe_fix)

    def test_existing_contract_vocab_and_idiom(self):
        findings = find_zh_vocab("紅暈が広がる。从容不迫に立つ。")
        rules = {f.rule for f in findings}
        self.assertIn("vocab", rules)
        self.assertIn("idiom", rules)
        for f in findings:
            self.assertTrue(f.safe_fix)  # 候補でも検出として返る

    def test_known_terms_still_excluded(self):
        cfg = ResidueConfig(known_terms={"データ奪舍"})
        findings = find_zh_vocab("データ奪舍の影響で昏睡状態。", config=cfg)
        self.assertEqual(
            [f for f in findings if f.rule in ("simp", "vocab", "idiom")], [])


if __name__ == "__main__":
    unittest.main()
