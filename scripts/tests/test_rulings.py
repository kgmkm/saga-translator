"""phase3-taskH: rulings.py の単体テスト。合成データのみ使用。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import rulings  # noqa: E402


def _fields(**overrides):
    """妥当な裁定記録の必須フィールド一式。"""
    base = {
        "work_id": "testwork",
        "chapter": "ch001",
        "block": "b1",
        "paragraph": 3,
        "rule_id": "char_ratio",
        "src_hash": "aaaaaaaaaaaa",
        "draft_hash": "bbbbbbbbbbbb",
        "dict_version": "v1",
        "rationale": "原文と一致するため誤検出。",
        "adjudicator": "user",
        "decided_at": "2026-10-04T00:00:00Z",
        "disposition": "allow",
        "status": "active",
    }
    base.update(overrides)
    return base


class TestRulings(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── 必須フィールド検証 ──────────────────────────────────────────

    def test_add_rejects_missing_required(self) -> None:
        from rulings import RulingError
        for key in rulings.REQUIRED_FIELDS:
            f = _fields()
            del f[key]
            if key in ("dict_version", "decided_at"):
                # 省略時は既定値が補われるため検証対象外
                continue
            with self.subTest(missing=key):
                with self.assertRaises(RulingError):
                    rulings.add_ruling(self.work, **f)

    def test_add_rejects_bad_values(self) -> None:
        from rulings import RulingError
        for bad in (
            {"paragraph": "3"},
            {"paragraph": True},
            {"disposition": "bogus"},
            {"status": "bogus"},
            {"chapter": ""},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(RulingError):
                    rulings.add_ruling(self.work, **_fields(**bad))

    # ── 完全一致で返る ──────────────────────────────────────────────

    def test_find_returns_on_exact_match(self) -> None:
        rulings.add_ruling(self.work, **_fields())
        got = rulings.find_active_ruling(
            self.work, "char_ratio", "ch001", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v1", work_id="testwork")
        self.assertIsNotNone(got)
        self.assertEqual(got["disposition"], "allow")
        self.assertEqual(got["status"], "active")

    def test_find_returns_none_when_absent(self) -> None:
        self.assertIsNone(rulings.find_active_ruling(
            self.work, "char_ratio", "ch001", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v1", work_id="testwork"))

    # ── 回帰の中核: どれか1つを変えたら無効 ────────────────────────

    def test_each_key_mismatch_invalidates(self) -> None:
        rulings.add_ruling(self.work, **_fields())
        base = {
            "rule_id": "char_ratio", "chapter": "ch001", "block": "b1",
            "paragraph": 3, "src_hash": "aaaaaaaaaaaa",
            "draft_hash": "bbbbbbbbbbbb", "dict_version": "v1",
        }
        for key in ("chapter", "block", "paragraph", "rule_id",
                    "src_hash", "draft_hash", "dict_version"):
            q = dict(base)
            if key == "paragraph":
                q[key] = base[key] + 1
            else:
                q[key] = base[key] + "X"
            with self.subTest(changed=key):
                got = rulings.find_active_ruling(
                    self.work, q["rule_id"], q["chapter"], q["block"],
                    q["paragraph"], q["src_hash"], q["draft_hash"],
                    q["dict_version"], work_id="testwork")
                self.assertIsNone(got)

    def test_different_chapter_same_block_not_reused(self) -> None:
        """別章の同じ block・同じ本文に裁定が流用されないこと。"""
        rulings.add_ruling(self.work, **_fields(chapter="ch001"))
        self.assertIsNone(rulings.find_active_ruling(
            self.work, "char_ratio", "ch002", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v1", work_id="testwork"))

    def test_stale_dict_version_not_used(self) -> None:
        """検査設定（辞書版）が変わると無効になること。"""
        rulings.add_ruling(self.work, **_fields(dict_version="v1"))
        self.assertIsNone(rulings.find_active_ruling(
            self.work, "char_ratio", "ch001", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v2", work_id="testwork"))

    # ── revoke ───────────────────────────────────────────────────────

    def test_revoke_makes_inactive(self) -> None:
        rulings.add_ruling(self.work, **_fields())
        rulings.revoke_ruling(
            self.work, "char_ratio", "ch001", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v1", work_id="testwork")
        self.assertIsNone(rulings.find_active_ruling(
            self.work, "char_ratio", "ch001", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v1", work_id="testwork"))
        # 履歴として残る
        all_recs = rulings.list_rulings(self.work)
        self.assertEqual(len(all_recs), 1)
        self.assertEqual(all_recs[0]["status"], "revoked")

    def test_revoke_absent_is_noop(self) -> None:
        rulings.add_ruling(self.work, **_fields())
        rulings.revoke_ruling(
            self.work, "char_ratio", "ch999", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v1", work_id="testwork")
        got = rulings.find_active_ruling(
            self.work, "char_ratio", "ch001", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", "v1", work_id="testwork")
        self.assertIsNotNone(got)

    # ── 冪等な書き換え・永続化 ──────────────────────────────────────

    def test_two_adds_two_records(self) -> None:
        rulings.add_ruling(self.work, **_fields())
        rulings.add_ruling(self.work, **_fields(paragraph=4))
        recs = rulings.list_rulings(self.work)
        self.assertEqual(len(recs), 2)
        # ファイルが JSON として読める（atomic 書き換え）
        import json
        p = self.work / "_work" / "rulings.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(len(data["rulings"]), 2)
        # 一時ファイルが残っていない
        self.assertFalse(
            (self.work / "_work" / "rulings.json.tmp").exists())

    def test_dict_version_default_none(self) -> None:
        f = _fields()
        del f["dict_version"]
        rec = rulings.add_ruling(self.work, **f)
        self.assertEqual(rec["dict_version"], "none")
        got = rulings.find_active_ruling(
            self.work, "char_ratio", "ch001", "b1", 3,
            "aaaaaaaaaaaa", "bbbbbbbbbbbb", work_id="testwork")
        self.assertIsNotNone(got)

    # ── ハッシュ／正規化ユーティリティ ──────────────────────────────

    def test_text_hash_stable_and_normalized(self) -> None:
        h1 = rulings.text_hash("同じ本文")
        h2 = rulings.text_hash("同じ本文")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), rulings.HASH_LEN)
        # 前後空白・改行は正規化される
        self.assertEqual(h1, rulings.text_hash("  同じ本文\n"))
        self.assertEqual(h1, rulings.text_hash("同じ本文\r\n"))
        self.assertNotEqual(h1, rulings.text_hash("別の本文"))


if __name__ == "__main__":
    unittest.main()