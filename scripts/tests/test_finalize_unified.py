"""確定の一本化（#17）・スナップショット検査と由来ハッシュ（#16）・復旧/改訂の分離のテスト。

検証対象:
  - commit / safe_emit / qa_translation --emit の3経路が共通確定処理
    （saga._finalize_block）を通ること
  - 検査（align / QA / 残渣）に抵触したら確定しないこと（3経路それぞれ）
  - 検査・emit・記録が「正規化済みドラフトのバイト列スナップショット」に対して行われること
  - 由来ハッシュ（src / analysis_src / config / draft / final）が確定記録に残ること
  - 再実行の復旧（既定・記録不変・--revise 案内）と改訂（--revise・履歴保持）の分離

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402

# 確定記録に残る由来ハッシュのキー（保存形式の契約）
HASH_KEYS = ("src_hash", "analysis_src_hash", "config_hash", "draft_hash", "final_hash")

# 合成ドラフト（正規化で 强行→強行 に変わる字を含む）
CLEAN_DRAFT = [
    (1, "第一章　始まり"),
    (2, "彼は强行突破を選んだ。"),
    (3, "雪が降り始めた。"),
]
REVISED_DRAFT = [
    (1, "第一章　始まり"),
    (2, "空は曇っていた。風が冷たい。"),
    (3, "低い鐘が響く。雪が降る。"),
]


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_work(base: Path) -> Path:
    """zh→ja の最小合成ワーク（1章1ブロック）。"""
    work = base / "work"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}]
""")
    _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "第一章开始。"

[[paras]]
n = 2
text = "他选择了强行突破。"

[[paras]]
n = 3
text = "雪开始落下。"
""")
    _write(work / "analysis" / "characters-[ネタバレあり].toml",
           '[[characters]]\nname_src = "林霖"\nname_ja = "リンリン"\n')
    return work


def _analyze_commit(work: Path) -> None:
    """b1 の分析記録（analysis_src_hash）を作る。"""
    delta = work.parent / "delta.toml"
    delta.write_text(
        'block = "ch001/b1"\n\n[[plot_delta]]\nblock = "ch001/b1"\ntext = "要点。"\n',
        encoding="utf-8")
    r = subprocess.run(
        [PYTHON, str(SCRIPTS / "saga.py"), "analyze-commit", str(work), "--delta", str(delta)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise AssertionError(f"analyze-commit 失敗 rc={r.returncode}\n{r.stdout}\n{r.stderr}")


def _place_draft(work: Path, items: list[tuple[int, str]]) -> Path:
    """output/_work/ に番号付きドラフトを置く。"""
    draft = work / "output" / "_work" / "ch001_b1_ja_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("\n\n".join(f"[P{n:03d}] {t}" for n, t in items) + "\n",
                     encoding="utf-8")
    return draft


def _run_commit(work: Path, model: str = "test-model", prompt_version: str = "v1",
                revise: bool = False) -> tuple[int, str]:
    """cmd_commit を in-process で実行し (終了コード, 出力) を返す。"""
    from saga import cmd_commit
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = buf
        code = cmd_commit(work, "ch001", "b1", model, prompt_version, revise=revise)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, buf.getvalue()


def _run_cli(tail: list[str]) -> tuple[int, str]:
    """scripts/ 配下の CLI をサブプロセスで実行し (終了コード, 出力) を返す。"""
    r = subprocess.run(
        [PYTHON, str(SCRIPTS / tail[0]), *tail[1:]],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _block(work: Path) -> dict:
    meta = _lib.load_chapters_meta(work)
    return _lib.block_meta(meta, "ch001/b1")


def _out_path(work: Path) -> Path:
    return work / "output" / "ch001" / "b1_ja.md"


def _done_path(work: Path) -> Path:
    return work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md"


class _FixtureBase(unittest.TestCase):
    """合成ワークの共通セットアップ。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)


class TestSnapshotAndHashes(_FixtureBase):
    """スナップショット検査と由来ハッシュの記録（#16）。"""

    def test_normalized_snapshot_is_the_checked_and_recorded_bytes(self):
        """正規化は検査前に一括適用され、検査・emit・記録が同じバイト列で行われる。"""
        _analyze_commit(self.work)
        draft = _place_draft(self.work, CLEAN_DRAFT)
        raw_bytes = draft.read_bytes()
        self.assertIn("强行", raw_bytes.decode("utf-8"))  # 生ドラフトは未正規化

        code, out = _run_commit(self.work)
        self.assertEqual(code, 0, out)

        # 正規化されたドラフトが done/ へ退避される
        done = _done_path(self.work)
        self.assertTrue(done.is_file(), out)
        done_bytes = done.read_bytes()
        self.assertIn("強行", done_bytes.decode("utf-8"))
        self.assertNotEqual(raw_bytes, done_bytes, "正規化が反映されていない")

        # スナップショットは検査対象（done と同一バイト列）
        snaps = sorted((self.work / "output" / "_work" / "commit").glob("*_ch001_b1"))
        self.assertEqual(len(snaps), 1)
        snap_bytes = (snaps[0] / "draft.md").read_bytes()
        self.assertEqual(snap_bytes, done_bytes, "スナップショットと検査対象がずれている")

        b = _block(self.work)
        tr = b["translation"]
        # draft_hash は「正規化後ドラフト」、final_hash は「確定出力」のバイト列
        self.assertEqual(tr["draft_hash"], _sha12(done_bytes))
        self.assertNotEqual(tr["draft_hash"], _sha12(raw_bytes),
                            "生ドラフト（書き換え前）のハッシュが記録されている")
        self.assertEqual(tr["final_hash"], _sha12(_out_path(self.work).read_bytes()))
        # src_hash は原文、analysis_src_hash は分析記録の値
        self.assertEqual(tr["src_hash"],
                         _lib.block_src_hash(_lib.load_paras(self.work, "ch001"), b))
        self.assertEqual(tr["analysis_src_hash"], str(b.get("analysis_src_hash")))
        self.assertEqual(len(tr["analysis_src_hash"]), 12)
        # config_hash は検査設定の版（12桁）
        self.assertEqual(len(tr["config_hash"]), 12)
        # report.json にも同じハッシュが残る（既存キーは壊さない）
        rep = json.loads((snaps[0] / "report.json").read_text(encoding="utf-8"))
        for key in HASH_KEYS:
            self.assertEqual(rep["hashes"][key], tr[key])

    def test_config_hash_follows_house_style(self):
        """config_hash は検査設定（house_style.toml）の中身で変わる。"""
        from saga import _config_hash
        h1 = _config_hash(self.work)
        self.assertEqual(len(h1), 12)
        house = self.work / "_house" / "house_style.toml"
        house.parent.mkdir(parents=True, exist_ok=True)
        house.write_text('[residue]\nknown_terms = ["星辰"]\n', encoding="utf-8")
        h2 = _config_hash(self.work)
        self.assertEqual(len(h2), 12)
        self.assertNotEqual(h1, h2)


class TestRecoveryAndRevision(_FixtureBase):
    """確定済みブロックの再実行: 復旧（既定）と改訂（--revise）の分離（#16）。"""

    def test_recovery_without_revise_keeps_record_and_guides(self):
        """① 確定済み＋新ドラフト＋revise 無し → 記録不変・exit 0・案内表示。"""
        _place_draft(self.work, CLEAN_DRAFT)
        code, out = _run_commit(self.work, model="modelA", prompt_version="v1")
        self.assertEqual(code, 0, out)
        tr_before = json.loads(json.dumps(_block(self.work)["translation"]))
        self.assertEqual(tr_before["model"], "modelA")
        out_before = _out_path(self.work).read_bytes()

        draft2 = _place_draft(self.work, REVISED_DRAFT)
        code, out = _run_commit(self.work, model="modelB", prompt_version="v2")
        self.assertEqual(code, 0, out)

        b2 = _block(self.work)
        self.assertEqual(b2["translation"], tr_before, "由来の記録が上書きされた")
        self.assertNotIn("translation_history", b2)
        self.assertEqual(_out_path(self.work).read_bytes(), out_before,
                         "未使用の新ドラフトで確定出力が変わった")
        self.assertTrue(draft2.is_file(), "未使用の新ドラフトが消えた")
        self.assertIn("--revise", out, "--revise の案内が出ない")

    def test_revise_updates_record_and_keeps_history(self):
        """② --revise 付き → 記録更新＋履歴保持。"""
        _place_draft(self.work, CLEAN_DRAFT)
        code, out = _run_commit(self.work, model="modelA", prompt_version="v1")
        self.assertEqual(code, 0, out)
        old_tr = json.loads(json.dumps(_block(self.work)["translation"]))
        out_before = _out_path(self.work).read_bytes()

        _place_draft(self.work, REVISED_DRAFT)
        code, out = _run_commit(self.work, model="modelB", prompt_version="v2", revise=True)
        self.assertEqual(code, 0, out)

        b2 = _block(self.work)
        tr = b2["translation"]
        self.assertEqual(tr["model"], "modelB")
        self.assertEqual(tr["prompt_version"], "v2")
        self.assertEqual(tr["prev_draft_hash"], old_tr["draft_hash"])
        self.assertTrue(tr.get("revised_at"))
        hist = b2.get("translation_history")
        self.assertIsInstance(hist, list)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["model"], "modelA")
        self.assertEqual(hist[0]["draft_hash"], old_tr["draft_hash"])
        # 確定出力は新しい内容へ置き換わり、final_hash も新しくなる
        self.assertNotEqual(_out_path(self.work).read_bytes(), out_before)
        self.assertEqual(tr["final_hash"], _sha12(_out_path(self.work).read_bytes()))

    def test_revise_requires_new_draft(self):
        """--revise でも新しいドラフトが無ければ停止し、記録を壊さない。"""
        _place_draft(self.work, CLEAN_DRAFT)
        code, out = _run_commit(self.work)
        self.assertEqual(code, 0, out)
        code, out = _run_commit(self.work, revise=True)
        self.assertNotEqual(code, 0, out)
        self.assertIn("新しいドラフト", out)
        b = _block(self.work)
        self.assertTrue(b.get("translation"), "記録が消えた")
        self.assertEqual(b.get("status"), "emitted")

    def test_interrupted_recovery_completes_record_only(self):
        """③ 中断後の復旧（emit 済み・記録欠落）→ 記録だけ補完・exit 0。"""
        _place_draft(self.work, CLEAN_DRAFT)
        code, out = _run_commit(self.work, model="modelA", prompt_version="v1")
        self.assertEqual(code, 0, out)
        out_before = _out_path(self.work).read_bytes()
        done_before = _done_path(self.work).read_bytes()

        # 中断を模擬: 出力は在るが記録が欠落し、status が未反映
        meta = _lib.load_chapters_meta(self.work)
        b = _lib.block_meta(meta, "ch001/b1")
        b.pop("translation", None)
        b.pop("emitted_at", None)
        b["status"] = "pending"
        _lib.save_chapters_meta(self.work, meta)

        code, out = _run_commit(self.work, model="modelA", prompt_version="v1")
        self.assertEqual(code, 0, out)
        self.assertIn("補完", out)

        b2 = _block(self.work)
        self.assertEqual(b2.get("status"), "emitted")
        tr = b2.get("translation")
        self.assertIsInstance(tr, dict)
        self.assertEqual(tr["final_hash"], _sha12(_out_path(self.work).read_bytes()))
        self.assertEqual(tr["draft_hash"], _sha12(done_before))
        self.assertEqual(len(tr["config_hash"]), 12)
        # 確定出力・アーカイブは壊さない
        self.assertEqual(_out_path(self.work).read_bytes(), out_before)
        self.assertEqual(_done_path(self.work).read_bytes(), done_before)


class TestThreeRoutesCommonFinalize(_FixtureBase):
    """commit / safe_emit / qa_translation --emit の3経路が共通確定処理を通る（#17）。"""

    def test_commit_routes_through_common_finalize(self):
        import saga
        _place_draft(self.work, CLEAN_DRAFT)
        with mock.patch.object(saga, "_finalize_block", return_value=0) as m:
            code, out = _run_commit(self.work)
        self.assertEqual(code, 0, out)
        self.assertEqual(m.call_count, 1)
        args = m.call_args.args
        self.assertEqual(args[0], self.work)
        self.assertEqual(args[1:3], ("ch001", "b1"))

    def test_safe_emit_routes_through_common_finalize(self):
        import safe_emit
        import saga
        _place_draft(self.work, CLEAN_DRAFT)
        with mock.patch.object(saga, "_finalize_block", return_value=0) as m:
            old_argv = sys.argv
            sys.argv = ["safe_emit.py", str(self.work), "--chapter", "ch001", "--block", "b1"]
            try:
                code = safe_emit.main()
            finally:
                sys.argv = old_argv
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, 1)
        args = m.call_args.args
        self.assertEqual(args[0], self.work)
        self.assertEqual(args[1:3], ("ch001", "b1"))

    def test_qa_emit_routes_through_common_finalize(self):
        import qa_translation
        import saga
        _place_draft(self.work, CLEAN_DRAFT)
        ns = argparse.Namespace(work=self.work, chapter="ch001", block="b1",
                                draft=None, emit=True, no_strict=False, archive=False)
        with mock.patch.object(saga, "_finalize_block", return_value=0) as m:
            try:
                qa_translation._stage_impl(ns)
                code = None
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, 1)
        args = m.call_args.args
        self.assertEqual(args[0], self.work)
        self.assertEqual(args[1:3], ("ch001", "b1"))

    def test_all_routes_write_the_same_hashes_record(self):
        """同じ入力なら3経路の確定記録（由来ハッシュ）が一致する＝同じ検査・記録。"""
        routes = {
            "commit": lambda w: ["saga.py", "commit", str(w),
                                 "--chapter", "ch001", "--block", "b1"],
            "safe_emit": lambda w: ["safe_emit.py", str(w),
                                    "--chapter", "ch001", "--block", "b1"],
            "qa_emit": lambda w: ["qa_translation.py", str(w),
                                  "--chapter", "ch001", "--block", "b1", "--emit"],
        }
        records: dict[str, dict] = {}
        for name, build in routes.items():
            td = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, td, ignore_errors=True)
            work = _make_work(td)
            _place_draft(work, CLEAN_DRAFT)
            code, out = _run_cli(build(work))
            self.assertEqual(code, 0, f"{name}: {out}")
            self.assertTrue(_out_path(work).is_file(), f"{name}: 確定出力なし")
            tr = _block(work)["translation"]
            for key in HASH_KEYS:
                self.assertIn(key, tr, f"{name}: {key} が無い")
            records[name] = tr
        base = records["commit"]
        for name in ("safe_emit", "qa_emit"):
            for key in HASH_KEYS:
                self.assertEqual(records[name][key], base[key],
                                 f"{name} の {key} が commit と一致しない")

    def test_commit_blocks_on_align_failure(self):
        """commit: align fatal（段落欠落）では確定しない（非ゼロ・理由表示）。"""
        draft = _place_draft(self.work, [(1, "第一章　始まり"), (3, "雪が降り始めた。")])
        code, out = _run_commit(self.work)
        self.assertEqual(code, 1, out)
        self.assertFalse(_out_path(self.work).exists(), "検査で停止したのに確定した")
        self.assertIn("fatal", out)
        self.assertIn("段落番号", out)
        self.assertTrue(draft.is_file(), "停止時にドラフトを消してはいけない")

    def test_safe_emit_blocks_on_qa_failure(self):
        """safe_emit: QA 不合格（空段落）では確定しない（非ゼロ・理由表示）。"""
        _place_draft(self.work, [(1, "第一章　始まり"), (2, ""), (3, "雪が降り始めた。")])
        code, out = _run_cli(["safe_emit.py", str(self.work), "--chapter", "ch001", "--block", "b1"])
        self.assertEqual(code, 1, out)
        self.assertFalse(_out_path(self.work).exists(), "検査で停止したのに確定した")
        self.assertIn("QA", out)
        self.assertIn("検査で停止", out)

    def test_qa_emit_blocks_on_residue(self):
        """qa --emit: 残渣候補では確定しない（非ゼロ・理由表示）。"""
        _place_draft(self.work, [(1, "夜の星辰が輝く。"), (2, "二段目。"), (3, "三段目。")])
        code, out = _run_cli(["qa_translation.py", str(self.work),
                              "--chapter", "ch001", "--block", "b1", "--emit"])
        self.assertEqual(code, 1, out)
        self.assertFalse(_out_path(self.work).exists(), "検査で停止したのに確定した")
        self.assertIn("残渣", out)

    def test_qa_emit_locked_exits_3(self):
        """qa --emit も共有ロックの内側で確定する（ロック中は exit 3）。"""
        _place_draft(self.work, CLEAN_DRAFT)
        ok, reason = _lib.acquire_lock(self.work, "test")
        self.assertTrue(ok, reason)
        try:
            code, out = _run_cli(["qa_translation.py", str(self.work),
                                  "--chapter", "ch001", "--block", "b1", "--emit"])
        finally:
            _lib.release_lock(self.work)
        self.assertEqual(code, 3, out)
        self.assertIn("終了コード 3", out)


if __name__ == "__main__":
    unittest.main()
