"""単章作品の重複出力の解消（--keep-full 互換）と、assemble 後に残る中間物の一覧のテスト。

レビュー P3 / phase12-taskV2。合成データのみ使用し、作品固有情報は含めない。
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

SNAP_NAME = "20260101_120000_000000_ch001_b1"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, chapters: tuple[str, ...] = ("ch001",), with_audit: bool = False) -> Path:
    """全ブロック emit 済みの合成 work（assemble 直前）を作る。

    chapters の数で単章/複数章を切り替える。with_audit=True では監査記録
    （commit の報告・段落対応スナップショット）と退避済み done・renames.jsonl を付ける。
    """
    work = base / "work"
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
    meta_chapters = []
    titles = 'title_src = "合成作品"\ntitle_ja = "合成作品（改題）"\n'
    for cid in chapters:
        meta_chapters.append({
            "id": cid, "title": f"原題{cid}", "status": "done",
            "blocks": [{"id": "b1", "paras": [1, 2], "status": "emitted"}]})
        _lib.save_paras(work, cid, {"paras": [
            {"n": 1, "text": f"原文一（{cid}）。"},
            {"n": 2, "text": f"原文二（{cid}）。"}]})
        _write(work / "output" / cid / "b1_ja.md", f"訳一（{cid}）。\n\n訳二（{cid}）。\n")
        _write(work / "output" / cid / "b1_parallel.md",
               f"> [P001] 原文一（{cid}）。\n\n訳一（{cid}）。\n\n"
               f"> [P002] 原文二（{cid}）。\n\n訳二（{cid}）。\n")
        titles += f'\n[[chapters]]\nid = "{cid}"\nsrc = "原題{cid}"\nja = "訳題{cid}"\n'
    _lib.save_chapters_meta(work, {"work": {"title": "合成作品"}, "chapters": meta_chapters})
    _write(work / "analysis" / "titles.toml", titles)
    if with_audit:
        snap = work / "output" / "_work" / "commit" / SNAP_NAME
        _write(snap / "report.json", '{"commit_id": "4444444444dd"}\n')
        _write(snap / "draft.md", "[P001] 訳一。\n")
        _write(work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md", "[P001] 訳一。\n")
        _write(work / "_work" / "renames.jsonl", '{"from": "旧名", "to": "新名"}\n')
    return work


def _run_assemble(work: Path, *extra: str) -> str:
    """assemble.main を sys.argv 差し替えで実行し、出力を返す。"""
    from assemble import main as assemble_main
    buf = io.StringIO()
    with mock.patch.object(sys, "argv", ["assemble", str(work), *extra]):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            assemble_main()
    return buf.getvalue()


class TestSingleChapterOutputLayout(unittest.TestCase):
    """①②③④⑥: 単章は全文を出さず、複数章は従来どおり両方、--keep-full で互換。目次は常に生成。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_single_chapter_skips_full_and_keeps_chapter(self):
        # ① 単章: ch001_ja.md のみ。全文_訳文.md は出ず、完成記録にも載らない
        work = _make_work(self.td)
        out = _run_assemble(work)
        o = work / "output"
        self.assertTrue((o / "ch001_ja.md").is_file())
        self.assertFalse((o / "全文_訳文.md").exists())
        self.assertIn("--keep-full", out)
        man = _lib.load_structured(work / "manifest")
        outputs = list(man.get("assembled_outputs") or [])
        self.assertIn("output/ch001_ja.md", outputs)
        self.assertNotIn("output/全文_訳文.md", outputs)

    def test_multi_chapter_outputs_chapters_and_full(self):
        # ② 複数章: 従来どおり章ファイルと全文の両方（全文には両章が入る）
        work = _make_work(self.td, ("ch001", "ch002"))
        _run_assemble(work)
        o = work / "output"
        self.assertTrue((o / "ch001_ja.md").is_file())
        self.assertTrue((o / "ch002_ja.md").is_file())
        self.assertTrue((o / "全文_訳文.md").is_file())
        full = (o / "全文_訳文.md").read_text(encoding="utf-8")
        self.assertIn("訳一（ch001）", full)
        self.assertIn("訳一（ch002）", full)
        toc = (o / "目次.md").read_text(encoding="utf-8")
        self.assertIn("[訳題ch001](ch001_ja.md)", toc)
        self.assertIn("[訳題ch002](ch002_ja.md)", toc)
        man = _lib.load_structured(work / "manifest")
        self.assertIn("output/全文_訳文.md", list(man.get("assembled_outputs") or []))

    def test_keep_full_restores_both_for_single_chapter(self):
        # ③ --keep-full: 単章でも従来どおり両方（全文を参照する既存の運用・テストの互換）
        work = _make_work(self.td)
        _run_assemble(work, "--keep-full")
        o = work / "output"
        self.assertTrue((o / "ch001_ja.md").is_file())
        self.assertTrue((o / "全文_訳文.md").is_file())
        full = (o / "全文_訳文.md").read_text(encoding="utf-8")
        self.assertIn("# 合成作品（改題）", full)
        self.assertIn("訳一（ch001）", full)
        man = _lib.load_structured(work / "manifest")
        self.assertIn("output/全文_訳文.md", list(man.get("assembled_outputs") or []))

    def test_toc_always_generated(self):
        # ④ 目次は単章でも常に生成される（リンク先は chNNN_ja.md）
        work = _make_work(self.td)
        _run_assemble(work)
        toc = (work / "output" / "目次.md").read_text(encoding="utf-8")
        self.assertIn("# 合成作品（改題）", toc)
        self.assertIn("[訳題ch001](ch001_ja.md)", toc)

    def test_parallel_single_chapter_skips_full_parallel(self):
        # ⑥ 対訳も同方針: 単章は ch001_parallel.md のみで全文_対訳.md は出ない
        work = _make_work(self.td)
        out = _run_assemble(work, "--parallel")
        o = work / "output"
        self.assertTrue((o / "ch001_parallel.md").is_file())
        self.assertFalse((o / "全文_対訳.md").exists())
        self.assertIn("--keep-full", out)

    def test_parallel_keep_full_for_single_chapter(self):
        # ⑥ --keep-full 併用で全文対訳も従来どおり（対訳の互換）
        work = _make_work(self.td)
        _run_assemble(work, "--parallel", "--keep-full")
        o = work / "output"
        self.assertTrue((o / "ch001_parallel.md").is_file())
        self.assertTrue((o / "全文_対訳.md").is_file())
        par = (o / "全文_対訳.md").read_text(encoding="utf-8")
        self.assertIn("> [P001] 原文一（ch001）。", par)

    def test_parallel_multi_chapter_outputs_both(self):
        # ⑥ 複数章の対訳は従来どおり章対訳と全文対訳の両方
        work = _make_work(self.td, ("ch001", "ch002"))
        _run_assemble(work, "--parallel")
        o = work / "output"
        self.assertTrue((o / "ch001_parallel.md").is_file())
        self.assertTrue((o / "ch002_parallel.md").is_file())
        self.assertTrue((o / "全文_対訳.md").is_file())


class TestRemainingIntermediatesList(unittest.TestCase):
    """⑤ assemble 後に残る中間物（監査記録・退避済み done・renames.jsonl）の一覧。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_listing_includes_audit_records_after_assemble(self):
        work = _make_work(self.td, with_audit=True)
        out = _run_assemble(work)
        self.assertIn("[assemble] 残る中間物:", out)
        # 監査記録（commit の報告・段落対応スナップショット）が載る
        self.assertIn(f"output/_work/commit/{SNAP_NAME}", out)
        self.assertIn("（監査記録）", out)
        # 退避済み done と改名の記録も載る
        self.assertIn("output/_work/done/ch001_b1_ja_numbered.md", out)
        self.assertIn("（退避済み done）", out)
        self.assertIn("_work/renames.jsonl", out)
        self.assertIn("（改名の記録）", out)
        # 一覧に出たものは purge 後も実際に残っている（監査保持契約）
        self.assertTrue((work / "output" / "_work" / "commit" / SNAP_NAME / "report.json").is_file())
        self.assertTrue((work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md").is_file())
        self.assertTrue((work / "_work" / "renames.jsonl").is_file())

    def test_listing_empty_without_intermediates(self):
        # 中間物が無い完成では「なし」と明示する
        work = _make_work(self.td)
        out = _run_assemble(work)
        self.assertIn("[assemble] 残る中間物: なし", out)


if __name__ == "__main__":
    unittest.main()
