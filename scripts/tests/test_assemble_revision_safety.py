"""assemble の改訂路・冪等化の回帰テスト（G23 / G25 / G26 / AS-3）。

検証対象:
  - 空の組立て（確定済みブロックなし）は exit 1 で停止し、削除も完成記録もしない
  - purge は chapters/*.paras.toml を残す（assemble 後の --revise の受け皿。G23/#59）
  - 単章作品の output/<ch>/ は purge 後も残る（G23/#61）。複数章では従来どおり削除
  - purge の前に成果物の実在・サイズを確認し、不備なら原文を消さずに停止する（AS-1）
  - 全文は既定で出力（--no-full で抑制。G25）、対訳も既定（--no-parallel で抑制。G26）
  - 完成記録に成果物のバイト数が出る（AS-3）

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


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, chapters: tuple[str, ...] = ("ch001",),
               with_parallel: bool = True, titles: bool = True) -> Path:
    """全ブロック確定済みの合成 work（assemble 直前）を作る。

    chapters の数で単章/複数章を切り替える。with_parallel=False では
    ブロック対訳（b1_parallel.md）を置かない（対訳欠落の警告検証用）。
    """
    work = base / "work"
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
    meta_chapters = []
    titles_body = 'title_src = "合成作品"\ntitle_ja = "合成作品（改題）"\n'
    for cid in chapters:
        meta_chapters.append({
            "id": cid, "title": f"原題{cid}", "status": "done",
            "blocks": [{"id": "b1", "paras": [1, 2], "status": "emitted"}]})
        _lib.save_paras(work, cid, {"paras": [
            {"n": 1, "text": f"原文一（{cid}）。"},
            {"n": 2, "text": f"原文二（{cid}）。"}]})
        _write(work / "originals" / "source.md", f"原文一（{cid}）。\n\n原文二（{cid}）。\n")
        _write(work / "output" / cid / "b1_ja.md", f"訳一（{cid}）。\n\n訳二（{cid}）。\n")
        if with_parallel:
            _write(work / "output" / cid / "b1_parallel.md",
                   f"> [P001] 原文一（{cid}）。\n\n訳一（{cid}）。\n\n"
                   f"> [P002] 原文二（{cid}）。\n\n訳二（{cid}）。\n")
        titles_body += f'\n[[chapters]]\nid = "{cid}"\nsrc = "原題{cid}"\nja = "訳題{cid}"\n'
    _lib.save_chapters_meta(work, {"work": {"title": "合成作品"}, "chapters": meta_chapters})
    if titles:
        _write(work / "analysis" / "titles.toml", titles_body)
    return work


def _run_assemble_capture(work: Path, *extra: str) -> tuple[int, str]:
    """assemble.main を sys.argv 差し替えで実行し、(終了コード, 出力) を返す。"""
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


class TestAssembleRevisionSafety(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)


class TestEmptyAssembleFails(TestAssembleRevisionSafety):
    """空の組立ては exit 1 で停止し、何も消さず・完成記録もしない（G23/AS-1）。"""

    def test_empty_assemble_exits_1_and_keeps_everything(self):
        work = _make_work(self.td)
        # ブロック出力を消して「確定済みブロックなし」を作る
        (work / "output" / "ch001" / "b1_ja.md").unlink()
        (work / "output" / "ch001" / "b1_parallel.md").unlink()

        code, out = _run_assemble_capture(work)

        self.assertEqual(code, 1)
        self.assertIn("確定済みブロックなし", out)
        # 何も消していない（originals・chapters は残る）
        self.assertTrue((work / "originals" / "source.md").is_file())
        self.assertTrue((work / "chapters" / "ch001.paras.toml").is_file())
        # 完成記録・出力もしない（目次すら書かれない）
        man = _lib.load_structured(work / "manifest")
        self.assertNotIn("assembled_at", man)
        self.assertFalse((work / "output" / "ch001_ja.md").exists())
        self.assertFalse((work / "output" / "目次.md").exists())
        self.assertFalse((work / "output" / "全文_訳文.md").exists())

    def test_rerun_without_block_outputs_does_not_degrade_chapter_file(self):
        # 一度 assemble 済みの後でブロック出力だけが消えた状態（purge 後の再実行と
        # 同じ状況）で再実行しても、既存の章ファイルを「見出しだけ」で上書きしない
        # （F8-AS-1: 再実行で章ファイルが 9B/縮退に破壊、の再発防止）。
        work = _make_work(self.td)
        code, out = _run_assemble_capture(work, "--keep-source")
        self.assertEqual(code, 0, out)
        ch = work / "output" / "ch001_ja.md"
        good = ch.read_text(encoding="utf-8")
        self.assertIn("訳一（ch001）", good)

        shutil.rmtree(work / "output" / "ch001")  # ブロック出力が無い状態にする
        code, out = _run_assemble_capture(work)

        self.assertEqual(code, 1)
        self.assertIn("確定済みブロックなし", out)
        self.assertEqual(ch.read_text(encoding="utf-8"), good)


class TestPurgeKeepsRevisionSources(TestAssembleRevisionSafety):
    """purge は chapters/*.paras.toml と単章の output/<ch>/ を残す（G23）。"""

    def test_assemble_keeps_chapter_paras_and_single_chapter_outputs(self):
        work = _make_work(self.td)  # 単章
        code, out = _run_assemble_capture(work)
        self.assertEqual(code, 0, out)
        # 段落原文が残る（--revise の受け皿）
        self.assertTrue((work / "chapters" / "ch001.paras.toml").is_file())
        # 単章はブロック訳も残る
        self.assertTrue((work / "output" / "ch001" / "b1_ja.md").is_file())
        # originals は消える（従来どおり）
        self.assertFalse((work / "originals").exists())
        # 章ファイル・全文は出力済み
        self.assertTrue((work / "output" / "ch001_ja.md").is_file())
        self.assertTrue((work / "output" / "全文_訳文.md").is_file())

    def test_assemble_multi_chapter_removes_block_outputs_keeps_paras(self):
        work = _make_work(self.td, ("ch001", "ch002"))
        code, out = _run_assemble_capture(work)
        self.assertEqual(code, 0, out)
        self.assertTrue((work / "chapters" / "ch001.paras.toml").is_file())
        self.assertTrue((work / "chapters" / "ch002.paras.toml").is_file())
        # 複数章は従来どおり章別出力を削除
        self.assertFalse((work / "output" / "ch001").exists())
        self.assertFalse((work / "output" / "ch002").exists())
        self.assertTrue((work / "output" / "ch001_ja.md").is_file())
        self.assertTrue((work / "output" / "全文_訳文.md").is_file())

    def test_purge_source_direct_single_chapter(self):
        work = _make_work(self.td)
        removed = _lib.purge_source(work, keep_parallel=True)
        self.assertNotIn("chapters", removed)
        self.assertTrue((work / "chapters" / "ch001.paras.toml").is_file())
        self.assertTrue((work / "output" / "ch001" / "b1_ja.md").is_file())
        self.assertFalse((work / "originals").exists())

    def test_purge_removes_non_paras_files_in_chapters(self):
        work = _make_work(self.td)
        _write(work / "chapters" / "scratch.txt", "残骸\n")
        removed = _lib.purge_source(work, keep_parallel=True)
        self.assertFalse((work / "chapters" / "scratch.txt").exists())
        self.assertTrue((work / "chapters" / "ch001.paras.toml").is_file())
        self.assertIn("chapters/scratch.txt", removed)


class TestArtifactCheckBeforePurge(TestAssembleRevisionSafety):
    """purge の前に成果物の実在・サイズを確認する（G23/AS-1）。"""

    def test_artifact_problems_detects_missing_and_empty(self):
        from assemble import _artifact_problems
        work = self.td / "w"
        _write(work / "output" / "ok.md", "本文。\n")
        _write(work / "output" / "empty.md", "")
        problems = _artifact_problems(work, ["output/ok.md", "output/empty.md", "output/none.md"])
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("empty.md" in p and "空" in p for p in problems))
        self.assertTrue(any("none.md" in p and "ありません" in p for p in problems))

    def test_assemble_stops_before_purge_when_artifact_broken(self):
        # 成果物の不備（実運用では書込み失敗等）を模す。purge が呼ばれず、
        # 原文（originals・chapters）と完成記録なしが保たれることを確認する。
        work = _make_work(self.td)
        with mock.patch("assemble._artifact_problems",
                        return_value=["output/ch001_ja.md: 成果物が空です(0B)"]):
            with mock.patch("assemble.purge_source") as purge_mock:
                code, out = _run_assemble_capture(work)
        self.assertEqual(code, 1)
        self.assertIn("成果物の確認で停止", out)
        purge_mock.assert_not_called()
        self.assertTrue((work / "originals" / "source.md").is_file())
        self.assertTrue((work / "chapters" / "ch001.paras.toml").is_file())
        self.assertNotIn("assembled_at", _lib.load_structured(work / "manifest"))


class TestOutputDefaults(TestAssembleRevisionSafety):
    """全文・対訳は既定で出力、--no-full / --no-parallel で抑制（G25 / G26 / AS-3）。"""

    def test_full_and_parallel_emitted_by_default(self):
        work = _make_work(self.td)
        code, out = _run_assemble_capture(work)
        self.assertEqual(code, 0, out)
        self.assertTrue((work / "output" / "全文_訳文.md").is_file())
        self.assertTrue((work / "output" / "全文_対訳.md").is_file())
        self.assertNotIn("WARN: 対訳がありません", out)

    def test_no_full_suppresses_full_outputs(self):
        work = _make_work(self.td)
        code, out = _run_assemble_capture(work, "--no-full")
        self.assertEqual(code, 0, out)
        self.assertFalse((work / "output" / "全文_訳文.md").exists())
        self.assertFalse((work / "output" / "全文_対訳.md").exists())
        self.assertTrue((work / "output" / "ch001_ja.md").is_file())
        self.assertTrue((work / "output" / "ch001_parallel.md").is_file())
        self.assertIn("--no-full", out)

    def test_no_parallel_suppresses_parallel_outputs(self):
        work = _make_work(self.td)
        code, out = _run_assemble_capture(work, "--no-parallel")
        self.assertEqual(code, 0, out)
        self.assertFalse((work / "output" / "ch001_parallel.md").exists())
        self.assertFalse((work / "output" / "全文_対訳.md").exists())
        self.assertTrue((work / "output" / "全文_訳文.md").is_file())

    def test_missing_block_parallel_warns(self):
        work = _make_work(self.td, with_parallel=False)
        code, out = _run_assemble_capture(work)
        self.assertEqual(code, 0, out)
        self.assertIn("WARN: 対訳がありません", out)
        self.assertFalse((work / "output" / "全文_対訳.md").exists())
        self.assertTrue((work / "output" / "全文_訳文.md").is_file())

    def test_completion_record_shows_byte_sizes(self):
        work = _make_work(self.td)
        code, out = _run_assemble_capture(work)
        self.assertEqual(code, 0, out)
        self.assertIn("完成を記録", out)
        self.assertIn("出力サイズ:", out)
        self.assertIn("合計", out)
        man = _lib.load_structured(work / "manifest")
        self.assertIn("assembled_at", man)


if __name__ == "__main__":
    unittest.main()
