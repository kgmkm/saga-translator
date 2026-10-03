"""saga.py prepare/commit の統合テスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, lang: str = "zh", dst: str = "ja") -> Path:
    """最小の翻訳可能な作品ディレクトリ（zh→ja）。"""
    work = base / "test_work"
    _write(work / "manifest.toml", f'lang = "{lang}"\ndst = "{dst}"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}, {id="b2", paras=[4,5]}]

[[chapters]]
id = "ch002"
blocks = [{id="b1", paras=[1,1]}]
""")
    _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "第一段。测试内容。"

[[paras]]
n = 2
text = "第二段。她笑着说。"

[[paras]]
n = 3
text = "第三段。星辰闪烁在夜空中。"

[[paras]]
n = 4
text = "第四段。"

[[paras]]
n = 5
text = "第五段。他匆匆离开。"
""")
    _write(work / "chapters" / "ch002.paras.toml", """\
[[paras]]
n = 1
text = "第二章。继续。"
""")
    _write(work / "analysis" / "characters-[ネタバレあり].toml", """\
[[characters]]
name_src = "林霖"
name_ja = "林霖"
ruby = "りんりん"
first_person = "私"
second_person = "あなた"
""")
    _write(work / "analysis" / "terms.toml", """\
[[terms]]
term_src = "灵氣"
term_ja = "霊気"
""")
    _write(work / "analysis" / "plot-[ネタバレあり].toml", """\
[[entries]]
block = "ch001/b1"
text = "林霖が登場。"
""")
    return work


def _make_draft(work: Path, cid: str, bid: str, paras: list[tuple[int, str]]) -> Path:
    """output/_work/ にドラフトを作成し、そのパスを返す。"""
    import hashlib
    from _lib import direction, target_suffix
    _, dst = direction(work)
    suf = target_suffix(dst)
    draft = work / "output" / "_work" / f"{cid}_{bid}{suf}_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n, text in paras:
        lines.append(f"[P{n:03d}] {text}")
    draft.write_text("\n\n".join(lines), encoding="utf-8")
    return draft


class TestPrepare(unittest.TestCase):
    """saga.py prepare のテスト。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _prepare(self, work: str, **kwargs) -> int:
        """saga.py の cmd_prepare を直接呼ぶ。"""
        from saga import cmd_prepare
        return cmd_prepare(Path(work), kwargs.get("chapter"), kwargs.get("block"))

    def test_prepare_auto_selects_first_undone(self):
        """未確定ブロックを自動選択してコンテキストを生成。"""
        work = _make_work(self.td)
        rc = self._prepare(str(work))
        self.assertEqual(rc, 0)
        ctx = work / "translation_input" / "ch001_b1_translation.md"
        self.assertTrue(ctx.exists())
        content = ctx.read_text(encoding="utf-8")
        self.assertIn("b1", content)

    def test_prepare_with_chapter_and_block(self):
        """指定された章・ブロックのコンテキストを生成。"""
        work = _make_work(self.td)
        # b2 はまだ出力がないので選択される
        rc = self._prepare(str(work), chapter="ch001", block="b2")
        self.assertEqual(rc, 0)
        ctx = work / "translation_input" / "ch001_b2_translation.md"
        self.assertTrue(ctx.exists())

    def test_prepare_skips_emitted_block(self):
        """emit 済みブロックをスキップして次の未確定を選ぶ。"""
        work = _make_work(self.td)
        # b1 を emit 済みにする
        from _lib import direction, target_suffix
        _, dst = direction(work)
        suf = target_suffix(dst)
        out_dir = work / "output" / "ch001"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"b1{suf}.md").write_text("訳文。\n", encoding="utf-8")
        # prepare — b1 はスキップ、b2 が選ばれる
        rc = self._prepare(str(work), chapter="ch001")
        self.assertEqual(rc, 0)
        ctx = work / "translation_input" / "ch001_b2_translation.md"
        self.assertTrue(ctx.exists())

    def test_prepare_all_done_returns_1(self):
        """全ブロック確定済みなら exit 1。"""
        work = _make_work(self.td)
        from _lib import direction, target_suffix
        _, dst = direction(work)
        suf = target_suffix(dst)
        for cid in ("ch001", "ch002"):
            out_dir = work / "output" / cid
            out_dir.mkdir(parents=True, exist_ok=True)
            meta = _lib.load_chapters_meta(work)
            for c in meta["chapters"]:
                if c["id"] == cid:
                    for b in c["blocks"]:
                        (out_dir / f"{b['id']}{suf}.md").write_text("x\n", encoding="utf-8")
                    break
        # 章指定
        rc = self._prepare(str(work), chapter="ch001")
        self.assertEqual(rc, 1)
        # 全体
        rc = self._prepare(str(work))
        self.assertEqual(rc, 1)

    def test_prepare_idempotent(self):
        """同じブロックの prepare を2回実行しても決定論的。"""
        work = _make_work(self.td)
        rc1 = self._prepare(str(work))
        self.assertEqual(rc1, 0)
        ctx1 = (work / "translation_input" / "ch001_b1_translation.md").read_text(encoding="utf-8")
        rc2 = self._prepare(str(work))
        self.assertEqual(rc2, 0)
        ctx2 = (work / "translation_input" / "ch001_b1_translation.md").read_text(encoding="utf-8")
        self.assertEqual(ctx1, ctx2)

    def test_prepare_nonexistent_work(self):
        """存在しない作品 → 1。"""
        rc = self._prepare(str(self.td / "nonexistent"))
        self.assertEqual(rc, 1)


class TestCommit(unittest.TestCase):
    """saga.py commit のテスト。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _commit(self, **kwargs) -> int:
        """saga.py の cmd_commit を直接呼ぶ。"""
        from saga import cmd_commit
        return cmd_commit(
            self.work,
            kwargs.get("chapter", "ch001"),
            kwargs.get("block", "b1"),
            kwargs.get("model", "test-model"),
            kwargs.get("prompt_version", "v1"),
        )

    def _make_clean_draft(self):
        """QA を通る完全なドラフトを作成。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "一段目の訳。"),
            (2, "二段目の訳。"),
            (3, "三段目の訳。"),
        ])

    def test_commit_normal(self):
        """正常系: 整形→align→QA→残渣→emit→done移動→記録→next。"""
        self._make_clean_draft()
        rc = self._commit()
        self.assertEqual(rc, 0)
        # emit されたか
        out = self.work / "output" / "ch001" / "b1_ja.md"
        self.assertTrue(out.exists())
        # parallel
        par = self.work / "output" / "ch001" / "b1_parallel.md"
        self.assertTrue(par.exists())
        # ドラフトが _work/done/ に移動されたか
        done = self.work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md"
        self.assertTrue(done.exists())
        # 元のドラフトは消えている
        orig = self.work / "output" / "_work" / "ch001_b1_ja_numbered.md"
        self.assertFalse(orig.exists())
        # chapters_meta が emitted
        meta = _lib.load_chapters_meta(self.work)
        ch = next(c for c in meta["chapters"] if c["id"] == "ch001")
        blk = next(b for b in ch["blocks"] if b["id"] == "b1")
        self.assertEqual(blk.get("status"), "emitted")
        # 版記録
        tr = blk.get("translation", {})
        self.assertEqual(tr.get("model"), "test-model")

    def test_commit_fatal_missing_marker(self):
        """段落欠落ドラフト → exit 1、emit されない、_work/done/ に何も無い。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "一段目。"),
            (3, "三段目。"),  # P2 がない
        ])
        rc = self._commit()
        self.assertEqual(rc, 1)
        out = self.work / "output" / "ch001" / "b1_ja.md"
        self.assertFalse(out.exists())
        done = self.work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md"
        self.assertFalse(done.exists())

    def test_commit_residue(self):
        """残渣候補（safe_fix あり）を含むドラフト → emit は成功（候補として表示のみ）。"""
        _make_draft(self.work, "ch001", "b1", [
            (1, "夜の星辰が輝く。"),
            (2, "二段目の訳。"),
            (3, "三段目の訳。"),
        ])
        rc = self._commit()
        self.assertEqual(rc, 0)  # safe_fix ありの候補はブロックしない
        out = self.work / "output" / "ch001" / "b1_ja.md"
        self.assertTrue(out.exists())

    def test_commit_confirm_unacked(self):
        """confirm 未ack → exit 1、ack 後に成功。"""
        from saga import _draft_path, cmd_commit

        # 文字数比 confirm を発生させるために、原文を 25字以上に上書き
        long_src = "赤い月が静かに空に浮かんでいる。その下では、大きな祭りが開催されている。" * 3  # 51字
        (self.work / "chapters" / "ch001.paras.toml").write_text(f"""\
[[paras]]
n = 1
text = "{long_src}"

[[paras]]
n = 2
text = "普通の長さのテキスト。"

[[paras]]
n = 3
text = "普通の長さのテキスト。"
""", encoding="utf-8")

        # ドラフト: 原文 n=1 は 51字、訳文を短くして confirm を出す
        _make_draft(self.work, "ch001", "b1", [
            (1, "短すぎる訳。"),  # 原文51字→5字 → 比率 ~0.1
            (2, "普通の長さの訳文。"),
            (3, "普通の長さの訳文。"),
        ])

        # 初回: confirm 未ack → exit 1
        rc = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        self.assertEqual(rc, 1)

        # ack する
        draft_p = _draft_path(self.work, "ch001", "b1")
        acks_path = self.work / "_work" / "acks.jsonl"
        acks_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        from datetime import datetime, timezone
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "chapter": "ch001",
            "block": "b1",
            "paras": [1],
            "draft_hash": __import__("hashlib").sha256(draft_p.read_bytes()).hexdigest()[:12],
            "reason": "文字数比許容",
        }
        with open(acks_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # ack 後 → exit 0
        rc = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        self.assertEqual(rc, 0)

    def test_commit_idempotent(self):
        """emit 済みブロックへの再実行 → exit 0（記録補完のみ）。"""
        self._make_clean_draft()
        rc1 = self._commit()
        self.assertEqual(rc1, 0)

        # 再実行
        rc2 = self._commit()
        self.assertEqual(rc2, 0)

        # 版記録が補完されている
        meta = _lib.load_chapters_meta(self.work)
        ch = next(c for c in meta["chapters"] if c["id"] == "ch001")
        blk = next(b for b in ch["blocks"] if b["id"] == "b1")
        tr = blk.get("translation", {})
        self.assertEqual(tr.get("model"), "test-model")

    def test_commit_locked(self):
        """ロック中 → exit 3。"""
        self._make_clean_draft()
        from saga import _acquire_lock
        # 手動でロック
        locked = _acquire_lock(self.work)
        self.assertTrue(locked)
        # ロック中は commit が失敗
        rc = self._commit()
        self.assertEqual(rc, 3)

    def test_commit_meaning_not_applied(self):
        """ドラフトの meaning 候補（抽搐）は適用されず、そのまま残る。"""
        from saga import _draft_path, cmd_commit
        _make_draft(self.work, "ch001", "b1", [
            (1, "抽搐した身体が震える。"),
            (2, "二段目の訳。"),
            (3, "三段目の訳。"),
        ])
        # commit は glyph のみを適用。meaning（抽搐→身を捩る）は適用されない
        draft_p = _draft_path(self.work, "ch001", "b1")
        before = draft_p.read_text(encoding="utf-8")
        self.assertIn("抽搐", before)

        rc = cmd_commit(self.work, "ch001", "b1",
                        "test-model", "v1")
        self.assertEqual(rc, 1)  # 残渣で落ちる

        # ドラフトはまだ"抽搐"のまま（shape_glyph は glyph のみで meaning は触らない）
        after = draft_p.read_text(encoding="utf-8")
        self.assertIn("抽搐", after)

    def test_commit_with_ja2zh_residue(self):
        """ja→zh 方向のテスト。日本語残渣があれば失敗。"""
        work = _make_work(self.td, lang="ja", dst="zh")
        self.work = work
        _make_draft(work, "ch001", "b1", [
            (1, "日本語の文章です。彼女「こんにちは」"),
            (2, "二段目です。"),
            (3, "三段目です。"),
        ])
        rc = self._commit()
        self.assertEqual(rc, 1)  # 鉤括弧が残渣で検出される


class TestQaArchiveFlag(unittest.TestCase):
    """qa_translation.py --archive の挙動。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        work = self.td / "test_work"
        _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
        _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,2]}]
""")
        _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "一段目。"

[[paras]]
n = 2
text = "二段目。"
""")
        _write(work / "analysis" / "characters.json",
               json.dumps({"characters": []}, ensure_ascii=False))
        self.work = work
        # draft
        draft = work / "output" / "_work"
        draft.mkdir(parents=True, exist_ok=True)
        (draft / "ch001_b1_ja_numbered.md").write_text(
            "[P001] 一段目の訳。\n\n[P002] 二段目の訳。", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_emit_without_archive_deletes_draft(self):
        """--archive 無しではドラフト削除（従来挙動）。"""
        from qa_translation import _stage_impl
        import argparse
        args = argparse.Namespace(
            work=str(self.work),
            chapter="ch001",
            block="b1",
            draft=None,
            emit=True,
            no_strict=False,
            archive=False,
        )
        try:
            _stage_impl(args)
        except SystemExit:
            pass
        draft = self.work / "output" / "_work" / "ch001_b1_ja_numbered.md"
        self.assertFalse(draft.exists())
        done = self.work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md"
        self.assertFalse(done.exists())

    def test_emit_with_archive_moves_draft(self):
        """--archive 付きでは _work/done/ へ移動。"""
        from qa_translation import _stage_impl
        import argparse
        args = argparse.Namespace(
            work=str(self.work),
            chapter="ch001",
            block="b1",
            draft=None,
            emit=True,
            no_strict=False,
            archive=True,
        )
        try:
            _stage_impl(args)
        except SystemExit:
            pass
        draft = self.work / "output" / "_work" / "ch001_b1_ja_numbered.md"
        self.assertFalse(draft.exists())
        done = self.work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md"
        self.assertTrue(done.exists())


class TestBuildContextFunc(unittest.TestCase):
    """build_context.py の _stage_impl が saga.py から import 可能か。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_import_and_call(self):
        """build_context._stage_impl をインポートして呼べる。"""
        work = _make_work(self.td)
        from build_context import _stage_impl
        from pathlib import Path
        (work / "translation_input").mkdir(parents=True, exist_ok=True)
        _stage_impl(work, "ch001", "b1", "translation")
        ctx = work / "translation_input" / "ch001_b1_translation.md"
        self.assertTrue(ctx.exists())
        content = ctx.read_text(encoding="utf-8")
        self.assertIn("段落", content)


if __name__ == "__main__":
    unittest.main()