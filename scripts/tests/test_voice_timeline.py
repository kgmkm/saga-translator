"""声の時点解決（欄ごとの累積）と確認の候補・status 節のテスト。

spec: docs/phase8-taskQ2-voicetime.md（P0-4・#7b・#7）。

①b1 で一人称・b2 で口調 → b3 のコンテキストに両方
②b2 自身には b2 の変化が注記付き（このブロックの途中で変化）
③欄ごと独立（同一欄の複数変化は最新のみ）
④話者候補の抽出と表示（引用符の近く。セリフありとは扱わない）
⑤異表記候補の抽出（キャラ同士・原文側のみ・work レベル）
⑥status の確認待ち一覧
⑦旧データ（arcs 無し・候補無し）が壊れない

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

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
               n_blocks: int = 3, lock_prep: bool = True) -> Path:
    """chapters_meta / paras / analysis / manifest を持つ最小ワーク（合成データ）。"""
    work = base / "work"
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "_work").mkdir(parents=True)
    (work / "manifest.toml").write_text('title = "T"\nlang = "zh"\ndst = "ja"\n', encoding="utf-8")
    if lock_prep:
        (work / "_work" / "lock_prep.toml").write_text('done = true\n', encoding="utf-8")
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


def _plot_delta(block: str) -> str:
    return f'block = "{block}"\n\n[[plot_delta]]\nblock = "{block}"\ntext = "要点。"\n'


def _arcs_section(text: str) -> str:
    """コンテキスト出力の「## 声の変化」節（見出し行の次から次の ## 見出しまで）。"""
    i = text.find("## 声の変化")
    if i < 0:
        return ""
    i = text.find("\n", i) + 1
    j = text.find("\n## ", i)
    return text[i:j if j > i else len(text)]


def _pending_section(out: str) -> str:
    i = out.find("分析の確認待ち")
    return out[i:] if i >= 0 else ""


def _char(work: Path, name_src: str) -> dict:
    for c in (_lib.load_characters(work).get("characters") or []):
        if c.get("name_src") == name_src:
            return c
    raise AssertionError(f"character {name_src} not found")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="q2_voicetime_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _delta_file(self, text: str, name: str = "delta.toml") -> Path:
        d = self.tmp / name
        d.write_text(text, encoding="utf-8")
        return d

    def _context(self, work: Path, cid: str, bid: str) -> str:
        from build_context import _stage_impl
        _stage_impl(work, cid, bid, "translation")
        return (work / "translation_input" / f"{cid}_{bid}_translation.md").read_text(encoding="utf-8")

    def _commit(self, work: Path, delta_text: str, name: str = "delta.toml") -> tuple[int, str]:
        d = self._delta_file(delta_text, name)
        return _run_saga("analyze-commit", str(work), "--delta", str(d))


# ---------------------------------------------------------------------------
# ①②③ 声の時点解決（欄ごとの累積。build_context）
# ---------------------------------------------------------------------------

ARC_WORK_TEXTS = ["Alice walked in.", "夜は静かだった。"] * 3  # 3ブロック×2段落


class TestVoiceTimelineContext(_Base):
    def _work_with_arcs(self, arcs: list[dict]) -> Path:
        return _make_work(self.tmp, chars={"characters": [{
            "name_src": "Alice", "name_ja": "アリス", "first_person": "僕", "arcs": arcs,
        }]}, texts=list(ARC_WORK_TEXTS), n_blocks=3)

    def test_b3_shows_b1_first_person_and_b2_style(self):
        """① b1 の一人称と b2 の口調が両方 b3 に載る（#7b）"""
        work = self._work_with_arcs([
            {"at_block": "ch001/b1", "first_person": "私", "note": "変化1"},
            {"at_block": "ch001/b2", "speech_style": "タメ口。語尾は「〜だよ」", "note": "変化2"},
        ])
        section = _arcs_section(self._context(work, "ch001", "b3"))
        self.assertIn("- 一人称: 私（ch001/b1〜）", section)
        self.assertIn("- 口調: タメ口。語尾は「〜だよ」（ch001/b2〜）", section)
        self.assertNotIn("（このブロックの途中で変化）", section)
        self.assertIn("変化1", section)
        self.assertIn("変化2", section)

    def test_b2_applies_own_change_with_note(self):
        """② 対象ブロック（b2）の途中で起きた変化も適用し、注記を付ける"""
        work = self._work_with_arcs([
            {"at_block": "ch001/b1", "first_person": "私", "note": "変化1"},
            {"at_block": "ch001/b2", "speech_style": "タメ口。語尾は「〜だよ」", "note": "変化2"},
        ])
        section = _arcs_section(self._context(work, "ch001", "b2"))
        self.assertIn("- 一人称: 私（ch001/b1〜）", section)
        self.assertIn("- 口調: タメ口。語尾は「〜だよ」（このブロックの途中で変化）", section)

    def test_b1_applies_own_change_with_note_only(self):
        """② b1 自身の変化にも注記が付き、b2 の変化は載らない"""
        work = self._work_with_arcs([
            {"at_block": "ch001/b1", "first_person": "私", "note": "変化1"},
            {"at_block": "ch001/b2", "speech_style": "タメ口。語尾は「〜だよ」", "note": "変化2"},
        ])
        section = _arcs_section(self._context(work, "ch001", "b1"))
        self.assertIn("- 一人称: 私（このブロックの途中で変化）", section)
        self.assertNotIn("口調", section)
        self.assertNotIn("変化2", section)

    def test_same_field_latest_only(self):
        """③ 同一欄の複数変化は最新のみ（古い値・基本の設定に戻さない）"""
        work = self._work_with_arcs([
            {"at_block": "ch001/b1", "first_person": "私"},
            {"at_block": "ch001/b2", "first_person": "あたし"},
        ])
        section = _arcs_section(self._context(work, "ch001", "b3"))
        self.assertIn("- 一人称: あたし（ch001/b2〜）", section)
        self.assertNotIn("私", section)
        self.assertNotIn("僕", section)

    def test_future_arcs_not_applied(self):
        """対象より後の arc は載らない（b1 時点で b2 の変化は無い）"""
        work = self._work_with_arcs([
            {"at_block": "ch001/b2", "speech_style": "タメ口"},
        ])
        section = _arcs_section(self._context(work, "ch001", "b1"))
        self.assertIn("(なし)", section)


class TestCompactArcsUnit(unittest.TestCase):
    """_compact_arcs の欄ごとの累積（単体）"""

    def setUp(self):
        self.chars = {"characters": [{"name_ja": "アリス", "arcs": [
            {"at_block": "ch001/b1", "first_person": "私"},
            {"at_block": "ch001/b2", "speech_style": "タメ口", "note": "変化"},
            {"at_block": "ch002/b1", "first_person": "あたし"},
        ]}]}

    def test_accumulates_per_field(self):
        from build_context import _compact_arcs
        result, warns = _compact_arcs(self.chars, "ch002", "b1", include_target=True)
        arc = result["アリス"][0]
        self.assertEqual(arc["first_person"], "あたし")      # 同一欄は最新のみ
        self.assertEqual(arc["speech_style"], "タメ口")      # b2 の変化が残る
        self.assertEqual(arc["field_sources"],
                         {"first_person": "ch002/b1", "speech_style": "ch001/b2"})
        self.assertEqual(arc["mid_block_fields"], ["first_person"])  # 対象ブロックの途中
        self.assertFalse(warns)

    def test_default_excludes_target(self):
        """既定（include_target=False）は後方互換で対象より前だけ"""
        from build_context import _compact_arcs
        result, _ = _compact_arcs(self.chars, "ch002", "b1")
        arc = result["アリス"][0]
        self.assertEqual(arc["first_person"], "私")  # ch002/b1 の変化は入らない
        self.assertEqual(arc["speech_style"], "タメ口")
        self.assertEqual(arc["mid_block_fields"], [])

    def test_fmt_resolved_mid_block_note(self):
        from build_context import MID_BLOCK_NOTE, _compact_arcs, fmt_arcs_resolved
        result, _ = _compact_arcs(self.chars, "ch002", "b1", include_target=True)
        text = fmt_arcs_resolved(result)
        self.assertIn(f"- 一人称: あたし{MID_BLOCK_NOTE}", text)
        self.assertIn("- 口調: タメ口（ch001/b2〜）", text)


# ---------------------------------------------------------------------------
# ④⑤ 確認の候補（analyze-commit）
# ---------------------------------------------------------------------------

CAND_TEXTS = [
    "“你好。”张三说。“再见。”张三挥手。", "补一。",       # b1: 話者候補（引用符の近くに2回）
    "张三和張三都来了。", "补二。",                        # b2: 異表記（キャラ同士）
    "张三看着张叁。张叁笑了。张叁走了。", "补三。",          # b3: 異表記（該当キャラなし）
    "李四站在门口。", "补四。",                            # b4: 候補なし
]

CAND_CHARS = {"characters": [
    {"name_src": "张三", "name_ja": "チョウサン"},
    {"name_src": "張三", "name_ja": "ジャンサン"},
    {"name_src": "李四", "name_ja": "リシ"},
]}


class TestVoiceCandidates(_Base):
    def _cand_work(self) -> Path:
        return _make_work(self.tmp, chars=dict(CAND_CHARS), texts=list(CAND_TEXTS), n_blocks=4)

    def test_speaker_candidate_recorded_and_shown(self):
        """④ 名前が引用符の近くに閾値回数以上 → speaker_candidate（セリフありとは扱わない）"""
        work = self._cand_work()
        code, out = self._commit(work, _plot_delta("ch001/b1"))
        self.assertEqual(code, 0, out)
        self.assertIn("話者の確認候補: チョウサン (ch001/b1)", out)
        c = _char(work, "张三")
        self.assertEqual(c["speaker_candidate"], ["ch001/b1"])
        self.assertNotIn("voice_status", c)  # セリフありとは扱わない
        self.assertNotIn("speaker_candidate", _char(work, "張三"))
        self.assertNotIn("speaker_candidate", _char(work, "李四"))

    def test_speaker_threshold_not_met(self):
        """④ 引用符の近くが1回だけでは候補にしない（窓の外は数えない）"""
        texts = ["“你好。”张三说。他慢慢地走向远处。风很大。天色渐暗。雨落下来了。张三走了。",
                 "补。", "补二。", "补三。"]
        work = _make_work(self.tmp, chars={"characters": [
            {"name_src": "张三", "name_ja": "チョウサン"}]}, texts=texts, n_blocks=2)
        code, out = self._commit(work, _plot_delta("ch001/b1"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("話者の確認候補", out)
        self.assertNotIn("speaker_candidate", _char(work, "张三"))

    def test_variant_pair_recorded_both_sides(self):
        """⑤ 近い表記の名前が原文に併存 → 両方のキャラに variant_candidates"""
        work = self._cand_work()
        code, out = self._commit(work, _plot_delta("ch001/b2"))
        self.assertEqual(code, 0, out)
        self.assertIn("異表記候補: 张三⇔張三 (ch001/b2)", out)
        a = _char(work, "张三")
        b = _char(work, "張三")
        self.assertEqual(a["variant_candidates"],
                         [{"name_src": "張三", "similar_to_src": "张三", "block": "ch001/b2"}])
        self.assertEqual(b["variant_candidates"],
                         [{"name_src": "张三", "similar_to_src": "張三", "block": "ch001/b2"}])

    def test_variant_work_level_recorded(self):
        """⑤ 該当キャラが無い揺れ表記は work レベル（トップの variant_candidates）に置く"""
        work = self._cand_work()
        code, out = self._commit(work, _plot_delta("ch001/b3"))
        self.assertEqual(code, 0, out)
        self.assertIn("異表記候補: 张三⇔张叁 (ch001/b3)", out)
        self.assertIn("該当キャラなし", out)
        data = _lib.load_characters(work)
        self.assertEqual(data.get("variant_candidates"),
                         [{"name_src": "张叁", "similar_to_src": "张三", "block": "ch001/b3"}])
        for c in data.get("characters") or []:
            self.assertNotIn("variant_candidates", c)

    def test_no_changes_commit_still_records_candidates(self):
        """④ no_changes の commit でも候補は記録される（差分適用後の走査）"""
        work = self._cand_work()
        code, out = self._commit(work, 'block = "ch001/b1"\nno_changes = true\n')
        self.assertEqual(code, 0, out)
        self.assertEqual(_char(work, "张三")["speaker_candidate"], ["ch001/b1"])
        self.assertIn("analysis_no_changes = true を記録", out)

    def test_candidates_idempotent(self):
        """④⑤ 同一 delta の再実行で候補は重複しない"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b1"))
        self._commit(work, _plot_delta("ch001/b2"))
        self._commit(work, _plot_delta("ch001/b1"))
        self._commit(work, _plot_delta("ch001/b2"))
        a = _char(work, "张三")
        self.assertEqual(a["speaker_candidate"], ["ch001/b1"])
        self.assertEqual(len(a["variant_candidates"]), 1)
        self.assertEqual(len(_char(work, "張三")["variant_candidates"]), 1)

    def test_variant_resolved_by_alias_dropped(self):
        """⑤ aliases に入れたら候補は消え、再走査でも復活しない"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b2"))
        self._commit(work, _plot_delta("ch001/b3"))
        delta = ('block = "ch001/b4"\n\n[[characters_delta]]\nmatch_key = "name_src"\n'
                 'match_value = "张三"\naliases = ["張三"]\n')
        code, out = self._commit(work, delta)
        self.assertEqual(code, 0, out)
        a = _char(work, "张三")
        self.assertEqual(a["aliases"], ["張三"])
        self.assertNotIn("variant_candidates", a)
        self.assertNotIn("variant_candidates", _char(work, "張三"))
        # work レベル（該当キャラなし）の候補はまだ残る
        self.assertEqual(_lib.load_characters(work)["variant_candidates"],
                         [{"name_src": "张叁", "similar_to_src": "张三", "block": "ch001/b3"}])
        # 再走査（同じ b2 の再コミット）でもペアの候補が復活しない
        code, out = self._commit(work, _plot_delta("ch001/b2"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("variant_candidates", _char(work, "张三"))
        self.assertNotIn("variant_candidates", _char(work, "張三"))

    def test_work_level_candidate_resolved_by_new_character(self):
        """⑤ 揺れ表記を別人として登録したら work レベルの候補は消える"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b3"))
        self.assertIn("variant_candidates", _lib.load_characters(work))
        delta = ('block = "ch001/b4"\n\n[[characters_delta]]\nmatch_key = "name_src"\n'
                 'match_value = "张叁"\nname_ja = "サンサン"\nrole = "脇役"\n')
        code, out = self._commit(work, delta)
        self.assertEqual(code, 0, out)
        self.assertEqual(_char(work, "张叁")["name_ja"], "サンサン")
        self.assertNotIn("variant_candidates", _lib.load_characters(work))

    def test_speaker_candidate_expired_on_confirmed(self):
        """④ 声が確定した人物の話者候補は役目を終える"""
        work = self._cand_work()
        self._commit(work, _plot_delta("ch001/b1"))
        self.assertEqual(_char(work, "张三")["speaker_candidate"], ["ch001/b1"])
        delta = ('block = "ch001/b4"\n\n[[characters_delta]]\nmatch_key = "name_src"\n'
                 'match_value = "张三"\nvoice_status = "confirmed"\n'
                 'speech_style = "冷静。語尾は「〜だ。」"\n')
        code, out = self._commit(work, delta)
        self.assertEqual(code, 0, out)
        self.assertNotIn("speaker_candidate", _char(work, "张三"))


# ---------------------------------------------------------------------------
# ⑥ status の確認待ち一覧
# ---------------------------------------------------------------------------

class TestStatusPending(_Base):
    def test_status_lists_voice_and_candidates(self):
        """⑥ 未確定の声と未解決の候補を列挙。確定済み・発話なしは出さない"""
        work = _make_work(self.tmp, chars={"characters": [
            {"name_src": "Alice", "name_ja": "アリス", "speaker_candidate": ["ch001/b1"]},
            {"name_src": "Bob", "name_ja": "ボブ", "voice_status": "insufficient_evidence",
             "variant_candidates": [
                 {"name_src": "ボブス", "similar_to_src": "ボブ", "block": "ch001/b2"}]},
            {"name_src": "Carol", "name_ja": "キャロル", "voice_status": "confirmed",
             "speech_style": "合成の口調。語尾は「〜です」"},
            {"name_src": "Dave", "name_ja": "デイブ", "voice_status": "no_speech_yet"},
        ], "variant_candidates": [
            {"name_src": "アリサ", "similar_to_src": "アリス", "block": "ch001/b3"},
        ]})
        code, out = _run_saga("status", str(work))
        self.assertEqual(code, 0, out)
        section = _pending_section(out)
        self.assertIn("分析の確認待ち:", section)
        self.assertIn("声の状態(未確認/根拠不足): アリス (未確認), ボブ (根拠不足)", section)
        self.assertIn("話者の確認候補: アリス (ch001/b1)", section)
        self.assertIn("ボブ⇔ボブス (ch001/b2)", section)
        self.assertIn("アリサ⇔アリス (ch001/b3)（該当キャラなし）", section)
        self.assertIn("検収済みの「未確定」は許容", section)
        self.assertNotIn("キャロル", section)
        self.assertNotIn("デイブ", section)

    def test_status_no_pending(self):
        """⑥ 未確定も候補も無ければ「なし」"""
        work = _make_work(self.tmp, chars={"characters": [
            {"name_src": "Carol", "name_ja": "キャロル", "voice_status": "confirmed",
             "speech_style": "合成の口調。語尾は「〜です」"},
        ]})
        code, out = _run_saga("status", str(work))
        self.assertEqual(code, 0, out)
        self.assertIn("分析の確認待ち: なし", out)


# ---------------------------------------------------------------------------
# ⑦ 旧データ（arcs 無し・候補無し）が壊れない
# ---------------------------------------------------------------------------

class TestLegacyData(_Base):
    def test_no_arcs_context_shows_none(self):
        """⑦ arcs が無い人物表でもコンテキスト生成は壊れない"""
        work = _make_work(self.tmp, chars={"characters": [
            {"name_src": "Alice", "name_ja": "アリス", "role": "主人公"},
        ]}, texts=["Alice walked.", "夜。"] * 3, n_blocks=3)
        section = _arcs_section(self._context(work, "ch001", "b3"))
        self.assertIn("(なし)", section)

    def test_no_candidates_status_ok(self):
        """⑦ 候補欄が無い旧データでも status が動き、既定は未確認として出る"""
        work = _make_work(self.tmp, chars={"characters": [
            {"name_src": "Alice", "name_ja": "アリス", "role": "主人公"},
        ]})
        code, out = _run_saga("status", str(work))
        self.assertEqual(code, 0, out)
        self.assertIn("分析の確認待ち:", out)
        self.assertIn("アリス (未確認)", out)

    def test_commit_adds_no_candidate_fields(self):
        """⑦ 候補が無い原文の commit は候補欄を生やさない"""
        work = _make_work(self.tmp, chars={"characters": [
            {"name_src": "Alice", "name_ja": "アリス", "role": "主人公"},
        ]}, texts=["Alice walked. The room was quiet.", "夜。"] * 3, n_blocks=3)
        code, out = self._commit(work, _plot_delta("ch001/b2"))
        self.assertEqual(code, 0, out)
        c = _char(work, "Alice")
        self.assertNotIn("speaker_candidate", c)
        self.assertNotIn("variant_candidates", c)
        self.assertNotIn("variant_candidates", _lib.load_characters(work))
        self.assertNotIn("話者の確認候補", out)
        self.assertNotIn("異表記候補", out)

    def test_candidate_helpers_tolerate_garbage(self):
        """⑦ 候補欄の壊れた値は読み取り・追記で落ちない"""
        from _lib import (merge_speaker_candidate, merge_variant_candidates,
                          speaker_candidate_blocks, variant_candidates_of)
        self.assertEqual(speaker_candidate_blocks({"speaker_candidate": "ch001/b1"}), [])
        self.assertEqual(speaker_candidate_blocks({"speaker_candidate": ["ch001/b1", 3, ""]}),
                         ["ch001/b1"])
        self.assertEqual(variant_candidates_of({"variant_candidates": ["壊れ", 1]}), [])
        self.assertEqual(merge_speaker_candidate("壊れ", ["ch001/b1"]), ["ch001/b1"])
        merged = merge_variant_candidates(None, [
            {"name_src": "X", "similar_to_src": "Y", "block": "ch001/b1"},
            {"name_src": "X", "similar_to_src": "Y", "block": "ch001/b2"},  # 重複は足さない
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["block"], "ch001/b1")


if __name__ == "__main__":
    unittest.main()
