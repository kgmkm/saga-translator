"""ルビ検査（G8: ruby_leftover 系検査の両方向化 / G11: 旧形式ルビの警告 / G9: config ruby の注入）のテスト。

検証対象:
  - zh→ja の QA がルビ未適用（terms / characters の ruby 宣言が原文初出ブロックで
    未付与）を検出し、commit が止まること（従来は if dst=="zh" 内にあり素通り）
  - 旧形式 親{読み} の検出（QA レポートは警告のみ・commit 時は警告を出して確定は続行）
  - VFM 記法異常（{親|} / {親|読み 等）の検出で commit が止まること
  - 原文初出ブロック以外・台帳で消費済みの語は未適用としないこと（誤爆防止）
  - 設定 [translation] ruby の「ルビ方針」としてのコンテキスト注入
    （none のときは未適用検査を省略）
  - ja→zh にはルビ検査が付かないこと（従来分の非破壊）

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


_TERMS = """\
[[terms]]
term_src = "剑"
term_ja = "剣"
ruby = "つるぎ"

[[terms]]
term_src = "雪"
term_ja = "雪"
"""

_CHARS = """\
[[characters]]
name_src = "阿雪"
name_ja = "阿雪"
ruby = "あゆき"
"""

_CHARS_KANA = """\
[[characters]]
name_src = "Alice"
name_ja = "アリス"
ruby = "ありす"
"""

_PARAS = """\
[[paras]]
n = 1
text = "他拔出剑，剑光很冷。"

[[paras]]
n = 2
text = "雪落下了。"

[[paras]]
n = 3
text = "剑还在手中。"

[[paras]]
n = 4
text = "风吹过。"
"""

_PARAS_CHAR = """\
[[paras]]
n = 1
text = "阿雪拔出了剑。"

[[paras]]
n = 2
text = "雪落下了。"

[[paras]]
n = 3
text = "剑还在手中。"

[[paras]]
n = 4
text = "风吹过。"
"""


def _make_work(base: Path, *, chars: str = "", paras: str = _PARAS,
               lang: str = "zh", dst: str = "ja") -> Path:
    """最小合成ワーク（1章2ブロック。剑にルビ宣言・雪は宣言なし）。"""
    work = base / "work"
    _write(work / "manifest.toml", f'lang = "{lang}"\ndst = "{dst}"\n')
    _write(work / "chapters_meta.toml",
           '[[chapters]]\nid = "ch001"\n'
           'blocks = [{id="b1", paras=[1,2]}, {id="b2", paras=[3,4]}]\n')
    _write(work / "chapters" / "ch001.paras.toml", paras)
    _write(work / "analysis" / "terms.toml", _TERMS)
    if chars:
        _write(work / "analysis" / "characters-[ネタバレあり].toml", chars)
    _write(work / "analysis" / "plot-[ネタバレあり].toml", 'entries = []\n')
    return work


def _place_draft(work: Path, cid: str, bid: str, items: list[tuple[int, str]],
                 suf: str = "_ja") -> Path:
    draft = work / "output" / "_work" / f"{cid}_{bid}{suf}_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("\n\n".join(f"[P{n:03d}] {t}" for n, t in items) + "\n",
                     encoding="utf-8", newline="\n")
    return draft


def _run_commit(work: Path, cid: str = "ch001", bid: str = "b1") -> tuple[int, str]:
    """cmd_commit を in-process で実行し (終了コード, 出力) を返す。"""
    from saga import cmd_commit
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = buf
        code = cmd_commit(work, cid, bid, "test-model", "v1")
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, buf.getvalue()


class _ConfigIsolated(unittest.TestCase):
    """~/.config/saga-translator/config.toml を一時ファイルへ隔離する基底。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self._orig_config_path = _lib.CONFIG_PATH
        _lib.CONFIG_PATH = self.td / "config.toml"
        self.addCleanup(setattr, _lib, "CONFIG_PATH", self._orig_config_path)

    def _set_config(self, body: str) -> None:
        _write(_lib.CONFIG_PATH, body)


# ── 検出器（ledger.VFM_RE 近傍） ─────────────────────────────────────────


class TestOldStyleRubyDetector(unittest.TestCase):
    """ledger.find_old_style_ruby の単体（G11/T20 の検出器）。"""

    def test_detects_old_style(self):
        from ledger import find_old_style_ruby
        hits = find_old_style_ruby("彼は戦鎧{せんがい}を纏い、触手{しょくしゅ}を伸ばす。")
        self.assertEqual([h["form"] for h in hits],
                         ["戦鎧{せんがい}", "触手{しょくしゅ}"])
        self.assertEqual(hits[0]["base"], "戦鎧")
        self.assertEqual(hits[0]["reading"], "せんがい")

    def test_vfm_is_not_old_style(self):
        from ledger import find_old_style_ruby
        self.assertEqual(find_old_style_ruby("彼の{女神|めがみ}は笑う"), [])
        self.assertEqual(find_old_style_ruby("{戦鎧|せんがい}"), [])
        self.assertEqual(find_old_style_ruby("の女神{女神|めがみ}"), [])

    def test_non_kana_reading_not_matched(self):
        from ledger import find_old_style_ruby
        self.assertEqual(find_old_style_ruby("波括弧{注記}と第{1}章"), [])
        self.assertEqual(find_old_style_ruby("なお{あとがき}は無し"), [])  # 親が漢字でない


# ── QA（zh→ja のルビ検査） ───────────────────────────────────────────────


class TestQaRubyZhToJa(_ConfigIsolated):
    """run_qa が zh→ja でルビ未適用・旧形式・記法異常を検出する（G8）。"""

    def _qa(self, work: Path, bid: str, items: list[tuple[int, str]]):
        from qa_translation import run_qa
        d = _place_draft(work, "ch001", bid, items)
        return run_qa(work, "ch001", bid, d)[0]

    def test_not_applied_detected_on_first_block(self):
        """ruby_leftover 系の検査が zh→ja でも走る（初出ブロックで VFM 欠落）。"""
        work = _make_work(self.td)
        rep = self._qa(work, "b1", [(1, "彼は剣を抜いた。"), (2, "雪が降り始めた。")])
        self.assertFalse(rep["pass"])
        na = rep["checks"]["ruby_not_applied"]
        self.assertFalse(na["pass"])
        self.assertEqual(na["found"][0]["term"], "剣")
        self.assertEqual(na["found"][0]["kind"], "term")
        self.assertEqual(na["found"][0]["first_block"], "ch001/b1")
        self.assertTrue(rep["checks"]["ruby_notation"]["pass"])

    def test_vfm_applied_passes(self):
        work = _make_work(self.td)
        rep = self._qa(work, "b1", [(1, "彼は{剣|つるぎ}を抜いた。"), (2, "雪が降り始めた。")])
        self.assertTrue(rep["pass"], rep["checks"])
        self.assertTrue(rep["checks"]["ruby_not_applied"]["pass"])

    def test_rb_kana_applied_passes(self):
        """[[rb:親>読み]] もルビ付与として扱う。"""
        work = _make_work(self.td)
        rep = self._qa(work, "b1", [(1, "彼は[[rb:剣>つるぎ]]を抜いた。"), (2, "雪が降り始めた。")])
        self.assertTrue(rep["pass"], rep["checks"])

    def test_reoccurrence_block_not_checked(self):
        """原文初出が b1 の語は b2 でルビ無しでも未適用としない（初出のみの方針）。"""
        work = _make_work(self.td)
        rep = self._qa(work, "b2", [(3, "剣は重い。"), (4, "風が吹いた。")])
        self.assertTrue(rep["pass"], rep["checks"])
        self.assertTrue(rep["checks"]["ruby_not_applied"]["pass"])

    def test_ledger_consumed_not_checked(self):
        """台帳で消費済み（前の確定で付与済み）の語は未適用としない。"""
        work = _make_work(self.td)
        _write(work / "_work" / "ledger.toml", """\
[ledger]
version = 1
updated_at = ""

[[ruby_applied]]
term = "剣"
reading = "つるぎ"
block = "ch001/b1"
paragraph = 1
""")
        rep = self._qa(work, "b1", [(1, "彼は剣を抜いた。"), (2, "雪が降り始めた。")])
        self.assertTrue(rep["pass"], rep["checks"])

    def test_config_none_skips_not_applied(self):
        """設定 ruby="none" のときは未適用検査を省略する（注記つき）。"""
        self._set_config('[translation]\nruby = "none"\n')
        work = _make_work(self.td)
        rep = self._qa(work, "b1", [(1, "彼は剣を抜いた。"), (2, "雪が降り始めた。")])
        self.assertTrue(rep["pass"], rep["checks"])
        na = rep["checks"]["ruby_not_applied"]
        self.assertTrue(na["pass"])
        self.assertIn("ruby=none", na.get("note", ""))

    def test_old_style_recorded_as_warning_only(self):
        """旧形式は警告として記録されるが、それだけでは不合格にしない。"""
        work = _make_work(self.td)
        rep = self._qa(work, "b1", [(1, "彼は剣{つるぎ}を抜いた。"), (2, "雪が降り始めた。")])
        self.assertTrue(rep["pass"], rep["checks"])
        n = rep["checks"]["ruby_notation"]
        self.assertTrue(n["pass"])
        self.assertEqual([e["form"] for e in n["old_style"]], ["剣{つるぎ}"])

    def test_malformed_detected(self):
        """{親|} 等の壊れた記法は不合格（宣言語と無関係でも検出）。"""
        work = _make_work(self.td)
        rep = self._qa(work, "b1", [(1, "空は{女神|}の下。"), (2, "雪が降り始めた。")])
        self.assertFalse(rep["pass"])
        n = rep["checks"]["ruby_notation"]
        self.assertFalse(n["pass"])
        self.assertIn("{女神|}", n["malformed"])
        self.assertTrue(rep["checks"]["ruby_not_applied"]["pass"])  # 二重報告しない

    def test_unclosed_detected(self):
        work = _make_work(self.td)
        rep = self._qa(work, "b1", [(1, "空は{女神|めがみの下。"), (2, "雪が降り始めた。")])
        self.assertFalse(rep["pass"])
        self.assertTrue(rep["checks"]["ruby_notation"]["malformed"])

    def test_character_ruby_checked(self):
        """characters の ruby 宣言も未適用検査の対象。"""
        work = _make_work(self.td, chars=_CHARS, paras=_PARAS_CHAR)
        rep = self._qa(work, "b1", [(1, "阿雪は笑った。"), (2, "雪が降り始めた。")])
        self.assertFalse(rep["pass"])
        found = rep["checks"]["ruby_not_applied"]["found"]
        self.assertIn(("character", "阿雪"), [(f["kind"], f["term"]) for f in found])

    def test_katakana_name_not_required(self):
        """漢字を含まない表記（カタカナ名）の宣言は未適用にしない。"""
        work = _make_work(self.td, chars=_CHARS_KANA, paras=_PARAS_CHAR)
        rep = self._qa(work, "b1", [(1, "アリスは笑った。"), (2, "雪が降り始めた。")])
        self.assertTrue(rep["pass"], rep["checks"])

    def test_ja2zh_has_no_ruby_checks(self):
        """ja→zh には zh→ja 用のルビ検査を付けない（従来分の非破壊）。"""
        work = _make_work(self.td, lang="ja", dst="zh")
        from qa_translation import run_qa
        d = _place_draft(work, "ch001", "b1", [(1, "这是译文。"), (2, "第二段。")], suf="_zh")
        rep, _ = run_qa(work, "ch001", "b1", d)
        self.assertNotIn("ruby_not_applied", rep["checks"])
        self.assertNotIn("ruby_notation", rep["checks"])


# ── commit（確定路） ─────────────────────────────────────────────────────


class TestCommitRubyGate(_ConfigIsolated):
    """commit のルビ検査と旧形式警告（G8/G11）。"""

    def test_commit_blocks_missing_ruby(self):
        work = _make_work(self.td)
        _place_draft(work, "ch001", "b1", [(1, "彼は剣を抜いた。"), (2, "雪が降り始めた。")])
        code, out = _run_commit(work)
        self.assertEqual(code, 1, out)
        self.assertIn("ruby_not_applied", out)
        self.assertFalse((work / "output" / "ch001" / "b1_ja.md").exists())

    def test_commit_warns_old_style_and_confirms(self):
        """旧形式は警告のみで確定は続行し、台帳には載らない（既知の制限の固定）。"""
        work = _make_work(self.td)
        _place_draft(work, "ch001", "b1", [(1, "彼は剣{つるぎ}を抜いた。"), (2, "雪が降り始めた。")])
        code, out = _run_commit(work)
        self.assertEqual(code, 0, out)
        self.assertIn("旧形式のルビ", out)
        self.assertIn("剣{つるぎ}", out)
        self.assertTrue((work / "output" / "ch001" / "b1_ja.md").exists())
        from ledger import load_ledger
        d = load_ledger(work)
        self.assertEqual([r for r in d["ruby_applied"] if r["term"] == "剣"], [])

    def test_commit_confirms_with_vfm_and_records_ledger(self):
        work = _make_work(self.td)
        _place_draft(work, "ch001", "b1", [(1, "彼は{剣|つるぎ}を抜いた。"), (2, "雪が降り始めた。")])
        code, out = _run_commit(work)
        self.assertEqual(code, 0, out)
        self.assertNotIn("旧形式のルビ", out)
        from ledger import load_ledger
        d = load_ledger(work)
        self.assertEqual([r["term"] for r in d["ruby_applied"]], ["剣"])


# ── config [translation] ruby のコンテキスト注入（G9） ────────────────────


class TestConfigRubyInjection(_ConfigIsolated):
    """build_context が ruby 設定を「ルビ方針」として注入する。"""

    def _context(self, work: Path) -> str:
        from build_context import _stage_impl
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            _stage_impl(work, "ch001", "b1", "translation")
        return (work / "translation_input" / "ch001_b1_translation.md"
                ).read_text(encoding="utf-8")

    def test_default_first_injected(self):
        work = _make_work(self.td)
        text = self._context(work)
        self.assertIn("ルビ方針: first（人名・固有名詞の初出のみ）", text)

    def test_none_injected(self):
        self._set_config('[translation]\nruby = "none"\n')
        work = _make_work(self.td)
        text = self._context(work)
        self.assertIn("ルビ方針: none（ルビを振らない）", text)

    def test_invalid_value_falls_back_to_first(self):
        self._set_config('[translation]\nruby = "sometimes"\n')
        work = _make_work(self.td)
        text = self._context(work)
        self.assertIn("ルビ方針: first", text)

    def test_ja2zh_no_injection(self):
        work = _make_work(self.td, lang="ja", dst="zh")
        text = self._context(work)
        self.assertNotIn("ルビ方針", text)


if __name__ == "__main__":
    unittest.main()
