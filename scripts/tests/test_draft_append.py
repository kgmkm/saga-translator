"""draft-append（ドラフトの冪等分割書き込み）と status の再開票（P2-4）のテスト。

検証対象:
  - 分割受領→完成（全範囲が揃ったら従来と同じ場所・形式のドラフトへ切り替え）
  - 同一 request-id の再送が重複しない（記録のみ。内容不一致は拒否）
  - 範囲の重複・欠落・範囲外の拒否（内容マーカーの不一致も拒否）
  - 部分状態の再開票表示（工程・対象ブロック・分析/辞書の版・未裁定・受領済み範囲）
  - 完成後の再開票が消える
  - 既存ドラフト配置（prepare→手書き→commit）との共存

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import hashlib
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

import _lib  # noqa: E402

DRAFT_SUFFIX = "_ja"

# 確定（commit）まで通る合成ドラフト（字形正規化 强行→強行 を含む）
CLEAN_DRAFT_ITEMS = [
    (1, "第一章　始まり"),
    (2, "彼は强行突破を選んだ。"),
    (3, "雪が降り始めた。"),
]


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_work(base: Path) -> Path:
    """zh→ja の最小合成ワーク（1章2ブロック: b1=[1,3], b2=[4,6]）。"""
    work = base / "work"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}, {id="b2", paras=[4,6]}]
""")
    paras = []
    for n, text in ((1, "第一章开始。"), (2, "他选择了强行突破。"), (3, "雪开始落下。"),
                    (4, "钟声响了。"), (5, "风很冷。"), (6, "他向前走。")):
        paras.append(f'[[paras]]\nn = {n}\ntext = "{text}"\n')
    _write(work / "chapters" / "ch001.paras.toml", "\n".join(paras))
    _write(work / "analysis" / "characters-[ネタバレあり].toml",
           '[[characters]]\nname_src = "林霖"\nname_ja = "リンリン"\n')
    return work


def _numbered(*pairs: tuple[int, str]) -> str:
    return "\n\n".join(f"[P{n:03d}] {t}" for n, t in pairs) + "\n"


def _draft_path(work: Path, cid: str = "ch001", bid: str = "b1") -> Path:
    return work / "output" / "_work" / f"{cid}_{bid}{DRAFT_SUFFIX}_numbered.md"


def _record_path(work: Path, cid: str = "ch001", bid: str = "b1") -> Path:
    return work / "output" / "_work" / "draft_parts" / f"{cid}_{bid}.json"


def _load_record(work: Path, cid: str = "ch001", bid: str = "b1") -> dict:
    return json.loads(_record_path(work, cid, bid).read_text(encoding="utf-8"))


def _mark_analyzed(work: Path, block_key: str) -> str:
    """block に分析済みの記録（現行原文と一致する analysis_src_hash）を付ける。"""
    meta = _lib.load_chapters_meta(work)
    b = _lib.block_meta(meta, block_key)
    cid = block_key.split("/", 1)[0]
    b["analysis_committed_at"] = "2026-01-01T00:00:00+00:00"
    b["analysis_src_hash"] = _lib.block_src_hash(_lib.load_paras(work, cid), b)
    _lib.save_chapters_meta(work, meta)
    return str(b["analysis_src_hash"])


class _FixtureBase(unittest.TestCase):
    """合成ワークの共通セットアップと実行ヘルパ。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.work = _make_work(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    # ---- 実行ヘルパ ----
    def append(self, *, chapter: str = "ch001", block: str = "b1",
               start: str = "P1", end: str = "P3", request_id: str = "r1",
               content: str | None = None, file: str | None = None,
               stdin_text: str | None = None) -> tuple[int, str]:
        """cmd_draft_append を in-process で実行し (終了コード, 出力) を返す。"""
        from saga import cmd_draft_append
        if file is None and stdin_text is None:
            part = self.td / f"part_{request_id}_{start}_{end}.md"
            part.write_bytes((content or "").encode("utf-8"))
            file = str(part)
        buf = io.StringIO()
        old_out, old_err, old_in = sys.stdout, sys.stderr, sys.stdin
        try:
            sys.stdout = sys.stderr = buf
            if stdin_text is not None:
                sys.stdin = io.StringIO(stdin_text)
            code = cmd_draft_append(self.work, chapter, block, start, end,
                                    request_id, file)
        finally:
            sys.stdout, sys.stderr, sys.stdin = old_out, old_err, old_in
        return code, buf.getvalue()

    def status(self) -> tuple[int, str]:
        """cmd_status を in-process で実行し (終了コード, 出力) を返す。"""
        from saga import cmd_status
        buf = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_status(self.work)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code, buf.getvalue()

    def run_commit(self) -> tuple[int, str]:
        from saga import cmd_commit
        buf = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code, buf.getvalue()


# ---------------------------------------------------------------------------
# ① 分割受領→完成
# ---------------------------------------------------------------------------


class TestReceiveAndComplete(_FixtureBase):
    def test_split_receive_then_complete(self):
        """分割で受領し、全範囲が揃ったら従来のドラフト配置へ切り替わる。"""
        code, out = self.append(start="P1", end="P2", request_id="r1",
                                content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        self.assertIn("受領: P1-P2", out)
        # 未完成では完成ドラフトを置かない
        self.assertFalse(_draft_path(self.work).exists())
        rec = _load_record(self.work)
        self.assertFalse(rec["complete"])
        self.assertEqual(len(rec["parts"]), 1)

        code, out = self.append(start="P3", end="P3", request_id="r2",
                                content=_numbered((3, "訳三。")))
        self.assertEqual(code, 0)
        self.assertIn("完成", out)
        draft = _draft_path(self.work)
        self.assertTrue(draft.is_file())
        self.assertEqual(draft.read_text(encoding="utf-8"),
                         "[P001] 訳一。\n\n[P002] 訳二。\n\n[P003] 訳三。\n")
        rec = _load_record(self.work)
        self.assertTrue(rec["complete"])
        self.assertEqual(rec["draft_path"], "output/_work/ch001_b1_ja_numbered.md")

    def test_record_has_request_id_range_hash(self):
        """受領の記録に依頼ID・範囲・ハッシュが残る（置き場は _work/draft_parts/）。"""
        content = _numbered((1, "訳一。"), (2, "訳二。"))
        code, _out = self.append(start="P1", end="P2", request_id="req-A",
                                 content=content)
        self.assertEqual(code, 0)
        self.assertTrue(_record_path(self.work).is_file())
        rec = _load_record(self.work)
        self.assertEqual(rec["block_range"], [1, 3])
        part = rec["parts"][0]
        self.assertEqual(part["request_id"], "req-A")
        self.assertEqual([part["start"], part["end"]], [1, 2])
        self.assertEqual(part["hash"], _sha12(content.encode("utf-8")))

    def test_crlf_content_normalized_in_assembly(self):
        """CRLF の部分ドラフトも LF に正規化して決定的に連結する。"""
        part1 = self.td / "crlf1.md"
        part1.write_bytes("[P001] 訳一。\r\n".encode("utf-8"))
        code, _out = self.append(start="P1", end="P1", request_id="r1",
                                 file=str(part1))
        self.assertEqual(code, 0)
        code, _out = self.append(start="P2", end="P3", request_id="r2",
                                 content=_numbered((2, "訳二。"), (3, "訳三。")))
        self.assertEqual(code, 0)
        self.assertEqual(_draft_path(self.work).read_text(encoding="utf-8"),
                         "[P001] 訳一。\n\n[P002] 訳二。\n\n[P003] 訳三。\n")


# ---------------------------------------------------------------------------
# ② 同一 request-id の再送が重複しない
# ---------------------------------------------------------------------------


class TestIdempotency(_FixtureBase):
    def test_same_request_id_resend_is_record_only(self):
        content = _numbered((1, "訳一。"), (2, "訳二。"))
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=content)
        self.assertEqual(code, 0)
        code, out = self.append(start="P1", end="P2", request_id="r1",
                                content=content)
        self.assertEqual(code, 0)
        self.assertIn("再送を受理", out)
        rec = _load_record(self.work)
        self.assertEqual(len(rec["parts"]), 1)  # 重複しない
        self.assertEqual(rec["parts"][0]["resends"], 1)
        self.assertFalse(_draft_path(self.work).exists())

    def test_resend_after_complete_keeps_draft_bytes(self):
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        code, _out = self.append(start="P3", end="P3", request_id="r2",
                                 content=_numbered((3, "訳三。")))
        self.assertEqual(code, 0)
        before = _draft_path(self.work).read_bytes()

        code, out = self.append(start="P3", end="P3", request_id="r2",
                                content=_numbered((3, "訳三。")))
        self.assertEqual(code, 0)
        self.assertIn("再送を受理", out)
        self.assertEqual(_draft_path(self.work).read_bytes(), before)
        rec = _load_record(self.work)
        self.assertEqual(len(rec["parts"]), 2)
        self.assertEqual(rec["parts"][1]["resends"], 1)

    def test_same_request_id_with_different_content_rejected(self):
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        # 同じ範囲で内容が違う再送
        code, out = self.append(start="P1", end="P2", request_id="r1",
                                content=_numbered((1, "別の訳。"), (2, "訳二。")))
        self.assertEqual(code, 1)
        self.assertIn("内容が一致しません", out)
        # 同じ依頼IDで範囲が違う再送
        code, out = self.append(start="P3", end="P3", request_id="r1",
                                content=_numbered((3, "訳三。")))
        self.assertEqual(code, 1)
        rec = _load_record(self.work)
        self.assertEqual(len(rec["parts"]), 1)
        self.assertEqual(rec["parts"][0]["resends"], 0)


# ---------------------------------------------------------------------------
# ③ 範囲の重複・欠落・範囲外の拒否
# ---------------------------------------------------------------------------


class TestRejections(_FixtureBase):
    def test_overlap_rejected(self):
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        code, out = self.append(start="P2", end="P3", request_id="r2",
                                content=_numbered((2, "訳二。"), (3, "訳三。")))
        self.assertEqual(code, 1)
        self.assertIn("重複", out)
        self.assertEqual(len(_load_record(self.work)["parts"]), 1)

    def test_gap_rejected(self):
        code, _out = self.append(start="P1", end="P1", request_id="r1",
                                 content=_numbered((1, "訳一。")))
        self.assertEqual(code, 0)
        code, out = self.append(start="P3", end="P3", request_id="r2",
                                content=_numbered((3, "訳三。")))
        self.assertEqual(code, 1)
        self.assertIn("欠落", out)
        self.assertIn("P2", out)
        # 欠けていた範囲を送れば続行できる
        code, out = self.append(start="P2", end="P3", request_id="r3",
                                content=_numbered((2, "訳二。"), (3, "訳三。")))
        self.assertEqual(code, 0)
        self.assertTrue(_draft_path(self.work).is_file())

    def test_out_of_range_rejected(self):
        # ブロック b1 は P1-P3。末尾を超える
        code, out = self.append(start="P2", end="P4", request_id="r1",
                                content=_numbered((2, "訳二。"), (3, "訳三。"), (4, "訳四。")))
        self.assertEqual(code, 1)
        self.assertIn("範囲外", out)
        # ブロック b2 は P4-P6。先頭より前
        code, out = self.append(chapter="ch001", block="b2", start="P3", end="P4",
                                request_id="r2",
                                content=_numbered((3, "訳三。"), (4, "訳四。")))
        self.assertEqual(code, 1)
        self.assertIn("範囲外", out)
        # 逆転
        code, out = self.append(start="P3", end="P1", request_id="r3",
                                content=_numbered((1, "訳一。"), (3, "訳三。")))
        self.assertEqual(code, 1)
        self.assertIn("逆転", out)
        # 拒否された受領は記録を作らない
        self.assertFalse(_record_path(self.work).exists())

    def test_content_marker_mismatch_rejected(self):
        # 欠落（宣言 P1-P2 に P1 しか無い）
        code, out = self.append(start="P1", end="P2", request_id="r1",
                                content=_numbered((1, "訳一。")))
        self.assertEqual(code, 1)
        self.assertIn("欠落", out)
        # 範囲外（宣言 P1-P2 に P3 が混ざる）
        code, out = self.append(start="P1", end="P2", request_id="r2",
                                content=_numbered((1, "訳一。"), (2, "訳二。"), (3, "訳三。")))
        self.assertEqual(code, 1)
        self.assertIn("範囲外", out)
        # 重複（同じマーカーが2回）
        code, out = self.append(start="P1", end="P2", request_id="r3",
                                content=_numbered((1, "訳一。"), (1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 1)
        self.assertIn("重複", out)
        # マーカーなし
        code, out = self.append(start="P1", end="P2", request_id="r4",
                                content="訳一。\n訳二。\n")
        self.assertEqual(code, 1)
        self.assertIn("マーカーがありません", out)
        # 形式不正（接尾辞付きマーカー）
        code, out = self.append(start="P1", end="P2", request_id="r5",
                                content="[P1x] 訳一。\n[P002] 訳二。\n")
        self.assertEqual(code, 1)
        self.assertIn("形式が不正", out)
        # いずれも記録を作らない
        self.assertFalse(_record_path(self.work).exists())

    def test_rejected_submission_does_not_change_record(self):
        code, _out = self.append(start="P1", end="P1", request_id="r1",
                                 content=_numbered((1, "訳一。")))
        self.assertEqual(code, 0)
        for start, end, rid, content in (
            ("P1", "P2", "r2", _numbered((1, "訳一。"), (2, "訳二。"))),  # 重複
            ("P3", "P3", "r3", _numbered((3, "訳三。"))),                  # 欠落
            ("P1", "P1", "r4", _numbered((1, "別。"))),                    # 依頼ID別・重複
        ):
            code, _out = self.append(start=start, end=end, request_id=rid,
                                     content=content)
            self.assertEqual(code, 1)
        rec = _load_record(self.work)
        self.assertEqual(len(rec["parts"]), 1)
        self.assertEqual(rec["parts"][0]["resends"], 0)


# ---------------------------------------------------------------------------
# ④ 部分状態の再開票表示 ／ ⑤ 完成後の再開票が消える
# ---------------------------------------------------------------------------


class TestResumeTicket(_FixtureBase):
    def test_partial_state_ticket_shows_ranges_and_versions(self):
        """未完了のドラフトがあると、再開票に受領済み範囲と版・未裁定が出る。"""
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        # age_policy の記録（裁定済み1件・未裁定1件）
        _write(self.work / "analysis" / "age_policy_report-[ネタバレあり].toml", """\
[[mentions]]
block = "ch001/b1"
paragraph = 2
matched = "少女"
class = "other_person"
context_hash = "aaaa00000000"
ruling = "allow"
rationale = "確認済み"
source_line = "她不是少女。"

[[mentions]]
block = "ch001/b1"
paragraph = 3
matched = "妹妹"
class = "other_person"
context_hash = "bbbb00000000"
ruling = ""
rationale = ""
source_line = "妹妹在哭。"
""")
        code, out = self.status()
        self.assertEqual(code, 0)
        self.assertIn("再開票（圧縮後の再開用）:", out)
        self.assertIn("工程: 分析 / 対象ブロック: ch001/b1", out)
        self.assertIn("分析の版: 未分析", out)
        self.assertIn("辞書の版: none", out)
        self.assertIn("confirm —・語彙 candidates —（ドラフト未配置）", out)
        self.assertIn("age_policy 1件（作品全体）", out)
        self.assertIn("draft-append 受領済み（未完了）:", out)
        self.assertIn("ch001/b1: 受領 P1-P2 / 全体 P1-P3（残り P3）", out)

    def test_translation_phase_ticket_and_analysis_version(self):
        """分析済みなら工程は翻訳。analysis_src_hash の一致状態が版として出る。"""
        cur = _mark_analyzed(self.work, "ch001/b1")
        _mark_analyzed(self.work, "ch001/b2")
        code, _out = self.append(start="P1", end="P1", request_id="r1",
                                 content=_numbered((1, "訳一。")))
        self.assertEqual(code, 0)
        code, out = self.status()
        self.assertEqual(code, 0)
        self.assertIn("工程: 翻訳 / 対象ブロック: ch001/b1", out)
        self.assertIn(f"分析の版: 一致（analysis_src_hash={cur}）", out)

        # 原文が変わって記録が古くなったら「不一致」と出る
        meta = _lib.load_chapters_meta(self.work)
        _lib.block_meta(meta, "ch001/b1")["analysis_src_hash"] = "000000000000"
        _lib.save_chapters_meta(self.work, meta)
        code, out = self.status()
        self.assertIn("分析の版: 不一致（記録 000000000000 != 現行", out)

    def test_pending_counts_from_draft(self):
        """ドラフト配置済みなら confirm・語彙 candidates の未裁定件数を検査して出す。"""
        _mark_analyzed(self.work, "ch001/b1")
        _mark_analyzed(self.work, "ch001/b2")
        # 語彙 candidates（作品 terms）と、文字数比の confirm を仕込む
        _write(self.work / "analysis" / "terms.toml",
               '[[terms]]\nterm_src = "灵石"\nterm_ja = "霊石"\n')
        paras = _lib.load_paras(self.work, "ch001")
        for p in paras["paras"]:
            if p["n"] == 2:
                p["text"] = "他选择了强行突破，可是雪越下越大，风也越来越冷，前方的路完全看不见了。"
        _lib.save_paras(self.work, "ch001", paras)
        _mark_analyzed(self.work, "ch001/b1")  # 原文変更後のハッシュで記録し直す

        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "序章。灵石が光る。"), (2, "彼は强行突破を選んだ。")))
        self.assertEqual(code, 0)
        code, _out = self.append(start="P3", end="P3", request_id="r2",
                                 content=_numbered((3, "雪が降り始めた。")))
        self.assertEqual(code, 0)
        code, out = self.status()
        self.assertEqual(code, 0)
        self.assertIn("confirm 1件・語彙 candidates 1件（対象ブロックのドラフトから検査）", out)

    def test_ticket_lists_only_incomplete_records(self):
        """完成済みの記録は再開票に出さず、未完了の記録だけを出す。"""
        _mark_analyzed(self.work, "ch001/b1")
        _mark_analyzed(self.work, "ch001/b2")
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        code, _out = self.append(start="P3", end="P3", request_id="r2",
                                 content=_numbered((3, "訳三。")))
        self.assertEqual(code, 0)  # b1 は完成
        code, _out = self.append(block="b2", start="P4", end="P4", request_id="r3",
                                 content=_numbered((4, "訳四。")))
        self.assertEqual(code, 0)  # b2 は未完了
        code, out = self.status()
        self.assertIn("ch001/b2: 受領 P4 / 全体 P4-P6（残り P5-P6）", out)
        self.assertNotIn("ch001/b1: 受領", out)

    def test_ranges_line_disappears_after_draft_complete(self):
        """完成（従来のドラフト配置へ切り替え）後は、再開票から受領済み範囲が消える。"""
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        code, _out = self.append(start="P3", end="P3", request_id="r2",
                                 content=_numbered((3, "訳三。")))
        self.assertEqual(code, 0)
        code, out = self.status()
        self.assertEqual(code, 0)
        self.assertIn("再開票（圧縮後の再開用）:", out)  # 翻訳はまだ未完了
        self.assertNotIn("draft-append 受領済み", out)
        # ドラフト配置済みなので、confirm・語彙はドラフトから検査される
        self.assertIn("confirm 0件・語彙 candidates 0件（対象ブロックのドラフトから検査）", out)

    def test_no_ticket_when_all_blocks_done(self):
        """未完了ブロックが無ければ再開票を出さない（完成後）。"""
        for key in ("ch001/b1", "ch001/b2"):
            _mark_analyzed(self.work, key)
        _write(self.work / "analysis" / "plot-[ネタバレあり].toml", """\
[[entries]]
block = "ch001/b1"
text = "要点。"

[[entries]]
block = "ch001/b2"
text = "要点。"
""")
        _write(self.work / "output" / "ch001" / "b1_ja.md", "訳一。\n")
        _write(self.work / "output" / "ch001" / "b2_ja.md", "訳四。\n")
        code, out = self.status()
        self.assertEqual(code, 0)
        self.assertNotIn("再開票", out)


# ---------------------------------------------------------------------------
# ⑥ 既存ドラフト配置（prepare→手書き→commit）との共存
# ---------------------------------------------------------------------------


class TestCoexistenceWithExistingFlow(_FixtureBase):
    def test_start_refused_when_manual_draft_exists(self):
        """手書きドラフトがある状態では受領を始めない（上書きしない）。"""
        manual = _draft_path(self.work)
        _write(manual, _numbered(*CLEAN_DRAFT_ITEMS))
        before = manual.read_bytes()
        code, out = self.append(start="P1", end="P2", request_id="r1",
                                content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 1)
        self.assertIn("既存のドラフト", out)
        self.assertEqual(manual.read_bytes(), before)
        self.assertFalse(_record_path(self.work).exists())

    def test_manual_draft_still_commits(self):
        """従来フロー（手書きドラフト→commit）は draft-append の追加後も通る。"""
        _write(_draft_path(self.work), _numbered(*CLEAN_DRAFT_ITEMS))
        code, out = self.run_commit()
        self.assertEqual(code, 0, out)
        self.assertTrue((self.work / "output" / "ch001" / "b1_ja.md").is_file())
        self.assertFalse(_record_path(self.work).exists())

    def test_appended_draft_commits(self):
        """draft-append で完成したドラフトが、そのまま commit で確定できる。"""
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered(*CLEAN_DRAFT_ITEMS[:2]))
        self.assertEqual(code, 0)
        code, out = self.append(start="P3", end="P3", request_id="r2",
                                content=_numbered(CLEAN_DRAFT_ITEMS[2]))
        self.assertEqual(code, 0)
        self.assertIn("完成", out)
        code, out = self.run_commit()
        self.assertEqual(code, 0, out)
        self.assertTrue((self.work / "output" / "ch001" / "b1_ja.md").is_file())
        # 完成の記録は残る（status の再開票には出ない）
        self.assertTrue(_load_record(self.work)["complete"])

    def test_completion_refuses_to_overwrite_unrelated_draft(self):
        """受領の途中で置かれた無関係のドラフトも、完成時に上書きしない。"""
        code, _out = self.append(start="P1", end="P2", request_id="r1",
                                 content=_numbered((1, "訳一。"), (2, "訳二。")))
        self.assertEqual(code, 0)
        stray = _draft_path(self.work)
        _write(stray, "別の書きかけ。\n")
        code, out = self.append(start="P3", end="P3", request_id="r2",
                                content=_numbered((3, "訳三。")))
        self.assertEqual(code, 1)
        self.assertIn("既存ファイルがあります", out)
        self.assertEqual(stray.read_text(encoding="utf-8"), "別の書きかけ。\n")
        self.assertFalse(_load_record(self.work)["complete"])
        # 退避すれば再送で完成できる
        stray.unlink()
        code, out = self.append(start="P3", end="P3", request_id="r2",
                                content=_numbered((3, "訳三。")))
        self.assertEqual(code, 0)
        self.assertIn("完成", out)
        self.assertTrue(_draft_path(self.work).is_file())


# ---------------------------------------------------------------------------
# CLI（サブプロセス起動）
# ---------------------------------------------------------------------------


class TestDraftAppendCLI(_FixtureBase):
    def _run_cli(self, *args: str, input_text: str | None = None) -> tuple[int, str]:
        r = subprocess.run(
            [PYTHON, str(SCRIPTS / "saga.py"), "draft-append", str(self.work), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            input=input_text)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def test_cli_with_file(self):
        part = self.td / "part1.md"
        part.write_text(_numbered((1, "訳一。"), (2, "訳二。")), encoding="utf-8")
        code, out = self._run_cli("--chapter", "ch001", "--block", "b1",
                                  "--start", "P1", "--end", "P2",
                                  "--request-id", "cli-1", "--file", str(part))
        self.assertEqual(code, 0, out)
        self.assertIn("受領: P1-P2", out)
        self.assertTrue(_record_path(self.work).is_file())

    def test_cli_stdin(self):
        code, out = self._run_cli("--chapter", "ch001", "--block", "b1",
                                  "--start", "P1", "--end", "P1",
                                  "--request-id", "cli-2",
                                  input_text=_numbered((1, "訳一。")))
        self.assertEqual(code, 0, out)
        self.assertIn("受領: P1", out)

    def test_cli_rejection_is_nonzero(self):
        code, out = self._run_cli("--chapter", "ch001", "--block", "b1",
                                  "--start", "P1", "--end", "P1",
                                  "--request-id", "cli-3",
                                  input_text="マーカーなし。\n")
        self.assertEqual(code, 1)
        self.assertIn("マーカーがありません", out)


if __name__ == "__main__":
    unittest.main()
