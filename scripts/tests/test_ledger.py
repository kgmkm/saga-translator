"""ブロック横断状態の外部化台帳（_work/ledger.toml。レビュー提案1）のテスト。

検証対象:
  - commit 確定時にルビ（VFM {親|読み}）初出と用語初出が台帳へ記録されること
  - ブロックをまたいだ同一キーの再記録がないこと（ルビ term+reading / 用語 term）
  - ledger-ono の申告・重複拒否・timeline 記録・prepare への注入
  - 台帳が無い work では注入節が出ないこと（後方互換）
  - 再確定（復旧・--revise）が冪等で、消えたルビを削除しないこと
  - carry_analysis の引き継ぎ（前作の台帳コピー）
  - purge_source が台帳を消さないこと
  - status 再開票の台帳1行

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS))

# 合成ドラフト（b1: ルビ初出と用語初出 / b2: 同一ルビの再付与と新しい用語）
DRAFT_B1 = [
    (1, "彼は{剣|つるぎ}を抜いた。"),
    (2, "雪が降り始めた。"),
]
DRAFT_B2 = [
    (3, "再び{剣|つるぎ}が光る。"),
    (4, "剣は重い。"),
]
# 改訂ドラフト（ルビが消えた版。台帳からの削除はしない＝既知の制限の検証）
REVISED_B1 = [
    (1, "彼は剣を抜いた。"),
    (2, "雪が降り始めた。"),
]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_work(base: Path) -> Path:
    """zh→ja の最小合成ワーク（1章2ブロック。terms 3語）。"""
    work = base / "work"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,2]}, {id="b2", paras=[3,4]}]
""")
    _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "第一段落。"

[[paras]]
n = 2
text = "第二段落。"

[[paras]]
n = 3
text = "第三段落。"

[[paras]]
n = 4
text = "第四段落。"
""")
    _write(work / "analysis" / "terms.toml", """\
[[terms]]
term_src = "剑"
term_ja = "剣"

[[terms]]
term_src = "雪"
term_ja = "雪"

[[terms]]
term_src = "光"
term_ja = "光"
""")
    return work


def _place_draft(work: Path, cid: str, bid: str, items: list[tuple[int, str]]) -> Path:
    """output/_work/ に番号付きドラフトを置く。"""
    draft = work / "output" / "_work" / f"{cid}_{bid}_ja_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("\n\n".join(f"[P{n:03d}] {t}" for n, t in items) + "\n",
                     encoding="utf-8")
    return draft


def _run_commit(work: Path, cid: str = "ch001", bid: str = "b1",
                revise: bool = False) -> tuple[int, str]:
    """cmd_commit を in-process で実行し (終了コード, 出力) を返す。"""
    from saga import cmd_commit
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = buf
        code = cmd_commit(work, cid, bid, "test-model", "v1", revise=revise)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, buf.getvalue()


def _run_cli(tail: list[str]) -> tuple[int, str]:
    """scripts/ 配下の CLI をサブプロセスで実行し (終了コード, 出力) を返す。"""
    r = subprocess.run(
        [PYTHON, str(SCRIPTS / tail[0]), *tail[1:]],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _run_stage(work: Path, cid: str, bid: str, mode: str = "translation",
               style: str = "compact") -> str:
    """build_context._stage_impl を実行し、生成されたコンテキストの内容を返す。"""
    from build_context import _stage_impl
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = buf
        _stage_impl(work, cid, bid, mode, context_style=style)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return (work / "translation_input" / f"{cid}_{bid}_{mode}.md").read_text(encoding="utf-8")


def _section(text: str, title: str) -> str:
    """コンテキスト本文から節（## title 〜 次の ## まで）を抜き出す。無ければ空文字。"""
    marker = f"## {title}\n"
    i = text.find(marker)
    if i < 0:
        return ""
    j = text.find("\n## ", i + len(marker))
    return text[i:j if j >= 0 else len(text)]


def _timeline(work: Path) -> list[dict]:
    p = work / "logs" / "timeline.jsonl"
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _capture(fn, *args, **kwargs) -> tuple[int, str]:
    """stdout/stderr を捕まえて (戻り値, 出力) を返す。"""
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = buf
        rc = fn(*args, **kwargs)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, buf.getvalue()


class _FixtureBase(unittest.TestCase):
    """合成ワークの共通セットアップ。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.work = _make_work(self.td)


class TestCommitLedger(_FixtureBase):
    """commit 確定時の台帳追記（ルビ初出・用語初出）。"""

    def test_commit_records_ruby_and_term_first_use(self):
        """① 確定で VFM 初出（term+reading・段落）と用語初出が記録される。"""
        from ledger import load_ledger
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work)
        self.assertEqual(code, 0, out)

        self.assertTrue((self.work / "_work" / "ledger.toml").is_file(), out)
        d = load_ledger(self.work)
        self.assertEqual(len(d["ruby_applied"]), 1, d)
        r = d["ruby_applied"][0]
        self.assertEqual((r["term"], r["reading"]), ("剣", "つるぎ"))
        self.assertEqual((r["block"], r["paragraph"]), ("ch001/b1", 1))

        first = {t["term"]: (t["block"], t["paragraph"]) for t in d["term_first_use"]}
        self.assertEqual(first.get("剣"), ("ch001/b1", 1))
        self.assertEqual(first.get("雪"), ("ch001/b1", 2))
        self.assertNotIn("光", first)  # b1 には現れない

    def test_same_ruby_key_not_re_recorded_next_block(self):
        """② 2ブロック目で同一キーのルビを再付与しても増えない（初出のまま）。"""
        from ledger import load_ledger
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        _place_draft(self.work, "ch001", "b2", DRAFT_B2)
        code, out = _run_commit(self.work, "ch001", "b2")
        self.assertEqual(code, 0, out)

        d = load_ledger(self.work)
        ruby = [r for r in d["ruby_applied"] if r["term"] == "剣"]
        self.assertEqual(len(ruby), 1, d)
        self.assertEqual(ruby[0]["block"], "ch001/b1")  # 初出ブロックのまま
        self.assertEqual(len(d["ruby_applied"]), 1)

    def test_term_first_use_not_re_recorded_next_block(self):
        """③ 用語初出は初出ブロックのまま。b2 で新出の語だけ追加される。"""
        from ledger import load_ledger
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        _place_draft(self.work, "ch001", "b2", DRAFT_B2)
        code, out = _run_commit(self.work, "ch001", "b2")
        self.assertEqual(code, 0, out)

        d = load_ledger(self.work)
        by_term = {t["term"]: (t["block"], t["paragraph"]) for t in d["term_first_use"]}
        self.assertEqual(by_term.get("剣"), ("ch001/b1", 1))
        self.assertEqual(by_term.get("雪"), ("ch001/b1", 2))
        self.assertEqual(by_term.get("光"), ("ch001/b2", 3))
        self.assertEqual(len(d["term_first_use"]), 3)

    def test_reconfirm_and_revise_are_idempotent(self):
        """⑥ 再確定（復旧・--revise）で重複せず、消えたルビも履歴として残る。"""
        from ledger import load_ledger
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        d0 = load_ledger(self.work)
        counts0 = (len(d0["ruby_applied"]), len(d0["term_first_use"]))

        # 復旧（非 revise の再実行）: 記録は変わらない
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        d1 = load_ledger(self.work)
        self.assertEqual((len(d1["ruby_applied"]), len(d1["term_first_use"])), counts0)

        # 改訂（--revise）: ルビの無いドラフトで再確定しても削除しない（履歴）
        _place_draft(self.work, "ch001", "b1", REVISED_B1)
        code, out = _run_commit(self.work, "ch001", "b1", revise=True)
        self.assertEqual(code, 0, out)
        d2 = load_ledger(self.work)
        self.assertEqual((len(d2["ruby_applied"]), len(d2["term_first_use"])), counts0)
        self.assertEqual([r["term"] for r in d2["ruby_applied"]], ["剣"])

    def test_commit_output_reports_additions(self):
        """確定時の1行表示（追加があったときだけ）。"""
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        self.assertIn("[commit] 台帳: ルビ +1件 / 用語初出 +2件", out)

    def test_purge_keeps_ledger(self):
        """台帳は purge 対象外（assemble 後も残る。analysis_edits.jsonl と同列）。"""
        from _lib import purge_source
        from ledger import load_ledger
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)

        removed = purge_source(self.work)
        self.assertIn("chapters", removed)
        self.assertTrue((self.work / "_work" / "ledger.toml").is_file())
        d = load_ledger(self.work)
        self.assertEqual(len(d["ruby_applied"]), 1)
        self.assertEqual(len(d["term_first_use"]), 2)

    def test_ledger_file_has_no_stray_keys_and_roundtrips(self):
        """空節の裸キー行を書かない（配列ofテーブルへの吸収を防ぐ）。再保存で構造が不変。"""
        from _lib import load_toml
        from ledger import load_ledger, save_ledger
        from saga import cmd_ledger_ono
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        rc, _ = _capture(cmd_ledger_ono, self.work, "どきどき", "ch001/b2", 3, "")
        self.assertEqual(rc, 0)

        p = self.work / "_work" / "ledger.toml"
        text = p.read_text(encoding="utf-8")
        self.assertNotIn("= []", text)  # 空節の裸キー行を出さない
        raw = load_toml(p)
        self.assertEqual(set(raw), {"ledger", "ruby_applied", "term_first_use", "ono_used"})
        for t in raw["term_first_use"]:
            self.assertEqual(set(t), {"term", "block", "paragraph"})

        d1 = load_ledger(self.work)
        save_ledger(self.work, d1)  # 再保存（冪等）
        d2 = load_ledger(self.work)
        for key in ("ruby_applied", "term_first_use", "ono_used"):
            self.assertEqual(d1[key], d2[key], key)


class TestLedgerOnoCli(_FixtureBase):
    """ledger-ono サブコマンド（申告・重複拒否・timeline）。"""

    def test_declare_duplicate_and_timeline(self):
        """④ CLI での申告、同一 form+block+para の重複拒否、timeline 記録。"""
        from ledger import load_ledger
        base_args = ["saga.py", "ledger-ono", str(self.work),
                     "--form", "ドキドキ", "--block", "ch001/b2",
                     "--para", "3", "--context", "鼓動"]
        code, out = _run_cli(base_args)
        self.assertEqual(code, 0, out)
        self.assertIn("[ledger-ono] 記録しました", out)
        self.assertIn("ドキドキ", out)

        d = load_ledger(self.work)
        self.assertEqual(len(d["ono_used"]), 1, d)
        e = d["ono_used"][0]
        self.assertEqual(e["form"], "ドキドキ")
        self.assertEqual(e["block"], "ch001/b2")
        self.assertEqual(e["paragraph"], 3)
        self.assertEqual(e["context"], "鼓動")

        # 同一 form+block+para の再申告は重複しない
        code2, out2 = _run_cli(base_args)
        self.assertEqual(code2, 0, out2)
        self.assertIn("既に記録済み", out2)
        self.assertEqual(len(load_ledger(self.work)["ono_used"]), 1)

        stages = [t["stage"] for t in _timeline(self.work)]
        self.assertEqual(stages.count("ledger-ono"), 2)

    def test_different_para_is_new_key(self):
        """段落が違えば別キーとして追記される（1行表示・exit 0）。"""
        from ledger import load_ledger
        from saga import cmd_ledger_ono
        rc, out = _capture(cmd_ledger_ono, self.work, "ドキドキ", "ch001/b2", 3, "")
        self.assertEqual(rc, 0)
        self.assertEqual(len([l for l in out.splitlines() if l.strip()]), 1, out)
        rc, out = _capture(cmd_ledger_ono, self.work, "ドキドキ", "ch001/b2", 4, "")
        self.assertEqual(rc, 0)
        self.assertIn("記録しました", out)
        self.assertEqual(len(load_ledger(self.work)["ono_used"]), 2)


class TestPrepareInjection(_FixtureBase):
    """prepare（翻訳モード）への「消費済み」注入。"""

    def test_sections_injected_compact_and_full(self):
        """④ 台帳のデータが prepare の翻訳コンテキストへ注入される。"""
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        from saga import cmd_ledger_ono
        rc, _ = _capture(cmd_ledger_ono, self.work, "ドキドキ", "ch001/b2", 3, "鼓動")
        self.assertEqual(rc, 0)

        for style in ("compact", "full"):
            text = _run_stage(self.work, "ch001", "b2", "translation", style)
            self.assertIn("剣", _section(text, "消費済みルビ（再付与不可）"), style)
            self.assertIn("ドキドキ（鼓動）", _section(text, "使用済みオノマトペ"), style)
            terms = _section(text, "用語の初出済み")
            self.assertIn("剣", terms, style)
            self.assertIn("雪", terms, style)

    def test_no_sections_without_ledger(self):
        """⑤ 台帳が無い work では節ごと出ない（後方互換）。"""
        text = _run_stage(self.work, "ch001", "b1", "translation")
        self.assertNotIn("消費済みルビ", text)
        self.assertNotIn("使用済みオノマトペ", text)
        self.assertNotIn("用語の初出済み", text)
        self.assertFalse((self.work / "_work" / "ledger.toml").exists())

    def test_analysis_mode_has_no_sections(self):
        """注入は翻訳モードだけ（分析コンテキストには出さない）。"""
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        text = _run_stage(self.work, "ch001", "b2", "analysis")
        self.assertNotIn("消費済みルビ", text)
        self.assertNotIn("使用済みオノマトペ", text)

    def test_list_cap_shows_omission(self):
        """件数上限を超えた分は「…他N件」で省略する。"""
        from ledger import save_ledger
        save_ledger(self.work, {
            "ruby_applied": [
                {"term": f"語{i}", "reading": "よみ", "block": "ch001/b1", "paragraph": 1}
                for i in range(10)
            ],
            "term_first_use": [],
            "ono_used": [],
        })
        text = _run_stage(self.work, "ch001", "b1", "translation")
        sec = _section(text, "消費済みルビ（再付与不可）")
        self.assertIn("語0", sec)
        self.assertIn("…他2件", sec)
        self.assertNotIn("語8", sec)


class TestStatusLedgerLine(_FixtureBase):
    """status 再開票の台帳1行。"""

    def _run_status(self, work: Path) -> tuple[int, str]:
        from saga import cmd_status
        return _capture(cmd_status, work)

    def test_status_shows_ledger_counts(self):
        _place_draft(self.work, "ch001", "b1", DRAFT_B1)
        code, out = _run_commit(self.work, "ch001", "b1")
        self.assertEqual(code, 0, out)
        code, out = self._run_status(self.work)
        self.assertEqual(code, 0, out)
        self.assertIn("[status] 台帳: ルビ消費 1件・用語初出 2件・オノマトペ使用 0件", out)

    def test_status_without_ledger_has_no_line(self):
        code, out = self._run_status(self.work)
        self.assertEqual(code, 0, out)
        self.assertNotIn("台帳:", out)


class TestCarryLedger(_FixtureBase):
    """carry_analysis の台帳引き継ぎ（連作）。"""

    def test_carry_copies_prev_ledger(self):
        from ledger import ledger_counts, save_ledger
        prev = _make_work(self.td / "prev")
        cur = _make_work(self.td / "cur")
        save_ledger(prev, {
            "ruby_applied": [{"term": "剣", "reading": "つるぎ",
                              "block": "ch001/b1", "paragraph": 1}],
            "term_first_use": [{"term": "雪", "block": "ch001/b1", "paragraph": 2}],
            "ono_used": [{"form": "ドキドキ", "block": "ch001/b1", "context": ""}],
        })
        code, out = _run_cli(["carry_analysis.py", str(cur), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        self.assertIn("[carry] ledger: 前作の台帳を引き継ぎました", out)

        copied = cur / "_work" / "ledger.toml"
        self.assertTrue(copied.is_file(), out)
        self.assertEqual(copied.read_text(encoding="utf-8"),
                         (prev / "_work" / "ledger.toml").read_text(encoding="utf-8"))
        self.assertEqual(ledger_counts(cur), (1, 1, 1))

    def test_carry_without_prev_ledger(self):
        from carry_analysis import _carry_ledger
        prev = _make_work(self.td / "prev")
        cur = _make_work(self.td / "cur")
        _, out = _capture(_carry_ledger, cur, prev)
        self.assertIn("[carry] ledger: 前作に台帳はありません", out)
        self.assertFalse((cur / "_work" / "ledger.toml").exists())


if __name__ == "__main__":
    unittest.main()
