"""assemble の段落境界（対訳の [Pn] から取り直す）の回帰テスト。

旧版で確定したブロックは段落内に空行が残ることがあり、全文・章ファイルが
それを段落境界と誤認して段落数が増える（原文と対応しなくなる）。
対訳（bN_parallel.md）の [Pn] から境界を取り直すことを検証する。

合成データのみ使用。
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


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path) -> Path:
    """段落内に空行を含むブロック出力を持つ合成 work（assemble 直前）。"""
    work = base / "work"
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
    _lib.save_paras(work, "ch001", {"paras": [
        {"n": 1, "text": "原文一。"},
        {"n": 2, "text": "原文二。"}]})
    _lib.save_chapters_meta(work, {"work": {"title": "合成作品"}, "chapters": [
        {"id": "ch001", "title": "原題", "status": "done",
         "blocks": [{"id": "b1", "paras": [1, 2], "status": "emitted"}]}]})
    _write(work / "analysis" / "titles.toml",
           'title_src = "合成作品"\ntitle_ja = "合成作品"\n\n'
           '[[chapters]]\nid = "ch001"\nsrc = "原題"\nja = "訳題"\n')
    # ブロック訳: 1段落目が段落内の空行で2つに割れている（旧版の確定物）
    _write(work / "output" / "ch001" / "b1_ja.md",
           "訳一（前半）。\n\n訳一（後半）。\n\n訳二。\n")
    # 対訳: [Pn] で正しい境界を持つ
    _write(work / "output" / "ch001" / "b1_parallel.md",
           "> [P001] 原文一。\n\n訳一（前半）。\n\n訳一（後半）。\n\n"
           "> [P002] 原文二。\n\n訳二。\n")
    return work


def _run(work: Path) -> tuple[int, str]:
    from assemble import main as assemble_main
    buf = io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["assemble", str(work)]):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                assemble_main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


class TestParagraphBounds(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_full_text_uses_parallel_boundaries(self):
        work = _make_work(self.td)
        code, out = _run(work)
        self.assertEqual(code, 0, out)
        body = (work / "output" / "全文_訳文.md").read_text(encoding="utf-8")
        chunks = [c.strip() for c in body.split("\n\n") if c.strip()]
        # 見出し + 章コメント + 2段落（空行で3つに割れない）
        paras = [c for c in chunks if not c.startswith(("#", "<!--"))]
        self.assertEqual(paras, ["訳一（前半）。\n訳一（後半）。", "訳二。"])
        # 章ファイルも同じ境界
        ch = (work / "output" / "ch001_ja.md").read_text(encoding="utf-8")
        self.assertNotIn("訳一（前半）。\n\n訳一（後半）。", ch)

    def test_parallel_is_not_rewritten(self):
        work = _make_work(self.td)
        before = (work / "output" / "ch001" / "b1_parallel.md").read_text(encoding="utf-8")
        _run(work)
        after = (work / "output" / "全文_対訳.md").read_text(encoding="utf-8")
        self.assertIn(before.strip(), after)

    def test_no_boundary_warning_when_parallel_matches(self):
        work = _make_work(self.td)
        _code, out = _run(work)
        self.assertNotIn("段落数が原文と一致しません", out)

    def test_fallback_when_parallel_is_missing(self):
        work = _make_work(self.td)
        (work / "output" / "ch001" / "b1_parallel.md").unlink()
        code, out = _run(work)
        self.assertEqual(code, 0, out)
        # 対訳が無いので従来どおり（分割されたまま）でも組立ては通る
        self.assertTrue((work / "output" / "全文_訳文.md").is_file())


if __name__ == "__main__":
    unittest.main()
