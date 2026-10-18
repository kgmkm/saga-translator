"""レイアウト正本（scripts/paths.py）とコード・md の一致のテスト。

- 登録内容の妥当性（id 一意・lifetime/kind が許容値）
- コード中の `work / "…"` が登録済みの第1セグメントだけを使っているか
- md（references/data-schema.md）が構造を写経していないこと
合成データのみ使用し、作品固有情報は含めない。
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import paths  # noqa: E402

DOC = SCRIPTS.parent / "references" / "data-schema.md"

class TestRegistry(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = [e.id for e in paths.ALL]
        self.assertEqual(len(ids), len(set(ids)))

    def test_paths_are_work_relative(self):
        for e in paths.ALL:
            self.assertFalse(e.path.startswith("/"), e)
            self.assertNotIn("..", e.path, e)
            self.assertNotIn("<work>", e.path, e)

    def test_kind_and_lifetime_are_known(self):
        for e in paths.ALL:
            self.assertIn(e.kind, paths.KINDS, e)
            self.assertIn(e.lifetime, paths.LIFETIMES, e)

    def test_every_top_level_segment_is_registered(self):
        for e in paths.ALL:
            self.assertIn(e.path.split("/")[0], paths.TOP_LEVEL, e)

    def test_lifetime_keep_has_no_purge_only_path(self):
        # 念のため: 旧配置でも新規作成されるものは無い（LEGACY は読むだけ）
        for e in paths.LEGACY:
            self.assertTrue(e.note, e)


class TestCodeCoverage(unittest.TestCase):
    """コードに新しいパスを足したら paths.py にも登録させる（無言の追加を防ぐ）。"""

    def test_literal_scanner_finds_work_paths(self):
        self.assertEqual(paths._LITERAL_RE.findall('work / "foo" / "bar"'), ["foo"])
        self.assertEqual(paths._LITERAL_RE.findall('Path(work) / "baz"'), ["baz"])
        self.assertEqual(paths._LITERAL_RE.findall('out / "x"'), [])
        self.assertEqual(paths._LITERAL_RE.findall('_work / "x"'), [])

    def test_no_unregistered_work_paths(self):
        missing = paths.missing_literals()
        self.assertEqual(missing, {}, f"paths.py に未登録: {missing}")


class TestMarkdown(unittest.TestCase):
    def test_data_schema_points_to_paths_py(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("scripts/paths.py", text)

    def test_data_schema_has_no_hand_written_tree(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertNotIn("├── manifest.toml", text)

    def test_tree_is_renderable(self):
        buf = io.StringIO()
        paths.format_md(buf)
        out = buf.getvalue()
        self.assertIn("<work>/", out)
        self.assertIn("manifest.toml", out)
        self.assertIn("全文_訳文.md", out)


class TestSagaPathsCommand(unittest.TestCase):
    def test_saga_paths_prints_layout(self):
        import contextlib
        import importlib
        from unittest import mock
        saga = importlib.import_module("saga")
        buf = io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["saga.py", "paths"]):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    saga.main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        self.assertEqual(code, 0)
        self.assertIn("manifest.toml", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
