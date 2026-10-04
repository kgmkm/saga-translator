"""saga.py の工程計測（timed_stage / timeline.jsonl）のテスト。

合成ワークで saga.py CLI をサブプロセス実行し、
  - prepare / analyze-commit / commit が timeline.jsonl に stage 付きで残ること
  - commit 失敗時に reason が種類別に記録されること
を検査する。作品固有情報は一切使わない。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
SAGA = SCRIPTS / "saga.py"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, lang: str = "zh", dst: str = "ja") -> Path:
    """最小の翻訳可能ワーク（合成データのみ）。"""
    work = base / "test_work"
    _write(work / "manifest.toml", f'lang = "{lang}"\ndst = "{dst}"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}, {id="b2", paras=[4,5]}]

[[chapters]]
id = "ch002"
blocks = [{id="b1", paras=[1,1]}]
""")
    _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "第一段。测试内容。"

[[paras]]
n = 2
text = "第二段。她笑着说。"

[[paras]]
n = 3
text = "第三段。星辰闪烁在夜空中。"

[[paras]]
n = 4
text = "第四段。"

[[paras]]
n = 5
text = "第五段。他匆匆离开。"
""")
    _write(work / "chapters" / "ch002.paras.toml", """\
[[paras]]
n = 1
text = "第二章。继续。"
""")
    _write(work / "analysis" / "characters-[ネタバレあり].toml", """\
[[characters]]
name_src = "林霖"
name_ja = "林霖"
ruby = "りんりん"
first_person = "私"
second_person = "あなた"
""")
    _write(work / "analysis" / "terms.toml", """\
[[terms]]
term_src = "灵氣"
term_ja = "霊気"
""")
    _write(work / "analysis" / "plot-[ネタバレあり].toml", """\
[[entries]]
block = "ch001/b1"
text = "林霖が登場。"
""")
    return work


def _make_draft(work: Path, cid: str, bid: str, paras: list[tuple[int, str]]) -> Path:
    draft = work / "output" / "_work" / f"{cid}_{bid}_ja_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"[P{n:03d}] {t}" for n, t in paras]
    draft.write_text("\n\n".join(lines), encoding="utf-8", newline="\n")
    return draft


def _run_saga(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SAGA), *args],
        capture_output=True, text=True, cwd=str(cwd),
    )


def _timeline(work: Path) -> list[dict]:
    p = work / "logs" / "timeline.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _stages(work: Path) -> list[str]:
    return [e["stage"] for e in _timeline(work)]


class TestSagaStageTimeline(unittest.TestCase):
    """各サブコマンドが timeline.jsonl に stage 付きで1行ずつ書く。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_prepare_records_stage_with_mode(self):
        """prepare が stage=prepare、mode=translation で1行記録する。"""
        r = _run_saga("prepare", str(self.work), "--mode", "translation", cwd=self.td)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entries = [e for e in _timeline(self.work) if e["stage"] == "prepare"]
        self.assertEqual(len(entries), 1, _stages(self.work))
        e = entries[0]
        self.assertEqual(e["exit_code"], 0)
        self.assertEqual(e["mode"], "translation")
        self.assertEqual(e["chapter"], "ch001")
        self.assertEqual(e["block"], "b1")
        self.assertIn("duration_ms", e)

    def test_prepare_analysis_mode_recorded(self):
        """prepare --mode analysis は mode=analysis を記録する。"""
        r = _run_saga("prepare", str(self.work), "--mode", "analysis", cwd=self.td)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        e = next(e for e in _timeline(self.work) if e["stage"] == "prepare")
        self.assertEqual(e["mode"], "analysis")

    def test_prepare_failure_still_recorded(self):
        """失敗した prepare（全ブロック確定済み等）も timeline に残る。"""
        # 全ブロックに出力を作って prepare を失敗させる
        for cid in ("ch001", "ch002"):
            out_dir = self.work / "output" / cid
            out_dir.mkdir(parents=True, exist_ok=True)
            for bid in ("b1", "b2"):
                (out_dir / f"{bid}_ja.md").write_text("x\n", encoding="utf-8")
        r = _run_saga("prepare", str(self.work), cwd=self.td)
        self.assertNotEqual(r.returncode, 0)
        entries = [e for e in _timeline(self.work) if e["stage"] == "prepare"]
        self.assertEqual(len(entries), 1)
        self.assertNotEqual(entries[0]["exit_code"], 0)

    def test_analyze_commit_records_stage(self):
        """analyze-commit が timeline に残る。"""
        delta = self.td / "delta.toml"
        delta.write_text(
            'block = "ch001/b1"\n'
            '[[plot_delta]]\n'
            'block = "ch001/b1"\n'
            'text = "要点。"\n',
            encoding="utf-8", newline="\n",
        )
        r = _run_saga("analyze-commit", str(self.work), "--delta", str(delta), cwd=self.td)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entries = [e for e in _timeline(self.work) if e["stage"] == "analyze-commit"]
        self.assertEqual(len(entries), 1, _stages(self.work))
        self.assertEqual(entries[0]["exit_code"], 0)
        self.assertEqual(entries[0]["reason"], "success")

    def test_commit_success_records_stage_and_success_reason(self):
        """成功 commit は stage=commit, reason=success, commit_id 付きで1行。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "一段目の訳。"), (2, "二段目の訳。"), (3, "三段目の訳。"),
        ])
        r = _run_saga("commit", str(self.work), "--chapter", "ch001",
                      "--block", "b1", "--model", "test", cwd=self.td)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entries = [e for e in _timeline(self.work) if e["stage"] == "commit"]
        self.assertEqual(len(entries), 1, _stages(self.work))
        e = entries[0]
        self.assertEqual(e["exit_code"], 0)
        self.assertEqual(e["reason"], "success")
        self.assertEqual(e["chapter"], "ch001")
        self.assertEqual(e["block"], "b1")
        self.assertTrue(e.get("commit_id"))

    def test_commit_align_failure_reason(self):
        """段落欠落（align fatal）の失敗は reason=align になる。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "一段目。"), (3, "三段目。"),  # P2 欠落
        ])
        r = _run_saga("commit", str(self.work), "--chapter", "ch001",
                      "--block", "b1", cwd=self.td)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        e = next(e for e in _timeline(self.work) if e["stage"] == "commit")
        self.assertEqual(e["exit_code"], 1)
        self.assertEqual(e["reason"], "align")

    def test_commit_residue_failure_reason(self):
        """残渣候補を含む失敗は reason=residue になる。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "夜の星辰が輝く。"), (2, "二段目の訳。"), (3, "三段目の訳。"),
        ])
        r = _run_saga("commit", str(self.work), "--chapter", "ch001",
                      "--block", "b1", cwd=self.td)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        e = next(e for e in _timeline(self.work) if e["stage"] == "commit")
        self.assertEqual(e["reason"], "residue")

    def test_commit_draft_missing_reason(self):
        """ドラフト不在の失敗は reason=draft_missing。"""
        r = _run_saga("commit", str(self.work), "--chapter", "ch001",
                      "--block", "b1", cwd=self.td)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        e = next(e for e in _timeline(self.work) if e["stage"] == "commit")
        self.assertEqual(e["reason"], "draft_missing")

    def test_commit_locked_reason(self):
        """ロック中の commit（exit 3）は reason=locked で残る。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "一段目の訳。"), (2, "二段目の訳。"), (3, "三段目の訳。"),
        ])
        lock = self.work / "_work" / ".lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("99999\n", encoding="utf-8")
        r = _run_saga("commit", str(self.work), "--chapter", "ch001",
                      "--block", "b1", cwd=self.td)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        e = next(e for e in _timeline(self.work) if e["stage"] == "commit")
        self.assertEqual(e["exit_code"], 3)
        self.assertEqual(e["reason"], "locked")

    def test_commit_failures_are_one_line_each(self):
        """commit を2回失敗させると2行（種類別）残る。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "一段目。"), (3, "三段目。"),
        ])
        _run_saga("commit", str(self.work), "--chapter", "ch001", "--block", "b1", cwd=self.td)
        _make_draft(self.work, "ch001", "b2", [
            (4, "四段目。"),  # P5 欠落
        ])
        _run_saga("commit", str(self.work), "--chapter", "ch001", "--block", "b2", cwd=self.td)
        entries = [e for e in _timeline(self.work) if e["stage"] == "commit"]
        self.assertEqual(len(entries), 2, _stages(self.work))
        self.assertTrue(all(e["reason"] == "align" for e in entries))

    def test_status_and_review_recorded(self):
        """status / review も timeline に残る（軽量工程）。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "一段目の訳。"), (2, "二段目の訳。"), (3, "三段目の訳。"),
        ])
        # status
        r = _run_saga("status", str(self.work), cwd=self.td)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # commit してから review（確定ブロックが必要）
        _run_saga("commit", str(self.work), "--chapter", "ch001", "--block", "b1", cwd=self.td)
        r = _run_saga("review", str(self.work), cwd=self.td)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stages = _stages(self.work)
        self.assertIn("status", stages)
        self.assertIn("review", stages)


class TestClassifyCommitFailureUnit(unittest.TestCase):
    """_classify_commit_failure の分類ユニットテスト（report.json 直読み）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _classify(self, stages: dict) -> str:
        import sys as _sys
        _sys.path.insert(0, str(SCRIPTS))
        from saga import _classify_commit_failure
        snap = self.td / "snap"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "report.json").write_text(
            json.dumps({"stages": stages}, ensure_ascii=False), encoding="utf-8")
        return _classify_commit_failure(snap)

    def test_align_fatal(self):
        self.assertEqual(self._classify({"align": {"fatal": 2, "confirm_unacked": 0}}), "align")

    def test_confirm_unacked(self):
        self.assertEqual(self._classify({"align": {"fatal": 0, "confirm_unacked": 1}}), "confirm_unacked")

    def test_qa_merged(self):
        self.assertEqual(self._classify({"qa": {"pass": False, "checks": {"merged_paragraphs": False}}}),
                         "merged_paragraphs")

    def test_qa_non_jis(self):
        self.assertEqual(self._classify({"qa": {"pass": False, "checks": {"non_jis_han": False}}}),
                         "non_jis")

    def test_qa_generic(self):
        self.assertEqual(self._classify({"qa": {"pass": False, "checks": {"paragraph_sequence": False}}}),
                         "qa")

    def test_residue(self):
        self.assertEqual(self._classify({"residue": {"candidates": 3, "errors": 0}}), "residue")
        self.assertEqual(self._classify({"residue": {"errors": 1}}), "residue")

    def test_missing_report_is_other(self):
        import sys as _sys
        _sys.path.insert(0, str(SCRIPTS))
        from saga import _classify_commit_failure
        self.assertEqual(_classify_commit_failure(self.td / "nope"), "other")


if __name__ == "__main__":
    unittest.main()