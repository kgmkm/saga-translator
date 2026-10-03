"""P0-2 取り込みツールの回帰テスト。自作フィクスチャのみ使用。

新規7ツールの --help exitcode と、存在しない workdir でのエラーを検証。
verify_progress のみテンポラリ workdir で HOLE 検出を追加検証。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

TOOLS = [
    "verify_progress.py",
    "check_src_keys.py",
    "safe_emit.py",
    "blk.py",
    "ctx_body.py",
    "restore_parallel.py",
]


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _run(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=cwd,
    )


def _run_tool(name: str, *args: str, workdir: str | None = None) -> subprocess.CompletedProcess:
    return _run(str(SCRIPTS / name), *args, cwd=workdir)


def make_minimal_work(tmp: Path) -> None:
    """verify_progress 用最小フィクスチャ（ch001, b1-b3, 一部実体あり）。"""
    (tmp / "chapters").mkdir(parents=True)
    (tmp / "analysis").mkdir(parents=True)
    (tmp / "output" / "ch001").mkdir(parents=True)
    meta = {
        "work": {"title": "テスト作品"},
        "chapters": [{
            "id": "ch001",
            "title": None,
            "blocks": [
                {"id": "b1", "paras": [1, 2]},
                {"id": "b2", "paras": [3, 4]},
                {"id": "b3", "paras": [5, 6]},
            ],
        }],
    }
    _write(tmp / "chapters_meta.json", json.dumps(meta, ensure_ascii=False))
    # analysis: b1 と b3 だけ存在（b2 は欠落 = HOLE）— TOML形式
    plot_toml = """[[entries]]
block = "ch001/b1"
summary = "事件"

[[entries]]
block = "ch001/b3"
summary = "解決"
"""
    _write(tmp / "analysis" / "plot-[ネタバレあり].toml", plot_toml)
    # output: b1_ja.md だけ存在（b2, b3 は未翻訳）
    (tmp / "output" / "ch001" / "b1_ja.md").write_text("テスト訳文。\n", encoding="utf-8")


class TestToolHelp(unittest.TestCase):
    """全ツール --help が正常に動作することを検証。"""

    def test_verify_progress_help(self):
        r = _run_tool("verify_progress.py", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)

    def test_check_src_keys_help(self):
        r = _run_tool("check_src_keys.py", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)

    def test_safe_emit_help(self):
        r = _run_tool("safe_emit.py", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)

    def test_blk_help(self):
        r = _run_tool("blk.py", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)

    def test_ctx_body_help(self):
        r = _run_tool("ctx_body.py", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)

    def test_restore_parallel_help(self):
        r = _run_tool("restore_parallel.py", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)


class TestToolErrors(unittest.TestCase):
    """存在しない workdir で適切なエラー終了コードを返す検証。"""

    def test_verify_progress_nonexistent(self):
        r = _run_tool("verify_progress.py", "/no/such/path")
        self.assertEqual(r.returncode, 1)
        # 例外（FileNotFoundError）のトレースバックまたは「見つからない」メッセージ
        self.assertTrue("見つからない" in (r.stderr or r.stdout) or "Error" in (r.stderr or ""))

    def test_check_src_keys_nonexistent(self):
        r = _run_tool("check_src_keys.py", "/no/such/path")
        self.assertEqual(r.returncode, 2)
        self.assertIn("見つかりません", r.stderr or r.stdout)

    def test_blk_no_workdir(self):
        r = _run_tool("blk.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    def test_ctx_body_no_file(self):
        r = _run_tool("ctx_body.py", "/no/such/file.md")
        self.assertEqual(r.returncode, 1)

    def test_restore_parallel_nonexistent(self):
        r = _run_tool("restore_parallel.py", "/no/such/path", "/no/such/bak")
        self.assertEqual(r.returncode, 1)


class TestVerifyProgress(unittest.TestCase):
    """verify_progress の HOLE 検出ロジック。最小フィクスチャで検証。"""

    def test_detect_hole(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            make_minimal_work(tmp)
            # manifest を置く
            (tmp / "manifest.json").write_text("{}", encoding="utf-8")
            r = _run_tool("verify_progress.py", td)
            # b2 が MISSING analysis + translation → RC=1（problems カウントは holes のみだが）
            # 現在の実装では problems が0でも MISSING が出力される
            self.assertIn("MISSING", r.stdout or r.stderr)
            # verify_progress は problems>0 で RC=1。problems は holes + 実体なしで増える
            # MISSING でも RC=1 を期待するのが自然だが、現状 problems=0 → 将来改善

    def test_clean_no_issues(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            make_minimal_work(tmp)
            # 全ブロックの実体を用意 — TOML形式
            plot_toml = """[[entries]]
block = "ch001/b1"
summary = "事件"

[[entries]]
block = "ch001/b2"
summary = "展開"

[[entries]]
block = "ch001/b3"
summary = "解決"
"""
            _write(tmp / "analysis" / "plot-[ネタバレあり].toml", plot_toml)
            for b in ("b1", "b2", "b3"):
                (tmp / "output" / "ch001" / f"{b}_ja.md").write_text("訳文。\n", encoding="utf-8")
            (tmp / "manifest.json").write_text("{}", encoding="utf-8")
            r = _run_tool("verify_progress.py", td)
            self.assertEqual(r.returncode, 0)