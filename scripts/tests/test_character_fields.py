"""人物表の追加欄（aliases / voice_status / voice_note）の横断テスト。

spec: docs/phase8-taskQ1-charfields.md（P0-4・#7）。

①aliases の delta 追記マージ（重複排除）
②compact 表示（aliases と声の状態。空なら省略）
③QA 原語残照合が aliases を拾う
④carry の引き継ぎ
⑤rename が aliases を対象にし、画像パス/ルビ読みを壊さない
⑥voice_status の既定 unconfirmed
⑦confirmed に voice 無しで遷移しようとすると拒否
⑧旧データ（両欄なし）が壊れない

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
CARRY = SCRIPTS / "carry_analysis.py"
RENAME = SCRIPTS / "rename_terms.py"

CHARACTERS_REL = "analysis/characters-[ネタバレあり].toml"


def _run(script: Path, args: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, cwd=str(SCRIPTS))
    return r.returncode, r.stdout + r.stderr


def _make_work(base: Path, characters: dict | None = None) -> Path:
    """chapters_meta / paras / analysis 3ファイルを持つ最小ワーク（合成データ）。"""
    work = base / "work"
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "_work").mkdir(parents=True)
    (work / "manifest.toml").write_text('title = "T"\nlang = "zh"\ndst = "ja"\n', encoding="utf-8")
    meta = ('[work]\ntitle = "T"\nlang = "zh"\n\n'
            '[[chapters]]\nid = "ch001"\ntitle = ""\nstatus = "pending"\n'
            '[[chapters.blocks]]\nid = "b1"\nparas = [1, 4]\nn_chars = 40\n'
            '[[chapters.blocks]]\nid = "b2"\nparas = [5, 8]\nn_chars = 40\n')
    (work / "chapters_meta.toml").write_text(meta, encoding="utf-8")
    plines = []
    for i in range(1, 9):
        text = "张三は笑った。" if i == 1 else f"原文{i}。"
        plines += ["[[paras]]", f"n = {i}", f'text = "{text}"', ""]
    (work / "chapters" / "ch001.paras.toml").write_text("\n".join(plines), encoding="utf-8")
    _lib.save_toml(work / CHARACTERS_REL,
                   characters if characters is not None else {"characters": []})
    _lib.save_toml(work / "analysis" / "terms.toml", {"terms": []})
    _lib.save_toml(work / "analysis" / "plot-[ネタバレあり].toml", {"entries": []})
    return work


def _load_chars(work: Path) -> list[dict]:
    return _lib.load_characters(work).get("characters") or []


def _char(work: Path, name_src: str) -> dict:
    for c in _load_chars(work):
        if c.get("name_src") == name_src:
            return c
    raise AssertionError(f"character {name_src} not found")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="q1_charfields_"))

    def tearDown(self):
        import shutil
        lock = self.tmp / "work" / "_work" / ".lock"
        if lock.exists():
            lock.unlink()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _delta_file(self, text: str) -> Path:
        d = self.tmp / "delta.toml"
        d.write_text(text, encoding="utf-8")
        return d


# ---------------------------------------------------------------------------
# ① aliases の delta 追記マージ（重複排除）
# ---------------------------------------------------------------------------

class TestAliasesDeltaMerge(_Base):
    def test_apply_appends_and_dedupes(self):
        """既存 aliases へ追記し、既存・差分内の重複は足さない。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン", "aliases": ["三儿", "アーサン"]}]
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "aliases": ["アーサン", "チョウちゃん", "三儿"]}])
        self.assertEqual(chars[0]["aliases"], ["三儿", "アーサン", "チョウちゃん"])

    def test_apply_new_entry_dedupes(self):
        """新規エントリでも差分内の重複を足さない。"""
        from saga import _apply_chars_delta
        chars: list[dict] = []
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "李四",
                                    "aliases": ["四儿", "四儿", "リー"]}])
        self.assertEqual(chars[0]["aliases"], ["四儿", "リー"])

    def test_apply_via_updates_table(self):
        """updates / new_fields 経由でも追記マージになる。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン", "aliases": ["三儿"]}]
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "updates": {"aliases": ["三儿", "サン"]}}])
        self.assertEqual(chars[0]["aliases"], ["三儿", "サン"])
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "new_fields": {"aliases": ["サン", "さん"]}}])
        self.assertEqual(chars[0]["aliases"], ["三儿", "サン", "さん"])

    def test_apply_legacy_aliases_not_a_list(self):
        """壊れた既存 aliases（文字列）でも追記マージが通る。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン", "aliases": "壊れた値"}]
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "aliases": ["三儿"]}])
        self.assertEqual(chars[0]["aliases"], ["三儿"])

    def test_cli_append_merge_persists_and_idempotent(self):
        """CLI 経由で保存され、同一 delta の再実行で重複しない。"""
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "aliases": ["三儿"]}]})
        d = self._delta_file('block = "ch001/b1"\n'
                             '[[characters_delta]]\nmatch_key = "name_src"\n'
                             'match_value = "张三"\naliases = ["三儿", "アーサン"]\n')
        code, out = _run(SAGA, ["analyze-commit", str(work), "--delta", str(d)])
        self.assertEqual(code, 0, out)
        self.assertEqual(_char(work, "张三")["aliases"], ["三儿", "アーサン"])
        code, out = _run(SAGA, ["analyze-commit", str(work), "--delta", str(d)])
        self.assertEqual(code, 0, out)
        self.assertEqual(_char(work, "张三")["aliases"], ["三儿", "アーサン"])

    def test_cli_rejects_aliases_not_a_list(self):
        """aliases が文字列の配列でない delta は拒否し、何も書かない。"""
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "aliases": ["三儿"]}]})
        before = (work / CHARACTERS_REL).read_bytes()
        d = self._delta_file('block = "ch001/b1"\n'
                             '[[characters_delta]]\nmatch_key = "name_src"\n'
                             'match_value = "张三"\naliases = "三儿"\n')
        code, out = _run(SAGA, ["analyze-commit", str(work), "--delta", str(d)])
        self.assertEqual(code, 1, out)
        self.assertIn("配列", out)
        self.assertEqual((work / CHARACTERS_REL).read_bytes(), before)


# ---------------------------------------------------------------------------
# ⑥⑦ voice_status / voice_note（既定と遷移）
# ---------------------------------------------------------------------------

class TestVoiceStatus(_Base):
    def test_default_is_unconfirmed(self):
        """voice_status 欠落・不正値は unconfirmed 扱い。"""
        from _lib import voice_status_of
        self.assertEqual(voice_status_of({}), "unconfirmed")
        self.assertEqual(voice_status_of({"voice_status": "bogus"}), "unconfirmed")
        self.assertEqual(voice_status_of({"voice_status": "confirmed"}), "confirmed")

    def test_confirmed_without_voice_rejected(self):
        """声の記述が無いまま confirmed へ遷移 → 拒否。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン"}]
        with self.assertRaises(ValueError):
            _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                        "voice_status": "confirmed"}])
        # 拒否後もエントリは confirmed にならない
        self.assertNotEqual(chars[0].get("voice_status"), "confirmed")

    def test_confirmed_with_delta_description_ok(self):
        """同じ delta が声の記述を伴えば confirmed へ遷移できる。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン"}]
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "voice_status": "confirmed",
                                    "speech_style": "タメ口。語尾は「〜だよ」「〜じゃん」"}])
        self.assertEqual(chars[0]["voice_status"], "confirmed")

    def test_confirmed_with_existing_description_ok(self):
        """既存の声の記述があれば confirmed へ遷移できる。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン",
                  "first_person": "私(わたくし)"}]
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "voice_status": "confirmed"}])
        self.assertEqual(chars[0]["voice_status"], "confirmed")

    def test_voice_note_update(self):
        """voice_status / voice_note を delta で更新できる（confirmed 以外は記述不要）。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン"}]
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "voice_status": "insufficient_evidence",
                                    "voice_note": "ch001/b1 で発話候補のみ"}])
        self.assertEqual(chars[0]["voice_status"], "insufficient_evidence")
        self.assertEqual(chars[0]["voice_note"], "ch001/b1 で発話候補のみ")
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "voice_status": "no_speech_yet"}])
        self.assertEqual(chars[0]["voice_status"], "no_speech_yet")

    def test_invalid_status_value_rejected(self):
        """未知の voice_status 値は拒否。"""
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン"}]
        with self.assertRaises(ValueError):
            _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                        "voice_status": "maybe"}])

    def test_validate_rejects_bad_types(self):
        """受入検証: 型・値の不正は拒否理由を返す。"""
        from saga import validate_analysis_delta
        meta = {"chapters": [{"id": "ch001", "blocks": [{"id": "b1"}]}]}
        ok, reason, _ = validate_analysis_delta(meta, {"block": "ch001/b1", "characters_delta": [
            {"match_key": "name_src", "match_value": "张三", "voice_status": "maybe"}]})
        self.assertFalse(ok)
        self.assertIn("いずれか", reason or "")
        ok, reason, _ = validate_analysis_delta(meta, {"block": "ch001/b1", "characters_delta": [
            {"match_key": "name_src", "match_value": "张三", "voice_note": 3}]})
        self.assertFalse(ok)
        self.assertIn("文字列", reason or "")
        ok, reason, _ = validate_analysis_delta(meta, {"block": "ch001/b1", "characters_delta": [
            {"match_key": "name_src", "match_value": "张三", "aliases": "三儿"}]})
        self.assertFalse(ok)
        self.assertIn("配列", reason or "")

    def test_cli_confirmed_without_voice_rejected_writes_nothing(self):
        """CLI: 声無し confirmed は exit 1、分析ファイルと chapters_meta は無変更。"""
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン"}]})
        before = (work / CHARACTERS_REL).read_bytes()
        d = self._delta_file('block = "ch001/b1"\n'
                             '[[characters_delta]]\nmatch_key = "name_src"\n'
                             'match_value = "张三"\nvoice_status = "confirmed"\n')
        code, out = _run(SAGA, ["analyze-commit", str(work), "--delta", str(d)])
        self.assertEqual(code, 1, out)
        self.assertIn("confirmed", out)
        self.assertEqual((work / CHARACTERS_REL).read_bytes(), before)
        meta = _lib.load_chapters_meta(work)
        self.assertNotIn("analysis_committed_at", meta["chapters"][0]["blocks"][0])

    def test_cli_confirmed_with_voice_accepted(self):
        """CLI: 声の記述を伴う confirmed は保存される。"""
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン"}]})
        d = self._delta_file('block = "ch001/b1"\n'
                             '[[characters_delta]]\nmatch_key = "name_src"\n'
                             'match_value = "张三"\nspeech_style = "タメ口。語尾は「〜だよ」"\n'
                             'voice_status = "confirmed"\nvoice_note = "ch001/b1 で初発話"\n')
        code, out = _run(SAGA, ["analyze-commit", str(work), "--delta", str(d)])
        self.assertEqual(code, 0, out)
        c = _char(work, "张三")
        self.assertEqual(c["voice_status"], "confirmed")
        self.assertEqual(c["voice_note"], "ch001/b1 で初発話")


# ---------------------------------------------------------------------------
# ② compact 表示
# ---------------------------------------------------------------------------

class TestCompactDisplay(_Base):
    def test_fmt_characters_shows_aliases_and_voice(self):
        from build_context import fmt_characters
        text = fmt_characters({"characters": [{
            "name_src": "张三", "name_ja": "チョウサン",
            "aliases": ["三儿", "チョウちゃん"],
            "voice_status": "insufficient_evidence", "voice_note": "ch001/b1 で発話候補のみ",
        }]})
        self.assertIn("- 別名: 三儿 / チョウちゃん", text)
        self.assertIn("- 声: 根拠不足（根拠: ch001/b1 で発話候補のみ）", text)

    def test_fmt_characters_omits_empty_aliases_and_defaults_voice(self):
        from build_context import fmt_characters
        text = fmt_characters({"characters": [{"name_src": "张三", "name_ja": "チョウサン"}]})
        self.assertNotIn("別名", text)
        self.assertIn("- 声: 未確認", text)

    def test_compact_context_includes_fields(self):
        """compact のコンテキスト出力に aliases と声の状態が入る。"""
        from build_context import _stage_impl
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン",
             "aliases": ["三儿"], "voice_status": "no_speech_yet",
             "voice_note": "原文に発話なし"}]})
        _stage_impl(work, "ch001", "b1", "translation")
        text = (work / "translation_input" / "ch001_b1_translation.md").read_text(encoding="utf-8")
        self.assertIn("別名: 三儿", text)
        self.assertIn("声: 現時点で発話なし（根拠: 原文に発話なし）", text)


# ---------------------------------------------------------------------------
# ③ QA 原語残照合が aliases を拾う
# ---------------------------------------------------------------------------

class TestQaAliases(_Base):
    def _draft(self, body: str) -> Path:
        d = self.tmp / "draft.md"
        d.write_text(body, encoding="utf-8")
        return d

    def test_alias_leftover_detected(self):
        from qa_translation import run_qa
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "aliases": ["三儿"]}]})
        draft = self._draft("[P1] 三儿が来た。\n\n[P2] 訳二。\n\n[P3] 訳三。\n\n[P4] 訳四。")
        report, _ = run_qa(work, "ch001", "b1", draft)
        check = report["checks"]["leftover_source_names"]
        self.assertFalse(check["pass"])
        self.assertIn("三儿", [f["name_src"] for f in check["found"]])

    def test_alias_kept_in_ja_not_flagged(self):
        """訳語にそのまま含まれる別名（意図的な保持）は残留扱いしない。"""
        from qa_translation import run_qa
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン（三儿）", "aliases": ["三儿"]}]})
        draft = self._draft("[P1] チョウサン（三儿）が来た。\n\n[P2] 訳二。\n\n[P3] 訳三。\n\n[P4] 訳四。")
        report, _ = run_qa(work, "ch001", "b1", draft)
        self.assertTrue(report["checks"]["leftover_source_names"]["pass"])

    def test_no_alias_no_change(self):
        """aliases が無い旧データの照合は従来どおり。"""
        from qa_translation import run_qa
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン"}]})
        draft = self._draft("[P1] 訳一。\n\n[P2] 訳二。\n\n[P3] 訳三。\n\n[P4] 訳四。")
        report, _ = run_qa(work, "ch001", "b1", draft)
        self.assertTrue(report["checks"]["leftover_source_names"]["pass"])


# ---------------------------------------------------------------------------
# ④ carry の引き継ぎ
# ---------------------------------------------------------------------------

class TestCarry(_Base):
    def _prev(self, characters: dict) -> Path:
        prev = self.tmp / "prev"
        (prev / "analysis").mkdir(parents=True)
        _lib.save_toml(prev / CHARACTERS_REL, characters)
        return prev

    def test_carry_copies_aliases_and_voice(self):
        prev = self._prev({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "aliases": ["三儿", "アーサン"],
             "speech_style": "タメ口。語尾は「〜だよ」",
             "voice_status": "confirmed", "voice_note": "前作 ch001/b1 で初発話"}]})
        work = _make_work(self.tmp)
        code, out = _run(CARRY, [str(work), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        c = _char(work, "张三")
        self.assertEqual(c["aliases"], ["三儿", "アーサン"])
        self.assertEqual(c["voice_status"], "confirmed")
        self.assertEqual(c["voice_note"], "前作 ch001/b1 で初発話")
        self.assertEqual(c["arcs"][0]["at_block"], "carry")

    def test_carry_fills_empty_fields_of_existing(self):
        """現作の空欄（aliases=[] / 未確定 status / 空 note）だけ前作で埋める。"""
        prev = self._prev({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "aliases": ["三儿"],
             "voice_status": "confirmed", "voice_note": "前作で初発話"}]})
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "aliases": [],
             "voice_status": "unconfirmed", "voice_note": ""}]})
        code, out = _run(CARRY, [str(work), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        c = _char(work, "张三")
        self.assertEqual(c["aliases"], ["三儿"])
        self.assertEqual(c["voice_status"], "confirmed")
        self.assertEqual(c["voice_note"], "前作で初発話")

    def test_carry_does_not_override_determined_status(self):
        """現作で確定済み（confirmed 以外の決定値）の status は前作で上書きしない。"""
        prev = self._prev({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "voice_status": "confirmed"}]})
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン",
             "voice_status": "no_speech_yet", "voice_note": "現作で確認"}]})
        code, out = _run(CARRY, [str(work), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        c = _char(work, "张三")
        self.assertEqual(c["voice_status"], "no_speech_yet")
        self.assertEqual(c["voice_note"], "現作で確認")

    def test_carry_legacy_prev_gets_defaults(self):
        """⑧ 前作が旧データ（両欄なし）でも壊れず、既定の明示値が入る。"""
        prev = self._prev({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "role": "主人公"}]})
        work = _make_work(self.tmp)
        code, out = _run(CARRY, [str(work), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        c = _char(work, "张三")
        self.assertEqual(c["aliases"], [])
        self.assertEqual(c["voice_status"], "unconfirmed")


# ---------------------------------------------------------------------------
# ⑤ rename が aliases を対象にし、画像パス/ルビ読みを壊さない
# ---------------------------------------------------------------------------

RENAME_TEXT = (
    "旧訳が歩いた。\n\n"
    "![旧訳の挿絵](images/旧訳-001.png)\n\n"
    "{旧訳|きゅうやく}と書いてある。\n\n"
    "[[rb:旧訳 > きゅうやく]]の形。\n\n"
    "また旧訳だ。\n"
)


class TestRename(_Base):
    def _work_with_text(self, characters: dict | None = None) -> Path:
        work = _make_work(self.tmp, characters if characters is not None else {"characters": []})
        (work / "output" / "ch001").mkdir(parents=True)
        (work / "output" / "ch001" / "b1_ja.md").write_text(RENAME_TEXT, encoding="utf-8")
        return work

    def test_replace_protected(self):
        from rename_terms import replace_protected
        new, n, protected = replace_protected(RENAME_TEXT, "旧訳", "新訳")
        self.assertEqual(n, 3)          # 地の文2 + VFM 親文字1
        self.assertEqual(protected, 3)  # 画像リンク(alt+パス)2 + [[rb:]]1
        self.assertIn("![旧訳の挿絵](images/旧訳-001.png)", new)  # 画像パスは無傷
        self.assertIn("{新訳|きゅうやく}", new)                   # 親文字は改名、読みは無傷
        self.assertIn("[[rb:旧訳 > きゅうやく]]", new)            # [[rb:]] は全体保護
        self.assertNotIn("旧訳が歩いた", new)
        self.assertNotIn("また旧訳だ", new)

    def test_reading_is_protected(self):
        """ルビの読みを旧語に指定しても、読みの中は置換しない。"""
        from rename_terms import replace_protected
        new, n, _protected = replace_protected(RENAME_TEXT, "きゅうやく", "しんやく")
        self.assertEqual(n, 0)
        self.assertIn("{旧訳|きゅうやく}", new)
        self.assertIn("[[rb:旧訳 > きゅうやく]]", new)

    def test_scan_counts_only_replaceable(self):
        from rename_terms import scan
        work = self._work_with_text()
        hits = scan(work, "旧訳")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["total"], 3)
        self.assertEqual(hits[0]["protected"], 3)
        self.assertFalse(hits[0]["multi_in_para"])

    def test_scan_aliases(self):
        from rename_terms import scan_aliases
        work = self._work_with_text({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン",
             "aliases": ["旧訳", "旧訳（仮）", "無関係"]}]})
        hits = scan_aliases(work, "旧訳")
        self.assertEqual([h["alias"] for h in hits], ["旧訳", "旧訳（仮）"])
        self.assertEqual(sum(h["total"] for h in hits), 2)

    def test_cli_apply_updates_text_and_aliases(self):
        """--apply: 訳文と人物表 aliases の両方を更新し、保護は維持する。"""
        work = self._work_with_text({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン",
             "aliases": ["旧訳", "旧訳（仮）", "無関係"]}]})
        code, out = _run(RENAME, [str(work), "--term", "旧訳", "--to", "新訳", "--apply"])
        self.assertEqual(code, 0, out)
        text = (work / "output" / "ch001" / "b1_ja.md").read_text(encoding="utf-8")
        self.assertIn("![旧訳の挿絵](images/旧訳-001.png)", text)
        self.assertIn("{新訳|きゅうやく}", text)
        self.assertIn("[[rb:旧訳 > きゅうやく]]", text)
        self.assertIn("新訳が歩いた。", text)
        self.assertIn("また新訳だ。", text)
        self.assertEqual(_char(work, "张三")["aliases"], ["新訳", "新訳（仮）", "無関係"])
        report = _lib.load_json(work / "logs" / "rename_last.json")
        self.assertTrue(report["applied"])
        self.assertEqual(report["alias_applied"], 2)
        self.assertEqual(len(report["aliases"]), 2)

    def test_scan_aliases_missing_file(self):
        """⑧ 人物表が無い/aliases が無い旧データでも壊れない。"""
        from rename_terms import apply_alias_renames, scan_aliases
        work = self._work_with_text({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン"}]})
        self.assertEqual(scan_aliases(work, "旧訳"), [])
        self.assertEqual(apply_alias_renames(work, "旧訳", "新訳"), (0, []))


# ---------------------------------------------------------------------------
# ⑧ 旧データ（両欄なし）が壊れない
# ---------------------------------------------------------------------------

class TestLegacyData(_Base):
    def test_fmt_defaults_for_legacy_entry(self):
        from build_context import fmt_characters
        text = fmt_characters({"characters": [{"name_src": "张三", "name_ja": "チョウサン"}]})
        self.assertNotIn("別名", text)
        self.assertIn("- 声: 未確認", text)

    def test_helpers_tolerate_missing_fields(self):
        from _lib import character_aliases, voice_description_present, voice_status_of
        self.assertEqual(character_aliases({}), [])
        self.assertEqual(voice_status_of({}), "unconfirmed")
        self.assertFalse(voice_description_present({"name_src": "张三"}))
        self.assertTrue(voice_description_present({"catchphrase": "〜だよ"}))

    def test_delta_on_legacy_entry_updates_without_adding_fields(self):
        from saga import _apply_chars_delta
        chars = [{"name_src": "张三", "name_ja": "チョウサン", "appearance": "黒髪"}]
        _apply_chars_delta(chars, [{"match_key": "name_src", "match_value": "张三",
                                    "appearance": "銀髪"}])
        self.assertEqual(chars[0]["appearance"], "銀髪")
        self.assertNotIn("aliases", chars[0])
        self.assertNotIn("voice_status", chars[0])

    def test_cli_plot_delta_on_legacy_data(self):
        """旧データの人物表は、無関係の delta で新欄が生えないまま保存される。"""
        work = _make_work(self.tmp, {"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "role": "主人公"}]})
        d = self._delta_file('block = "ch001/b1"\n'
                             '[[plot_delta]]\nblock = "ch001/b1"\ntext = "要点。"\n')
        code, out = _run(SAGA, ["analyze-commit", str(work), "--delta", str(d)])
        self.assertEqual(code, 0, out)
        c = _char(work, "张三")
        self.assertEqual(c["role"], "主人公")
        self.assertNotIn("aliases", c)
        self.assertNotIn("voice_status", c)


if __name__ == "__main__":
    unittest.main()
