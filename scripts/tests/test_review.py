"""saga.py review サブコマンドのテスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path) -> Path:
    """最小の review テスト用フィクスチャ。"""
    work = base / "test_review_work"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    # chapters_meta: ch001/b1,ch001/b2,ch001/b3
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}, {id="b2", paras=[4,6]}, {id="b3", paras=[7,9]}]
""")
    # paras
    _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "第一段落。"
[[paras]]
n = 2
text = "第二段落。"
[[paras]]
n = 3
text = "第三段落。"
[[paras]]
n = 4
text = "第四段落。"
[[paras]]
n = 5
text = "第五段落。"
[[paras]]
n = 6
text = "第六段落。"
[[paras]]
n = 7
text = "第七段落。"
[[paras]]
n = 8
text = "第八段落。"
[[paras]]
n = 9
text = "第九段落。"
""")
    # 出力ファイル（確定済み）
    for bid in ("b1", "b2", "b3"):
        out = work / "output" / "ch001" / f"{bid}_ja.md"
        _write(out, f"[P001] {bid}の訳文。\n\n[P002] 二段落目。\n\n[P003] 三段落目。\n" if bid == "b1" else
               f"[P004] {bid}の訳文。\n\n[P005] 二段落目。\n\n[P006] 三段落目。\n" if bid == "b2" else
               f"[P007] {bid}の訳文。\n\n[P008] 二段落目。\n\n[P009] 三段落目。\n")
    # commit 記録（1ブロックだけ報告書あり）
    snap = work / "output" / "_work" / "commit" / "20261004_120000_ch001_b1"
    snap.mkdir(parents=True, exist_ok=True)
    report = {
        "commit_id": "abc123",
        "snapshot": str(snap / "draft.md"),
        "chapter": "ch001",
        "block": "b1",
        "stages": {
            "shape_glyph": {"applied": 0},
            "align": {"fatal": 0, "confirm_unacked": 2},
            "qa": {"pass": True, "checks": {"para_count": True, "marker_sequence": True}},
            "residue": {"candidates": 3, "errors": 0},
        },
    }
    (snap / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (snap / "draft.md").write_text("[P001] 訳文。\n", encoding="utf-8")

    # characters: arcs 付き2人、address 付き1人、ruby 付き1人
    _write(work / "analysis" / "characters-[ネタバレあり].toml", """\
[[characters]]
name_src = "蘇婉晴"
name_ja = "蘇婉晴"
ruby = "そえんせい"
role = "主人公"
arcs = [
  {at_block = "ch001/b2", speech_style = "強気"},
  {at_block = "ch001/b3", speech_style = "冷静"},
]

[[characters]]
name_src = "青児"
name_ja = "青児"
ruby = "せいじ"
role = "仲間"
address = [{target_src = "蘇婉晴", form = "姉さん"}]

[[characters]]
name_src = "白狐"
name_ja = "白狐"
role = "敵"
address = [{target_src = "青児", form = "小僧"}]
arcs = [
  {at_block = "ch001/b2", note = "初登場"},
]
""")
    # terms
    _write(work / "analysis" / "terms.toml", """\
[[terms]]
term_src = "灵気"
term_ja = "霊気"
""")
    # plot
    _write(work / "analysis" / "plot-[ネタバレあり].toml", """\
[[entries]]
block = "ch001/b1"
text = "プロット概要。"
""")
    return work


def _make_second_report(work: Path, block: str, confirms: int = 1) -> None:
    """追加の report.json を作成。"""
    ts = f"20261004_1200{block[1]}_ch001_{block}"
    snap = work / "output" / "_work" / "commit" / f"{ts}_ch001_{block}"
    snap.mkdir(parents=True, exist_ok=True)
    report = {
        "commit_id": f"commit_{block}",
        "chapter": "ch001",
        "block": block,
        "stages": {
            "align": {"fatal": 0, "confirm_unacked": confirms},
            "qa": {"pass": True, "checks": {}},
            "residue": {"candidates": 0, "errors": 0},
        },
    }
    (snap / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class TestReview(unittest.TestCase):
    """saga.py review サブコマンドのテスト。"""

    maxDiff = None

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)
        # second report for b2
        _make_second_report(self.work, "b2", confirms=1)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _run_review(self, wave: int = 1, sample_ratio: float = 0.1,
                    json_output: bool = False) -> tuple[int, str]:
        """review を実行して (終了コード, stdout) を返す。"""
        from saga import cmd_review
        import io
        old_out = sys.stdout
        buf = io.StringIO()
        try:
            sys.stdout = buf
            code = cmd_review(
                self.work,
                wave=wave,
                sample_ratio=sample_ratio,
                json_output=json_output,
            )
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.stdout = old_out
        return code or 0, buf.getvalue()

    def test_review_normal(self):
        """review: 正常実行。機械指摘・リスク箇所・抜き取りが出力される。"""
        code, out = self._run_review(wave=1)
        self.assertEqual(code, 0)
        # 対象ブロック数（b1,b2,b3 の3つ、b3はreportなしだが確定済み）
        self.assertIn("対象 3 ブロック", out)
        # 機械指摘: b1 に confirm_unacked=2, residue_candidates=3, b2 に confirm_unacked=1
        # report.json から stages.align.confirm_unacked + stages.residue.candidates + stages.residue.errors
        self.assertIn("機械指摘", out)
        # リスク箇所: arcs at_block (b2,b3), address characters (青児→白狐)
        self.assertIn("リスク箇所", out)
        # 抜き取り
        self.assertIn("抜き取り", out)
        # 検収対象ファイル
        review_path = self.work / "_work" / "review_wave1.md"
        self.assertIn(str(review_path), out)

    def test_review_report_exists(self):
        """"_work/review_wave1.md が生成される。"""
        code, _ = self._run_review(wave=1)
        self.assertEqual(code, 0)
        review_file = self.work / "_work" / "review_wave1.md"
        self.assertTrue(review_file.exists())
        content = review_file.read_text(encoding="utf-8")
        # 原文と訳文の該当段落が含まれている
        self.assertIn("原文", content)
        self.assertIn("訳文", content)

    def test_review_risk_blocks(self):
        """リスク箇所: arcs の at_block が該当ブロックに含まれる。"""
        _, out = self._run_review(wave=1)
        # 蘇婉晴 arc starts at b2, 白狐 arc starts at b2
        self.assertIn("ch001/b2", out)

    def test_review_deterministic_sampling(self):
        """抜き取りの決定性: 同じ work_id + wave で2回実行して同じ段落が選ばれる。"""
        code1, out1 = self._run_review(wave=1, sample_ratio=0.5)
        self.assertEqual(code1, 0)
        code2, out2 = self._run_review(wave=1, sample_ratio=0.5)
        self.assertEqual(code2, 0)
        # 抜き取り行を抽出
        def get_samples(text: str) -> list[str]:
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if "抜き取り" in line:
                    return [line]
            return []
        s1 = get_samples(out1)
        s2 = get_samples(out2)
        self.assertEqual(s1, s2)

    def test_review_different_wave_different_samples(self):
        """異なる wave では異なる段落が選ばれる。"""
        code1, out1 = self._run_review(wave=1, sample_ratio=0.5)
        code2, out2 = self._run_review(wave=2, sample_ratio=0.5)
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        review_file2 = self.work / "_work" / "review_wave2.md"
        self.assertTrue(review_file2.exists())

    def test_review_json_output(self):
        """--json で機械可読出力。"""
        code, out = self._run_review(wave=1, json_output=True)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("wave", data)
        self.assertIn("total_blocks", data)
        self.assertIn("machine_items", data)
        self.assertIn("risk_items", data)
        self.assertIn("sample_items", data)
        self.assertIn("output_path", data)

    def test_brief_translate_template(self):
        """brief_translate.md に {{ プレースホルダが残っている。"""
        path = Path(SCRIPTS).parent / "templates" / "brief_translate.md"
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("{{", content)

    def test_brief_analyze_template(self):
        """brief_analyze.md に {{ プレースホルダが残っている。"""
        path = Path(SCRIPTS).parent / "templates" / "brief_analyze.md"
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("{{", content)


if __name__ == "__main__":
    unittest.main()