"""normalize_txt.py のテスト。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import normalize_txt  # noqa: E402


class TestNormalizeTxt(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_single_newline_inserts_blank_lines(self):
        src = self.td / "in.txt"
        src.write_text("一話目の本文。\n二段落目。\n三段落目。\n", encoding="utf-8")
        r = normalize_txt.normalize(src, None)
        self.assertFalse(r["skipped"])
        dst = Path(r["out"])
        self.assertEqual(dst.name, "in_norm.txt")
        self.assertEqual(
            dst.read_text(encoding="utf-8"),
            "一話目の本文。\n\n二段落目。\n\n三段落目。",
        )
        self.assertEqual(r["lines"], 3)

    def test_existing_blank_lines_skipped(self):
        src = self.td / "in.txt"
        src.write_text("一話目の本文。\n\n二段落目。\n", encoding="utf-8")
        r = normalize_txt.normalize(src, None)
        self.assertTrue(r["skipped"])
        self.assertFalse(result_path_exists(self.td, "in_norm.txt"))

    def test_explicit_output_path(self):
        src = self.td / "in.txt"
        src.write_text("行A。\n行B。\n", encoding="utf-8")
        dst = self.td / "out.md"
        normalize_txt.normalize(src, dst)
        self.assertEqual(dst.read_text(encoding="utf-8"), "行A。\n\n行B。")


def result_path_exists(td: Path, name: str) -> bool:
    return (td / name).exists()


if __name__ == "__main__":
    unittest.main()
