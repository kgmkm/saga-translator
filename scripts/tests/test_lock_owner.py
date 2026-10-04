"""共有ロックの一元化と所有者確認のテスト（phase4-taskK1）。

ロックは <work>/_work/.lock の1つ。stale（10分超）でも所有者の死亡を
確認できた場合のみ上書きし、所有者生存・生死判定不能なら exit 3 で拒否する。
解放は finally で必ず行い、他人のロックは消さない。

実在の別プロセスは操作しない。ロックファイルに書く PID を偽装するだけ。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


def _make_work(tmp: Path) -> Path:
    """minimal work fixture（ロック以外の検証に必要な最小構成）。"""
    work = tmp / "test_work"
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "_work").mkdir(parents=True)
    (work / "manifest.toml").write_text('title = "テスト"\nlang = "zh"\n', encoding="utf-8")

    chapters = []
    for ci in (1,):
        cid = f"ch{ci:03d}"
        (work / "output" / cid).mkdir(parents=True)
        blocks = []
        for bi in (1, 2, 3):
            blocks.append({"id": f"b{bi}", "paras": [(bi - 1) * 4 + 1, bi * 4], "n_chars": 100})
        chapters.append({"id": cid, "title": "", "status": "pending", "blocks": blocks})

    meta = {"work": {"title": "テスト", "author": "x", "lang": "zh"}, "chapters": chapters}
    _lib.save_chapters_meta(work, meta)
    _lib.save_paras(work, "ch001",
                    {"paras": [{"n": i + 1, "text": f"原文{i + 1}。"} for i in range(12)]})
    # analyze-commit が読み書きする分析ファイルの初期形
    _lib.save_toml(work / "analysis" / "characters-[ネタバレあり].toml", {"characters": []})
    _lib.save_toml(work / "analysis" / "terms.toml", {"terms": []})
    _lib.save_toml(work / "analysis" / "plot-[ネタバレあり].toml", {"entries": []})
    return work


def _write_lock(path: Path, *, pid: int, host: str | None, age_seconds: float) -> None:
    """ロックファイルを書いて mtime を古くする（host=None は旧形式=ホスト未記録）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if host is None:
        # 旧形式: PID を1行だけ書いたテキスト
        path.write_text(f"{pid}\n", encoding="utf-8")
    else:
        path.write_text(json.dumps({
            "pid": pid, "host": host,
            "acquired_at": "2000-01-01T00:00:00+00:00", "stage": "test",
        }) + "\n", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(str(path), (old, old))


class LockBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lock_owner_"))
        self.work = _make_work(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def lock(self) -> Path:
        return self.work / "_work" / ".lock"


class TestAcquireRelease(LockBase):
    """_lib の共有ロック単体。"""

    def test_acquire_then_exclusive_fail(self):
        """取得成功 → 2回目は排他失敗。所有者は自プロセス。"""
        ok, why = _lib.acquire_lock(self.work, "test")
        self.assertTrue(ok, why)
        owner = _lib._read_lock_owner(self.lock)
        self.assertEqual(owner["pid"], os.getpid())
        self.assertTrue(owner["host"])
        self.assertEqual(owner["stage"], "test")

        ok2, why2 = _lib.acquire_lock(self.work, "test")
        self.assertFalse(ok2)
        self.assertIn(str(os.getpid()), why2)

    def test_release_removes_own_lock(self):
        _lib.acquire_lock(self.work, "test")
        _lib.release_lock(self.work)
        self.assertFalse(self.lock.exists())

    def test_release_does_not_remove_others_lock(self):
        """他人（別 PID）のロックは解放処理が消さない。"""
        _write_lock(self.lock, pid=99999, host=_lib._HOSTNAME, age_seconds=1.0)
        before = self.lock.read_text(encoding="utf-8")
        _lib.release_lock(self.work)
        self.assertTrue(self.lock.exists(), "他人のロックを消してはいけない")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), before)

    def test_stale_owner_dead_is_overridden(self):
        """stale かつ所有者死亡 → 上書きして取得できる。"""
        _write_lock(self.lock, pid=99999, host=_lib._HOSTNAME,
                    age_seconds=_lib.LOCK_STALE_SECONDS + 5)
        ok, why = _lib.acquire_lock(self.work, "test")
        self.assertTrue(ok, why)
        self.assertEqual(_lib._read_lock_owner(self.lock)["pid"], os.getpid())

    def test_stale_owner_alive_is_not_overridden(self):
        """中核: stale でも所有者が生存 → 上書きしない。"""
        _write_lock(self.lock, pid=os.getpid(), host=_lib._HOSTNAME,
                    age_seconds=_lib.LOCK_STALE_SECONDS + 5)
        before = self.lock.read_text(encoding="utf-8")
        ok, why = _lib.acquire_lock(self.work, "test")
        self.assertFalse(ok)
        self.assertIn("生存中", why)
        self.assertEqual(self.lock.read_text(encoding="utf-8"), before,
                         "所有者生存の stale ロックを書き換えてはいけない")

    def test_stale_other_host_is_not_overridden(self):
        """生死判定不能（別ホスト）→ 上書きしない。"""
        _write_lock(self.lock, pid=1234, host="other-host-xyz",
                    age_seconds=_lib.LOCK_STALE_SECONDS + 5)
        ok, why = _lib.acquire_lock(self.work, "test")
        self.assertFalse(ok)
        self.assertIn("別ホスト", why)

    def test_stale_legacy_pid_only_uses_liveness(self):
        """旧形式（PID のみ）でも生存確認を行う: 死亡 PID なら上書き。"""
        _write_lock(self.lock, pid=99999, host=None,
                    age_seconds=_lib.LOCK_STALE_SECONDS + 5)
        ok, why = _lib.acquire_lock(self.work, "test")
        self.assertTrue(ok, why)

    def test_stale_unreadable_owner_is_not_overridden(self):
        """所有者情報を読めない → 上書きしない（推測しない）。"""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("??? not a pid\n", encoding="utf-8")
        old = time.time() - (_lib.LOCK_STALE_SECONDS + 5)
        os.utime(str(self.lock), (old, old))
        ok, why = _lib.acquire_lock(self.work, "test")
        self.assertFalse(ok)


def _make_delta(path: Path) -> None:
    path.write_text(
        'block = "ch001/b1"\n\n[[plot_delta]]\nblock = "ch001/b1"\ntext = "要点。"\n',
        encoding="utf-8")


def _run_analyze_commit(work: Path, delta: Path) -> tuple[int, str]:
    import io
    from scripts.saga import cmd_analyze_commit
    old_out, old_err = sys.stdout, sys.stderr
    buf = io.StringIO()
    try:
        sys.stdout = sys.stderr = buf
        code = cmd_analyze_commit(work, delta)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code or 0, buf.getvalue()


class TestAnalyzeCommitLock(LockBase):
    """analyze-commit が共有ロックで排他されること。"""

    def _delta(self) -> Path:
        d = self.tmp / "delta.toml"
        _make_delta(d)
        return d

    def test_live_lock_exits_3(self):
        """同一プロセスが保持するロック中は exit 3（上書きしない）。"""
        ok, _ = _lib.acquire_lock(self.work, "test")
        self.assertTrue(ok)
        code, out = _run_analyze_commit(self.work, self._delta())
        self.assertEqual(code, 3)
        self.assertIn("終了コード 3", out)

    def test_stale_dead_owner_runs_and_releases(self):
        """stale かつ所有者死亡 → 上書きして実行、終了時にロック解放。"""
        _write_lock(self.lock, pid=99999, host=_lib._HOSTNAME,
                    age_seconds=_lib.LOCK_STALE_SECONDS + 5)
        code, out = _run_analyze_commit(self.work, self._delta())
        self.assertEqual(code, 0, out)
        self.assertFalse(self.lock.exists(), "終了時にロックを解放する")

    def test_stale_alive_owner_exits_3_and_keeps_lock(self):
        """stale でも所有者生存 → exit 3、ロックは残す。"""
        _write_lock(self.lock, pid=os.getpid(), host=_lib._HOSTNAME,
                    age_seconds=_lib.LOCK_STALE_SECONDS + 5)
        before = self.lock.read_text(encoding="utf-8")
        code, out = _run_analyze_commit(self.work, self._delta())
        self.assertEqual(code, 3)
        self.assertTrue(self.lock.exists())
        self.assertEqual(self.lock.read_text(encoding="utf-8"), before)

    def test_stale_undeterminable_owner_exits_3(self):
        """生死判定不能（別ホスト）→ exit 3、上書きしない。"""
        _write_lock(self.lock, pid=4321, host="other-host-xyz",
                    age_seconds=_lib.LOCK_STALE_SECONDS + 5)
        code, out = _run_analyze_commit(self.work, self._delta())
        self.assertEqual(code, 3)

    def test_exception_releases_lock(self):
        """処理中に例外が出ても finally でロックを解放する。"""
        import scripts.saga as saga
        # delta の読み込み段で例外を起こす（monkeypatch）
        orig = saga._parse_delta

        def _boom(_p):
            raise RuntimeError("boom")

        saga._parse_delta = _boom
        try:
            with self.assertRaises(RuntimeError):
                _run_analyze_commit(self.work, self._delta())
        finally:
            saga._parse_delta = orig
        self.assertFalse(self.lock.exists(), "例外時もロックを解放する")


class TestSharedLockAcrossEntrypoints(LockBase):
    """safe_emit と analyze-commit が同じロックで排他されること。"""

    def test_analyze_commit_blocked_by_common_lock(self):
        """safe_emit が使う共有ロック（.lock）を保持中は analyze-commit が exit 3。"""
        ok, _ = _lib.acquire_lock(self.work, "safe_emit")
        self.assertTrue(ok)
        d = self.tmp / "delta.toml"
        _make_delta(d)
        code, _ = _run_analyze_commit(self.work, d)
        self.assertEqual(code, 3)

    def test_safe_emit_uses_common_lock(self):
        """safe_emit のロック取得は共有ロック（.lock）を使う（.emit.lock ではない）。"""
        import safe_emit
        # safe_emit.acquire_lock は _lib の関数を指す（一元化の確認）
        self.assertIs(safe_emit.acquire_lock, _lib.acquire_lock)
        self.assertIs(safe_emit.release_lock, _lib.release_lock)
        # 旧ロックディレクトリを作らないこと（間接確認）
        self.assertFalse((self.work / "output" / "_work" / ".emit.lock").exists())

    def test_safe_emit_main_returns_2_when_locked(self):
        """safe_emit 実行時、共有ロック保持中は exit 2（リトライ）。"""
        import safe_emit
        ok, _ = _lib.acquire_lock(self.work, "analyze-commit")
        self.assertTrue(ok)
        old_argv = sys.argv
        sys.argv = ["safe_emit.py", str(self.work), "--chapter", "ch001", "--block", "b1"]
        try:
            code = safe_emit.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 2)


class TestCommitAndPrepareLock(LockBase):
    """commit / prepare も共有ロックを使い、finally で解放すること。"""

    def test_prepare_locked_exits_3(self):
        import io
        from scripts.saga import cmd_prepare
        ok, _ = _lib.acquire_lock(self.work, "test")
        self.assertTrue(ok)
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_prepare(self.work, "ch001", "b1")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 3)

    def test_prepare_releases_lock_on_failure(self):
        """prepare が失敗（例外）してもロックを解放する。"""
        import io
        from scripts.saga import cmd_prepare
        # 存在しない章 → build_context が SystemExit を投げる
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            try:
                code = cmd_prepare(self.work, "ch999", "b1")
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertNotEqual(code, 0)
        self.assertFalse(self.lock.exists(), "失敗時もロックを解放する")

    def test_commit_releases_lock_on_failure(self):
        """commit が失敗（ドラフト不在）しても finally でロックを解放する。"""
        import io
        from scripts.saga import cmd_commit
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 1)
        self.assertFalse(self.lock.exists(), "commit 失敗時もロックを解放する")

    def test_commit_locked_exits_3(self):
        import io
        from scripts.saga import cmd_commit
        ok, _ = _lib.acquire_lock(self.work, "test")
        self.assertTrue(ok)
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()