"""方向ディスパッチ（zh→ja / ja→zh）の回帰テスト。自作フィクスチャのみ使用。

既存テストは manifest 無し = zh→ja 既定で通る。ここでは manifest ありの
ja→zh と、方向ヘルパーの単位検査だけを扱う。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def make_qa_work(tmp: Path, *, lang=None, dst=None):
    """run_qa/emit 用の最小 work。manifest は指定時のみ書く。"""
    (tmp / "chapters").mkdir(parents=True, exist_ok=True)
    (tmp / "analysis").mkdir(parents=True, exist_ok=True)
    (tmp / "output").mkdir(parents=True, exist_ok=True)
    paras = [{"n": 1, "text": "原文一。"}, {"n": 2, "text": "原文二。"}]
    (tmp / "chapters" / "ch001.paras.json").write_text(
        json.dumps({"chapter": "ch001", "title": None, "paras": paras}, ensure_ascii=False),
        encoding="utf-8")
    meta = {"chapters": [{"id": "ch001", "title": None, "blocks": [{"id": "b1", "paras": [1, 2]}]}]}
    (tmp / "chapters_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (tmp / "analysis" / "characters.json").write_text(
        json.dumps({"characters": [{"name_src": "张三", "name_ja": "チョウサン"}]},
                   ensure_ascii=False), encoding="utf-8")
    if lang or dst:
        lines = ""
        if lang:
            lines += f'lang = "{lang}"\n'
        if dst:
            lines += f'dst = "{dst}"\n'
        _write(tmp / "manifest.toml", lines)


class TestDirection(unittest.TestCase):
    def test_default_is_zh2ja(self):
        from _lib import direction
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(direction(Path(td)), ("zh", "ja"))

    def test_manifest_ja_implies_zh(self):
        from _lib import direction
        with tempfile.TemporaryDirectory() as td:
            make_qa_work(Path(td), lang="ja")
            self.assertEqual(direction(Path(td)), ("ja", "zh"))

    def test_explicit_dst_wins(self):
        from _lib import direction
        with tempfile.TemporaryDirectory() as td:
            make_qa_work(Path(td), lang="zh", dst="zh")
            self.assertEqual(direction(Path(td)), ("zh", "zh"))

    def test_suffix_quotes_titles(self):
        from _lib import align_ratio_floor, chapter_title, dialogue_heads, target_quotes, target_suffix, target_title
        self.assertEqual(target_suffix("ja"), "_ja")
        self.assertEqual(target_suffix("zh"), "_zh")
        self.assertEqual(target_quotes("ja"), ("「", "」"))
        self.assertEqual(target_quotes("zh"), ("“", "”"))
        self.assertEqual(target_title({"title_ja": "和題", "title_zh": "中題"}, "zh"), "中題")
        self.assertEqual(target_title({"title_ja": "和題"}, "zh"), "和題")
        self.assertEqual(chapter_title({"ja": "序章", "zh": "序幕"}, "zh"), "序幕")
        src_heads, dst_heads = dialogue_heads("ja", "zh")
        self.assertIn("「", src_heads)
        self.assertIn("“", dst_heads)
        self.assertEqual(align_ratio_floor("ja", "zh"), 0.30)
        self.assertEqual(align_ratio_floor("zh", "ja"), 0.45)

    def test_lang_packs(self):
        from _lib import LANG_PACKS, compile_chapter_re
        self.assertIn("zh", LANG_PACKS)
        self.assertIn("ja", LANG_PACKS)
        pat = compile_chapter_re("ja")
        self.assertTrue(pat.match("[chapter:第一章 再会]"))

    def test_detect_ja2zh(self):
        from detect.ja2zh import JA2ZH, find_jp_quotes, find_kana, find_non_gbk_han  # noqa: F401
        self.assertEqual(JA2ZH.get("剣"), "剑")
        self.assertTrue(find_kana("あ"))
        self.assertFalse(find_kana("啊"))
        self.assertTrue(find_jp_quotes("「」"))
        self.assertFalse(find_jp_quotes("“”"))
        self.assertTrue(find_non_gbk_han("剣"))

    def test_normalize_ja2zh(self):
        from normalize.ja2zh import fix_text
        new, applied = fix_text("剣と魔法")
        self.assertEqual(new, "剑と魔法")
        self.assertTrue(applied)
        kana = "あいう"
        new, _ = fix_text(kana)
        self.assertEqual(new, kana)

    def test_detect_zh2ja(self):
        from detect.zh2ja import find_non_jis_han
        self.assertEqual(find_non_jis_han("沈剑平"), [("剑", 1)])
        self.assertEqual(find_non_jis_han("沈見平"), [])


class TestQaDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        make_qa_work(self.tmp, lang="ja", dst="zh")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_zh_passes(self):
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 他拔出了剑。\n\n[P002] “早上好。”\n", encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertTrue(report["pass"], report["checks"])

    def test_kana_leftover_fails(self):
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 彼は走った。\n\n[P002] 跑了。\n", encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertFalse(report["pass"])
        self.assertFalse(report["checks"]["kana_leftover"]["pass"])

    def test_emit_writes_zh(self):
        from qa_translation import emit
        from _lib import save_chapters_meta, save_paras
        (self.tmp / "output" / "_work").mkdir(parents=True, exist_ok=True)
        save_paras(self.tmp, "ch001", {"paras": [{"n": 1, "text": "原一。"}, {"n": 2, "text": "原二。"}]})
        save_chapters_meta(self.tmp, {"chapters": [{"id": "ch001", "status": "pending",
            "blocks": [{"id": "b1", "status": "pending", "paras": [1, 2], "n_chars": 0}]}]})
        emit(self.tmp, "ch001", "b1", [(1, "译一。"), (2, "译二。")])
        got = (self.tmp / "output" / "ch001" / "b1_zh.md").read_text(encoding="utf-8")
        self.assertIn("译一。", got)


class TestAlignDispatch(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"
        (self.wd / "chapters").mkdir(parents=True)
        (self.wd / "output" / "_work").mkdir(parents=True)
        _write(self.wd / "manifest.toml", 'lang = "ja"\ndst = "zh"\n')
        _write(self.wd / "chapters_meta.toml",
               '[work]\nwork_id = "w1"\n\n[[chapters]]\nid = "ch001"\ntitle = ""\nn_paras = 1\nn_chars = 4\n'
               'blocks = [{ id = "b1", paras = [1, 1], n_chars = 4, scene_break_cut = false }]\nstatus = "pending"\n\n'
               '[[chapters]]\nid = "ch002"\ntitle = "第二章"\nn_paras = 3\nn_chars = 34\n'
               'blocks = [{ id = "b1", paras = [1, 3], n_chars = 34, scene_break_cut = false }]\nstatus = "pending"\n')
        _write(self.wd / "chapters" / "ch001.paras.toml",
               'chapter = "ch001"\n\ntitle = ""\n\n[[paras]]\nn = 1\ntext = "前置き"\n')
        _write(self.wd / "chapters" / "ch002.paras.toml",
               'chapter = "ch002"\n\ntitle = "第二章"\n\n'
               '[[paras]]\nn = 1\ntext = "「おはよう、いい朝だね」"\n\n'
               '[[paras]]\nn = 2\ntext = "「テスト台詞です」"\n\n'
               '[[paras]]\nn = 3\ntext = "今日はいい天気だと彼女は思った。"\n')
        _write(self.wd / "output" / "_work" / "ch002_b1_zh_numbered.md",
               "[P1] “早上好，天气不错呢”\n\n[P2] “这是测试台词。”\n\n[P3] 今天天气很好，她想。\n")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def run_script(self, name, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_align_check_finds_zh_draft(self):
        r = self.run_script("align_check.py", str(self.wd), "--chapter", "ch002", "--block", "b1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("要確認 0 件", r.stdout)

    def test_check_align_finds_zh_draft(self):
        r = self.run_script("check_align.py", str(self.wd), "--chapter", "ch002", "--block", "b1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_check_align_detects_broken_dialogue_zh(self):
        p = self.wd / "output" / "_work" / "ch002_b1_zh_numbered.md"
        _write(p, "[P1] “早上好，天气不错呢”\n\n[P2] 说是测试台词。\n\n[P3] 今天天气很好，她想。\n")
        r = self.run_script("check_align.py", str(self.wd), "--chapter", "ch002", "--block", "b1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("原文は会話だが訳が会話になっていない", r.stdout)


class TestFixLayoutQuotes(unittest.TestCase):
    def test_zh_quotes_detected_with_params(self):
        import fix_layout
        text = "“？！\n地の文。\n\n後続。\n"
        issues = fix_layout.analyze(text, qopen="“", qclose="”")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "不閉じ")

    def test_default_quotes_unchanged(self):
        import fix_layout
        text = "「？！\n地の文。\n\n後続。\n"
        issues = fix_layout.analyze(text)
        self.assertEqual(len(issues), 1)


if __name__ == "__main__":
    unittest.main()
