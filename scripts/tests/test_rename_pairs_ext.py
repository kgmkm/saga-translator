# -*- coding: utf-8 -*-
"""rename_terms.py のペア入力拡張（F4-B5 / F5-H3）のテスト。

- 複数 --term / --to（順番に対応）と --pairs-json を受け付ける
- 置換は「旧訳の長い順」に自動ソートされる（部分文字列の衝突を防ぐ）
- 1ペア時は従来どおりの単票レポート（rename_last.json）を保つ
- 不正な入力（件数不一致・空文字・形式不正・モード同時指定）はエラー

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

RENAME = SCRIPTS / "rename_terms.py"
DONE = "output/_work/done/ch001_b1_ja_numbered.md"


def _run(args: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(RENAME), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, r.stdout + r.stderr


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "work"
        (self.work / "output" / "_work" / "done").mkdir(parents=True)
        (self.work / "manifest.toml").write_text(
            'title = "T"\nlang = "zh"\ndst = "ja"\n', encoding="utf-8")
        # 「魔力灌注」と「灌注」が併存（長い方を先に置換しないと壊れる例）
        (self.work / DONE).write_text(
            "[P1] 魔力灌注を行い、灌注を繰り返した。\n\n[P2] 月光を見た。\n",
            encoding="utf-8", newline="\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self) -> str:
        return (self.work / DONE).read_text(encoding="utf-8")


class TestRenamePairsExt(_Base):
    def test_multiple_term_applied_longest_first(self):
        """複数 --term/--to。長い旧訳（魔力灌注）が先に処理される。"""
        code, out = _run([str(self.work),
                          "--term", "灌注", "--to", "注ぎ込む",
                          "--term", "魔力灌注", "--to", "魔力注入",
                          "--apply"])
        self.assertEqual(code, 0, out)
        text = self._read()
        self.assertIn("魔力注入", text)
        self.assertIn("注ぎ込む", text)
        self.assertNotIn("魔力注ぎ込む", text)  # 短い方を先に処理すると壊れる
        self.assertNotIn("灌注", text)

    def test_single_term_keeps_single_report_format(self):
        """1ペア（--term/--to 1回）は従来どおり単票レポート。"""
        code, out = _run([str(self.work), "--term", "月光", "--to", "ムーンライト",
                          "--apply"])
        self.assertEqual(code, 0, out)
        rep = json.loads((self.work / "logs" / "rename_last.json")
                         .read_text(encoding="utf-8"))
        self.assertNotIn("pairs", rep)
        self.assertEqual(rep["term"], "月光")
        self.assertIn("ムーンライト", self._read())

    def test_pairs_json_applied(self):
        """--pairs-json（配列・dict 混在形式）を適用する。"""
        pf = self.tmp / "pairs.json"
        pf.write_text(json.dumps([
            ["月光", "ムーンライト"],
            {"term": "灌注", "to": "注ぎ込む"},
        ], ensure_ascii=False), encoding="utf-8")
        code, out = _run([str(self.work), "--pairs-json", str(pf), "--apply"])
        self.assertEqual(code, 0, out)
        text = self._read()
        self.assertIn("ムーンライト", text)
        self.assertIn("注ぎ込む", text)
        rep = json.loads((self.work / "logs" / "rename_last.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(len(rep["pairs"]), 2)
        self.assertTrue(rep["applied"])

    def test_pairs_json_invalid_form_errors(self):
        pf = self.tmp / "bad.json"
        pf.write_text('[["a"]]', encoding="utf-8")
        code, out = _run([str(self.work), "--pairs-json", str(pf), "--apply"])
        self.assertEqual(code, 1, out)
        self.assertIn("不正", out)

    def test_term_count_mismatch_errors(self):
        code, out = _run([str(self.work), "--term", "a", "--term", "b",
                          "--to", "c", "--apply"])
        self.assertEqual(code, 1, out)
        self.assertIn("同数", out)

    def test_empty_term_errors(self):
        code, out = _run([str(self.work), "--term", "", "--to", "x", "--apply"])
        self.assertEqual(code, 1, out)

    def test_modes_conflict_errors(self):
        pf = self.tmp / "p.json"
        pf.write_text("[]", encoding="utf-8")
        code, out = _run([str(self.work), "--pairs-json", str(pf),
                          "--term", "a", "--to", "b"])
        self.assertEqual(code, 1, out)
        self.assertIn("同時に指定できません", out)

    def test_same_length_keeps_input_order(self):
        """同じ長さの旧訳は入力順のまま（安定ソート）。"""
        code, out = _run([str(self.work), "--term", "月光", "--to", "ツキ",
                          "--term", "灌注", "--to", "ソソ", "--apply"])
        self.assertEqual(code, 0, out)
        lines = [json.loads(l) for l in
                 (self.work / "_work" / "renames.jsonl")
                 .read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual([r["term"] for r in lines], ["月光", "灌注"])


if __name__ == "__main__":
    unittest.main()
