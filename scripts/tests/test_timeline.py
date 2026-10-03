"""timeline 記録の単体テスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

# _lib が別パスから既にキャッシュされている可能性があるので強制的に再読み込み
import importlib
import _lib as _lib_mod
_lib = importlib.reload(_lib_mod)
del _lib_mod


class TestRecordTimeline(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def _get_lines(self) -> list[dict]:
        """timeline.jsonl を読んでパースしたリストを返す。"""
        p = self.td / "logs" / "timeline.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines() if l.strip()]

    def test_writes_one_line(self):
        """record_timeline を1回呼ぶと1行書かれる。"""
        _lib.record_timeline(self.td, "test_stage", exit_code=0)
        lines = self._get_lines()
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertEqual(entry["stage"], "test_stage")
        self.assertEqual(entry["exit_code"], 0)
        self.assertIn("duration_ms", entry)
        self.assertIn("ts", entry)

    def test_append_not_overwrite(self):
        """2回呼ぶと2行追記される（上書きでない）。"""
        _lib.record_timeline(self.td, "s1")
        _lib.record_timeline(self.td, "s2")
        lines = self._get_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["stage"], "s1")
        self.assertEqual(lines[1]["stage"], "s2")

    def test_creates_logs_dir(self):
        """logs/ が無い workdir でも自動で作られる。"""
        # setUp では logs/ を作らない
        _lib.record_timeline(self.td, "auto_mkdir")
        self.assertTrue((self.td / "logs").is_dir())
        self.assertTrue((self.td / "logs" / "timeline.jsonl").exists())

    def test_chapter_block_fields(self):
        """chapter/block が反映される。"""
        _lib.record_timeline(self.td, "s", chapter="ch001", block="b1")
        entry = self._get_lines()[0]
        self.assertEqual(entry["chapter"], "ch001")
        self.assertEqual(entry["block"], "b1")

    def test_exit_code_nonzero(self):
        """exit_code が反映される。"""
        _lib.record_timeline(self.td, "s", exit_code=42)
        self.assertEqual(self._get_lines()[0]["exit_code"], 42)

    def test_oserror_silenced(self):
        """OSError は握りつぶされる（例: 書き込み権限がない）。"""
        # 読み取り専用のパスを渡す
        readonly = self.td / "nonexistent_deep" / "sub"
        try:
            _lib.record_timeline(readonly, "should_not_crash")
        except OSError:
            self.fail("OSError should be silenced")


class TestTimedStage(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def _get_lines(self):
        p = self.td / "logs" / "timeline.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines() if l.strip()]

    def test_normal_exit(self):
        """正常終了で exit_code=0 が記録される。"""
        with _lib.timed_stage(self.td, "normal"):
            pass
        entry = self._get_lines()[0]
        self.assertEqual(entry["stage"], "normal")
        self.assertEqual(entry["exit_code"], 0)
        self.assertGreaterEqual(entry["duration_ms"], 0)

    def test_systemexit_caught_and_reraises(self):
        """SystemExit(N) は記録してから再送出。"""
        with self.assertRaises(SystemExit):
            with _lib.timed_stage(self.td, "sys_exit"):
                raise SystemExit(5)
        entry = self._get_lines()[0]
        self.assertEqual(entry["exit_code"], 5)

    def test_systemexit_non_int(self):
        """SystemExit('msg') は exit_code=1 になる。"""
        with self.assertRaises(SystemExit):
            with _lib.timed_stage(self.td, "sys_exit_str"):
                raise SystemExit("bye")
        entry = self._get_lines()[0]
        self.assertEqual(entry["exit_code"], 1)

    def test_baseexception_caught_and_reraises(self):
        """未処理の例外は exit_code=1 で記録して再送出。"""
        with self.assertRaises(ValueError):
            with _lib.timed_stage(self.td, "exception"):
                raise ValueError("test")
        entry = self._get_lines()[0]
        self.assertEqual(entry["exit_code"], 1)

    def test_chapter_block_passed_through(self):
        """chapter/block が timed_stage から record_timeline に渡る。"""
        with _lib.timed_stage(self.td, "cb_test", chapter="ch002", block="b3"):
            pass
        entry = self._get_lines()[0]
        self.assertEqual(entry["chapter"], "ch002")
        self.assertEqual(entry["block"], "b3")


class TestBuildContextTimeline(unittest.TestCase):
    """build_context.main() を呼んで timeline に stage=build_context が記録されることを確認。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "test_work"
        (self.wd / "chapters").mkdir(parents=True)
        (self.wd / "logs").mkdir(parents=True)
        (self.wd / "translation_input").mkdir(parents=True)
        # 最小の manifest
        (self.wd / "manifest.toml").write_text(
            'work_id = "test"\n'
            'title = "テスト作品"\n'
            'lang = "zh"\n'
            'dst = "ja"\n',
            encoding="utf-8", newline="\n",
        )
        # chapters_meta: 1章1ブロック
        (self.wd / "chapters_meta.toml").write_text(
            '[[chapters]]\n'
            'id = "ch001"\n'
            'title = ""\n'
            'n_paras = 2\n'
            'n_chars = 10\n'
            'status = "pending"\n'
            'blocks = [{ id = "b1", paras = [1, 2], n_chars = 10, scene_break_cut = false }]\n',
            encoding="utf-8", newline="\n",
        )
        # paras
        (self.wd / "chapters" / "ch001.paras.toml").write_text(
            'chapter = "ch001"\n'
            'title = ""\n'
            '[[paras]]\n'
            'n = 1\n'
            'text = "这是一段。"\n'
            '[[paras]]\n'
            'n = 2\n'
            'text = "这是二段。"\n',
            encoding="utf-8", newline="\n",
        )

    def test_build_context_records_timeline(self):
        """build_context.main() → timeline.jsonl に stage=build_context がある。"""
        from unittest import mock
        import build_context  # noqa: E402
        argv = ["build_context", str(self.wd), "--chapter", "ch001", "--block", "b1", "--mode", "translation"]
        with mock.patch.object(sys, "argv", argv):
            # build_context は SystemExit を投げずに main() が return する
            try:
                build_context.main()
            except SystemExit:
                pass
        lines_path = self.wd / "logs" / "timeline.jsonl"
        self.assertTrue(lines_path.exists(), f"timeline.jsonl not found at {lines_path}")
        with open(lines_path, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        stages = [e["stage"] for e in lines]
        self.assertIn("build_context", stages, f"stages found: {stages}")


if __name__ == "__main__":
    unittest.main()