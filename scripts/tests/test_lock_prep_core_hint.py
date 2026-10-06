"""lock_prep の core = true 推奨案内（P1-4 の提案3）。合成データのみ使用。

本文で名前が伏せられるキャラ（「女」「彼」等で呼ばれる）は compact コンテキストで
落ちるため、人物表で core = true を付ける推奨を lock_prep の出力（md と標準出力）
に明記する。
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import lock_prep  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_minimal_work(base: Path) -> Path:
    work = base / "_saga" / "99999"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml",
           '[[chapters]]\nid = "ch001"\nblocks = ["b1"]\n')
    _write(work / "chapters" / "ch001.paras.toml",
           '[[paras]]\nn = 1\ntext = "张三很聪明。"\n\n'
           '[[paras]]\nn = 2\ntext = "张三又来了。"\n\n'
           '[[paras]]\nn = 3\ntext = "张三笑了。"\n')
    return work


class TestCoreHint(unittest.TestCase):
    """lock_prep の出力に core = true の推奨案内が入る（P1-4）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.work = _make_minimal_work(self.td)

    def test_md_and_stdout_include_core_hint(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lock_prep.main([str(self.work)])
        self.assertEqual(rc, 0, buf.getvalue())
        md = (self.work / "_work" / "lock_prep.md").read_text(encoding="utf-8")
        self.assertIn("core = true", md)
        self.assertIn("compact", md)
        self.assertIn("core = true", buf.getvalue())
        self.assertIn("compact", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
