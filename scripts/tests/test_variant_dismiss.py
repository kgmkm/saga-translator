"""異表記候補の別人棄却（dismiss_variant）のテスト（5b）。

- dismiss_variant で対象キャラの dismissed_variants へ追記し、該当ペアを
  char レベル・work レベルの variant_candidates から除去する
- 再スキャン（analyze-commit の再実行）で棄却済みペアを再追加しない
- 監査は _work/analysis_edits.jsonl に via="variant-dismiss" で1行
- status の異表記候補に出さない
- 存在しない相手名の dismiss はエラー終了（黙殺しない）
- aliases 解決の既存挙動は不変

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402

SAGA = SCRIPTS / "saga.py"
CHARACTERS_REL = "analysis/characters-[ネタバレあり].toml"


def _run_saga(*args: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(SAGA), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def _make_work(base: Path, chars: dict | None = None, texts: list[str] | None = None,
               n_blocks: int = 4) -> Path:
    """chapters_meta / paras / analysis / manifest を持つ最小ワーク（合成データ）。"""
    work = base / "work"
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "_work").mkdir(parents=True)
    (work / "manifest.toml").write_text('title = "T"\nlang = "zh"\ndst = "ja"\n', encoding="utf-8")
    (work / "_work" / "lock_prep.toml").write_text("done = true\n", encoding="utf-8")
    blocks = [{"id": f"b{i}", "paras": [(i - 1) * 2 + 1, i * 2], "n_chars": 10}
              for i in range(1, n_blocks + 1)]
    _lib.save_chapters_meta(work, {
        "work": {"title": "T", "lang": "zh"},
        "chapters": [{"id": "ch001", "title": "", "status": "pending", "blocks": blocks}],
    })
    texts = texts or [f"文{i}。" for i in range(1, n_blocks * 2 + 1)]
    _lib.save_paras(work, "ch001", {"paras": [{"n": i, "text": t} for i, t in enumerate(texts, 1)]})
    _lib.save_toml(work / CHARACTERS_REL,
                   chars if chars is not None else {"characters": []})
    _lib.save_toml(work / "analysis" / "terms.toml", {"terms": []})
    _lib.save_toml(work / "analysis" / "plot-[ネタバレあり].toml", {"entries": []})
    return work


# b2: キャラ同士の異表記（张三⇔張三） / b3: 該当キャラなしの揺れ表記（张叁）
CAND_TEXTS = [
    "“你好。”张三说。“再见。”张三挥手。", "补一。",
    "张三和張三都来了。", "补二。",
    "张三看着张叁。张叁笑了。张叁走了。", "补三。",
    "李四站在门口。", "补四。",
]

CAND_CHARS = {"characters": [
    {"name_src": "张三", "name_ja": "チョウサン"},
    {"name_src": "張三", "name_ja": "ジャンサン"},
    {"name_src": "李四", "name_ja": "リシ"},
]}


def _plot_delta(block: str) -> str:
    return f'block = "{block}"\n\n[[plot_delta]]\nblock = "{block}"\ntext = "要点。"\n'


def _dismiss_delta(block: str, target: str, partner: str, reason: str | None = None) -> str:
    body = (f'block = "{block}"\n\n[[characters_delta]]\nmatch_key = "name_src"\n'
            f'match_value = "{target}"\ndismiss_variant = "{partner}"\n')
    if reason is not None:
        body += f'dismiss_reason = "{reason}"\n'
    return body


def _pending_section(out: str) -> str:
    i = out.find("分析の確認待ち")
    return out[i:] if i >= 0 else ""


def _char(work: Path, name_src: str) -> dict:
    for c in (_lib.load_characters(work).get("characters") or []):
        if c.get("name_src") == name_src:
            return c
    raise AssertionError(f"character {name_src} not found")


def _edits(work: Path) -> list[dict]:
    p = work / "_work" / "analysis_edits.jsonl"
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="q5b_dismiss_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit(self, work: Path, text: str, name: str = "delta.toml") -> tuple[int, str]:
        d = self.tmp / name
        d.write_text(text, encoding="utf-8")
        return _run_saga("analyze-commit", str(work), "--delta", str(d))

    def _cand_work(self) -> Path:
        return _make_work(self.tmp, chars=dict(CAND_CHARS), texts=list(CAND_TEXTS))


class TestVariantDismiss(_Base):
    def test_dismiss_removes_pair_records_audit_and_status(self):
        """棄却で両側の候補が消え、dismissed_variants・監査1行が残る。"""
        work = self._cand_work()
        code, out = self._commit(work, _plot_delta("ch001/b2"))
        self.assertEqual(code, 0, out)
        self.assertIn("異表記候補: 张三⇔張三 (ch001/b2)", out)

        code, out = self._commit(work, _dismiss_delta(
            "ch001/b4", "张三", "張三", reason="似た表記の別人"))
        self.assertEqual(code, 0, out)
        self.assertIn("異表記棄却: 张三⇔張三", out)
        a = _char(work, "张三")
        b = _char(work, "張三")
        self.assertEqual(a["dismissed_variants"], ["張三"])
        self.assertNotIn("variant_candidates", a)
        self.assertNotIn("variant_candidates", b)

        edits = _edits(work)
        self.assertEqual(len(edits), 1, edits)
        rec = edits[0]
        self.assertEqual(rec["key"], "dismiss_variant")
        self.assertEqual(rec["via"], "variant-dismiss")
        self.assertEqual(rec["match"], "张三")
        self.assertEqual(rec["old"], "")
        self.assertEqual(rec["new"], "張三")
        self.assertEqual(rec["reason"], "似た表記の別人")
        self.assertEqual(rec["file"], "characters-[ネタバレあり].toml")

        # status の異表記候補に出さない
        code, out = _run_saga("status", str(work))
        self.assertEqual(code, 0, out)
        section = _pending_section(out)
        self.assertNotIn("张三⇔張三", section)
        self.assertNotIn("張三⇔张三", section)
        self.assertNotIn("異表記候補:", section)

    def test_rescan_does_not_resurrect_and_audit_stays_one_line(self):
        """再スキャンで棄却済みペアが復活せず、監査も増えない。"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b2"))
        code, out = self._commit(work, _dismiss_delta("ch001/b4", "张三", "張三"))
        self.assertEqual(code, 0, out)
        # 同じ b2 を再コミット（原文に両表記が併存している）
        code, out = self._commit(work, _plot_delta("ch001/b2"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("異表記候補", out)
        self.assertNotIn("variant_candidates", _char(work, "张三"))
        self.assertNotIn("variant_candidates", _char(work, "張三"))
        # 同一の棄却 delta の再実行でも監査は1行のまま
        code, out = self._commit(work, _dismiss_delta("ch001/b4", "张三", "張三"))
        self.assertEqual(code, 0, out)
        self.assertEqual(_char(work, "张三")["dismissed_variants"], ["張三"])
        self.assertEqual(len(_edits(work)), 1)

    def test_dismiss_work_level_pair_removed_and_not_readded(self):
        """work レベル（該当キャラなし）の候補も棄却で除去し、再検出しない。

        相手名に非JIS字を含む（张/叁）ため、残渣検査に誤検出されないことも兼ねる。
        """
        work = self._cand_work()
        code, out = self._commit(work, _plot_delta("ch001/b3"))
        self.assertEqual(code, 0, out)
        self.assertEqual(_lib.load_characters(work)["variant_candidates"],
                         [{"name_src": "张叁", "similar_to_src": "张三", "block": "ch001/b3"}])

        code, out = self._commit(work, _dismiss_delta("ch001/b4", "张三", "张叁"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("variant_candidates", _lib.load_characters(work))
        self.assertEqual(_char(work, "张三")["dismissed_variants"], ["张叁"])
        self.assertEqual(len(_edits(work)), 1)

        # 再コミット（b3 の再スキャン）で work レベルに復活しない
        code, out = self._commit(work, _plot_delta("ch001/b3"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("variant_candidates", _lib.load_characters(work))

        # 保存後の lint も残渣なし（dismissed_variants の原文名を誤検出しない）
        code, out = _run_saga("lint", str(work))
        self.assertEqual(code, 0, out)
        self.assertIn("残渣なし", out)

    def test_dismiss_unknown_partner_errors_and_writes_nothing(self):
        """存在しない相手名の dismiss はエラー終了し、何も書かない。"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b2"))
        before = (work / CHARACTERS_REL).read_bytes()
        code, out = self._commit(work, _dismiss_delta(
            "ch001/b4", "张三", "存在しない名"))
        self.assertEqual(code, 1, out)
        self.assertIn("存在しない相手名", out)
        self.assertEqual((work / CHARACTERS_REL).read_bytes(), before)
        self.assertEqual(_edits(work), [])
        meta = _lib.load_chapters_meta(work)
        blk = _lib.block_meta(meta, "ch001/b4")
        self.assertNotIn("analysis_committed_at", blk)

    def test_dismiss_variant_must_be_nonempty_string(self):
        """dismiss_variant が文字列でない・空の delta は受入検証で拒否する。"""
        work = self._cand_work()
        before = (work / CHARACTERS_REL).read_bytes()
        d = self.tmp / "bad_type.toml"
        d.write_text('block = "ch001/b4"\n\n[[characters_delta]]\nmatch_key = "name_src"\n'
                     'match_value = "张三"\ndismiss_variant = 3\n', encoding="utf-8")
        code, out = _run_saga("analyze-commit", str(work), "--delta", str(d))
        self.assertEqual(code, 1, out)
        self.assertIn("name_src", out)
        self.assertEqual((work / CHARACTERS_REL).read_bytes(), before)

    def test_dismiss_self_rejected(self):
        """自分自身を相手にした dismiss は拒否する（候補を巻き込まない）。"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b2"))
        code, out = self._commit(work, _dismiss_delta("ch001/b4", "张三", "张三"))
        self.assertEqual(code, 1, out)
        self.assertIn("自分自身", out)
        self.assertNotIn("dismissed_variants", _char(work, "张三"))
        self.assertIn("variant_candidates", _char(work, "张三"))

    def test_aliases_resolution_unchanged(self):
        """aliases に入れた解決は従来どおり（棄却とは別。再走査でも復活しない）。"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b2"))
        delta = ('block = "ch001/b4"\n\n[[characters_delta]]\nmatch_key = "name_src"\n'
                 'match_value = "张三"\naliases = ["張三"]\n')
        code, out = self._commit(work, delta)
        self.assertEqual(code, 0, out)
        a = _char(work, "张三")
        self.assertEqual(a["aliases"], ["張三"])
        self.assertNotIn("variant_candidates", a)
        self.assertNotIn("variant_candidates", _char(work, "張三"))
        self.assertNotIn("dismissed_variants", a)  # aliases は棄却記録を作らない
        self.assertEqual(_edits(work), [])
        code, out = self._commit(work, _plot_delta("ch001/b2"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("variant_candidates", _char(work, "张三"))

    def test_dismiss_leaves_other_pairs(self):
        """棄却したペア以外の候補は残る（巻き込み除去しない）。"""
        texts = [
            "“你好。”张三说。“再见。”张三挥手。", "补一。",
            "张三、張三、张四都来了。", "补二。",
            "补三。", "补三b。",
            "补四。", "补四b。",
        ]
        chars = {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン"},
            {"name_src": "張三", "name_ja": "ジャンサン"},
            {"name_src": "张四", "name_ja": "チャンスー"},
        ]}
        work = _make_work(self.tmp, chars=chars, texts=texts)
        code, out = self._commit(work, _plot_delta("ch001/b2"))
        self.assertEqual(code, 0, out)
        a = _char(work, "张三")
        self.assertEqual(sorted(c["name_src"] for c in a["variant_candidates"]),
                         ["张四", "張三"])

        code, out = self._commit(work, _dismiss_delta("ch001/b4", "张三", "張三"))
        self.assertEqual(code, 0, out)
        a = _char(work, "张三")
        self.assertEqual([c["name_src"] for c in a["variant_candidates"]], ["张四"])
        z = _char(work, "张四")
        self.assertEqual([c["name_src"] for c in z["variant_candidates"]], ["张三"])
        self.assertNotIn("variant_candidates", _char(work, "張三"))

    def test_dismissed_helpers_tolerate_garbage(self):
        """dismissed_variants の壊れた値は読み取り・追記で落ちない。"""
        from saga import _dismissed_variants_of, _merge_dismissed_variants
        self.assertEqual(_dismissed_variants_of({}), [])
        self.assertEqual(_dismissed_variants_of({"dismissed_variants": "壊れ"}), [])
        self.assertEqual(_dismissed_variants_of({"dismissed_variants": ["甲", 3, ""]}), ["甲"])
        self.assertEqual(_merge_dismissed_variants("壊れ", ["甲"]), ["甲"])
        self.assertEqual(_merge_dismissed_variants(["甲", "乙"], ["乙", "丙"]), ["甲", "乙", "丙"])


    def test_dismiss_keys_excluded_by_detector(self):
        """dismiss 系の欄は検出器側（_is_src_key）で除外される（単一の正本）。"""
        from detect.zh2ja import find_cn_residue_fields
        data = {"dismiss_variant": "张叁",
                "dismissed_variants": ["张叁"],
                "dismiss_reason": "别人",
                "memo": "日本語の覚書。"}
        self.assertEqual(find_cn_residue_fields(data), [])
        # 対照: 同じ語が日本語欄にあれば検出される
        hit = find_cn_residue_fields({"memo": "张叁の件。"})
        self.assertTrue(hit)
        self.assertIn("memo", hit[0].context)

    def test_dismissed_variants_carried_to_next_work(self):
        """dismissed_variants は carry の CHAR_FIELDS で次作へ引き継がれる。"""
        from carry_analysis import CHAR_FIELDS, _merge_items
        self.assertIn("dismissed_variants", CHAR_FIELDS)
        prev = [{"name_src": "张三", "dismissed_variants": ["张叁"]}]
        merged, added, filled = _merge_items(
            prev, [{"name_src": "张三"}], "name_src", CHAR_FIELDS, "n")
        self.assertEqual(merged[0]["dismissed_variants"], ["张叁"])
        self.assertEqual(filled, 1)


if __name__ == "__main__":
    unittest.main()
