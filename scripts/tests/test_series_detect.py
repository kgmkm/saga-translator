"""連作候補の検出（series_detect）と ingest の系列決定記録の回帰テスト。

合成データ・一時ディレクトリのみ。実プロファイルには触れない。
CLI（ingest）はサブプロセスで実行する。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402
import series_detect  # noqa: E402

PYTHON = sys.executable or "python3"

# 合成の本文（5段落以上にして行単位フォールバックを避ける）。
SRC_TEXT = (
    "第一章 出发\n\n"
    "天空阴沉沉的，她站在门口。\n\n"
    "远处传来低沉的钟声，他握紧了手中的剑。\n\n"
    "第二章 相遇\n\n"
    "第二天清晨，她出现在客栈门前。\n\n"
    "风停了，两个人都没有说话。\n"
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _src_text(author: str, title: str) -> str:
    return f"---\ntitle: {title}\nauthor: {author}\n---\n\n" + SRC_TEXT


def _make_work(saga: Path, name: str, author: str, title: str, via: str = "manifest") -> Path:
    """既存 work の合成フィクスチャ。via=manifest / chapters_meta。"""
    d = saga / name
    if via == "manifest":
        _write(d / "manifest.toml",
               f'work_id = "{name}"\ntitle = "{title}"\nauthor = "{author}"\nlang = "zh"\ndst = "ja"\n')
    else:
        _write(d / "chapters_meta.toml",
               f'[work]\nwork_id = "{name}"\ntitle = "{title}"\nauthor = "{author}"\nlang = "zh"\n\n'
               '[[chapters]]\nid = "ch001"\ntitle = ""\nn_paras = 5\nblocks = []\nstatus = "pending"\n')
    return d


class TestFindCandidates(unittest.TestCase):
    """find_candidates 直接呼び出しの検出ロジック（①②）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.saga = self.td / "_saga"

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_same_author_via_manifest(self):
        """① 既存 work の manifest の作者が一致する入力を候補にする。"""
        _make_work(self.saga, "1110001", "合成作者", "古い合成物語 第一部")
        _make_work(self.saga, "1110009", "別の作者", "無関係な物語")
        src = self.td / "1110002-新しい合成物語.txt"
        _write(src, _src_text("合成作者", "新しい合成物語"))

        cands = series_detect.find_candidates(src, self.saga)

        self.assertEqual([c["work_id"] for c in cands], ["1110001"])
        c = cands[0]
        self.assertEqual(c["author"], "合成作者")
        self.assertEqual(c["title"], "古い合成物語 第一部")
        self.assertEqual(c["path"], str(self.saga / "1110001"))
        self.assertIn("作者の一致", c["reason"])

    def test_same_author_via_chapters_meta(self):
        """① chapters_meta の work テーブルからも作者/題を読む。"""
        _make_work(self.saga, "1210001", "合成作者", "森の合成譚", via="chapters_meta")
        src = self.td / "1210002-旅立ちの朝.txt"
        _write(src, _src_text("合成作者", "旅立ちの朝"))

        cands = series_detect.find_candidates(src, self.saga)

        self.assertEqual([c["work_id"] for c in cands], ["1210001"])
        self.assertIn("作者の一致", cands[0]["reason"])
        self.assertEqual(cands[0]["title"], "森の合成譚")

    def test_unrelated_works_not_candidates(self):
        """② 無関係な作品は候補にしない。"""
        _make_work(self.saga, "2020001", "別の作者", "星界の門")
        _make_work(self.saga, "2020002", "別の作者", "海辺の日々", via="chapters_meta")
        src = self.td / "2020003-合成未来記.txt"
        _write(src, _src_text("合成作者", "合成未来記"))

        self.assertEqual(series_detect.find_candidates(src, self.saga), [])

    def test_ingest_target_excluded(self):
        """取り込み対象そのもの（同じ work 名）は候補にしない。"""
        # 入力名から導かれる work 名は 101-p2（ingest と同じ規則）。
        _make_work(self.saga, "101-p2", "合成作者", "合成未来記 第二部")
        _make_work(self.saga, "1010002", "別の作者", "星の記憶")
        src = self.td / "101-合成未来記【2】.txt"
        _write(src, _src_text("合成作者", "合成未来記 第二部"))

        cands = series_detect.find_candidates(src, self.saga)

        self.assertEqual(cands, [])
        self.assertNotIn(str(self.saga / "101-p2"), [c["path"] for c in cands])


class TestIngestSeriesDecision(unittest.TestCase):
    """ingest CLI の候補表示と決定記録（③〜⑥）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.saga = self.td / "_saga"
        self.src = self.td / "101-合成未来記【2】.txt"
        _write(self.src, _src_text("合成作者", "合成未来記 第二部"))

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _run_ingest(self, *flags: str) -> tuple[subprocess.CompletedProcess, Path | None]:
        r = subprocess.run(
            [PYTHON, str(SCRIPTS / "ingest.py"), str(self.src),
             "-o", str(self.saga), "--dst", "ja", *flags],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        work = self.saga / "101-p2"  # 入力名から導かれる work 名
        return r, (work if work.exists() else None)

    def _manifest(self, work: Path) -> dict:
        return _lib.load_structured(work / "manifest")

    def test_standalone_records_decision(self):
        """③ --standalone で series_decision=standalone。"""
        _make_work(self.saga, "1010001", "合成作者", "合成未来記 第一部")
        r, work = self._run_ingest("--standalone")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIsNotNone(work)
        man = self._manifest(work)
        self.assertEqual(man.get("series_decision"), "standalone")
        self.assertNotIn("series_from", man)

    def test_series_from_records_carry(self):
        """④ --series-from で series_decision=carry＋対象 work の記録。"""
        prev = _make_work(self.saga, "1010001", "合成作者", "合成未来記 第一部")
        r, work = self._run_ingest("--series-from", str(prev))
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        man = self._manifest(work)
        self.assertEqual(man.get("series_decision"), "carry")
        self.assertEqual(Path(man.get("series_from") or "").resolve(), prev.resolve())

    def test_undecided_when_candidates_exist(self):
        """⑤ 候補あり・フラグ無しは exit 0 で undecided と案内。"""
        _make_work(self.saga, "1010001", "合成作者", "合成未来記 第一部")
        r, work = self._run_ingest()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        man = self._manifest(work)
        self.assertEqual(man.get("series_decision"), "undecided")
        cands = man.get("series_candidates") or []
        self.assertEqual([c["work_id"] for c in cands], ["1010001"])
        # 候補はパス・推定根拠つきで記録される（作者一致＋タイトル共通プレフィックス）。
        self.assertEqual(cands[0]["path"], str(self.saga / "1010001"))
        self.assertIn("作者の一致", cands[0]["reason"])
        self.assertIn("タイトルの共通プレフィックス", cands[0]["reason"])
        # 表示と案内。
        self.assertIn("連作の候補", r.stdout)
        self.assertIn("undecided", r.stdout)
        self.assertIn("--standalone", r.stdout)
        self.assertIn("--series-from", r.stdout)

    def test_no_candidates_no_decision(self):
        """⑥ 候補なしなら decision を記録しない。"""
        _make_work(self.saga, "1010001", "別の作者", "星界の門")
        r, work = self._run_ingest()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        man = self._manifest(work)
        self.assertNotIn("series_decision", man)
        self.assertNotIn("series_candidates", man)
        self.assertNotIn("連作の候補", r.stdout)

    def test_both_flags_rejected(self):
        """--series-from と --standalone の同時指定はエラー。"""
        _make_work(self.saga, "1010001", "合成作者", "合成未来記 第一部")
        r, work = self._run_ingest("--standalone", "--series-from", str(self.saga / "1010001"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("同時に指定", r.stderr)
        self.assertIsNone(work)

    def test_series_from_missing_rejected(self):
        """--series-from の実在確認。無ければ exit 1。"""
        r, work = self._run_ingest("--series-from", str(self.td / "no_such_work"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("見つかりません", r.stderr)
        self.assertIsNone(work)


if __name__ == "__main__":
    unittest.main()
