"""assemble 後の監査記録の保持と完成状態（assembled_at）のテスト（#20）。自作フィクスチャのみ使用。"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402

SNAP_NAME = "20260101_120000_000000_ch001_b1"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_completed_work(base: Path) -> Path:
    """全ブロック emit 済み＋監査記録付きの work を作る（assemble 前の状態）。"""
    work = base / "audit_work"
    # manifest は実運用と同じくテーブル([stats])を末尾に置く（再書込の検証用）
    _write(work / "manifest.toml",
           'title = "合成作品"\nlang = "zh"\ndst = "ja"\nskill_version = "0.0.0-synth"\n'
           "images_missing = []\n\n[stats]\nhan = 10\nkana = 0\nhangul = 0\nlatin = 2\n")
    _lib.save_chapters_meta(work, {"work": {"title": "合成作品"}, "chapters": [
        {"id": "ch001", "title": "", "status": "done", "blocks": [
            {"id": "b1", "paras": [1, 2], "status": "emitted",
             "emitted_at": "2026-01-01T00:00:00+00:00",
             "analysis_started_at": "2026-01-01T00:00:00+00:00",
             "analysis_committed_at": "2026-01-01T00:00:00+00:00",
             "translation": {
                 "model": "synth", "prompt_version": "v1",
                 "translated_at": "2026-01-01T00:00:00+00:00",
                 "src_hash": "1111111111aa", "analysis_src_hash": "2222222222bb",
                 "config_hash": "3333333333cc", "draft_hash": "4444444444dd",
                 "final_hash": "5555555555ee"}},
        ]}]})
    _lib.save_paras(work, "ch001", {"paras": [
        {"n": 1, "text": "原文一。"}, {"n": 2, "text": "原文二。"}]})
    _write(work / "originals" / "source.md", "原文一。\n\n原文二。\n")
    # 確定出力と対訳（assemble が読むブロック出力）
    _write(work / "output" / "ch001" / "b1_ja.md", "訳一。\n\n訳二。\n")
    _write(work / "output" / "ch001" / "b1_parallel.md",
           "> [P001] 原文一。\n\n訳一。\n\n> [P002] 原文二。\n\n訳二。\n")
    # 監査記録: commit の報告（report.json）と段落対応スナップショット（draft.md）
    snap = work / "output" / "_work" / "commit" / SNAP_NAME
    report = {
        "commit_id": "4444444444dd",
        "snapshot": str(snap / "draft.md"),
        "chapter": "ch001", "block": "b1", "revise": False,
        "stages": {"align": {"fatal": 0, "confirm_unacked": 1},
                   "qa": {"pass": True, "checks": {}},
                   "residue": {"candidates": 0, "errors": 0, "pass": True}},
        "hashes": {"src_hash": "1111111111aa", "analysis_src_hash": "2222222222bb",
                   "config_hash": "3333333333cc", "draft_hash": "4444444444dd",
                   "final_hash": "5555555555ee"},
    }
    _write(snap / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    _write(snap / "draft.md", "[P001] 訳一。\n\n[P002] 訳二。\n")
    # 確定ドラフトの退避（done。purge 後も残る契約）
    _write(work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md",
           "[P001] 訳一。\n\n[P002] 訳二。\n")
    return work


def _make_partial_work(base: Path) -> Path:
    """一部のブロックだけ emit 済みの work（assemble 途中状態）。"""
    work = base / "partial_work"
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\n')
    _lib.save_chapters_meta(work, {"work": {"title": "合成作品"}, "chapters": [
        {"id": "ch001", "title": "", "status": "pending", "blocks": [
            {"id": "b1", "paras": [1, 2], "status": "emitted"},
            {"id": "b2", "paras": [3, 4], "status": "pending"},
        ]}]})
    _lib.save_paras(work, "ch001", {"paras": [
        {"n": 1, "text": "原文一。"}, {"n": 2, "text": "原文二。"},
        {"n": 3, "text": "原文三。"}, {"n": 4, "text": "原文四。"}]})
    _write(work / "output" / "ch001" / "b1_ja.md", "訳一。\n\n訳二。\n")
    return work


def _run_assemble(work: Path, *extra: str) -> str:
    """assemble.main を sys.argv 差し替えで実行し、出力を返す。"""
    from assemble import main as assemble_main
    buf = io.StringIO()
    with mock.patch.object(sys, "argv", ["assemble", str(work), *extra]):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            assemble_main()
    return buf.getvalue()


def _call_cmd(fn, *args, **kwargs) -> tuple[int, str]:
    """saga の cmd_* を呼び、終了コードと stdout+stderr を返す。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = fn(*args, **kwargs)
    return int(code or 0), buf.getvalue()


def _cmd_status():
    from saga import cmd_status
    return cmd_status


class TestPurgeKeepsAuditRecords(unittest.TestCase):
    """purge は監査記録（commit の報告・段落対応）と done を残す（#20）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = _make_completed_work(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_purge_keeps_commit_report_and_snapshot(self):
        # 中間物は消える（原文・章ディレクトリ・作業ドラフト）
        _write(self.work / "output" / "_work" / "ch001_b1_ja_numbered.md", "一時ドラフト。\n")
        removed = _lib.purge_source(self.work)
        self.assertIn("originals", removed)
        self.assertIn("chapters", removed)
        snap = self.work / "output" / "_work" / "commit" / SNAP_NAME
        self.assertTrue((snap / "report.json").is_file())
        self.assertTrue((snap / "draft.md").is_file())
        # done（確定ドラフト）は従来どおり残る
        self.assertTrue((self.work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md").is_file())
        # 退避用の一時ディレクトリは残さない
        self.assertFalse((self.work / "output" / "_work_keep").exists())
        # 中間物は消えた
        self.assertFalse((self.work / "originals").exists())
        self.assertFalse((self.work / "chapters").exists())
        self.assertFalse((self.work / "output" / "ch001").exists())
        self.assertFalse((self.work / "output" / "_work" / "ch001_b1_ja_numbered.md").exists())

    def test_purge_keeps_report_hashes_readable(self):
        _lib.purge_source(self.work)
        snap = self.work / "output" / "_work" / "commit" / SNAP_NAME
        data = json.loads((snap / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["hashes"]["final_hash"], "5555555555ee")
        self.assertEqual(data["chapter"], "ch001")

    def test_purge_without_audit_removes_work_dir(self):
        # done も commit も無い work では output/_work ごと消える（従来どおり）
        work = self.tmp / "plain_work"
        _write(work / "manifest.toml", 'title = "x"\n')
        _write(work / "output" / "_work" / "scratch.md", "作業ファイル。\n")
        _lib.purge_source(work)
        self.assertFalse((work / "output" / "_work").exists())


class TestAssembleRecordsCompletion(unittest.TestCase):
    """assemble 成功時に完成状態を manifest へ記録する（#20）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_records_assembled_at_outputs_and_keeps_audit(self):
        work = _make_completed_work(self.tmp)
        out = _run_assemble(work)

        # ⑤ assembled_at の記録（ISO8601）
        man = _lib.load_structured(work / "manifest")
        self.assertIn("assembled_at", man)
        parsed = datetime.fromisoformat(str(man["assembled_at"]))
        self.assertIsNotNone(parsed.tzinfo)
        self.assertIn("完成を記録", out)

        # 出力成果物の一覧
        outputs = list(man.get("assembled_outputs") or [])
        self.assertIn("output/ch001_ja.md", outputs)
        self.assertIn("output/目次.md", outputs)
        self.assertIn("output/全文_訳文.md", outputs)
        for rel in outputs:
            self.assertTrue((work / rel).is_file(), f"{rel} が無い")

        # 既存キーを壊さない（テーブル stats も含む）
        self.assertEqual(man.get("title"), "合成作品")
        self.assertEqual(man.get("skill_version"), "0.0.0-synth")
        self.assertIsInstance(man.get("stats"), dict)
        self.assertEqual(man["stats"].get("han"), 10)

        # ① assemble 後に監査記録（報告・ハッシュ・段落対応）が実在する
        snap = work / "output" / "_work" / "commit" / SNAP_NAME
        report = json.loads((snap / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["hashes"]["draft_hash"], "4444444444dd")
        self.assertTrue((snap / "draft.md").is_file())
        self.assertIn("[P001]", (snap / "draft.md").read_text(encoding="utf-8"))
        self.assertTrue((work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md").is_file())

        # ブロック別出力は削除済み
        self.assertFalse((work / "output" / "ch001").exists())

    def test_parallel_outputs_kept_and_listed(self):
        work = _make_completed_work(self.tmp)
        _run_assemble(work, "--parallel")
        man = _lib.load_structured(work / "manifest")
        outputs = list(man.get("assembled_outputs") or [])
        self.assertIn("output/ch001_parallel.md", outputs)
        self.assertIn("output/全文_対訳.md", outputs)
        self.assertTrue((work / "output" / "ch001_parallel.md").is_file())
        self.assertTrue((work / "output" / "全文_対訳.md").is_file())
        self.assertTrue((work / "output" / "_work" / "commit" / SNAP_NAME / "report.json").is_file())

    def test_partial_assemble_does_not_record_completion(self):
        # 全ブロック未確定の assemble では「完成・中間物削除済み」にしない
        # （記録すると prepare が残りブロックの翻訳を止めてしまう）
        work = _make_partial_work(self.tmp)
        _run_assemble(work)
        man = _lib.load_structured(work / "manifest")
        self.assertNotIn("assembled_at", man)
        self.assertTrue((work / "chapters").exists())
        self.assertTrue((work / "output" / "ch001" / "b1_ja.md").is_file())
        # status も従来どおり未完了として表示する
        code, out = _call_cmd(_cmd_status(), work)
        self.assertEqual(code, 0)
        self.assertIn("翻訳 1/2 ブロック", out)
        self.assertNotIn("完成（assemble", out)


class TestStatusAssembledState(unittest.TestCase):
    """status は assemble 済みを完成として表示し、未完了と誤認しない（#20）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_shows_completed_state(self):
        work = _make_completed_work(self.tmp)
        _run_assemble(work)
        code, out = _call_cmd(_cmd_status(), work)
        self.assertEqual(code, 0)
        self.assertIn("完成（assemble 済み・中間物削除済み）", out)
        self.assertIn("assembled_at", out)
        self.assertIn("output/全文_訳文.md", out)
        self.assertIn("次の作業: なし", out)
        # 未完了 n ブロックの誤認を起こさない
        self.assertNotIn("翻訳 0/", out)

    def test_status_before_assemble_shows_progress(self):
        work = _make_completed_work(self.tmp)
        code, out = _call_cmd(_cmd_status(), work)
        self.assertEqual(code, 0)
        self.assertIn("翻訳 1/1 ブロック", out)
        self.assertNotIn("完成（assemble", out)


class TestPrepareAssembledStop(unittest.TestCase):
    """prepare は assemble 済み work で案内付き非ゼロ停止する（#20）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare(self, work: Path, mode: str = "translation") -> tuple[int, str]:
        from saga import cmd_prepare
        return _call_cmd(cmd_prepare, work, None, None, mode=mode)

    def test_prepare_stops_with_guidance_after_assemble(self):
        work = _make_completed_work(self.tmp)
        _run_assemble(work)
        code, out = self._prepare(work)
        self.assertNotEqual(code, 0)
        self.assertIn("assemble 済みです", out)
        self.assertIn("assembled_at", out)
        self.assertIn("確定し直し", out)
        self.assertIn("再 assemble", out)

    def test_prepare_stops_in_analysis_mode_too(self):
        work = _make_completed_work(self.tmp)
        _run_assemble(work)
        code, out = self._prepare(work, mode="analysis")
        self.assertNotEqual(code, 0)
        self.assertIn("assemble 済みです", out)

    def test_prepare_not_blocked_before_assemble(self):
        # assemble 前は従来の案内（全ブロック確定済み）で止まる。完成扱いはしない。
        work = _make_completed_work(self.tmp)
        code, out = self._prepare(work)
        self.assertNotEqual(code, 0)
        self.assertIn("全ブロック確定済みです", out)
        self.assertNotIn("assemble 済みです", out)


class TestReviewAssembledState(unittest.TestCase):
    """review は assemble 済みでも残した監査記録を読んで動く（#20）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _review(self, work: Path, wave: int = 1) -> tuple[int, str]:
        from saga import cmd_review
        return _call_cmd(cmd_review, work, wave=wave)

    def test_review_after_assemble_reads_audit_records(self):
        work = _make_completed_work(self.tmp)
        _run_assemble(work)
        code, out = self._review(work)
        self.assertEqual(code, 0, out)
        self.assertIn("assemble 済み", out)
        self.assertIn("監査記録", out)
        rev = work / "_work" / "review_wave1.md"
        self.assertTrue(rev.is_file())
        content = rev.read_text(encoding="utf-8")
        self.assertIn("ch001/b1", content)
        self.assertIn("confirm未ack", content)

    def test_review_after_assemble_without_reports_uses_meta(self):
        # 監査記録を残す前の assemble で作られた work でもエラーにならない
        work = _make_completed_work(self.tmp)
        _run_assemble(work)
        shutil.rmtree(work / "output" / "_work" / "commit")
        code, out = self._review(work)
        self.assertEqual(code, 0, out)
        self.assertTrue((work / "_work" / "review_wave1.md").is_file())


if __name__ == "__main__":
    unittest.main()
