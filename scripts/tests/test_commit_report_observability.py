"""commit 失敗の可観測性（棚卸し #73/#74/#75）の回帰テスト。

- #73: 失敗時も report.json に検出明細（語・段落の配列）が残り、節が欠けない
       （stdout 明細はスナップショット配下の stdout.log にも残る）
- #74: 中止メッセージに検出語一覧と「次の一手」が入る
- #75: review の _scrape_commit_reports がブロックごとに最新スナップショットのみ読む

合成データのみ使用（実在の作品情報・語彙は使わない）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

# テストの環境独立: 兄弟ディレクトリの拡張 vocab.toml（自動探索）に依存しない
os.environ.setdefault("SAGA_VOCAB_SKIP_AUTO", "1")


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, name: str = "test_work") -> Path:
    """最小の翻訳可能な作品ディレクトリ（zh→ja）。合成語のみ。"""
    work = base / name
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}]
""")
    _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "合成語B闪着光。"

[[paras]]
n = 2
text = "合成語C发光。"

[[paras]]
n = 3
text = "合成語D伸长。"
""")
    return work


def _make_draft(work: Path, paras: list[tuple[int, str]]) -> Path:
    draft = work / "output" / "_work" / "ch001_b1_ja_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("\n\n".join(f"[P{n:03d}] {t}" for n, t in paras),
                     encoding="utf-8", newline="\n")
    return draft


# 合成の拡張 vocab.toml（version "1"）。合成語B=fixed / 合成語C=candidates
_VOCAB_TOML = """\
version = "1"

[[vocab]]
src = "合成語B"
dst = "合成訳B"
kind = "fixed"

[[vocab]]
src = "合成語C"
dst = "合成訳C"
kind = "candidates"
"""


@contextlib.contextmanager
def _vocab_env(*paths: Path):
    """SAGA_VOCAB_PATH を一時的に設定する（終了時に元へ戻す）。"""
    old = os.environ.get("SAGA_VOCAB_PATH")
    os.environ["SAGA_VOCAB_PATH"] = os.pathsep.join(str(p) for p in paths)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("SAGA_VOCAB_PATH", None)
        else:
            os.environ["SAGA_VOCAB_PATH"] = old


def _latest_snap(work: Path) -> Path:
    commit_dir = work / "output" / "_work" / "commit"
    snaps = sorted(p for p in commit_dir.iterdir() if p.is_dir())
    assert snaps, "commit スナップショットがありません"
    return snaps[-1]


class _TempBase(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)


class TestCommitFailureReport(_TempBase):
    """#73/#74: 失敗時も report.json に明細（語・段落）が残り、stdout.log と
    中止メッセージに検出語・次の一手が出る。"""

    def setUp(self):
        super().setUp()
        self.work = _make_work(self.td)
        self.ext = self.td / "ext"
        _write(self.ext / "vocab.toml", _VOCAB_TOML)

    def _commit_capture(self) -> tuple[int, str]:
        from saga import cmd_commit
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        return rc, out.getvalue() + err.getvalue()

    def test_vocab_stop_writes_details_and_log(self):
        """辞書の残渣で停止: report.json に語の配列・stdout.log・中止メッセージ。"""
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成語Cを拭く。"),
                                (3, "合成訳Dが伸びる。")])
        with _vocab_env(self.ext):
            rc, text = self._commit_capture()
        self.assertEqual(rc, 1)
        snap = _latest_snap(self.work)
        report = json.loads((snap / "report.json").read_text(encoding="utf-8"))
        stops = report["stages"]["residue"]["vocab"]["stops"]
        self.assertIsInstance(stops, list)
        self.assertEqual([s["value"] for s in stops], ["合成語C"])
        self.assertEqual(stops[0]["kind"], "candidates")
        self.assertEqual(stops[0]["paragraph"], 2)
        # 失敗しても節が欠けない（#73 C2: QA で止まっても residue が残る）
        self.assertEqual(report["stages"]["align"]["fatal"], [])
        self.assertTrue(report["stages"]["qa"]["pass"])
        self.assertEqual(report["stages"]["residue"]["errors"], [])
        self.assertEqual(report["stages"]["residue"]["candidates"], [])
        # stdout 明細がログファイルにも残る（#73 B1b）
        log = (snap / "stdout.log").read_text(encoding="utf-8")
        self.assertIn("合成語C", log)
        # 中止メッセージに検出語と次の一手（#74）
        self.assertIn("「合成語C", text)
        self.assertIn("次の一手", text)

    def test_align_fatal_writes_details(self):
        """align fatal で停止: report.json に段落付き明細、メッセージに次の一手。"""
        # P2 の [P002] を欠落させる（align の fatal）
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (3, "合成訳Dが伸びる。")])
        rc, text = self._commit_capture()
        self.assertEqual(rc, 1)
        snap = _latest_snap(self.work)
        report = json.loads((snap / "report.json").read_text(encoding="utf-8"))
        fatals = report["stages"]["align"]["fatal"]
        self.assertIsInstance(fatals, list)
        self.assertTrue(fatals)
        self.assertEqual(fatals[0]["kind"], "count_mismatch")
        self.assertEqual(fatals[0]["paragraph"], None)
        self.assertIn("欠落", fatals[0]["detail"])
        # 未到達の工程の節も空の形で書かれている（#73）
        self.assertEqual(report["stages"]["qa"]["pass"], None)
        self.assertEqual(report["stages"]["residue"]["errors"], [])
        # メッセージ: 検出（段落・種別）と次の一手（#74）
        self.assertIn("count_mismatch", text)
        self.assertIn("次の一手", text)
        log = (snap / "stdout.log").read_text(encoding="utf-8")
        self.assertIn("count_mismatch", log)

    def test_qa_failure_keeps_all_sections(self):
        """QA で停止: residue 節が欠けず、failures に段落付き検出が残る。"""
        # 末尾段落の引用を閉じない（QA unclosed_quote）
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成訳Cを拭く。"),
                                (3, "「まだ閉じていない")])
        rc, text = self._commit_capture()
        self.assertEqual(rc, 1)
        snap = _latest_snap(self.work)
        report = json.loads((snap / "report.json").read_text(encoding="utf-8"))
        qa = report["stages"]["qa"]
        self.assertFalse(qa["pass"])
        checks = {f["check"]: f for f in qa["failures"]}
        self.assertIn("unclosed_quote", checks)
        found = checks["unclosed_quote"]["found"]
        self.assertEqual(found[0]["para"], 3)
        # 失敗しても residue 節（配列）が書かれている（#73 C2）
        self.assertEqual(report["stages"]["residue"]["errors"], [])
        self.assertEqual(report["stages"]["residue"]["candidates"], [])
        self.assertIn("unclosed_quote", text)
        self.assertIn("次の一手", text)

    def test_ja2zh_residue_items_mapping(self):
        """ja→zh 残渣の明細変換: 検出語（value）と行（line）が配列に入る（#73）。"""
        from detect.ja2zh import find_ja_residue
        from saga import _residue_finding_items
        items = _residue_finding_items(find_ja_residue("ありがとう。\n彼は笑った。"))
        self.assertTrue(items)
        self.assertTrue(all(i["value"] for i in items))
        self.assertTrue(all(isinstance(i["line"], int) for i in items))
        self.assertTrue(any(i["rule"] == "kana" for i in items))
        self.assertEqual(items[0]["line"], 1)


class TestReviewLatestSnapshotOnly(_TempBase):
    """#75: review の report 収集はブロックごとに最新スナップショットのみ参照する。"""

    def _snap(self, work: Path, name: str, report: dict) -> Path:
        snap = work / "output" / "_work" / "commit" / name
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return snap

    def test_scrape_reads_latest_only(self):
        """旧スナップショットの指摘（解消済み）は拾わない。"""
        work = _make_work(self.td)
        old = {"commit_id": "old", "chapter": "ch001", "block": "b1",
               "stages": {
                   "align": {"fatal": [], "confirm_unacked": [
                       {"kind": "char_ratio", "paragraph": 1, "detail": "旧指摘"}]},
                   "qa": {"pass": True, "checks": {}},
                   "residue": {"errors": [], "candidates": [
                       {"value": "旧語", "line": 1, "context": "", "fix": ""}]}}}
        new = {"commit_id": "new", "chapter": "ch001", "block": "b1",
               "stages": {
                   "align": {"fatal": [], "confirm_unacked": []},
                   "qa": {"pass": True, "checks": {}},
                   "residue": {"errors": [], "candidates": []}}}
        self._snap(work, "20261010_100000_000000_ch001_b1", old)
        self._snap(work, "20261010_110000_000000_ch001_b1", new)
        from saga import _extract_machine_findings, _scrape_commit_reports
        reports = _scrape_commit_reports(work, [("ch001", "b1")])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["commit_id"], "new")
        self.assertEqual(_extract_machine_findings(reports), [])

    def test_scrape_keeps_latest_failure_and_reads_old_int_format(self):
        """最新が失敗なら指摘は残る。旧形式（件数=数値）の report も読める。"""
        work = _make_work(self.td)
        old_ok = {"commit_id": "old", "chapter": "ch001", "block": "b1",
                  "stages": {"align": {"fatal": 0, "confirm_unacked": 0},
                             "qa": {"pass": True, "checks": {}},
                             "residue": {"errors": 0, "candidates": 0}}}
        new_fail = {"commit_id": "new", "chapter": "ch001", "block": "b1",
                    "stages": {"align": {"fatal": 0, "confirm_unacked": 1},
                               "qa": {"pass": True, "checks": {}},
                               "residue": {"errors": 0, "candidates": 0}}}
        self._snap(work, "20261010_100000_000000_ch001_b1", old_ok)
        self._snap(work, "20261010_120000_000000_ch001_b1", new_fail)
        from saga import _extract_machine_findings, _scrape_commit_reports
        reports = _scrape_commit_reports(work, [("ch001", "b1")])
        self.assertEqual([r["commit_id"] for r in reports], ["new"])
        findings = _extract_machine_findings(reports)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "confirm未ack")
        self.assertEqual(findings[0]["count"], 1)

    def test_cmd_review_uses_latest_snapshot(self):
        """cmd_review の出力（検収リスト）から解消済みの指摘が消える。"""
        work = _make_work(self.td)
        _write(work / "output" / "ch001" / "b1_ja.md",
               "[P001] 合成訳Bが光る。\n\n[P002] 合成訳Cを拭く。\n\n[P003] 合成訳Dが伸びる。\n")
        old = {"commit_id": "old", "chapter": "ch001", "block": "b1",
               "stages": {"align": {"fatal": [], "confirm_unacked": []},
                          "qa": {"pass": True, "checks": {}},
                          "residue": {"errors": [], "candidates": [
                              {"value": "旧語", "line": 1, "context": "", "fix": ""}]}}}
        new = {"commit_id": "new", "chapter": "ch001", "block": "b1",
               "stages": {"align": {"fatal": [], "confirm_unacked": []},
                          "qa": {"pass": True, "checks": {}},
                          "residue": {"errors": [], "candidates": []}}}
        self._snap(work, "20261010_100000_000000_ch001_b1", old)
        self._snap(work, "20261010_110000_000000_ch001_b1", new)
        from saga import cmd_review
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_review(work, wave=1, sample_ratio=0.1)
        self.assertEqual(rc, 0)
        review = (work / "_work" / "review_wave1.md").read_text(encoding="utf-8")
        self.assertIn("## 1. 機械の指摘（0件）", review)
        self.assertNotIn("旧語", review)


if __name__ == "__main__":
    unittest.main()
