# -*- coding: utf-8 -*-
"""改名の確定後契約（正本→派生物の伝播と stale 化）のテスト（P0-6・#22）。

docs/phase9-taskR1b-rename.md の検査項目:
① 正本と確定ブロック出力が置換される
② 対訳の訳文側のみ置換・原文側ハッシュ不変
③ 画像パス・ルビ読みが無傷（親文字の変更は読み要確認リストへ）
④ 派生物が stale 表示になり status が再生成を促す
⑤ stale ブロックは review で「最新・検収済み」と扱われない
⑥ _work/renames.jsonl に記録
⑦ 2回実行の冪等（2回目は「変更なし」で exit 0）

合成データのみ。作品固有の情報は含めない。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402
import text_scope as ts  # noqa: E402

RENAME = SCRIPTS / "rename_terms.py"
SAGA = SCRIPTS / "saga.py"
OLD = "旧訳"
NEW = "新訳"

DONE_B1 = "output/_work/done/ch001_b1_ja_numbered.md"
EMIT_B1 = "output/ch001/b1_ja.md"
EMIT_B2 = "output/ch001/b2_ja.md"
PAR_B1 = "output/ch001/b1_parallel.md"
PAR_CH = "output/ch001_parallel.md"
PAR_FULL = "output/全文_対訳.md"
DERIV_CH = "output/ch001_ja.md"
DERIV_FULL = "output/全文_訳文.md"
JSONL = "_work/renames.jsonl"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _read(work: Path, rel: str) -> str:
    return (work / rel).read_text(encoding="utf-8")


def _run(script: Path, args: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, cwd=str(SCRIPTS))
    return r.returncode, r.stdout + r.stderr


def _make_work(base: Path) -> Path:
    """改名契約テスト用の合成ワーク（正本・対訳・派生物を一通り持つ）。"""
    work = base / "work"
    _write(work / "manifest.toml", 'title = "T"\nlang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
title = ""
status = "pending"

[[chapters.blocks]]
id = "b1"
paras = [1, 2]

[[chapters.blocks]]
id = "b2"
paras = [3, 4]
""")
    _write(work / "chapters" / "ch001.paras.toml", "".join(
        f'[[paras]]\nn = {i}\ntext = "原文{i}。"\n\n' for i in range(1, 5)))
    # 正本: done（番号付き訳文）と確定ブロック出力（b1）。b2 は確定ブロック出力のみ
    _write(work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md",
           "[P001] 旧訳が歩いた。\n\n"
           "[P002] 画像 ![挿絵](images/old-001.png) と {旧訳|きゅうやく}、"
           "[[rb:旧訳 > きゅうやく]]。\n")
    _write(work / "output" / "ch001" / "b1_ja.md",
           "旧訳が歩いた。\n\n"
           "画像 ![挿絵](images/old-001.png) と {旧訳|きゅうやく}、"
           "[[rb:旧訳 > きゅうやく]]。\n")
    _write(work / "output" / "ch001" / "b2_ja.md", "旧訳の二つ目。\n\n二段落目。\n")
    # 対訳（ブロック対訳・章対訳・全文対訳）。原文側にも旧語を置き、無傷を検査できる形にする
    par = ("> [P001] 原文一。旧訳は原文側の語。\n\n"
           "旧訳が歩いた。\n\n"
           "> [P002] 原文二。\n\n"
           "画像 ![挿絵](images/old-001.png) と {旧訳|きゅうやく}、"
           "[[rb:旧訳 > きゅうやく]]。\n")
    _write(work / "output" / "ch001" / "b1_parallel.md", par)
    _write(work / "output" / "ch001_parallel.md", par)
    _write(work / "output" / "全文_対訳.md", "<!-- ch001 -->\n\n" + par)
    # 派生物（書き換えない。stale 表示の対象）
    _write(work / "output" / "ch001_ja.md", "旧訳が歩いた。\n")
    _write(work / "output" / "全文_訳文.md", "旧訳の全文。\n")
    # 人物表 aliases（既存契約の対象。監査記録にも載る）
    _lib.save_toml(work / "analysis" / "characters-[ネタバレあり].toml", {"characters": [
        {"name_src": "张三", "name_ja": "チョウサン", "aliases": [OLD]},
    ]})
    return work


def _set_manifest_field(work: Path, key: str, value) -> None:
    man = _lib.load_structured(work / "manifest")
    man[key] = value
    _lib.save_structured(work / "manifest", man)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r1b_rename_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = _make_work(self.tmp)

    def _apply(self) -> tuple[int, str]:
        return _run(RENAME, [str(self.work), "--term", OLD, "--to", NEW, "--apply"])


# ---------------------------------------------------------------------------
# ① 正本と確定ブロック出力が置換される
# ---------------------------------------------------------------------------

class TestCanonicalUpdate(_Base):
    def test_canonical_and_emitted_replaced(self):
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        done = _read(self.work, DONE_B1)
        self.assertIn("[P001] 新訳が歩いた。", done)
        self.assertIn("{新訳|きゅうやく}", done)
        emit1 = _read(self.work, EMIT_B1)
        self.assertIn("新訳が歩いた。", emit1)
        self.assertNotIn("旧訳が歩いた", emit1)
        # done が無いブロックは確定ブロック出力を置換する
        emit2 = _read(self.work, EMIT_B2)
        self.assertIn("新訳の二つ目。", emit2)
        self.assertNotIn("旧訳の二つ目", emit2)


# ---------------------------------------------------------------------------
# ② 対訳の訳文側のみ置換・原文側ハッシュ不変
# ---------------------------------------------------------------------------

class TestParallelUpdate(_Base):
    def test_parallel_dst_only_and_src_hash_unchanged(self):
        before = _read(self.work, PAR_B1)
        src_before = ts.scope_hash(before, ts.parallel_ranges(before), side=ts.SIDE_SRC)
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        after = _read(self.work, PAR_B1)
        src_after = ts.scope_hash(after, ts.parallel_ranges(after), side=ts.SIDE_SRC)
        self.assertEqual(src_before, src_after, "原文側のハッシュが変化した")
        # 原文ブロックは文字列としても無傷
        self.assertIn("> [P001] 原文一。旧訳は原文側の語。", after)
        self.assertIn("> [P002] 原文二。", after)
        # 訳文側は置換されている
        self.assertIn("新訳が歩いた。", after)
        self.assertIn("{新訳|きゅうやく}", after)
        # 章対訳・全文対訳も訳文側だけ更新（原文側は無傷）
        for rel in (PAR_CH, PAR_FULL):
            text = _read(self.work, rel)
            self.assertIn("新訳が歩いた。", text)
            self.assertIn("旧訳は原文側の語。", text)

    def test_apply_propagation_parallel_report(self):
        """対訳の更新記録に原文側ハッシュ（前後一致）が残る。"""
        from rename_terms import apply_propagation
        prop = apply_propagation(self.work, OLD, NEW)
        self.assertTrue(prop["changed"])
        rec = next(h for h in prop["parallel"] if h["file"] == PAR_B1)
        self.assertEqual(rec["src_hash"], rec["src_hash_after"])
        self.assertGreater(rec["total"], 0)


# ---------------------------------------------------------------------------
# ③ 画像パス・ルビ読みが無傷（親文字の変更は読み要確認リストへ）
# ---------------------------------------------------------------------------

class TestProtectionAndRubyList(_Base):
    def test_images_and_ruby_readings_protected_and_listed(self):
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        for rel in (DONE_B1, EMIT_B1):
            text = _read(self.work, rel)
            self.assertIn("![挿絵](images/old-001.png)", text)   # 画像パスは無傷
            self.assertIn("{新訳|きゅうやく}", text)             # 親文字は改名・読みは無傷
            self.assertIn("[[rb:旧訳 > きゅうやく]]", text)      # [[rb:]] は全体保護
        # 要確認リスト（読みは自動で変えない）
        self.assertIn("ルビ読みの要確認リスト", out)
        self.assertIn("{旧訳|きゅうやく} → {新訳|きゅうやく}", out)
        self.assertIn("[[rb:旧訳 > きゅうやく]]", out)


# ---------------------------------------------------------------------------
# ④ 派生物が stale 表示になり status が再生成を促す
# ---------------------------------------------------------------------------

class TestStaleAndStatus(_Base):
    def test_derivatives_not_rewritten_and_stale_recorded(self):
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        # 派生物そのものは書き換えない（再生成は assemble）
        self.assertIn("旧訳が歩いた。", _read(self.work, DERIV_CH))
        self.assertIn("旧訳の全文。", _read(self.work, DERIV_FULL))
        self.assertIn("再 assemble が必要（改名により stale）", out)
        # stale の記録: chapters_meta の該当ブロック
        meta = _lib.load_chapters_meta(self.work)
        flags = {f"{c['id']}/{b['id']}": b.get("stale_after_rename")
                 for c in meta["chapters"] for b in c["blocks"]}
        self.assertTrue(flags["ch001/b1"])
        self.assertTrue(flags["ch001/b2"])
        # stale の記録: manifest の derivatives_stale_at
        man = _lib.load_structured(self.work / "manifest")
        self.assertTrue(man.get("derivatives_stale_at"))

    def test_status_prompts_reassemble_after_rename(self):
        code, _out = self._apply()
        self.assertEqual(code, 0, _out)
        rcode, sout = _run(SAGA, ["status", str(self.work)])
        self.assertEqual(rcode, 0, sout)
        self.assertIn("再 assemble が必要（改名により stale）", sout)
        self.assertIn("ch001/b1", sout)

    def test_status_prompts_reassemble_for_completed_work(self):
        """assemble 済みの完成表示でも、stale が新しければ再生成を促す。"""
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        stale_at = _lib.load_structured(self.work / "manifest")["derivatives_stale_at"]
        older = (datetime.fromisoformat(stale_at) - timedelta(days=1)).isoformat(timespec="seconds")
        _set_manifest_field(self.work, "assembled_at", older)
        rcode, sout = _run(SAGA, ["status", str(self.work)])
        self.assertEqual(rcode, 0, sout)
        self.assertIn("完成（assemble 済み・中間物削除済み）", sout)
        self.assertIn("再 assemble が必要（改名により stale）", sout)

    def test_status_stale_prompt_cleared_by_newer_assemble(self):
        """stale より新しい assemble の記録があれば解消済みとして扱う。"""
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        stale_at = _lib.load_structured(self.work / "manifest")["derivatives_stale_at"]
        newer = (datetime.fromisoformat(stale_at) + timedelta(days=1)).isoformat(timespec="seconds")
        _set_manifest_field(self.work, "assembled_at", newer)
        rcode, sout = _run(SAGA, ["status", str(self.work)])
        self.assertEqual(rcode, 0, sout)
        self.assertNotIn("再 assemble が必要", sout)
        self.assertIn("次の作業: なし", sout)


# ---------------------------------------------------------------------------
# ⑤ stale ブロックは review で「最新・検収済み」と扱われない
# ---------------------------------------------------------------------------

class TestReviewStale(_Base):
    def test_review_flags_stale_blocks(self):
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        rcode, rout = _run(SAGA, ["review", str(self.work), "--json"])
        self.assertEqual(rcode, 0, rout)
        data = json.loads(rout)
        self.assertEqual(data["stale_blocks"], ["ch001/b1", "ch001/b2"])
        rcode, rout = _run(SAGA, ["review", str(self.work)])
        self.assertEqual(rcode, 0, rout)
        self.assertIn("改名により stale", rout)
        md = _read(self.work, "_work/review_wave1.md")
        self.assertIn("## 0. 改名により stale（再検収が必要）", md)
        self.assertIn("- ch001/b1: 改名後に再検収が必要", md)
        self.assertIn("- ch001/b2: 改名後に再検収が必要", md)


# ---------------------------------------------------------------------------
# ⑥ _work/renames.jsonl に記録
# ---------------------------------------------------------------------------

class TestAuditRecord(_Base):
    def test_renames_jsonl_audit_record(self):
        code, out = self._apply()
        self.assertEqual(code, 0, out)
        p = self.work / JSONL
        self.assertTrue(p.is_file())
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["term"], OLD)
        self.assertEqual(rec["to"], NEW)
        self.assertTrue(rec["ts"])
        blocks = {b["block"]: b for b in rec["blocks"]}
        self.assertIn("ch001/b1", blocks)
        self.assertEqual(blocks["ch001/b1"]["paras"], [1, 2])
        self.assertIn(DONE_B1, blocks["ch001/b1"]["files"])
        self.assertIn(EMIT_B1, blocks["ch001/b1"]["files"])
        self.assertIn("ch001/b2", blocks)
        self.assertEqual(blocks["ch001/b2"]["paras"], [3])
        # ルビの要確認リストと aliases の記録
        kinds = {r["kind"] for r in rec["ruby_check"]}
        self.assertEqual(kinds, {"vfm", "rb"})
        self.assertEqual(rec["aliases"]["count"], 1)
        # 対訳の更新も記録されている
        self.assertIn(PAR_B1, [h["file"] for h in rec["parallel"]])


# ---------------------------------------------------------------------------
# ⑦ 2回実行の冪等（2回目は「変更なし」で exit 0）
# ---------------------------------------------------------------------------

class TestIdempotent(_Base):
    def test_second_run_idempotent(self):
        code, out1 = self._apply()
        self.assertEqual(code, 0, out1)
        jsonl = self.work / JSONL
        first_audit = jsonl.read_text(encoding="utf-8")
        snapshot = {rel: (self.work / rel).read_bytes() for rel in
                    (DONE_B1, EMIT_B1, EMIT_B2, PAR_B1, PAR_CH, PAR_FULL,
                     DERIV_CH, DERIV_FULL, "manifest.toml", "chapters_meta.toml")}
        code, out2 = self._apply()
        self.assertEqual(code, 0, out2)
        self.assertIn("変更なし", out2)
        for rel, data in snapshot.items():
            self.assertEqual((self.work / rel).read_bytes(), data, rel)
        self.assertEqual(jsonl.read_text(encoding="utf-8"), first_audit)

    def test_dry_run_does_not_change(self):
        before = (self.work / DONE_B1).read_bytes()
        code, out = _run(RENAME, [str(self.work), "--term", OLD, "--to", NEW])
        self.assertEqual(code, 0, out)
        self.assertIn("正本", out)
        self.assertEqual((self.work / DONE_B1).read_bytes(), before)
        self.assertFalse((self.work / JSONL).exists())
        man = _lib.load_structured(self.work / "manifest")
        self.assertNotIn("derivatives_stale_at", man)


if __name__ == "__main__":
    unittest.main()
