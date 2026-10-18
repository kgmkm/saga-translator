"""検収の完了記録（review --done）と status の表示のテスト。

検収リストの生成だけでは「読んだ」ことにならないため、--done を明示した
ときだけ記録する。status は記録があれば「検収済み」と出す。
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

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402
import saga  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, assembled: bool = True) -> Path:
    work = base / "work"
    man = 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n'
    if assembled:
        man += ('assembled_at = "2026-01-02T00:00:00+00:00"\n'
                'assembled_outputs = ["output/ch001_ja.md"]\n')
    _write(work / "manifest.toml", man)
    _lib.save_paras(work, "ch001", {"paras": [{"n": 1, "text": "原文一。"}]})
    meta = {"work": {"title": "合成作品"},
            "chapters": [{"id": "ch001", "title": "原題", "status": "done",
                          "blocks": [{"id": "b1", "paras": [1, 1], "status": "emitted",
                                      "translation_done_at": "2026-01-01T00:00:00+00:00"}]}]}
    if assembled:
        _write(work / "output" / "ch001_ja.md", "訳題\n\n訳一。\n")
    _lib.save_chapters_meta(work, meta)
    _write(work / "output" / "ch001" / "b1_ja.md", "訳一。\n")
    _write(work / "analysis" / "titles.toml",
           'title_src = "合成作品"\ntitle_ja = "合成作品"\n\n'
           '[[chapters]]\nid = "ch001"\nsrc = "原題"\nja = "訳題"\n')
    return work


def _status(work: Path) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        saga.cmd_status(work)
    return buf.getvalue()


class TestReviewDone(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_done_records_review_in_meta(self):
        work = _make_work(self.td)
        with contextlib.redirect_stdout(io.StringIO()):
            saga._mark_review_done(work, 2, "全段落を照合")
        meta = _lib.load_chapters_meta(work)
        rev = meta["work"]["review"]
        self.assertEqual(rev["wave"], 2)
        self.assertTrue(rev["done_at"])
        self.assertEqual(rev["note"], "全段落を照合")

    def test_status_shows_reviewed(self):
        work = _make_work(self.td)
        with contextlib.redirect_stdout(io.StringIO()):
            saga._mark_review_done(work, 2, "")
        out = _status(work)
        self.assertIn("検収済み: wave 2", out)

    def test_status_shows_unreviewed_by_default(self):
        work = _make_work(self.td)
        out = _status(work)
        self.assertIn("検収は未実施", out)

    def test_status_asks_recheck_when_renamed_after_review(self):
        work = _make_work(self.td)
        with contextlib.redirect_stdout(io.StringIO()):
            saga._mark_review_done(work, 2, "")
        man = _lib.load_structured(work / "manifest")
        # 検収より後に改名された状態（done_at は実行時の now なので未来日で代用）
        man["derivatives_stale_at"] = "2999-01-01T00:00:00+00:00"
        _lib.save_structured(work / "manifest", man)
        out = _status(work)
        self.assertIn("再検収が必要", out)


if __name__ == "__main__":
    unittest.main()
