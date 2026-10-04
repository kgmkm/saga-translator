"""分析マージのトランザクション（#18）と、確定と次コンテキスト生成の分離（#24）のテスト。

合成データのみ使用。作品固有情報は含めない。

- #18: ``_merge_delta`` は all-or-nothing。3ファイルのうち書き込み途中で失敗しても、
  書き込み済みファイルを元のバイト列に復元し、部分状態を残さない。
- #24: commit は確定（emit）成功で exit 0。確定後の「次のコンテキスト生成」は
  確定の外にあり、失敗しても exit 0 のまま警告を残す。確定自体の失敗は非ゼロのまま。
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------

def _make_analysis_work(base: Path, blk_count: int = 2) -> Path:
    """analysis 3ファイルを持つ最小ワーク（合成データ）。"""
    work = base / "test_work"
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "_work").mkdir(parents=True)
    (work / "manifest.toml").write_text('title = "T"\nlang = "zh"\ndst = "ja"\n',
                                        encoding="utf-8")
    blocks = [{"id": f"b{i}", "paras": [(i - 1) * 4 + 1, i * 4], "n_chars": 100}
              for i in range(1, blk_count + 1)]
    meta = {"work": {"title": "T", "author": "x", "lang": "zh"},
            "chapters": [{"id": "ch001", "title": "", "status": "pending",
                          "blocks": blocks}]}
    _lib.save_chapters_meta(work, meta)
    _lib.save_paras(work, "ch001",
                    {"paras": [{"n": i + 1, "text": f"原文{i + 1}。"}
                               for i in range(blk_count * 4)]})

    # analysis 3ファイル（初期は空構造）
    _lib.save_toml(work / "analysis" / "characters-[ネタバレあり].toml",
                   {"characters": []})
    _lib.save_toml(work / "analysis" / "terms.toml", {"terms": []})
    _lib.save_toml(work / "analysis" / "plot-[ネタバレあり].toml", {"entries": []})
    return work


def _write_delta(path: Path, block: str, chars: list[dict] = None,
                 terms: list[dict] = None, plot: list[dict] = None) -> None:
    lines = [f'block = "{block}"\n']
    for c in (chars or []):
        lines.append("[[characters_delta]]\n")
        for k, v in c.items():
            lines.append(f'{k} = "{v}"\n')
    for t in (terms or []):
        lines.append("[[terms_delta]]\n")
        for k, v in t.items():
            lines.append(f'{k} = "{v}"\n')
    for p in (plot or []):
        lines.append("[[plot_delta]]\n")
        for k, v in p.items():
            lines.append(f'{k} = "{v}"\n')
    path.write_text("".join(lines), encoding="utf-8")


_ANALYSIS_FILES = (
    "characters-[ネタバレあり].toml",
    "terms.toml",
    "plot-[ネタバレあり].toml",
)


def _snapshot(work: Path) -> dict[Path, bytes]:
    return {(work / "analysis" / n): (work / "analysis" / n).read_bytes()
            for n in _ANALYSIS_FILES}


def _assert_all_unchanged(tc: unittest.TestCase, work: Path, before: dict[Path, bytes]) -> None:
    for path, data in before.items():
        tc.assertEqual(path.read_bytes(), data, f"{path.name} が書き換わった")


# ---------------------------------------------------------------------------
# #18: _merge_delta の all-or-nothing
# ---------------------------------------------------------------------------

class TestMergeDeltaAllOrNothing(unittest.TestCase):
    """_merge_delta が書き込み途中で失敗しても部分状態を残さない。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = _make_analysis_work(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _delta(self, block: str = "ch001/b1") -> dict:
        return {
            "block": block,
            "characters_delta": [{"match_key": "name_src", "match_value": "张三",
                                   "name_ja": "チョウサン", "role": "主人公"}],
            "terms_delta": [{"match_key": "term_src", "match_value": "太清宗",
                             "term_ja": "太清宗"}],
            "plot_delta": [{"block": block, "text": "要点。"}],
        }

    def test_merge_success_writes_all_three(self):
        """成功時: 3ファイルがすべて更新される（正常系の回帰）。"""
        from saga import _merge_delta
        ok, logs = _merge_delta(self.work, self._delta())
        self.assertTrue(ok, logs)
        chars = _lib.load_characters(self.work)
        self.assertTrue(any(c.get("name_src") == "张三" for c in chars["characters"]))
        terms = _lib.load_terms(self.work)
        self.assertTrue(any(t.get("term_src") == "太清宗" for t in terms["terms"]))
        plot = _lib.load_plot(self.work)
        self.assertTrue(any(e.get("block") == "ch001/b1" for e in plot["entries"]))

    def test_third_write_failure_restores_all_bytes(self):
        """3ファイル目の書き込みを強制失敗 → 全ファイルがバイト無変更、失敗を返す。

        1・2番目は実際に書き込みが走るため、復元が効かなければバイトが変わる。
        """
        from saga import _merge_delta
        before = _snapshot(self.work)

        real_save = _lib.save_toml
        calls = {"n": 0}

        def flaky_save(path, obj):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk full (injected)")
            return real_save(path, obj)

        import saga
        with mock.patch.object(saga, "save_toml", side_effect=flaky_save):
            ok, logs = _merge_delta(self.work, self._delta())

        self.assertFalse(ok, "書き込み失敗は False を返す")
        self.assertTrue(any("書き込み失敗" in l for l in logs), logs)
        # 1・2番目は書かれた後に復元されているはず → 全ファイルがバイト無変更
        _assert_all_unchanged(self, self.work, before)

    def test_write_failure_leaves_no_partial_state_via_cli(self):
        """CLI 経由で3ファイル目書き込み失敗 → exit 非ゼロ・全ファイル無変更・フラグ立たず。"""
        from saga import cmd_analyze_commit
        d = self.tmp / "delta.toml"
        _write_delta(d, "ch001/b1",
                     chars=[{"match_key": "name_src", "match_value": "张三",
                             "name_ja": "チョウサン"}],
                     terms=[{"match_key": "term_src", "match_value": "太清宗",
                             "term_ja": "太清宗"}],
                     plot=[{"block": "ch001/b1", "text": "要点。"}])
        before = _snapshot(self.work)
        meta_before = (self.work / "chapters_meta.toml").read_bytes()

        real_save = _lib.save_toml
        calls = {"n": 0}

        def flaky_save(path, obj):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk full (injected)")
            return real_save(path, obj)

        import saga
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            with mock.patch.object(saga, "save_toml", side_effect=flaky_save):
                code = cmd_analyze_commit(self.work, d)
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        self.assertNotEqual(code, 0, "書き込み失敗は非ゼロで終了する")
        _assert_all_unchanged(self, self.work, before)
        self.assertEqual((self.work / "chapters_meta.toml").read_bytes(), meta_before,
                         "失敗時は chapters_meta も書かない")
        meta = _lib.load_chapters_meta(self.work)
        self.assertNotIn("analysis_committed_at", meta["chapters"][0]["blocks"][0])

    def test_corrupt_existing_file_no_partial_write(self):
        """既存の terms.toml が壊れている → 何も書かずに失敗（部分書き込みしない）。"""
        from saga import cmd_analyze_commit
        terms_path = self.work / "analysis" / "terms.toml"
        terms_path.write_text("this is not = = valid [[[", encoding="utf-8")
        before = _snapshot(self.work)

        d = self.tmp / "delta.toml"
        _write_delta(d, "ch001/b1", plot=[{"block": "ch001/b1", "text": "要点。"}])
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_analyze_commit(self.work, d)
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        self.assertEqual(code, 1)
        # 壊れたファイルはそのまま、他ファイルも無変更
        self.assertEqual(terms_path.read_text(encoding="utf-8"),
                         "this is not = = valid [[[")
        _assert_all_unchanged(self, self.work, before)

    def test_missing_target_file_is_created_atomically(self):
        """対象ファイルが無い（新規パス）でも all-or-nothing が成立する。"""
        from saga import _merge_delta
        # characters ファイルを消す（_analysis_path はデフォルト名を指す）
        (self.work / "analysis" / "characters-[ネタバレあり].toml").unlink()
        ok, logs = _merge_delta(self.work, self._delta())
        self.assertTrue(ok, logs)
        self.assertTrue((self.work / "analysis" /
                         "characters-[ネタバレあり].toml").is_file())

    def test_missing_target_file_write_failure_removes_created(self):
        """新規作成したファイルの後続書き込みが失敗 → 作成したファイルも消す。"""
        from saga import _merge_delta
        # characters を消しておく（1番目で新規作成される）
        (self.work / "analysis" / "characters-[ネタバレあり].toml").unlink()

        real_save = _lib.save_toml
        calls = {"n": 0}

        def flaky_save(path, obj):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk full (injected)")
            return real_save(path, obj)

        import saga
        with mock.patch.object(saga, "save_toml", side_effect=flaky_save):
            ok, logs = _merge_delta(self.work, self._delta())

        self.assertFalse(ok, logs)
        # 1番目で作った新規ファイルは復元（=削除）される
        self.assertFalse((self.work / "analysis" /
                          "characters-[ネタバレあり].toml").exists(),
                         "新規作成ファイルもロールバックされる")


# ---------------------------------------------------------------------------
# #24: 確定と次コンテキスト生成の分離
# ---------------------------------------------------------------------------

def _make_translatable_work(base: Path) -> Path:
    """commit 可能な最小ワーク（zh→ja、確定できるドラフトを後で置く）。"""
    work = base / "test_work"
    (work / "manifest.toml").parent.mkdir(parents=True, exist_ok=True)
    (work / "manifest.toml").write_text('lang = "zh"\ndst = "ja"\n', encoding="utf-8")
    (work / "chapters_meta.toml").write_text(
        '[[chapters]]\nid = "ch001"\n'
        'blocks = [{id="b1", paras=[1,3]}, {id="b2", paras=[4,5]}]\n',
        encoding="utf-8")
    (work / "chapters").mkdir(parents=True, exist_ok=True)
    (work / "chapters" / "ch001.paras.toml").write_text(
        '[[paras]]\nn = 1\ntext = "第一段。测试内容。"\n\n'
        '[[paras]]\nn = 2\ntext = "第二段。她笑着说。"\n\n'
        '[[paras]]\nn = 3\ntext = "第三段。星辰闪烁在夜空中。"\n\n'
        '[[paras]]\nn = 4\ntext = "第四段。"\n\n'
        '[[paras]]\nn = 5\ntext = "第五段。他匆匆离开。"\n',
        encoding="utf-8")
    (work / "analysis").mkdir(exist_ok=True)
    (work / "analysis" / "characters-[ネタバレあり].toml").write_text(
        '[[characters]]\nname_src = "张三"\nname_ja = "张三"\n', encoding="utf-8")
    (work / "analysis" / "terms.toml").write_text(
        '[[terms]]\nterm_src = "灵氣"\nterm_ja = "霊気"\n', encoding="utf-8")
    (work / "analysis" / "plot-[ネタバレあり].toml").write_text(
        '[[entries]]\nblock = "ch001/b1"\ntext = "张三が登場。"\n', encoding="utf-8")
    return work


def _make_draft(work: Path, cid: str, bid: str, paras: list[tuple[int, str]]) -> Path:
    from _lib import direction, target_suffix
    _, dst = direction(work)
    suf = target_suffix(dst)
    draft = work / "output" / "_work" / f"{cid}_{bid}{suf}_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("\n\n".join(f"[P{n:03d}] {t}" for n, t in paras),
                     encoding="utf-8")
    return draft


class TestCommitNextContextSeparation(unittest.TestCase):
    """commit の確定と次コンテキスト生成が分離されている（#24）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = _make_translatable_work(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clean_draft(self):
        _make_draft(self.work, "ch001", "b1",
                    [(1, "一段目の訳。"), (2, "二段目の訳。"), (3, "三段目の訳。")])

    def _run_commit(self) -> tuple[int, str]:
        from saga import cmd_commit
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code, buf.getvalue()

    def test_next_context_failure_keeps_exit_zero(self):
        """確定後に次コンテキスト生成を強制失敗 → exit 0・確定記録あり・警告あり。"""
        self._clean_draft()

        import build_context
        def boom(*a, **k):
            raise RuntimeError("context build failed (injected)")

        with mock.patch.object(build_context, "_stage_impl", side_effect=boom):
            code, out = self._run_commit()

        self.assertEqual(code, 0, f"確定は成功なので exit 0 のはず:\n{out}")
        # 確定記録（output と status=emitted）
        out_p = self.work / "output" / "ch001" / "b1_ja.md"
        self.assertTrue(out_p.exists(), "確定出力が存在する")
        meta = _lib.load_chapters_meta(self.work)
        blk = meta["chapters"][0]["blocks"][0]
        self.assertEqual(blk.get("status"), "emitted")
        # 警告が出る
        self.assertIn("次のコンテキスト生成に失敗", out)

    def test_next_context_success_line_present(self):
        """正常時: 次コンテキストの案内が出て exit 0。"""
        self._clean_draft()
        code, out = self._run_commit()
        self.assertEqual(code, 0, out)
        self.assertIn("[next]", out)

    def test_confirm_failure_still_nonzero(self):
        """確定自体の失敗（QA 不合格）は従来どおり非ゼロ。"""
        # 段落欠落 → align fatal
        _make_draft(self.work, "ch001", "b1", [(1, "一段目。"), (3, "三段目。")])
        code, out = self._run_commit()
        self.assertNotEqual(code, 0)
        out_p = self.work / "output" / "ch001" / "b1_ja.md"
        self.assertFalse(out_p.exists(), "確定していない")

    def test_timeline_records_success_when_next_context_fails(self):
        """次コンテキスト失敗時も commit の timeline は success のまま。"""
        import json
        self._clean_draft()

        import build_context
        def boom(*a, **k):
            raise RuntimeError("context build failed (injected)")

        with mock.patch.object(build_context, "_stage_impl", side_effect=boom):
            code, out = self._run_commit()
        self.assertEqual(code, 0)

        tl = self.work / "logs" / "timeline.jsonl"
        self.assertTrue(tl.is_file(), "timeline が書かれる")
        entries = [json.loads(l) for l in tl.read_text(encoding="utf-8").splitlines()
                   if l.strip()]
        commit_entries = [e for e in entries if e.get("stage") == "commit"]
        self.assertEqual(len(commit_entries), 1, entries)
        self.assertEqual(commit_entries[0].get("reason"), "success")
        self.assertEqual(commit_entries[0].get("exit_code"), 0)


if __name__ == "__main__":
    unittest.main()