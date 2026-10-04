"""saga.py CLI のテスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


def _make_work(tmp: Path, ch_count: int = 1, blk_count: int = 3) -> Path:
    """minimal work fixture."""
    work = tmp / "test_work"
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "_work").mkdir(parents=True)
    (work / "manifest.toml").write_text('title = "テスト"\nlang = "zh"\n', encoding="utf-8")

    chapters = []
    for ci in range(1, ch_count + 1):
        cid = f"ch{ci:03d}"
        (work / "output" / cid).mkdir(parents=True)
        blocks = []
        for bi in range(1, blk_count + 1):
            bid = f"b{bi}"
            blocks.append({
                "id": bid,
                "paras": [(bi - 1) * 4 + 1, bi * 4],
                "n_chars": 100,
            })
        chapters.append({"id": cid, "title": "", "status": "pending", "blocks": blocks})

    meta = {"work": {"title": "テスト", "author": "x", "lang": "zh"}, "chapters": chapters}
    _lib.save_chapters_meta(work, meta)

    # paras
    for ci in range(1, ch_count + 1):
        cid = f"ch{ci:03d}"
        _lib.save_paras(work, cid, {"paras": [{"n": i + 1, "text": f"原文{i + 1}。"} for i in range(blk_count * 4)]})

    return work


def _make_plot_file(work: Path, blocks: list[str]) -> None:
    """plot.toml を作成。"""
    path = work / "analysis" / "plot-[ネタバレあり].toml"
    lines = ["entries = []\n"] if not blocks else []
    for b in blocks:
        lines.append(f'[[entries]]\nblock = "{b}"\ntext = "概要。"\n')
    path.write_text("".join(lines), encoding="utf-8")


def _make_delta_file(path: Path, block: str, chars: list[dict] = None,
                     terms: list[dict] = None, plot: list[dict] = None) -> None:
    """差分TOMLファイルを作成。"""
    lines = [f'block = "{block}"\n']
    for c in (chars or []):
        lines.append("[[characters_delta]]\n")
        for k, v in c.items():
            if isinstance(v, dict):
                lines.append(f"[characters_delta.{k}]\n")
                for k2, v2 in v.items():
                    lines.append(f'{k2} = "{v2}"\n')
            else:
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


class TestSagaStatus(unittest.TestCase):
    """status サブコマンドのテスト。"""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.shutil = shutil
        self.work = _make_work(self.tmp)

    def tearDown(self):
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_status(self, extra_args: list[str] = None) -> tuple[int, str]:
        """status を実行して (終了コード, stdout+stderr) を返す。"""
        from scripts.saga import cmd_status
        import io
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_status(self.work)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code or 0, buf.getvalue()

    def test_status_normal(self):
        """正常進捗: 分析3/3（フラグも実体もあり）, 翻訳0/3"""
        _make_plot_file(self.work, ["ch001/b1", "ch001/b2", "ch001/b3"])
        # フラグを立てる（analysis_done_at fallback をテスト）
        meta = _lib.load_chapters_meta(self.work)
        for b in meta["chapters"][0]["blocks"]:
            b["analysis_done_at"] = "2026-01-01T00:00:00+00:00"
        _lib.save_chapters_meta(self.work, meta)
        code, out = self._run_status()
        self.assertEqual(code, 0)
        self.assertIn("分析 3/3 ブロック", out)
        self.assertIn("翻訳 0/3 ブロック", out)
        self.assertIn("警告: なし", out)

    def test_status_hole_warning(self):
        """HOLE: b1, b3 のみ実体がある → フラグのみワーニングが出る"""
        _make_plot_file(self.work, ["ch001/b1", "ch001/b3"])
        code, out = self._run_status()
        self.assertEqual(code, 0)
        self.assertIn("分析 2/3 ブロック", out)

    def test_status_flag_only_warning(self):
        """フラグのみ: analysis_done_at はあるが plot 実体がない"""
        meta = _lib.load_chapters_meta(self.work)
        meta["chapters"][0]["blocks"][0]["analysis_started_at"] = "2026-01-01T00:00:00+00:00"
        _lib.save_chapters_meta(self.work, meta)
        # plot ファイルは空
        _make_plot_file(self.work, [])
        code, out = self._run_status()
        self.assertEqual(code, 0)
        self.assertIn("フラグのみ", out)

    def test_status_body_only_warning(self):
        """実体のみ: plot 実体はあるがフラグがない"""
        _make_plot_file(self.work, ["ch001/b1", "ch001/b2", "ch001/b3"])
        # フラグを消す
        meta = _lib.load_chapters_meta(self.work)
        for b in meta["chapters"][0]["blocks"]:
            b.pop("analysis_done_at", None)
        _lib.save_chapters_meta(self.work, meta)
        code, out = self._run_status()
        self.assertEqual(code, 0)
        self.assertIn("実体のみ", out)

    def test_status_no_work_dir(self):
        """work が存在しない → 終了コード 1"""
        from scripts.saga import cmd_status
        code = cmd_status(self.tmp / "nonexistent")
        self.assertEqual(code, 1)

    def test_status_no_meta(self):
        """chapters_meta が無い → 終了コード 1"""
        bare = self.tmp / "bare"
        bare.mkdir()
        from scripts.saga import cmd_status
        code = cmd_status(bare)
        self.assertEqual(code, 1)


class TestSagaAnalyzeCommit(unittest.TestCase):
    """analyze-commit サブコマンドのテスト。"""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.shutil = shutil
        self.work = _make_work(self.tmp)
        # analysis TOML ファイルを作成（中身は空構造）
        chars_path = self.work / "analysis" / "characters-[ネタバレあり].toml"
        _lib.save_toml(chars_path, {"characters": []})
        terms_path = self.work / "analysis" / "terms.toml"
        _lib.save_toml(terms_path, {"terms": []})
        plot_path = self.work / "analysis" / "plot-[ネタバレあり].toml"
        _lib.save_toml(plot_path, {"entries": []})

    def tearDown(self):
        # cleanup lock if exists
        lock = self.work / "_work" / ".lock"
        if lock.exists():
            lock.unlink()
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_ac(self, delta_path: Path) -> tuple[int, str]:
        """analyze-commit を実行。"""
        from scripts.saga import cmd_analyze_commit
        import io
        old_out, old_err = sys.stdout, sys.stderr
        buf = io.StringIO()
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_analyze_commit(self.work, delta_path)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code or 0, buf.getvalue()

    def _assert_analysis_field(self, field: str, key: str, expected: str) -> None:
        """分析 TOML の特定エントリのフィールドを検証。"""
        chars = _lib.load_characters(self.work)
        for c in chars.get("characters", []):
            if c.get("name_src", "") == key:
                self.assertEqual(c.get(field, "").strip(), expected)
                return
        self.fail(f"characters に {key} が見つからない")

    def _assert_terms_field(self, field: str, key: str, expected: str) -> None:
        terms = _lib.load_terms(self.work)
        for t in terms.get("terms", []):
            if t.get("term_src", "") == key:
                self.assertEqual(t.get(field, "").strip(), expected)
                return
        self.fail(f"terms に {key} が見つからない")

    def _assert_plot_block(self, block: str) -> bool:
        plot = _lib.load_plot(self.work)
        return any(
            str(e.get("block", "")).strip() == block.strip()
            for e in plot.get("entries", [])
        )

    def test_ac_merge_success(self):
        """差分マージ成功: 3ファイルが更新される。"""
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1",
                         chars=[{"match_key": "name_src", "match_value": "张三",
                                 "name_ja": "チョウサン", "role": "主人公"}],
                         terms=[{"match_key": "term_src", "match_value": "合成門派A",
                                 "term_ja": "合成門派A"}],
                         plot=[{"block": "ch001/b1", "text": "张三登場。"}])
        code, out = self._run_ac(d)
        self.assertEqual(code, 0)
        self.assertIn("3 件マージ", out)
        self._assert_analysis_field("role", "张三", "主人公")
        self._assert_terms_field("term_ja", "合成門派A", "合成門派A")
        self.assertTrue(self._assert_plot_block("ch001/b1"))

    def test_ac_existing_ja_goes_to_proposed(self):
        """既存訳語変更は proposed に退避。"""
        # 事前にキャラを作る
        chars_path = self.work / "analysis" / "characters-[ネタバレあり].toml"
        _lib.save_toml(chars_path, {"characters": [{
            "name_src": "张三", "name_ja": "チョウサン", "role": "主人公"
        }]})
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1",
                         chars=[{"match_key": "name_src", "match_value": "张三",
                                 "name_ja": "ジョウサン"}])
        code, out = self._run_ac(d)
        self.assertEqual(code, 0)
        self.assertIn("proposed", out)
        chars = _lib.load_characters(self.work)
        for c in chars.get("characters", []):
            if c.get("name_src", "") == "张三":
                # 既存は変わらない
                self.assertEqual(c.get("name_ja", ""), "チョウサン")
                self.assertIn("name_ja", c.get("proposed", ""))
                return

    def test_ac_rollback_on_failure(self):
        """検証失敗（delta の型不正）→ 何も書かない（3ファイル all-or-nothing）。"""
        # 差分のスキーマ違反（characters_delta が配列でない）→ マージ検証失敗
        chars_path = self.work / "analysis" / "characters-[ネタバレあり].toml"
        terms_path = self.work / "analysis" / "terms.toml"
        plot_path = self.work / "analysis" / "plot-[ネタバレあり].toml"
        before = {
            chars_path: chars_path.read_bytes(),
            terms_path: terms_path.read_bytes(),
            plot_path: plot_path.read_bytes(),
        }
        d = self.tmp / "delta.toml"
        d.write_text('block = "ch001/b1"\ncharacters_delta = 1\n', encoding="utf-8")
        code, out = self._run_ac(d)
        self.assertEqual(code, 1)
        # 3ファイルとも一切書かれていない
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data, f"{path.name} が書き換わった")
        # block の確定フラグも立たない
        meta = _lib.load_chapters_meta(self.work)
        b = meta["chapters"][0]["blocks"][0]
        self.assertNotIn("analysis_committed_at", b)

    def test_ac_all_or_nothing_on_corrupt_existing_file(self):
        """既存の分析ファイルが壊れていても部分書き込みしない（all-or-nothing）。"""
        # terms.toml を壊す（読み込み時に例外）
        terms_path = self.work / "analysis" / "terms.toml"
        terms_path.write_text("this is not = = valid [[[", encoding="utf-8")
        chars_before = (self.work / "analysis" / "characters-[ネタバレあり].toml").read_bytes()
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1",
                         plot=[{"block": "ch001/b1", "text": "要点。"}])
        code, _ = self._run_ac(d)
        self.assertEqual(code, 1)
        # 壊れたファイルはそのまま（上書きしない）、他ファイルにも書かない
        self.assertEqual(terms_path.read_text(encoding="utf-8"),
                         "this is not = = valid [[[")
        self.assertEqual(
            (self.work / "analysis" / "characters-[ネタバレあり].toml").read_bytes(),
            chars_before)
        plot_path = self.work / "analysis" / "plot-[ネタバレあり].toml"
        self.assertNotIn("要点。", plot_path.read_text(encoding="utf-8"))

    def test_ac_reemit_is_record_only_idempotent(self):
        """冪等リカバリ: 同一 delta の再実行は重複せず、確定フラグも保たれる。"""
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1",
                         plot=[{"block": "ch001/b1", "text": "要点。"}])
        code1, _ = self._run_ac(d)
        self.assertEqual(code1, 0)
        meta1 = _lib.load_chapters_meta(self.work)
        flag1 = meta1["chapters"][0]["blocks"][0].get("analysis_committed_at")
        self.assertTrue(flag1)

        # 再実行 → exit 0、plot は重複しない、既存の確定フラグを上書きしない
        code2, _ = self._run_ac(d)
        self.assertEqual(code2, 0)
        plot = _lib.load_plot(self.work)
        entries = [e for e in plot.get("entries", [])
                   if str(e.get("block", "")).strip() == "ch001/b1"]
        self.assertEqual(len(entries), 1, "同一 plot は重複しない")
        meta2 = _lib.load_chapters_meta(self.work)
        self.assertEqual(
            meta2["chapters"][0]["blocks"][0].get("analysis_committed_at"), flag1,
            "既存の確定フラグを書き換えない")

    def test_ac_multiple(self):
        """複数 delta のマージ。"""
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1",
                         chars=[{"match_key": "name_src", "match_value": "张三",
                                 "name_ja": "チョウサン", "role": "主人公"}],
                         terms=[{"match_key": "term_src", "match_value": "合成門派A",
                                 "term_ja": "合成門派A"}],
                         plot=[{"block": "ch001/b1", "text": "张三登場。"}])
        code1, _ = self._run_ac(d)
        self.assertEqual(code1, 0)
        # 2回目 — plot は冪等、キャラは既存訳語なので proposed
        d2 = self.tmp / "delta2.toml"
        _make_delta_file(d2, "ch001/b2",
                         chars=[{"match_key": "name_src", "match_value": "张三",
                                 "name_ja": "ジョウサン"}],
                         terms=[{"match_key": "term_src", "match_value": "合成門派B",
                                 "term_ja": "合歓宗"}],
                         plot=[{"block": "ch001/b2", "text": "新展開。"}])
        code2, out2 = self._run_ac(d2)
        self.assertEqual(code2, 0)
        # キャラは proposed に退避
        chars = _lib.load_characters(self.work)
        for c in chars.get("characters", []):
            if c.get("name_src", "") == "张三":
                self.assertEqual(c.get("name_ja", ""), "チョウサン")  # 変わらない
                self.assertIn("proposed", c)
        # terms は新規追加
        self._assert_terms_field("term_ja", "合成門派B", "合歓宗")

    def test_idempotent_plot(self):
        """同じ plot delta を2回実行 → 重複しない。"""
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1",
                         plot=[{"block": "ch001/b1", "text": "イベント。"}])
        code1, _ = self._run_ac(d)
        self.assertEqual(code1, 0)
        code2, _ = self._run_ac(d)
        self.assertEqual(code2, 0)
        plot = _lib.load_plot(self.work)
        matches = [e for e in plot.get("entries", [])
                   if str(e.get("block", "")).strip() == "ch001/b1"
                   and str(e.get("text", "")).strip() == "イベント。"]
        self.assertEqual(len(matches), 1, "重複してはいけない")

    def test_lock_exclusion(self):
        """ロック中は終了コード 3。"""
        lock = self.work / "_work" / ".lock"
        lock.write_text("99999\n", encoding="utf-8")
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1", plot=[{"block": "ch001/b1", "text": "t"}])
        code, out = self._run_ac(d)
        self.assertEqual(code, 3)
        self.assertIn("終了コード 3", out)

    def test_lock_stale_override(self):
        """stale ロック（10分超）は上書きして実行される。"""
        lock = self.work / "_work" / ".lock"
        lock.write_text("99999\n", encoding="utf-8")
        old_mtime = time.time() - 601
        os.utime(str(lock), (old_mtime, old_mtime))
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1", plot=[{"block": "ch001/b1", "text": "t"}])
        code, out = self._run_ac(d)
        self.assertEqual(code, 0)
        self.assertFalse(lock.exists(), "ロックは後片付けされる")

    def test_committed_at_flag(self):
        """コミット成功後、block に analysis_committed_at が立つ。"""
        d = self.tmp / "delta.toml"
        _make_delta_file(d, "ch001/b1", plot=[{"block": "ch001/b1", "text": "t"}])
        code, _ = self._run_ac(d)
        self.assertEqual(code, 0)
        meta = _lib.load_chapters_meta(self.work)
        b = meta["chapters"][0]["blocks"][0]
        self.assertIn("analysis_committed_at", b)
        self.assertTrue(b["analysis_committed_at"])


class TestZeroStatePrepare(unittest.TestCase):
    """ゼロ状態（ingest 済み・split 未実行）の prepare は丁寧に終了する（Traceback 禁止）。

    CLI をサブプロセスで実行し、実行時エラー（NameError 等）も捕まえる。
    """

    def test_prepare_before_split_exits_cleanly(self):
        import subprocess, tempfile
        tmp = Path(tempfile.mkdtemp(prefix="zs_prepare_"))
        src = tmp / "novel.txt"
        src.write_text("---\ntitle: T\n---\n\n段落一。\n\n段落二。\n", encoding="utf-8")
        r1 = subprocess.run(
            [sys.executable, str(SCRIPTS / "ingest.py"), str(src), "--dst", "ja"],
            capture_output=True, text=True, cwd=str(tmp))
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        work = next(tmp.rglob("manifest.toml")).parent
        r2 = subprocess.run(
            [sys.executable, str(SCRIPTS / "saga.py"), "prepare", str(work), "--mode", "analysis"],
            capture_output=True, text=True, cwd=str(tmp))
        out = r2.stdout + r2.stderr
        self.assertEqual(r2.returncode, 1, out)
        self.assertIn("chapters が空です", out)
        self.assertNotIn("Traceback", out, "未初期化状態で Traceback を出してはいけない")


if __name__ == "__main__":
    unittest.main()