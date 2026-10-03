"""align.py の単体テスト。自作フィクスチャのみ。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(SCRIPTS))
import align  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _setup_basic_work(wd: Path) -> None:
    """zh→ja の3段落ブロック。正常ドラフト。"""
    _write(wd / "chapters_meta.toml", f"""\
[work]
work_id = "w1"

[[chapters]]
id = "ch001"
title = "第一章"
n_paras = 3
n_chars = 50
blocks = [{{ id = "b1", paras = [1, 3], n_chars = 50, scene_break_cut = false }}]
status = "pending"
""")
    _write(wd / "chapters" / "ch001.paras.toml", f'''\
chapter = "ch001"
title = "第一章"

[[paras]]
n = 1
text = "\\u201c你好，今天天气真好。\\u201d"

[[paras]]
n = 2
text = "\\u201c是啊，非常适合出门散步。\\u201d"

[[paras]]
n = 3
text = "他点了点头，然后继续往前走。"
''')
    _write(wd / "output" / "_work" / "ch001_b1_ja_numbered.md", """\
[P1] 「你好，今天天气真好。」
[P2] 「是啊，非常适合出门散步。」
[P3] 他点了点头，然后继续往前走。
""")


class TestParseNumbered(unittest.TestCase):
    def test_basic(self):
        items = align.parse_numbered("[P1] 本文一\n\n[P2] 本文二\n")
        self.assertEqual(items, [(1, "本文一"), (2, "本文二")])

    def test_duplicates_preserved(self):
        items = align.parse_numbered("[P1] 一\n\n[P1] 一の2\n\n[P2] 二\n")
        self.assertEqual(items, [(1, "一"), (1, "一の2"), (2, "二")])

    def test_empty_returns_empty(self):
        self.assertEqual(align.parse_numbered(""), [])

    def test_no_marker_ignored(self):
        items = align.parse_numbered("冒頭文\n\n[P1] 本文\n")
        self.assertEqual(items, [(1, "本文")])


class TestDraftHash(unittest.TestCase):
    def test_hex12(self):
        td = Path(tempfile.mkdtemp())
        p = td / "draft.md"
        p.write_text("hello", encoding="utf-8")
        h = align.draft_hash(p)
        self.assertEqual(len(h), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_changes_when_content_changes(self):
        td = Path(tempfile.mkdtemp())
        p = td / "draft.md"
        p.write_text("hello", encoding="utf-8")
        h1 = align.draft_hash(p)
        p.write_text("hello!", encoding="utf-8")
        h2 = align.draft_hash(p)
        self.assertNotEqual(h1, h2)


class TestAlignCheckFatal(unittest.TestCase):
    """fatal ケース: 番号欠落/重複/段落数不一致/接尾辞/会話頭不一致"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"
        _setup_basic_work(self.wd)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_ok(self):
        findings = align.check_block(self.wd, "ch001", "b1")
        self.assertEqual(findings, [])

    def test_missing_marker_fatal(self):
        # [P2] を削除
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] 「你好，今天天气真好。」\n\n[P3] 他点了点头，然后继续往前走。\n", encoding="utf-8")
        findings = align.check_block(self.wd, "ch001", "b1")
        self.assertTrue(any(f.severity == "fatal" and "欠落" in f.detail for f in findings))

    def test_duplicate_marker_fatal(self):
        # [P1] が2回
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] 一\n\n[P1] 一の2\n\n[P2] 二\n\n[P3] 三\n", encoding="utf-8")
        findings = align.check_block(self.wd, "ch001", "b1")
        self.assertTrue(any(f.severity == "fatal" and f.kind == "duplicate_marker" for f in findings))

    def test_suffixed_marker_fatal(self):
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] 一\n\n[P2b] 二\n\n[P3] 三\n", encoding="utf-8")
        findings = align.check_block(self.wd, "ch001", "b1")
        self.assertTrue(any(f.severity == "fatal" and f.kind == "suffixed_marker" for f in findings))

    def test_dialogue_head_fatal(self):
        # 原文 [P2] は会話「是啊…」なのに訳が地の文
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text(
            "[P1] 「你好，今天天气真好。」\n\n"
            "[P2] 是啊、と彼は言った。\n\n"
            "[P3] 他点了点头，然后继续往前走。\n",
            encoding="utf-8",
        )
        findings = align.check_block(self.wd, "ch001", "b1")
        self.assertTrue(any(f.severity == "fatal" and f.kind == "dialogue_head" for f in findings))

    def test_count_mismatch_extra_fatal(self):
        # 余剰: 実際は4段落
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] 一\n\n[P2] 二\n\n[P3] 三\n\n[P4] 四\n", encoding="utf-8")
        findings = align.check_block(self.wd, "ch001", "b1")
        self.assertTrue(any(f.severity == "fatal" and "余剰" in f.detail for f in findings))


class TestAlignCheckConfirm(unittest.TestCase):
    """confirm ケース: 文字数比逸脱"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"
        _setup_basic_work(self.wd)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_char_ratio_confirm(self):
        # 原文40字以上 → 訳5字（極端に短い）→ confirm
        _write(self.wd / "chapters" / "ch001.paras.toml", 'chapter = "ch001"\ntitle = "第一章"\n\n[[paras]]\nn = 1\ntext = "\\u201c你好，今天天气真好。我们一起去散步吧。今天是个好日子。\\u201d"\n\n[[paras]]\nn = 2\ntext = "\\u201c是啊，非常适合出门散步。\\u201d"\n\n[[paras]]\nn = 3\ntext = "他点了点头，然后继续往前走。"\n')
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text(
            "[P1] 短い。\n\n"
            "[P2] 「是啊，非常适合出门散步。」\n\n"
            "[P3] 他点了点头，然后继续往前走。\n",
            encoding="utf-8",
        )
        findings = align.check_block(self.wd, "ch001", "b1")
        self.assertTrue(any(f.severity == "confirm" and f.kind == "char_ratio" for f in findings))


class TestAlignAck(unittest.TestCase):
    """ack で confirm が抑制される"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"
        _setup_basic_work(self.wd)

        # 文字数比逸脱を起こす
        _write(self.wd / "chapters" / "ch001.paras.toml", 'chapter = "ch001"\ntitle = "第一章"\n\n[[paras]]\nn = 1\ntext = "\\u201c你好，今天天气真好。我们一起去散步吧。今天是个好日子。\\u201d"\n\n[[paras]]\nn = 2\ntext = "\\u201c是啊，非常适合出门散步。\\u201d"\n\n[[paras]]\nn = 3\ntext = "他点了点头，然后继续往前走。"\n')
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text(
            "[P1] 短い。\n\n"
            "[P2] 「是啊，非常适合出门散步。」\n\n"
            "[P3] 他点了点头，然后继续往前走。\n",
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_ack_suppresses_confirm(self):
        # ack が無い → confirm あり
        findings = align.check_block(self.wd, "ch001", "b1", acks=[])
        self.assertTrue(any(f.severity == "confirm" for f in findings))

        # ack を追加 → 抑制
        dh = align.draft_hash(self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md")
        acks = [
            {"block": "b1", "paras": [1], "draft_hash": dh, "reason": "誤検知"}
        ]
        findings2 = align.check_block(self.wd, "ch001", "b1", acks=acks)
        self.assertFalse(any(f.severity == "confirm" for f in findings2))

    def test_hash_change_invalidates_ack(self):
        dh = align.draft_hash(self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md")
        acks = [
            {"block": "b1", "paras": [1], "draft_hash": dh, "reason": "ok"}
        ]
        findings = align.check_block(self.wd, "ch001", "b1", acks=acks)
        self.assertFalse(any(f.severity == "confirm" for f in findings))

        # ドラフトを変更 → ハッシュが変わり ack は無効に
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text(
            "[P1] 短い！（変更）\n\n"
            "[P2] 「是啊，非常适合出门散步。」\n\n"
            "[P3] 他点了点头，然后继续往前走。\n",
            encoding="utf-8",
        )
        findings2 = align.check_block(self.wd, "ch001", "b1", acks=acks)
        self.assertTrue(any(f.severity == "confirm" for f in findings2))


class TestRecordAck(unittest.TestCase):
    def test_record_appends_jsonl(self):
        td = Path(tempfile.mkdtemp())
        wd = td / "w1"
        _write(wd / "output" / "_work" / "ch001_b1_ja_numbered.md", "[P1] 訳\n")

        entry = align.record_ack(wd, "ch001", "b1", [1], reason="確認済み")
        self.assertEqual(entry["chapter"], "ch001")
        self.assertEqual(entry["block"], "b1")
        self.assertEqual(entry["paras"], [1])
        self.assertEqual(len(entry["draft_hash"]), 12)

        lines = (wd / "_work" / "acks.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        loaded = json.loads(lines[0])
        self.assertEqual(loaded["chapter"], "ch001")


class TestRegressionOldToolsFatal(unittest.TestCase):
    """fatal ケースが旧ツールでも exit 1 になる回帰保証"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"
        _setup_basic_work(self.wd)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _run_old(self, name: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), str(self.wd), "--chapter", "ch001", "--block", "b1"],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_ok_passes_both(self):
        r1 = self._run_old("align_check.py")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        r2 = self._run_old("check_align.py")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

    def test_missing_marker_old_tools_fail(self):
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] 一\n\n[P3] 三\n", encoding="utf-8")
        for name in ("align_check.py", "check_align.py"):
            r = self._run_old(name)
            self.assertEqual(r.returncode, 1, f"{name}: {r.stdout} {r.stderr}")

    def test_duplicate_marker_new_is_fatal_old_may_ignore(self):
        # 重複マーカー: 新しい align.py は fatal として検出する。
        # 旧ツールは dict に上書きするため重複を見逃す場合がある（文字数比など
        # 他の条件で落ちることがあるが、重複そのものは検出しない）。
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] あいうえお\n\n[P1] かきくけこ\n\n[P2] さしすせそ\n\n[P3] たちつてと\n", encoding="utf-8")
        # 新しい align.py では fatal
        import align as _a
        findings = _a.check_block(self.wd, "ch001", "b1")
        self.assertTrue(any(f.kind == "duplicate_marker" for f in findings))

    def test_suffixed_marker_old_align_check_detects(self):
        # align_check.py は接尾辞を検出
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] 一\n\n[P2b] 二\n\n[P3] 三\n", encoding="utf-8")
        r = self._run_old("align_check.py")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("接尾辞", r.stdout)

    def test_suffixed_marker_old_check_align_detects_as_missing(self):
        # check_align.py は [P2b] をパースできず、[P3] だけ見えるので欠落扱いで exit 1
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text("[P1] 一\n\n[P2b] 二\n\n[P3] 三\n", encoding="utf-8")
        r = self._run_old("check_align.py")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("欠落", r.stdout)

    def test_dialogue_head_old_check_align_fails(self):
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text(
            "[P1] 「你好，今天天气真好。」\n\n"
            "[P2] 是啊、と彼は言った。\n\n"
            "[P3] 他点了点头，然后继续往前走。\n",
            encoding="utf-8",
        )
        r = self._run_old("check_align.py")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("会話になっていない", r.stdout)

    def test_dialogue_head_old_align_check_also_fails(self):
        # align_check.py は独自の is_dialogue() を使うので [] も会話扱い
        p = self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md"
        p.write_text(
            "[P1] 「你好，今天天气真好。」\n\n"
            "[P2] 是啊、と彼は言った。\n\n"
            "[P3] 他点了点头，然后继续往前走。\n",
            encoding="utf-8",
        )
        r = self._run_old("align_check.py")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()