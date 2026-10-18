"""house_style の置き場所（init / get）と、proposed の一覧・承認・却下のテスト。

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


def _run(mod_name: str, argv: list[str]) -> tuple[int, str]:
    import importlib
    mod = importlib.import_module(mod_name)
    buf = io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", [mod_name, *argv]):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                mod.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


class TestHouseStyleLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = Path(self.tmp) / "work"
        self.work.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_creates_house_dir_with_template(self):
        code, out = _run("house_style", ["init", str(self.work)])
        self.assertEqual(code, 0, out)
        self.assertTrue((self.work / "_house" / "house_style.toml").is_file())

    def test_init_carries_misplaced_root_file(self):
        _write(self.work / "house_style.toml",
               '[[rules]]\nid = "r1"\nkind = "naming"\nscope = "work"\n'
               'target = "w"\npattern = "旧表記"\nreplacement = "新表記"\n')
        code, out = _run("house_style", ["init", str(self.work)])
        self.assertEqual(code, 0, out)
        moved = (self.work / "_house" / "house_style.toml").read_text(encoding="utf-8")
        self.assertIn("旧表記", moved)
        self.assertIn("直下の誤配置", out)

    def test_init_refuses_overwrite(self):
        _run("house_style", ["init", str(self.work)])
        code, out = _run("house_style", ["init", str(self.work)])
        self.assertEqual(code, 1)
        self.assertIn("既にあります", out)

    def test_get_accepts_work_directory(self):
        _run("house_style", ["init", str(self.work)])
        code, out = _run("house_style", ["get", str(self.work)])
        self.assertEqual(code, 0, out)

    def test_get_missing_file_has_hint(self):
        code, out = _run("house_style", ["get", str(self.work)])
        self.assertEqual(code, 1)
        self.assertIn("init", out)

    def test_misplaced_file_is_reported(self):
        from vocab import house_style_misplaced
        _write(self.work / "house_style.toml", "[[rules]]\n")
        self.assertEqual(house_style_misplaced(self.work), self.work / "house_style.toml")
        _run("house_style", ["init", str(self.work)])
        self.assertIsNone(house_style_misplaced(self.work))


CHAR_TOML = """[[characters]]
name_src = "甲乙"
name_ja = "甲乙（こうおつ）"
role = "主人公"
proposed = \"\"\"name_ja: 「甲乙（甲）」→「甲乙（こうおつ）」
role: 「脇役」→「主人公」\"\"\"
"""


class TestProposedCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = Path(self.tmp) / "work"
        _write(self.work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
        _write(self.work / "analysis" / "characters-[ネタバレあり].toml", CHAR_TOML)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _char(self) -> dict:
        return _lib.load_characters(self.work)["characters"][0]

    def test_list_shows_pending_proposals(self):
        code, out = _run("saga", ["proposed", "list", str(self.work)])
        self.assertEqual(code, 0, out)
        self.assertIn("name_ja", out)
        self.assertIn("role", out)

    def test_approve_applies_value_and_removes_line(self):
        code, out = _run("saga", ["proposed", "approve", str(self.work),
                                  "--name", "甲乙（こうおつ）", "--key", "name_ja"])
        self.assertEqual(code, 0, out)
        c = self._char()
        self.assertEqual(c["name_ja"], "甲乙（こうおつ）")
        self.assertNotIn("name_ja:", c["proposed"])
        self.assertIn("role:", c["proposed"])
        log = (self.work / "_work" / "analysis_edits.jsonl").read_text(encoding="utf-8")
        self.assertIn("proposed-approve", log)

    def test_reject_removes_line_without_changing_value(self):
        code, out = _run("saga", ["proposed", "reject", str(self.work),
                                  "--name", "甲乙（こうおつ）", "--key", "role"])
        self.assertEqual(code, 0, out)
        c = self._char()
        self.assertEqual(c["role"], "主人公")
        self.assertNotIn("role:", c["proposed"])

    def test_unknown_key_fails_with_candidates(self):
        code, out = _run("saga", ["proposed", "approve", str(self.work),
                                  "--name", "甲乙（こうおつ）", "--key", "unknown"])
        self.assertEqual(code, 1)
        self.assertIn("name_ja", out)


if __name__ == "__main__":
    unittest.main()
