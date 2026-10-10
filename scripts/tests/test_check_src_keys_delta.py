# -*- coding: utf-8 -*-
"""check_src_keys.py の差分検査（--delta。G51 / F9-P0-1）のテスト。

- 未知キー（updates 内のキー名誤り）を analyze-commit の前に拒否する
- 必須キー（match_key / match_value / block / text）と型（replace / aliases）を検査する
- --delta の相対パスは work 相対で解決する（F4-C4 / F5-H1）
- 空差分は no_changes = true の明示を求める

合成データのみ使用。作品固有情報は含めない。
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

TOOL = SCRIPTS / "check_src_keys.py"


def _run(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(TOOL), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(cwd or SCRIPTS))
    return r.returncode, r.stdout + r.stderr


GOOD_DELTA = '''\
block = "ch001/b1"

[[characters_delta]]
match_key = "name_src"
match_value = "张三"
[characters_delta.updates]
name_ja = "チョウサン"
voice_status = "unconfirmed"
aliases = ["三儿"]

[[terms_delta]]
match_key = "term_src"
match_value = "心灵共感"
[terms_delta.updates]
term_ja = "心霊共感"

[[plot_delta]]
block = "ch001/b1"
text = "王五の悪夢と戴冠"
'''


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "work"
        (self.work / "_work").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _delta(self, text: str, name: str = "ch001_b1_delta.toml") -> Path:
        p = self.work / "_work" / name
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def _check(self, arg: str, cwd: Path | None = None):
        return _run([str(self.work), "--delta", arg], cwd=cwd)


class TestDeltaCheck(_Base):
    def test_good_delta_passes_with_work_relative_path(self):
        """正常な差分は OK。相対パスは work 相対で解決される（F4-C4/F5-H1）。"""
        self._delta(GOOD_DELTA)
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 0, out)
        self.assertIn("[delta-check]", out)
        self.assertIn("OK", out)

    def test_absolute_path_accepted(self):
        p = self._delta(GOOD_DELTA)
        code, out = self._check(str(p))
        self.assertEqual(code, 0, out)
        self.assertIn("OK", out)

    def test_unknown_key_in_updates_is_rejected(self):
        """updates のキー名誤り（実害: 原語をキーにした）を拒否する。"""
        self._delta('''\
block = "ch001/b1"

[[terms_delta]]
match_key = "term_src"
match_value = "卵巢"
[terms_delta.updates]
"卵巢" = "卵巣"
''')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("未知のキー", out)
        self.assertIn("卵巢", out)

    def test_unknown_flat_key_in_characters_is_rejected(self):
        self._delta('''\
block = "ch001/b1"

[[characters_delta]]
match_key = "name_src"
match_value = "张三"
name = "チョウサン"
''')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("未知のキー `name`", out)

    def test_missing_match_value_is_rejected(self):
        self._delta('''\
block = "ch001/b1"

[[characters_delta]]
match_key = "name_src"
''')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("match_value", out)

    def test_plot_block_mismatch_is_rejected(self):
        self._delta('''\
block = "ch001/b1"

[[plot_delta]]
block = "ch001/b9"
text = "別ブロックのプロット"
''')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("一致しません", out)

    def test_replace_must_be_boolean(self):
        self._delta('''\
block = "ch001/b1"

[[plot_delta]]
block = "ch001/b1"
text = "置換"
replace = "yes"
''')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("boolean", out)

    def test_aliases_must_be_string_list(self):
        self._delta('''\
block = "ch001/b1"

[[characters_delta]]
match_key = "name_src"
match_value = "张三"
[characters_delta.updates]
aliases = "三儿"
''')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("aliases", out)

    def test_empty_delta_requires_no_changes(self):
        self._delta('block = "ch001/b1"\n')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("no_changes", out)

    def test_no_changes_delta_passes(self):
        self._delta('block = "ch001/b1"\nno_changes = true\n')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 0, out)

    def test_missing_delta_file_exit_2(self):
        code, out = self._check("_work/no_such_delta.toml")
        self.assertEqual(code, 2, out)
        self.assertIn("見つかりません", out)

    def test_broken_toml_reports_problem(self):
        self._delta('block = "ch001/b1"\n[[terms_delta]\n')
        code, out = self._check("_work/ch001_b1_delta.toml")
        self.assertEqual(code, 1, out)
        self.assertIn("読めません", out)


if __name__ == "__main__":
    unittest.main()
