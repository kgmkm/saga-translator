"""sync_skill.py のテスト。合成ディレクトリのみ使用。

コピー、--check の差分検出、改行コード差の無視、除外リストの動作を検証する。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
TOOLS = SCRIPTS / "tools"
sys.path.insert(0, str(TOOLS))

import sync_skill  # noqa: E402

PYTHON = sys.executable or "python3"


def _write(p: Path, text: str, newline: str = "\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline=newline)


def _make_source(base: Path) -> Path:
    """配布元スキルの合成ツリー。"""
    src = base / "src"
    _write(src / "SKILL.md", "---\nname: t\nversion: 1.0.0\n---\n\n# t\n")
    _write(src / "README.md", "# readme\n")
    _write(src / "references" / "a.md", "ref a\n")
    _write(src / "templates" / "b.md", "tpl b\n")
    _write(src / "scripts" / "run.py", "print('x')\n")
    _write(src / "scripts" / "__pycache__" / "run.cpython.pyc", "\x00binary\n")
    _write(src / "docs" / "dev.md", "developer note\n")
    _write(src / "output" / "artifact.md", "work output\n")
    _write(src / "_saga" / "work" / "manifest.toml", "x = 1\n")
    return src


class TestSyncCopy(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.src = _make_source(self.td)
        self.dest = self.td / "dest"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_copy_creates_expected_files(self):
        rc = sync_skill.sync(self.src, self.dest, check=False)
        self.assertEqual(rc, 0)
        self.assertTrue((self.dest / "SKILL.md").exists())
        self.assertTrue((self.dest / "README.md").exists())
        self.assertTrue((self.dest / "references" / "a.md").exists())
        self.assertTrue((self.dest / "templates" / "b.md").exists())
        self.assertTrue((self.dest / "scripts" / "run.py").exists())

    def test_excludes_pycache_docs_and_artifacts(self):
        sync_skill.sync(self.src, self.dest, check=False)
        self.assertFalse((self.dest / "scripts" / "__pycache__").exists())
        self.assertFalse((self.dest / "docs").exists())
        self.assertFalse((self.dest / "output").exists())
        self.assertFalse((self.dest / "_saga").exists())

    def test_second_sync_is_identical(self):
        sync_skill.sync(self.src, self.dest, check=False)
        self.assertEqual(sync_skill.sync(self.src, self.dest, check=True), 0)


class TestSyncCheck(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.src = _make_source(self.td)
        self.dest = self.td / "dest"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_check_differs_when_dest_empty(self):
        self.assertEqual(sync_skill.sync(self.src, self.dest, check=True), 1)

    def test_check_passes_after_sync(self):
        sync_skill.sync(self.src, self.dest, check=False)
        self.assertEqual(sync_skill.sync(self.src, self.dest, check=True), 0)

    def test_check_no_copy_on_diff(self):
        sync_skill.sync(self.src, self.dest, check=True)
        # --check はコピーしない
        self.assertFalse(self.dest.exists() and any(self.dest.rglob("*")))

    def test_changed_content_detected(self):
        sync_skill.sync(self.src, self.dest, check=False)
        _write(self.dest / "references" / "a.md", "changed\n")
        self.assertEqual(sync_skill.sync(self.src, self.dest, check=True), 1)


class TestNewlineNormalization(unittest.TestCase):
    """改行コード差（CRLF/LF）は差分として数えない。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_crlf_vs_lf_not_a_diff(self):
        src = self.td / "src"
        dest = self.td / "dest"
        # 正本は CRLF、配布先は LF で同一内容
        _write(src / "SKILL.md", "line1\nline2\n", newline="\r\n")
        _write(dest / "SKILL.md", "line1\nline2\n", newline="\n")
        self.assertTrue(sync_skill.files_equal(src / "SKILL.md", dest / "SKILL.md"))
        self.assertEqual(sync_skill.sync(src, dest, check=True), 0)

    def test_real_text_diff_detected(self):
        src = self.td / "src"
        dest = self.td / "dest"
        _write(src / "SKILL.md", "line1\nline2\n")
        _write(dest / "SKILL.md", "line1\nLINE2\n")
        self.assertFalse(sync_skill.files_equal(src / "SKILL.md", dest / "SKILL.md"))

    def test_binary_byte_compare(self):
        src = self.td / "src"
        dest = self.td / "dest"
        (src).mkdir(parents=True, exist_ok=True)
        (dest).mkdir(parents=True, exist_ok=True)
        (src / "SKILL.md").write_bytes(b"\x00\x01\x02\x03")
        (dest / "SKILL.md").write_bytes(b"\x00\x01\x02\x03")
        self.assertTrue(sync_skill.files_equal(src / "SKILL.md", dest / "SKILL.md"))
        (dest / "SKILL.md").write_bytes(b"\x00\x01\x02\x04")
        self.assertFalse(sync_skill.files_equal(src / "SKILL.md", dest / "SKILL.md"))


class TestDistributionExclusions(unittest.TestCase):
    """G46: tests/ は既定で配布外（--with-tests で任意化）。G45: .hermes-tmp* は常に除外。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.src = _make_source(self.td)
        _write(self.src / "scripts" / "tests" / "test_x.py", "import unittest\n")
        _write(self.src / "scripts" / "tools" / "tests" / "test_tool.py", "import unittest\n")
        _write(self.src / "scripts" / ".hermes-tmp-1234" / "junk.py", "x = 1\n")
        _write(self.src / "scripts" / ".hermes-tmp-plan.md", "tmp\n")
        self.dest = self.td / "dest"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_tests_and_hermes_tmp_excluded_by_default(self):
        rc = sync_skill.sync(self.src, self.dest, check=False)
        self.assertEqual(rc, 0)
        self.assertFalse((self.dest / "scripts" / "tests").exists())
        self.assertFalse((self.dest / "scripts" / "tools" / "tests").exists())
        self.assertFalse((self.dest / "scripts" / ".hermes-tmp-1234").exists())
        self.assertFalse((self.dest / "scripts" / ".hermes-tmp-plan.md").exists())
        # 通常のスクリプトは配布される
        self.assertTrue((self.dest / "scripts" / "run.py").exists())

    def test_with_tests_includes_tests_but_not_hermes_tmp(self):
        rc = sync_skill.sync(self.src, self.dest, check=False, with_tests=True)
        self.assertEqual(rc, 0)
        self.assertTrue((self.dest / "scripts" / "tests" / "test_x.py").exists())
        self.assertTrue((self.dest / "scripts" / "tools" / "tests" / "test_tool.py").exists())
        # .hermes-tmp* は --with-tests でも除外したまま
        self.assertFalse((self.dest / "scripts" / ".hermes-tmp-1234").exists())
        self.assertFalse((self.dest / "scripts" / ".hermes-tmp-plan.md").exists())

    def test_check_ignores_stale_tests_dir_by_default(self):
        # 配布先に残る tests/ の残骸は既定の --check では差分にしない（除外扱い）
        _write(self.dest / "scripts" / "tests" / "old.py", "x\n")
        sync_skill.sync(self.src, self.dest, check=False)
        self.assertEqual(sync_skill.sync(self.src, self.dest, check=True), 0)

    def test_check_with_tests_sees_tests_diffs(self):
        _write(self.dest / "scripts" / "tests" / "old.py", "x\n")
        sync_skill.sync(self.src, self.dest, check=False)
        self.assertEqual(sync_skill.sync(self.src, self.dest, check=True, with_tests=True), 1)


class TestSyncCLI(unittest.TestCase):
    """CLI 経由の --check 終了コードを検証する。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.src = _make_source(self.td)
        self.dest = self.td / "dest"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(TOOLS / "sync_skill.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def test_cli_check_exit1_on_diff(self):
        p = self._run("--source", str(self.src), "--dest", str(self.dest), "--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("differ", p.stdout)

    def test_cli_sync_then_check_exit0(self):
        p = self._run("--source", str(self.src), "--dest", str(self.dest))
        self.assertEqual(p.returncode, 0)
        self.assertIn("copied", p.stdout)
        p2 = self._run("--source", str(self.src), "--dest", str(self.dest), "--check")
        self.assertEqual(p2.returncode, 0)

    def test_cli_with_tests_flag(self):
        _write(self.src / "scripts" / "tests" / "test_x.py", "import unittest\n")
        p = self._run("--source", str(self.src), "--dest", str(self.dest), "--with-tests")
        self.assertEqual(p.returncode, 0)
        self.assertIn("with_tests=true", p.stdout)
        self.assertTrue((self.dest / "scripts" / "tests" / "test_x.py").exists())


if __name__ == "__main__":
    unittest.main()