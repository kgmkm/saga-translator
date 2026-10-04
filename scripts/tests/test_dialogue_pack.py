"""会話頭判定の例外（構造タグ・擬音頭）の言語 pack 集約を固定するテスト。自作の合成データのみ。

対応（P2-2・会話頭の判定）:
- ① pack の一覧が align / align_check の両者から使われている
  （一覧から1項目外すと誤爆が復活することを確認する）
- ② zh/ja pack の例外定義を明示し、対応する言語の pack が使われる
  （zh→ja は zh、ja→zh は ja。align_check は原文側・訳文側で各言語の pack）
- ③ 既存2型（構造タグ始まり・擬音頭）と通常会話の検出が維持されている

作品固有の情報（作品名・ID・キャラ名・実ファイル名）は一切含めない。
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import align  # noqa: E402
import align_check  # noqa: E402
from _lib import dialogue_heads  # noqa: E402
from lang import ja as ja_pack  # noqa: E402
from lang import zh as zh_pack  # noqa: E402

# zh→ja / ja→zh の会話頭（判定の開き集合）
ZH_SRC_HEADS, _ = dialogue_heads("zh", "ja")
JA_SRC_HEADS, _ = dialogue_heads("ja", "zh")

# 合成の擬音頭（短い引用＋地の文）と通常会話頭
ONO_ZH = "\u201c扑腾\u201d一声，他猛地从地上坐了起来。"          # zh: 引用3字 + 地の文
ONO_JA = "\u300cどきっ\u300dと彼女は振り返り、長く続く地の文がここにあります。"  # ja: 「どきっ」+ 地の文
DIALOGUE_ZH = "\u201c今日はとても良い天気ですね、散歩にでも行きましょうか。\u201d"  # 長い引用のみ
STRUCT_HEAD = "[newpage]"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _toml_str(s: str) -> str:
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % esc


def _setup_work(wd: Path, src_texts: list[str], dst_texts: list[str],
                lang: str = "zh", dst: str = "ja") -> None:
    """原文/訳文の段落・manifest・chapters_meta・ドラフトを持つ最小の work を作る。"""
    n = len(src_texts)
    _write(wd / "manifest.toml", 'lang = "%s"\ndst = "%s"\n' % (lang, dst))
    blocks = '{ id = "b1", paras = [1, %d], n_chars = 50, scene_break_cut = false }' % n
    _write(
        wd / "chapters_meta.toml",
        '[work]\nwork_id = "w1"\n\n[[chapters]]\nid = "ch001"\ntitle = "t"\nn_paras = %d\n'
        'n_chars = 50\nblocks = [%s]\nstatus = "pending"\n' % (n, blocks),
    )
    paras = "".join('[[paras]]\nn = %d\ntext = %s\n' % (i, _toml_str(t)) for i, t in enumerate(src_texts, 1))
    _write(wd / "chapters" / "ch001.paras.toml", 'chapter = "ch001"\ntitle = "t"\n\n' + paras)
    suf = "_ja" if dst == "ja" else "_zh"
    body = "\n\n".join("[P%d] %s" % (i, t) for i, t in enumerate(dst_texts, 1))
    _write(wd / "output" / "_work" / ("ch001_b1%s_numbered.md" % suf), body + "\n")


@contextmanager
def _patched_exceptions(pack, **changes):
    """pack の例外定義を一時的に差し替える（終了時に必ず復元）。"""
    orig = pack.DIALOGUE_HEAD_EXCEPTIONS
    pack.DIALOGUE_HEAD_EXCEPTIONS = {**orig, **changes}
    try:
        yield pack.DIALOGUE_HEAD_EXCEPTIONS
    finally:
        pack.DIALOGUE_HEAD_EXCEPTIONS = orig


def _without(seq, item):
    return tuple(x for x in seq if x != item)


class TestPackDefinitions(unittest.TestCase):
    """② 例外定義が pack にあり、内容が明示されている（zh/ja の差の可視点）。"""

    def test_zh_pack_definition(self):
        exc = zh_pack.DIALOGUE_HEAD_EXCEPTIONS
        self.assertEqual(
            exc["structure_prefixes"],
            ("[[rb:", "[pixivimage:", "[chapter:", "[newpage]", "[uploadedimage:"),
        )
        self.assertEqual(exc["ono_head_max"], 8)
        self.assertEqual(set(exc["ono_head_closers"]), set("\u201d\u300d\u300f\""))
        self.assertEqual(exc["ono_head_rest_factor"], 2)
        self.assertIn("\u201c", exc["ono_head_quotes"])
        self.assertIn("[", exc["ono_head_quotes"])

    def test_ja_pack_definition(self):
        exc = ja_pack.DIALOGUE_HEAD_EXCEPTIONS
        self.assertEqual(
            exc["structure_prefixes"],
            ("[[rb:", "[pixivimage:", "[chapter:", "[newpage]", "[uploadedimage:"),
        )
        self.assertEqual(exc["ono_head_max"], 8)
        self.assertEqual(set(exc["ono_head_closers"]), set("\u201d\u300d\u300f\""))
        self.assertEqual(exc["ono_head_rest_factor"], 2)
        self.assertIn("\u300c", exc["ono_head_quotes"])

    def test_zh_ja_definition_relation_is_explicit(self):
        """両 pack の定義の関係を明示する（現時点は同値。差を入れる場合はここが可視点）。

        pixiv の構造タグ・擬音条件は ja/zh で共通のため、現状の値は同一。
        言語ごとに変えるときは、このテストを更新して差を記録する。
        """
        zh_exc = zh_pack.DIALOGUE_HEAD_EXCEPTIONS
        ja_exc = ja_pack.DIALOGUE_HEAD_EXCEPTIONS
        self.assertEqual(set(zh_exc), set(ja_exc))
        for key in zh_exc:
            self.assertEqual(zh_exc[key], ja_exc[key], "例外定義が zh/ja でずれています: %s" % key)

    def test_lookup_returns_pack_definition(self):
        # 定義はコピーせず pack モジュールそのものを引く（単一箇所の参照）
        self.assertIs(align.dialogue_head_exceptions("zh"), zh_pack.DIALOGUE_HEAD_EXCEPTIONS)
        self.assertIs(align.dialogue_head_exceptions("ja"), ja_pack.DIALOGUE_HEAD_EXCEPTIONS)
        # 未知の言語は会話頭の既定（zh→ja）に合わせて zh を使う
        self.assertIs(align.dialogue_head_exceptions("fr"), zh_pack.DIALOGUE_HEAD_EXCEPTIONS)

    def test_pack_difference_is_visible_in_judgement(self):
        """同じ文でも参照する pack の定義が違えば判定が変わる（差の効き方を明示）。"""
        text = "[custom:tag]彼は立ち上がった。"
        zh_exc = dict(zh_pack.DIALOGUE_HEAD_EXCEPTIONS)
        ja_exc = {**ja_pack.DIALOGUE_HEAD_EXCEPTIONS,
                  "structure_prefixes": tuple(ja_pack.DIALOGUE_HEAD_EXCEPTIONS["structure_prefixes"]) + ("[custom:",)}
        self.assertTrue(align_check.is_dialogue(text, zh_exc))
        self.assertFalse(align_check.is_dialogue(text, ja_exc))


class TestAlignReadsPack(unittest.TestCase):
    """①③ align.check_block が pack の一覧・条件を参照し、既存の検出が維持される。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.td, ignore_errors=True)

    def _findings(self, src_texts, dst_texts, lang="zh", dst="ja"):
        _setup_work(self.wd, src_texts, dst_texts, lang=lang, dst=dst)
        return align.check_block(self.wd, "ch001", "b1")

    def _has_dialogue_head(self, findings):
        return any(f.kind == "dialogue_head" for f in findings)

    def test_structure_tag_head_not_flagged(self):
        # ③ 構造タグ始まりは会話と判定しない
        f = self._findings([STRUCT_HEAD + "彼は立ち上がった。"], ["彼は立ち上がった。"])
        self.assertFalse(self._has_dialogue_head(f))

    def test_ono_head_not_flagged(self):
        # ③ 擬音頭（短い引用＋地の文）は会話と判定しない
        f = self._findings([ONO_ZH], ["彼は地面から飛び起きた。"])
        self.assertFalse(self._has_dialogue_head(f))

    def test_normal_dialogue_head_still_flagged(self):
        # ③ 通常会話（長い引用）で訳が会話頭でなければ fatal のまま
        f = self._findings([DIALOGUE_ZH], ["今日は良い天気だ、と彼は言った。"])
        self.assertTrue(self._has_dialogue_head(f))

    def test_removing_structure_item_revives_misdetection(self):
        # ① 一覧から [newpage] を外すと誤爆（会話頭 fatal）が復活する
        stripped = _without(zh_pack.DIALOGUE_HEAD_EXCEPTIONS["structure_prefixes"], "[newpage]")
        with _patched_exceptions(zh_pack, structure_prefixes=stripped):
            f = self._findings([STRUCT_HEAD + "彼は立ち上がった。"], ["彼は立ち上がった。"])
        self.assertTrue(self._has_dialogue_head(f))
        # 復元後は誤爆しない（pack が定義の正本であることの往復確認）
        f = self._findings([STRUCT_HEAD + "彼は立ち上がった。"], ["彼は立ち上がった。"])
        self.assertFalse(self._has_dialogue_head(f))

    def test_ono_condition_read_from_pack(self):
        # ① 擬音の条件（上限字数）も pack から読まれる: 0 にすると擬音頭が会話扱いになる
        with _patched_exceptions(zh_pack, ono_head_max=0):
            f = self._findings([ONO_ZH], ["彼は地面から飛び起きた。"])
        self.assertTrue(self._has_dialogue_head(f))


class TestAlignPackSelectionPerLanguage(unittest.TestCase):
    """② 原文言語の pack が使われる（zh→ja は zh、ja→zh は ja）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.td, ignore_errors=True)

    def _findings(self, src_texts, dst_texts, lang="zh", dst="ja"):
        _setup_work(self.wd, src_texts, dst_texts, lang=lang, dst=dst)
        return align.check_block(self.wd, "ch001", "b1")

    def _has_dialogue_head(self, findings):
        return any(f.kind == "dialogue_head" for f in findings)

    def test_zh2ja_uses_zh_pack(self):
        with _patched_exceptions(zh_pack, ono_head_max=0):
            f = self._findings([ONO_ZH], ["彼は地面から飛び起きた。"])
        self.assertTrue(self._has_dialogue_head(f))

    def test_zh2ja_unaffected_by_ja_pack(self):
        with _patched_exceptions(ja_pack, ono_head_max=0):
            f = self._findings([ONO_ZH], ["彼は地面から飛び起きた。"])
        self.assertFalse(self._has_dialogue_head(f))

    def test_ja2zh_uses_ja_pack(self):
        with _patched_exceptions(ja_pack, ono_head_max=0):
            f = self._findings([ONO_JA], ["彼女は振り返り、こちらを見た。"], lang="ja", dst="zh")
        self.assertTrue(self._has_dialogue_head(f))

    def test_ja2zh_unaffected_by_zh_pack(self):
        with _patched_exceptions(zh_pack, ono_head_max=0):
            f = self._findings([ONO_JA], ["彼女は振り返り、こちらを見た。"], lang="ja", dst="zh")
        self.assertFalse(self._has_dialogue_head(f))


class TestAlignCheckReadsPack(unittest.TestCase):
    """①③ align_check の is_dialogue / is_onomatopoeia_head が pack を参照する。"""

    def test_default_behavior_maintained(self):
        # ③ 構造タグ・擬音頭は会話としない / 通常会話は会話（既存挙動の固定）
        self.assertFalse(align_check.is_dialogue(STRUCT_HEAD + "彼は立ち上がった。"))
        self.assertFalse(align_check.is_dialogue(ONO_ZH))
        self.assertTrue(align_check.is_dialogue(DIALOGUE_ZH))
        self.assertTrue(align_check.is_onomatopoeia_head(ONO_ZH))

    def test_removing_structure_item_revives_misdetection(self):
        # ① 一覧から [newpage] を外すと誤爆（会話扱い）が復活する
        exc = {**zh_pack.DIALOGUE_HEAD_EXCEPTIONS,
               "structure_prefixes": _without(zh_pack.DIALOGUE_HEAD_EXCEPTIONS["structure_prefixes"], "[newpage]")}
        self.assertTrue(align_check.is_dialogue(STRUCT_HEAD + "彼は立ち上がった。", exc))

    def test_ono_condition_read_from_pack(self):
        # ① 擬音の条件も pack から読まれる
        with _patched_exceptions(zh_pack, ono_head_max=0):
            self.assertFalse(align_check.is_onomatopoeia_head(ONO_ZH))
            self.assertTrue(align_check.is_dialogue(ONO_ZH))  # 擬音でなくなれば会話扱い
        self.assertFalse(align_check.is_dialogue(ONO_ZH))


class TestAlignCheckMainUsesPack(unittest.TestCase):
    """② align_check.main は原文側・訳文側でそれぞれの言語の pack を使う。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"
        # ja→zh: 原文側・訳文側の両方に擬音頭（既定ではどちらも会話としない）
        _setup_work(self.wd, [ONO_JA], [ONO_ZH], lang="ja", dst="zh")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.td, ignore_errors=True)

    def _run_main(self):
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["align_check.py", str(self.wd), "--chapter", "ch001", "--block", "b1"]
        try:
            with redirect_stdout(buf):
                rc = align_check.main()
        finally:
            sys.argv = old_argv
        return rc, buf.getvalue()

    def test_default_is_clean(self):
        rc, out = self._run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("要確認 0 件", out)

    def test_source_side_uses_ja_pack(self):
        # 原文(ja)側の pack を変える → 原文側だけ会話扱いになり不一致が出る
        with _patched_exceptions(ja_pack, ono_head_max=0):
            rc, out = self._run_main()
        self.assertEqual(rc, 1, out)
        self.assertIn("会話/非会話の不一致", out)

    def test_target_side_uses_zh_pack(self):
        # 訳文(zh)側の pack を変える → 訳文側だけ会話扱いになり不一致が出る
        with _patched_exceptions(zh_pack, ono_head_max=0):
            rc, out = self._run_main()
        self.assertEqual(rc, 1, out)
        self.assertIn("会話/非会話の不一致", out)


if __name__ == "__main__":
    unittest.main()
