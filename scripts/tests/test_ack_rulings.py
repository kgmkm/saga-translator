"""align ack の裁定記録（rulings）移行と未裁定ゲート（S3）のテスト。

① ack が rulings に記録され、全キー一致でのみ有効
② 別章の同じ b1・同じ本文へ裁定が流用されない
③ 原文・ドラフト・検査設定の変更で裁定が無効になる
④ 未裁定で commit が確定せず、裁定の記録で通る（S3）
⑤ 旧 acks.jsonl の一回きりの取り込み（読み取り互換）
⑥ align.py ack / check の CLI 契約の維持

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import align  # noqa: E402
import rulings  # noqa: E402

PYTHON = sys.executable

# 確認（char_ratio）を出す合成データ。原文 P1 は 25 字以上・訳 P1 は極端に短い。
_SRC_P1 = "这是一个非常长的句子，用来测试文字数比的确认检查，并且确认段落长度足够。"
_SRC_P2 = "第二段。普通の長さ。"
_SRC_P3 = "第三段。普通の長さ。"
_DRAFT_P1 = "短い。"
_DRAFT_P2 = "二番目の訳文である。"
_DRAFT_P3 = "三番目の訳文である。"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _draft_path(work: Path, chapter: str = "ch001", block: str = "b1") -> Path:
    return work / "output" / "_work" / f"{chapter}_{block}_ja_numbered.md"


def _make_work(base: Path) -> Path:
    """2章（同じ b1・同じ原文・同じドラフト）を持つ合成作品。"""
    work = base / "w1"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}]

[[chapters]]
id = "ch002"
blocks = [{id="b1", paras=[1,3]}]
""")
    paras_toml = f"""\
[[paras]]
n = 1
text = "{_SRC_P1}"

[[paras]]
n = 2
text = "{_SRC_P2}"

[[paras]]
n = 3
text = "{_SRC_P3}"
"""
    for cid in ("ch001", "ch002"):
        _write(work / "chapters" / f"{cid}.paras.toml", paras_toml)
        _write(_draft_path(work, cid),
               f"[P001] {_DRAFT_P1}\n\n[P002] {_DRAFT_P2}\n\n[P003] {_DRAFT_P3}\n")
    return work


class _WorkBase(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _confirms(self, chapter: str = "ch001") -> list:
        return [f for f in align.check_block(self.work, chapter, "b1")
                if f.severity == "confirm"]

    def _commit(self, chapter: str = "ch001", block: str = "b1"):
        """commit を直接呼び、戻り値と出力を返す。"""
        from saga import cmd_commit
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cmd_commit(self.work, chapter, block, "test-model", "v1")
        return rc, out.getvalue() + err.getvalue()


class TestAckRecordedAsRuling(_WorkBase):
    """① ack は rulings に記録され、全キー一致でのみ有効。"""

    def test_ack_recorded_with_full_keys(self):
        entry = align.record_ack(self.work, "ch001", "b1", [1], reason="合成の裁定")
        self.assertEqual(entry["chapter"], "ch001")
        self.assertEqual(entry["block"], "b1")
        self.assertEqual(entry["paras"], [1])
        self.assertEqual(len(entry["draft_hash"]), 12)
        self.assertEqual(entry["rulings"], 1)
        self.assertTrue((self.work / "_work" / "rulings.json").is_file())
        recs = rulings.list_rulings(self.work)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["rule_id"], "char_ratio")
        self.assertEqual((r["chapter"], r["block"], r["paragraph"]), ("ch001", "b1", 1))
        self.assertEqual(r["draft_hash"], align.draft_hash(_draft_path(self.work)))
        self.assertEqual(len(r["src_hash"]), 12)
        self.assertEqual(len(r["dict_version"]), 12)
        # 全キー一致でのみ有効（1キーでも違えば None）
        got = rulings.find_active_ruling(
            self.work, rule_id="char_ratio", chapter="ch001", block="b1",
            paragraph=1, src_hash=r["src_hash"], draft_hash=r["draft_hash"],
            dict_version=r["dict_version"])
        self.assertIsNotNone(got)
        for bad in (
            dict(chapter="ch002"),
            dict(block="b2"),
            dict(paragraph=2),
            dict(src_hash="000000000000"),
            dict(draft_hash="000000000000"),
            dict(dict_version="000000000000"),
        ):
            q = dict(rule_id="char_ratio", chapter="ch001", block="b1", paragraph=1,
                     src_hash=r["src_hash"], draft_hash=r["draft_hash"],
                     dict_version=r["dict_version"])
            q.update(bad)
            self.assertIsNone(rulings.find_active_ruling(self.work, **q),
                              f"不一致のはずのキーで有効になった: {bad}")

    def test_ack_suppresses_confirm(self):
        self.assertEqual([f.para for f in self._confirms()], [1])
        align.record_ack(self.work, "ch001", "b1", [1], reason="合成の裁定")
        self.assertEqual(self._confirms(), [])


class TestNoReuseAcrossChapters(_WorkBase):
    """② 別章の同じ b1・同じ本文へ裁定が流用されない。"""

    def test_same_block_same_text_other_chapter_keeps_confirm(self):
        align.record_ack(self.work, "ch001", "b1", [1], reason="合成の裁定")
        self.assertEqual(self._confirms("ch001"), [])
        # ch002 は同じ b1・同じ原文・同じドラフトだが、章が違うので抑制されない
        self.assertEqual([f.para for f in self._confirms("ch002")], [1])

    def test_commit_for_other_chapter_still_blocked(self):
        align.record_ack(self.work, "ch002", "b1", [1], reason="別章の裁定")
        rc, text = self._commit("ch001", "b1")
        self.assertEqual(rc, 1, text)
        self.assertIn("未裁定", text)


class TestAckInvalidation(_WorkBase):
    """③ 原文・ドラフト・検査設定の変更で裁定が無効になる。"""

    def _ack(self):
        align.record_ack(self.work, "ch001", "b1", [1], reason="合成の裁定")
        self.assertEqual(self._confirms(), [])

    def test_src_change_invalidates(self):
        self._ack()
        paras_p = self.work / "chapters" / "ch001.paras.toml"
        _write(paras_p, paras_p.read_text(encoding="utf-8")
               .replace(_SRC_P1, _SRC_P1 + "追記。"))
        self.assertEqual([f.para for f in self._confirms()], [1])

    def test_draft_change_invalidates(self):
        self._ack()
        _write(_draft_path(self.work),
               f"[P001] {_DRAFT_P1}！\n\n[P002] {_DRAFT_P2}\n\n[P003] {_DRAFT_P3}\n")
        self.assertEqual([f.para for f in self._confirms()], [1])

    def test_config_change_invalidates(self):
        self._ack()
        # 検査設定（house_style.toml）が変わると dict_version が変わり無効になる
        _write(self.work / "_house" / "house_style.toml",
               '[residue]\nknown_terms = ["合成語"]\n')
        self.assertEqual([f.para for f in self._confirms()], [1])


class TestCommitUnresolvedGate(_WorkBase):
    """④ 未裁定で commit が確定せず、裁定の記録で通る（S3）。"""

    def test_commit_blocked_until_ack(self):
        rc, text = self._commit()
        self.assertEqual(rc, 1, text)
        self.assertIn("未裁定", text)
        self.assertFalse((self.work / "output" / "ch001" / "b1_ja.md").exists())
        # 裁定記録（正規化後のドラフトに紐づく）で通る
        entry = align.record_ack(self.work, "ch001", "b1", [1], reason="合成の裁定")
        self.assertEqual(entry["rulings"], 1)
        rc, text = self._commit()
        self.assertEqual(rc, 0, text)
        self.assertTrue((self.work / "output" / "ch001" / "b1_ja.md").exists())

    def test_commit_stops_when_ack_stale(self):
        # 裁定記録の後にドラフトを変えると、裁定が陳腐化して再び止まる
        align.record_ack(self.work, "ch001", "b1", [1], reason="合成の裁定")
        _write(_draft_path(self.work),
               f"[P001] {_DRAFT_P1}！\n\n[P002] {_DRAFT_P2}\n\n[P003] {_DRAFT_P3}\n")
        rc, text = self._commit()
        self.assertEqual(rc, 1, text)


class TestLegacyAcksMigration(_WorkBase):
    """⑤ 旧 acks.jsonl の一回きりの取り込み（読み取り互換）。"""

    def _write_legacy(self, entries: list[dict]) -> None:
        p = self.work / "_work" / "acks.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _legacy_entry(self, chapter: str = "ch001", block: str = "b1",
                      paras=(1,), dh=None, reason: str = "旧形式の裁定") -> dict:
        if dh is None:
            dh = align.draft_hash(_draft_path(self.work, chapter, block))
        return {"ts": "2026-01-01T00:00:00Z", "chapter": chapter, "block": block,
                "paras": list(paras), "draft_hash": dh, "reason": reason}

    def test_legacy_ack_migrated_and_honored(self):
        # 最初の commit は未裁定で止まる（ドラフトは正規化される）
        rc, text = self._commit()
        self.assertEqual(rc, 1, text)
        # 旧形式の ack を置く → 次の commit で取り込み、裁定記録として効く
        self._write_legacy([self._legacy_entry()])
        rc, text = self._commit()
        self.assertEqual(rc, 0, text)
        self.assertIn("取り込みました", text)
        recs = [r for r in rulings.list_rulings(self.work) if r["status"] == "active"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["rule_id"], "char_ratio")
        self.assertEqual(recs[0]["paragraph"], 1)

    def test_migration_is_idempotent(self):
        self._write_legacy([self._legacy_entry()])
        self.assertEqual(align.migrate_legacy_acks(self.work), 1)
        self.assertEqual(align.migrate_legacy_acks(self.work), 0)
        self.assertEqual(len(rulings.list_rulings(self.work)), 1)

    def test_unmigratable_entry_warns_only(self):
        # 原文に無い段落 → 取り込めないが、エラーにはせず警告だけ出す
        self._write_legacy([self._legacy_entry(paras=(99,))])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            n = align.migrate_legacy_acks(self.work)
        self.assertEqual(n, 0)
        self.assertIn("取り込めません", err.getvalue())
        self.assertEqual(rulings.list_rulings(self.work), [])
        # 必須キーが欠けた記録も警告だけ
        self._write_legacy([{"chapter": "ch001", "paras": [1]}])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            n = align.migrate_legacy_acks(self.work)
        self.assertEqual(n, 0)
        self.assertIn("欠落", err.getvalue())


class TestAckCliContract(_WorkBase):
    """⑥ align.py ack / check の CLI 契約（保存先が rulings に変わっても維持）。"""

    def _run(self, *args):
        return subprocess.run([PYTHON, str(SCRIPTS / "align.py"), *args],
                              capture_output=True, text=True, encoding="utf-8")

    def test_ack_cli_records_ruling_and_check_passes(self):
        p = self._run("check", str(self.work), "--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        p = self._run("ack", str(self.work), "--chapter", "ch001", "--block", "b1",
                      "--para", "P1", "--reason", "合成テストの裁定")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ack 記録", p.stdout)
        self.assertEqual(len(rulings.list_rulings(self.work)), 1)
        p = self._run("check", str(self.work), "--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("confirm 0 件", p.stdout)
        self.assertIn("裁定記録で通過 1 件", p.stdout)


if __name__ == "__main__":
    unittest.main()
