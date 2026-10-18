"""忠実性検査（未翻訳コピー検出）と、その commit / assemble への組み込みのテスト。

合成データのみ使用し、作品固有情報は含めない。
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402
from fidelity import DEFAULTS, cfg_from, check_block, check_pair, kana_ratio  # noqa: E402

ZH = ("帝国边境的渡口曾经只是个弥漫着铁锈与河水腥气的沉闷小镇。"
      "如今这份沉闷被彻底撕裂，取而代之的是粘腻的蠕动声与滴落的啪嗒声。")
JA = ("帝国の国境にある渡し場は、かつて鉄錆と川の生臭い匂いが漂う陰気な町にすぎなかった。"
      "今ではその陰気さは完全に引き裂かれ、代わりに粘つく蠢きの音と滴り落ちる音が満ちている。")


class TestFidelityUnit(unittest.TestCase):
    def test_copied_source_is_flagged(self):
        r = check_pair(ZH, ZH)
        self.assertTrue(r["flag"])
        self.assertIn("kana_low", r["reasons"])
        self.assertIn("copy", r["reasons"])

    def test_japanese_translation_is_not_flagged(self):
        r = check_pair(ZH, JA)
        self.assertTrue(r["checked"])
        self.assertFalse(r["flag"], r)

    def test_short_translation_is_not_judged(self):
        # 内心の断片など、原文より極端に短い訳文は判定しない（カナ比率が意味を持たない）
        r = check_pair(ZH, "【服従…】")
        self.assertFalse(r["checked"])
        self.assertFalse(r["flag"])

    def test_short_source_is_not_judged(self):
        r = check_pair("短い。", "短い。")
        self.assertFalse(r["checked"])

    def test_markup_is_ignored(self):
        src = "![](images/x.png)" + ZH
        r = check_pair(src, ZH)
        self.assertTrue(r["flag"])

    def test_block_level_copy_is_flagged(self):
        big = ZH * 8
        b = check_block([(big, big)])
        self.assertTrue(b["checked"])
        self.assertTrue(b["flag"])

    def test_small_block_is_not_judged(self):
        b = check_block([(ZH, ZH)])
        self.assertFalse(b["checked"])

    def test_cfg_from_overrides(self):
        cfg = cfg_from({"qa": {"copy_kana_min": 0.9, "copy_block_kana_min": 0.9}})
        self.assertAlmostEqual(cfg["kana_min"], 0.9)
        self.assertAlmostEqual(cfg["block_kana_min"], 0.9)
        self.assertEqual(cfg["min_src_chars"], DEFAULTS["min_src_chars"])

    def test_kana_ratio(self):
        self.assertAlmostEqual(kana_ratio("あいう"), 1.0)
        self.assertAlmostEqual(kana_ratio("漢字漢字"), 0.0)


class TestParseDraftInnerBlankLines(unittest.TestCase):
    def test_inner_blank_line_is_collapsed(self):
        from qa_translation import parse_draft
        items, notes = parse_draft("[P1] 一行目\n\n二行目\n\n[P2] 次の段落\n")
        self.assertEqual([n for n, _t in items], [1, 2])
        self.assertEqual(items[0][1], "一行目\n二行目")
        self.assertTrue(any("P1" in n for n in notes))

    def test_normal_paragraphs_have_no_notes(self):
        from qa_translation import parse_draft
        items, notes = parse_draft("[P1] 一行目\n二行目\n\n[P2] 次の段落\n")
        self.assertEqual(notes, [])


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, block_text: str, paras: list[str]) -> Path:
    work = base / "work"
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
    _lib.save_paras(work, "ch001", {"paras": [
        {"n": i + 1, "text": t} for i, t in enumerate(paras)]})
    _lib.save_chapters_meta(work, {"work": {"title": "合成作品"}, "chapters": [
        {"id": "ch001", "title": "原題", "status": "done",
         "blocks": [{"id": "b1", "paras": [1, len(paras)], "status": "emitted"}]}]})
    _write(work / "analysis" / "titles.toml",
           'title_src = "合成作品"\ntitle_ja = "合成作品（改題）"\n\n'
           '[[chapters]]\nid = "ch001"\nsrc = "原題"\nja = "訳題"\n')
    _write(work / "output" / "ch001" / "b1_ja.md", block_text)
    return work


def _run_assemble(work: Path, *extra: str) -> tuple[int, str]:
    from assemble import main as assemble_main
    buf = io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["assemble", str(work), *extra]):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                assemble_main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


class TestAssembleFidelityGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copied_block_stops_assemble(self):
        work = _make_work(self.base, ZH + "\n", [ZH])
        code, out = _run_assemble(work)
        self.assertEqual(code, 1)
        self.assertIn("未翻訳コピー", out)
        self.assertFalse((work / "output" / "全文_訳文.md").exists())

    def test_allow_quality_records_reason(self):
        work = _make_work(self.base, ZH + "\n", [ZH])
        code, out = _run_assemble(work, "--allow-quality", "固有名詞の引用")
        self.assertEqual(code, 0, out)
        self.assertTrue((work / "output" / "全文_訳文.md").exists())
        man = _lib.load_structured(work / "manifest")
        self.assertEqual(man["quality_waived"][-1]["reason"], "固有名詞の引用")

    def test_translated_block_passes(self):
        work = _make_work(self.base, JA + "\n", [ZH])
        code, out = _run_assemble(work)
        self.assertEqual(code, 0, out)


class TestRunQaSourceCopy(unittest.TestCase):
    """commit の QA 経路（run_qa）で未翻訳コピーが不合格になること。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = _make_work(Path(self.tmp), ZH + "\n", [ZH])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_qa_fails_on_copied_paragraph(self):
        from qa_translation import run_qa
        draft = self.work / "_work" / "ch001_b1_ja_numbered.md"
        _write(draft, f"[P1] {ZH}\n")
        report, _items = run_qa(self.work, "ch001", "b1", draft)
        self.assertFalse(report["pass"])
        check = report["checks"]["source_copy"]
        self.assertFalse(check["pass"])
        self.assertEqual(check["severity"], "fatal")
        self.assertEqual(check["found"][0]["para"], 1)

    def test_qa_can_downgrade_to_warn_by_config(self):
        import qa_translation
        # 非JIS漢字を含まない（他の fatal 検査に引っかからない）コピーで確認する
        jis_only = "日本語文字漢字文章" * 5
        self.work = _make_work(Path(self.tmp), jis_only + "\n", [jis_only])
        draft = self.work / "_work" / "ch001_b1_ja_numbered.md"
        _write(draft, f"[P1] {jis_only}\n")
        with mock.patch.object(qa_translation, "load_config",
                               return_value={"qa": {"copy_check": "warn"}}):
            report, _items = qa_translation.run_qa(self.work, "ch001", "b1", draft)
        self.assertTrue(report["pass"])
        self.assertFalse(report["checks"]["source_copy"]["pass"])

    def test_qa_passes_on_translation(self):
        from qa_translation import run_qa
        draft = self.work / "_work" / "ch001_b1_ja_numbered.md"
        _write(draft, f"[P1] {JA}\n")
        report, _items = run_qa(self.work, "ch001", "b1", draft)
        self.assertTrue(report["checks"]["source_copy"]["pass"])


if __name__ == "__main__":
    unittest.main()
