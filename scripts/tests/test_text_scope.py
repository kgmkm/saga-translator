# -*- coding: utf-8 -*-
"""訳文の正本と「訳文範囲」API（text_scope.py）の回帰テスト（P0-3・#23）。

テスト・コメントには作品固有の情報（作品名・実ファイル名・キャラ名）を
含めない。例は合成データのみ。

#23: 「訳文だけ」を数える共通の手段が無く、自作スキャナが原文を混ぜて
     集計をやり直した。対訳の原文は先頭行にしか '>' が付かず、折り返しの
     継続行は行単位の除外では残る。TOML は行の正規表現では *_src の全形を
     除外できない。
"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import text_scope as ts  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


class _TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="text_scope_"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── 合成の対訳（原文の折り返し・辞書語・画像タグ入り） ────────────────

PARALLEL = (
    "> [P001] 原文一の冒頭。\n"
    "> 折り返しの継続行一。\n"
    "\n"
    "訳文一。\n"
    "\n"
    "> [P002] 原文二の冒頭。合成辞書語を含む。\n"
    "折り返しの継続行二（先頭に '>' が付かない）。\n"
    "\n"
    "訳文二。\n"
    "\n"
    "> [P003] 原文三。![](images/001.png) を含む。\n"
    "\n"
    "訳文三。![](images/002.png) を含む訳。\n"
)

PARALLEL_DST = "訳文一。\n\n訳文二。\n\n訳文三。![](images/002.png) を含む訳。\n"

# 訳文側に残ってはいけない原文の断片（折り返し・辞書語・画像タグ）
PARALLEL_SRC_FRAGMENTS = (
    "原文一の冒頭。",
    "折り返しの継続行一。",
    "原文二の冒頭。",
    "折り返しの継続行二（先頭に '>' が付かない）。",
    "合成辞書語",
    "images/001.png",
    "原文三。",
)


class TestParallelTranslationSides(unittest.TestCase):
    """①訳文だけが返る ②原文ブロックが完全に落ちる。"""

    def test_returns_only_translation_blocks(self):
        self.assertEqual(ts.parallel_translation_sides(PARALLEL), PARALLEL_DST)

    def test_source_blocks_fully_removed(self):
        got = ts.parallel_translation_sides(PARALLEL)
        for frag in PARALLEL_SRC_FRAGMENTS:
            self.assertNotIn(frag, got)
        self.assertNotIn(">", got)

    def test_wrapped_continuation_without_marker_removed(self):
        """折り返しの継続行（'>' が付かない）も原文ブロックごと落ちる。"""
        text = "> [P001] 原文。\n折り返し。\n\n訳文。\n"
        self.assertEqual(ts.parallel_translation_sides(text), "訳文。\n")

    def test_empty_and_source_only(self):
        self.assertEqual(ts.parallel_translation_sides(""), "")
        self.assertEqual(ts.parallel_translation_sides("> [P001] 原文。\n"), "")

    def test_blank_separators_and_crlf_normalized(self):
        self.assertEqual(ts.parallel_translation_sides("\n\n訳文。\n\n\n"), "訳文。\n")
        crlf = PARALLEL.replace("\n", "\r\n")
        self.assertEqual(ts.parallel_translation_sides(crlf), PARALLEL_DST)

    def test_translation_block_kept_verbatim(self):
        """訳文ブロックはそのまま返す（マーカー付きでも書き換えない）。"""
        text = "> [P001] 原文。\n\n[P001] 訳文。\n"
        self.assertEqual(ts.parallel_translation_sides(text), "[P001] 訳文。\n")


class TestParallelRangesAndHash(unittest.TestCase):
    """③修正の前後で原文側ハッシュが不変（範囲オブジェクトの src/dst 区別）。"""

    def test_ranges_have_side_and_para(self):
        ranges = ts.parallel_ranges(PARALLEL)
        self.assertEqual([r.side for r in ranges],
                         [ts.SIDE_SRC, ts.SIDE_DST] * 3)
        self.assertEqual([r.para for r in ranges], [1, None, 2, None, 3, None])
        self.assertEqual(ranges[0].slice(PARALLEL),
                         "> [P001] 原文一の冒頭。\n> 折り返しの継続行一。")
        self.assertEqual(ranges[1].slice(PARALLEL), "訳文一。")

    def test_source_hash_unchanged_by_translation_fix(self):
        fixed = PARALLEL.replace("訳文二。", "訳文二（修正後）。")
        r_before = ts.parallel_ranges(PARALLEL)
        r_after = ts.parallel_ranges(fixed)
        # 原文側の範囲を連結したテキストが一致する（原文側は無傷）
        src_before = "".join(PARALLEL[r.start:r.end]
                             for r in r_before if r.side == ts.SIDE_SRC)
        src_after = "".join(fixed[r.start:r.end]
                            for r in r_after if r.side == ts.SIDE_SRC)
        self.assertEqual(src_before, src_after)
        # 原文側のハッシュは不変、訳文側のハッシュは変わる
        self.assertEqual(ts.scope_hash(PARALLEL, r_before, side=ts.SIDE_SRC),
                         ts.scope_hash(fixed, r_after, side=ts.SIDE_SRC))
        self.assertNotEqual(ts.scope_hash(PARALLEL, r_before, side=ts.SIDE_DST),
                            ts.scope_hash(fixed, r_after, side=ts.SIDE_DST))

    def test_source_hash_changes_when_source_changes(self):
        """原文側を壊せばハッシュは変わる（不変テストが空振りでないこと）。"""
        broken = PARALLEL.replace("原文一の冒頭。", "原文一の改変。")
        self.assertNotEqual(
            ts.scope_hash(PARALLEL, ts.parallel_ranges(PARALLEL), side=ts.SIDE_SRC),
            ts.scope_hash(broken, ts.parallel_ranges(broken), side=ts.SIDE_SRC))

    def test_scope_hash_normalizes_crlf(self):
        crlf = PARALLEL.replace("\n", "\r\n")
        self.assertEqual(
            ts.scope_hash(PARALLEL, ts.parallel_ranges(PARALLEL), side=ts.SIDE_SRC),
            ts.scope_hash(crlf, ts.parallel_ranges(crlf), side=ts.SIDE_SRC))


# ── 訳文の正本（確定ブロック） ────────────────────────────────────────

def _make_canonical_work(base: Path) -> Path:
    """正本（done）・確定ブロック出力・派生物が同居する合成ワーク。"""
    work = base / "canonical"
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", '''\
[[chapters]]
id = "ch001"
status = "done"

[[chapters.blocks]]
id = "b1"
paras = [1, 2]
status = "emitted"

[[chapters.blocks]]
id = "b2"
paras = [3, 4]
status = "emitted"

[[chapters]]
id = "ch002"
status = "done"

[[chapters.blocks]]
id = "b1"
paras = [1, 2]
status = "emitted"
''')
    # 正本（段落番号付き訳文。done/ にある）
    _write(work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md",
           "[P001] 訳一の甲。\n\n[P002] 訳一の乙。\n")
    _write(work / "output" / "_work" / "done" / "ch001_b2_ja_numbered.md",
           "[P003] 訳二の甲。\n\n[P004] 訳二の乙。\n")
    _write(work / "output" / "_work" / "done" / "ch002_b1_ja_numbered.md",
           "[P001] 訳三の甲。\n\n[P002] 訳三の乙。\n")
    # 確定ブロック出力（done が優先されるので読まれないはず）
    _write(work / "output" / "ch001" / "b1_ja.md", "確定出力（読まれてはいけない）。\n")
    # 派生物（章訳文・全文訳文・ブロック対訳）
    _write(work / "output" / "ch001_ja.md", "章訳文（派生物）。\n")
    _write(work / "output" / "全文_訳文.md", "全文訳文（派生物）。\n")
    _write(work / "output" / "ch001" / "b1_parallel.md",
           "> [P001] 原文（派生物）。\n\n対訳の訳（派生物）。\n")
    return work


class TestCanonicalBlockTexts(_TmpBase):
    """⑤正本優先（派生物を読まない）。"""

    def setUp(self):
        super().setUp()
        self.work = _make_canonical_work(self.tmp)

    def test_block_texts_prefers_done(self):
        got = ts.block_texts(self.work)
        self.assertEqual(
            [(b.chapter, b.block, b.para, b.text, b.origin) for b in got],
            [
                ("ch001", "b1", 1, "訳一の甲。", "done"),
                ("ch001", "b1", 2, "訳一の乙。", "done"),
                ("ch001", "b2", 3, "訳二の甲。", "done"),
                ("ch001", "b2", 4, "訳二の乙。", "done"),
                ("ch002", "b1", 1, "訳三の甲。", "done"),
                ("ch002", "b1", 2, "訳三の乙。", "done"),
            ])

    def test_translation_only_uses_canonical_only(self):
        got = ts.translation_only(self.work)
        self.assertEqual(
            got,
            "訳一の甲。\n\n訳一の乙。\n\n訳二の甲。\n\n訳二の乙。"
            "\n\n訳三の甲。\n\n訳三の乙。")
        for frag in ("確定出力（読まれてはいけない）。", "章訳文（派生物）。",
                     "全文訳文（派生物）。", "対訳の訳（派生物）。", "原文（派生物）。"):
            self.assertNotIn(frag, got)

    def test_derivatives_never_read(self):
        """派生物しか無いブロックは返さない（正本が無ければ空）。"""
        work = self.tmp / "derivatives_only"
        _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
        _write(work / "chapters_meta.toml",
               '[[chapters]]\nid = "ch001"\nstatus = "done"\n\n'
               '[[chapters.blocks]]\nid = "b1"\nparas = [1, 2]\nstatus = "emitted"\n')
        _write(work / "output" / "ch001_ja.md", "章訳文（派生物）。\n")
        _write(work / "output" / "全文_訳文.md", "全文訳文（派生物）。\n")
        _write(work / "output" / "ch001" / "b1_parallel.md",
               "> [P001] 原文。\n\n訳（派生物）。\n")
        self.assertEqual(ts.block_texts(work), [])
        self.assertEqual(ts.translation_only(work), "")

    def test_order_is_natural(self):
        """b10 と b2 は宣言順でなく自然順（b2 → b10）で返る。"""
        work = self.tmp / "order"
        _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
        _write(work / "chapters_meta.toml",
               '[[chapters]]\nid = "ch001"\nstatus = "done"\n\n'
               '[[chapters.blocks]]\nid = "b10"\nparas = [3, 4]\nstatus = "emitted"\n\n'
               '[[chapters.blocks]]\nid = "b2"\nparas = [1, 2]\nstatus = "emitted"\n')
        _write(work / "output" / "_work" / "done" / "ch001_b10_ja_numbered.md",
               "[P003] 十の甲。\n\n[P004] 十の乙。\n")
        _write(work / "output" / "_work" / "done" / "ch001_b2_ja_numbered.md",
               "[P001] 二の甲。\n\n[P002] 二の乙。\n")
        self.assertEqual([(b.block, b.para) for b in ts.block_texts(work)],
                         [("b2", 1), ("b2", 2), ("b10", 3), ("b10", 4)])


class TestEmittedFallback(_TmpBase):
    """done が無ければ確定ブロック出力を使う（段落番号は宣言範囲との対応）。"""

    def _make_work(self, name: str, b1_text: str) -> Path:
        work = self.tmp / name
        _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
        _write(work / "chapters_meta.toml",
               '[[chapters]]\nid = "ch001"\nstatus = "done"\n\n'
               '[[chapters.blocks]]\nid = "b1"\nparas = [1, 2]\nstatus = "emitted"\n')
        _write(work / "output" / "ch001" / "b1_ja.md", b1_text)
        return work

    def test_fallback_numbers_from_declared_range(self):
        work = self._make_work("emitted_ok", "訳一の甲。\n\n訳一の乙。\n")
        got = ts.block_texts(work)
        self.assertEqual([(b.chapter, b.block, b.para, b.text, b.origin) for b in got],
                         [("ch001", "b1", 1, "訳一の甲。", "emitted"),
                          ("ch001", "b1", 2, "訳一の乙。", "emitted")])
        self.assertEqual(ts.translation_only(work), "訳一の甲。\n\n訳一の乙。")

    def test_fallback_count_mismatch_raises(self):
        """段落数が宣言と一致しなければ推測せず停止する。"""
        work = self._make_work("emitted_bad", "訳一の甲。\n\n訳一の乙。\n\n訳一の丙。\n")
        with self.assertRaises(ts.TextScopeError):
            ts.block_texts(work)


class TestNoChaptersMeta(_TmpBase):
    """chapters_meta が無い場合は done/ を直接走査する。"""

    def test_glob_done_when_meta_missing(self):
        work = self.tmp / "no_meta"
        _write(work / "output" / "_work" / "done" / "ch001_b1_ja_numbered.md",
               "[P001] 訳一の甲。\n\n[P002] 訳一の乙。\n")
        _write(work / "output" / "_work" / "done" / "memo_numbered.md", "対象外。\n")
        got = ts.block_texts(work)
        self.assertEqual([(b.chapter, b.block, b.para, b.text, b.origin) for b in got],
                         [("ch001", "b1", 1, "訳一の甲。", "done"),
                          ("ch001", "b1", 2, "訳一の乙。", "done")])

    def test_missing_everything_is_empty(self):
        self.assertEqual(ts.block_texts(self.tmp / "nope"), [])
        self.assertEqual(ts.translation_only(self.tmp / "nope"), "")


# ── TOML（対象言語の欄） ──────────────────────────────────────────────

TOML_SAMPLE = '''\
title_src = "合成原題"
title_ja = "合成訳題"
note = "訳文側メモ"
version = 3
flag = true
extra = { inner_src = "内部の原文", inner = "内部の訳" }

[[chapters]]
id = "ch001"
src = "原文章題"
ja = "訳文章題"

[[characters]]
name_src = "合成名の原文"
name_ja = "合成名の訳"
address = [
  { target_src = "相手の原語", target_ja = "相手の訳語", form = "〜さん" },
]
arcs = [
  { at_block = "ch001/b1", speech_style = "合成の口調", note = "変化点" },
]
'''


class TestTomlTargetFields(unittest.TestCase):
    """④TOML の *_src が構造で除外される（inline table 含む）。"""

    def test_src_keys_excluded_structurally(self):
        data = tomllib.loads(TOML_SAMPLE)
        got = ts.toml_target_fields(data)
        self.assertEqual(got, [
            "合成訳題", "訳文側メモ", "内部の訳", "ch001", "訳文章題",
            "合成名の訳", "相手の訳語", "〜さん", "ch001/b1", "合成の口調", "変化点",
        ])
        for src_value in ("合成原題", "内部の原文", "原文章題",
                          "合成名の原文", "相手の原語"):
            self.assertNotIn(src_value, got)

    def test_non_string_values_are_not_fields(self):
        self.assertEqual(ts.toml_target_fields(
            {"version": 3, "flag": True, "rate": 0.5}), [])

    def test_src_prefix_but_not_src_key_is_kept(self):
        """'src2' のような別名キーは原文キーではない（過剰除外しない）。"""
        got = ts.toml_target_fields({"src": "除外", "term_src": "除外2",
                                     "src2": "残る", "note": "残る2"})
        self.assertEqual(got, ["残る", "残る2"])

    def test_empty_data(self):
        self.assertEqual(ts.toml_target_fields({}), [])


if __name__ == "__main__":
    unittest.main()
