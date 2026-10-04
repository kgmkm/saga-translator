"""review の実訳読取（text_scope 正本）と抜き取りの再現性・限界の明示のテスト。

#15 / P0-6（後半）:
- review が確定出力から [Pnnn] を文字列探索すると、通常の emit（マーカーを
  消す）では「該当段落なし」になり実訳を読めない。
- 抜き取りの乱数が hash() 由来だとプロセスごとに変わり（PYTHONHASHSEED）、
  同じ work・同じ wave でも再現しない。
- 抜き取りは補助であり、機械の PASS で品質を保証したことにはしない。

実訳は text_scope の正本（done → 確定ブロック出力の順）から段落番号で引く。
seed は work + wave から決定的に導き、出力に明記する。限界の明示文は出力の
冒頭に固定で入れる。

テスト・コメントには作品固有の情報を含めない。合成データのみ。
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

PYTHON = sys.executable

# 限界の明示（文言固定。仕様どおりの文言を実装とは独立にここへ固定して検査する）
LIMITATION = (
    "この抜き取りは補助です。意味・否定・話者・固有名・声・自然さは、"
    "機械の PASS では保証されません。必ず目視で確認してください。"
)

_SEED_RE = re.compile(r"^seed: ([0-9a-f]{16})$", re.M)
_SEL_RE = re.compile(r"^選定: (.+)$", re.M)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, name: str = "synth_review_work") -> Path:
    """合成の review 用 work。監査記録は置かず、確定出力はマーカー無し。"""
    work = base / name
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml",
           "[[chapters]]\n"
           'id = "ch001"\n'
           'blocks = [{id="b1", paras=[1,4]}, {id="b2", paras=[5,8]}]\n')
    _write(work / "chapters" / "ch001.paras.toml", "".join(
        f'[[paras]]\nn = {i}\ntext = "原文{i}。"\n' for i in range(1, 9)))
    # 確定ブロック出力（通常の emit は [Pnnn] マーカーを消す）
    _write(work / "output" / "ch001" / "b1_ja.md",
           "訳一の甲。\n\n訳一の乙。\n\n訳一の丙。\n\n訳一の丁。\n")
    _write(work / "output" / "ch001" / "b2_ja.md",
           "訳二の甲。\n\n訳二の乙。\n\n訳二の丙。\n\n訳二の丁。\n")
    return work


def _run_review(work: Path, wave: int = 1, sample_ratio: float = 0.1,
                json_output: bool = False) -> tuple[int, str]:
    """cmd_review を呼び、終了コードと stdout+stderr を返す。"""
    from saga import cmd_review
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = cmd_review(work, wave=wave, sample_ratio=sample_ratio,
                          json_output=json_output)
    return int(code or 0), buf.getvalue()


def _run_review_cli(work: Path, *extra: str,
                    hashseed: str = "") -> subprocess.CompletedProcess:
    """saga.py review を別プロセスで実行する（PYTHONHASHSEED を差し替え可能）。"""
    env = dict(os.environ)
    if hashseed:
        env["PYTHONHASHSEED"] = hashseed
    return subprocess.run(
        [PYTHON, str(SCRIPTS / "saga.py"), "review", str(work), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env)


def _read_review(work: Path, wave: int = 1) -> str:
    return (work / "_work" / f"review_wave{wave}.md").read_text(encoding="utf-8")


def _seed_of(content: str) -> str:
    m = _SEED_RE.search(content)
    return m.group(1) if m else ""


def _sampling_section(content: str) -> str:
    """出力の「## 3. 抜き取り」節だけを返す（再現性の比較用）。"""
    out: list[str] = []
    inside = False
    for line in content.splitlines():
        if line.startswith("## 3."):
            inside = True
            continue
        if inside and line == "---":
            break
        if inside:
            out.append(line)
    return "\n".join(out).strip()


class TestReviewCanonical(unittest.TestCase):
    """review: 実訳読取（正本）・固定 seed・限界の明示。"""

    maxDiff = None

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

    def test_reads_translation_from_markerless_emitted_output(self):
        """①[Pnnn] マーカーの無い確定出力から、段落対応で実訳を読む。"""
        code, out = _run_review(self.work, wave=1, sample_ratio=1.0)
        self.assertEqual(code, 0, out)
        content = _read_review(self.work)
        self.assertIn("### ch001/b1 P1", content)
        self.assertIn("訳文: 訳一の甲。", content)
        self.assertIn("訳文: 訳一の丁。", content)
        self.assertIn("訳文: 訳二の乙。", content)
        # 旧実装の失敗（[Pnnn] 探索で見つからない）に戻っていない
        self.assertNotIn("該当段落なし", content)
        self.assertNotIn("訳文: 不明", content)
        # 選択根拠（block/paragraph）が記録されている
        m = _SEL_RE.search(content)
        self.assertIsNotNone(m, "選定（block/paragraph）の記録が無い")
        parts = [p for p in m.group(1).split(", ") if p]
        self.assertTrue(parts)
        for part in parts:
            self.assertRegex(part, r"^ch001/b\d P\d+$")

    def test_reproducible_sampling_across_processes(self):
        """②別プロセス（PYTHONHASHSEED 違い）でも同じ seed・同じ抜き取り。"""
        r1 = _run_review_cli(self.work, "--wave", "1", "--sample-ratio", "0.5",
                             hashseed="0")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        c1 = _read_review(self.work)
        r2 = _run_review_cli(self.work, "--wave", "1", "--sample-ratio", "0.5",
                             hashseed="12345")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        c2 = _read_review(self.work)
        self.assertRegex(_seed_of(c1), r"^[0-9a-f]{16}$")
        self.assertEqual(_seed_of(c1), _seed_of(c2))
        s1 = _sampling_section(c1)
        self.assertTrue(s1, "抜き取り節が空（比較が自明になる）")
        self.assertEqual(s1, _sampling_section(c2))

    def test_limitation_statement_at_top(self):
        """③限界の明示文が出力の冒頭にある（文言固定）。"""
        code, out = _run_review(self.work, wave=1, sample_ratio=0.5)
        self.assertEqual(code, 0, out)
        content = _read_review(self.work)
        self.assertTrue(content.startswith(LIMITATION))
        self.assertEqual(content.splitlines()[0], LIMITATION)
        self.assertLess(content.index(LIMITATION), content.index("# 検収リスト"))
        # コンソール出力にも同じ文言が出る
        self.assertIn(LIMITATION, out)

    def test_old_work_without_audit_records(self):
        """④監査記録（report.json・snapshot）が無い旧 work でも動く。"""
        work = _make_work(self.td, "old_synth_work")
        self.assertFalse((work / "output" / "_work" / "commit").exists())
        self.assertFalse((work / "output" / "_work" / "done").exists())
        code, out = _run_review(work, wave=1, sample_ratio=1.0)
        self.assertEqual(code, 0, out)
        self.assertIn("対象 2 ブロック", out)
        content = _read_review(work)
        self.assertIn("## 1. 機械の指摘（0件）", content)
        self.assertIn("訳文: 訳一の甲。", content)

    def test_unreadable_paragraph_shows_unknown(self):
        """読めない段落は「不明」。読める範囲は読み続ける（旧形式対応）。"""
        work = _make_work(self.td, "broken_synth_work")
        # b2 の確定出力を宣言（paras [5,8] = 4段落）と食い違わせる
        _write(work / "output" / "ch001" / "b2_ja.md", "訳二の甲。\n\n訳二の乙。\n")
        code, out = _run_review(work, wave=1, sample_ratio=1.0)
        self.assertEqual(code, 0, out)
        content = _read_review(work)
        self.assertIn("訳文: 訳一の甲。", content)  # 読めるブロックは読む
        self.assertIn("訳文: 不明", content)        # 読めないブロックは不明
        self.assertNotIn("訳文: 訳二の甲。", content)

    def test_done_numbered_preferred_over_emitted(self):
        """正本の順（done → 確定ブロック出力）: done があればそちらを読む。"""
        work = _make_work(self.td, "done_pref_synth_work")
        _write(work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md",
               "[P001] 正本の一。\n\n[P002] 正本の二。\n\n"
               "[P003] 正本の三。\n\n[P004] 正本の四。\n")
        code, out = _run_review(work, wave=1, sample_ratio=1.0)
        self.assertEqual(code, 0, out)
        content = _read_review(work)
        self.assertIn("訳文: 正本の一。", content)
        self.assertNotIn("訳文: 訳一の甲。", content)  # done が優先（emit 側は読まない）
        self.assertIn("訳文: 訳二の甲。", content)      # done の無いブロックは emit から

    def test_different_wave_changes_seed_and_sampling(self):
        """⑤異なる wave では seed が変わり、抜き取りも変わる。"""
        code1, out1 = _run_review(self.work, wave=1, sample_ratio=0.5)
        self.assertEqual(code1, 0, out1)
        c1 = _read_review(self.work, wave=1)
        code2, out2 = _run_review(self.work, wave=2, sample_ratio=0.5)
        self.assertEqual(code2, 0, out2)
        c2 = _read_review(self.work, wave=2)
        s1, s2 = _seed_of(c1), _seed_of(c2)
        self.assertRegex(s1, r"^[0-9a-f]{16}$")
        self.assertRegex(s2, r"^[0-9a-f]{16}$")
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(_sampling_section(c1), _sampling_section(c2))


if __name__ == "__main__":
    unittest.main()
