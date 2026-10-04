"""会話頭判定（擬音頭除外・構造マーカー除外）の回帰テスト。自作合成データのみ。

作品固有の情報（作品名・ID・キャラ名・実ファイル名）は一切含めない。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(SCRIPTS))
import align  # noqa: E402
import align_check  # noqa: E402
from _lib import dialogue_heads  # noqa: E402


# zh→ja の会話頭。擬音頭の判定に使う。
SRC_HEADS, DST_HEADS = dialogue_heads("zh", "ja")

# 合成の擬音頭（短い引用＋地の文）。実在の作品本文は使わない。
ONO_HEAD = "\u201c扑腾\u201d一声，他猛地从地上坐了起来。"          # 引用3字 + 地の文
ONO_HEAD_8 = "\u201c啊啊啊啊啊啊\u201d，他一下子倒了下去，再也没有起来。"  # 引用8字（上限ちょうど）
DIALOGUE_HEAD = "\u201c今日はとても良い天気ですね、散歩にでも行きましょうか。\u201d"  # 長い引用のみ
STRUCT_HEAD = "[newpage]"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _setup_work(wd: Path, src_texts: list[str]) -> None:
    """src_texts を [P1..n] の原文段落とし、chapters_meta と空ドラフトを用意する。"""
    n = len(src_texts)
    blocks = "{ id = \"b1\", paras = [1, %d], n_chars = 50, scene_break_cut = false }" % n
    _write(
        wd / "chapters_meta.toml",
        "[work]\nwork_id = \"w1\"\n\n"
        "[[chapters]]\nid = \"ch001\"\ntitle = \"t\"\nn_paras = %d\nn_chars = 50\n"
        "blocks = [%s]\nstatus = \"pending\"\n" % (n, blocks),
    )
    para_blocks = []
    for i, t in enumerate(src_texts, start=1):
        para_blocks.append("[[paras]]\nn = %d\ntext = %s\n" % (i, _toml_str(t)))
    _write(wd / "chapters" / "ch001.paras.toml", "chapter = \"ch001\"\ntitle = \"t\"\n\n" + "\n".join(para_blocks))


def _toml_str(s: str) -> str:
    # 二重引用符 TOML 文字列として最小限エスケープ（テスト入力はASCII制御文字を含まない）
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % esc


class TestOnomatopoeiaHeadUnit(unittest.TestCase):
    """is_onomatopoeia_head() 単体の判定（境界を含む）。"""

    def test_short_ono_is_head(self):
        self.assertTrue(align.is_onomatopoeia_head(ONO_HEAD, SRC_HEADS))

    def test_exactly_max_is_ono(self):
        # 引用が _ONO_HEAD_MAX(8) ちょうど → 擬音扱い
        self.assertTrue(align.is_onomatopoeia_head(ONO_HEAD_8, SRC_HEADS))

    def test_one_over_max_is_not_ono(self):
        # 9字の引用 + 短い残り → 会話扱い（擬音ではない）
        nine = "\u201c" + ("あ" * 9) + "\u201d" + "。"
        self.assertFalse(align.is_onomatopoeia_head(nine, SRC_HEADS))

    def test_long_dialogue_is_not_ono(self):
        # 引用のみで残りが無い（長い引用）→ 擬音ではない
        self.assertFalse(align.is_onomatopoeia_head(DIALOGUE_HEAD, SRC_HEADS))

    def test_short_quote_with_short_rest_is_not_ono(self):
        # 引用が短くても閉じた後の本文が短い場合は会話扱い
        self.assertFalse(align.is_onomatopoeia_head("\u201c喂\u201d。", SRC_HEADS))

    def test_non_quote_head_is_not_ono(self):
        # 引用符で始まらない → 擬音ではない
        self.assertFalse(align.is_onomatopoeia_head("他点了点头，然后继续往前走。", SRC_HEADS))

    def test_unclosed_quote_is_not_ono(self):
        # 閉じ引用符が無い → 擬音ではない
        self.assertFalse(align.is_onomatopoeia_head("\u201c呔一声，他跑了出去。", SRC_HEADS))


class TestAlignDialogueHeadRegression(unittest.TestCase):
    """check_block() の dialogue_head fatal への回帰。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.wd = self.td / "w1"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.td, ignore_errors=True)

    def _findings(self, src_texts: list[str], dst_texts: list[str]):
        _setup_work(self.wd, src_texts)
        body = "\n\n".join("[P%d] %s" % (i, t) for i, t in enumerate(dst_texts, start=1))
        _write(self.wd / "output" / "_work" / "ch001_b1_ja_numbered.md", body + "\n")
        return align.check_block(self.wd, "ch001", "b1")

    def test_ono_head_not_flagged(self):
        # 原文が擬音頭、訳が地の文 → dialogue_head fatal にならない
        f = self._findings([ONO_HEAD], ["彼は地面から飛び起きた。"])
        self.assertFalse(any(x.kind == "dialogue_head" for x in f))

    def test_structure_marker_head_not_flagged(self):
        # 構造マーカー頭は従来どおり会話と判定しない
        f = self._findings([STRUCT_HEAD + "彼は立ち上がった。"], ["彼は立ち上がった。"])
        self.assertFalse(any(x.kind == "dialogue_head" for x in f))

    def test_real_dialogue_head_still_flagged(self):
        # 通常の会話頭（引用が長い）で訳が地の文 → 従来どおり fatal
        f = self._findings([DIALOGUE_HEAD], ["今日は良い天気だ、と彼は言った。"])
        self.assertTrue(any(x.kind == "dialogue_head" for x in f))


class TestAlignCheckConsistency(unittest.TestCase):
    """align.py と align_check.py の会話頭判定（擬音除外込み）が一致すること。"""

    def test_ono_exclusion_matches_align(self):
        cases = [
            ONO_HEAD,
            ONO_HEAD_8,
            "\u201c" + ("あ" * 9) + "\u201d" + "。",
            DIALOGUE_HEAD,
            "\u201c喂\u201d。",
            "他点了点头，然后继续往前走。",
            STRUCT_HEAD + "本文。",
        ]
        for c in cases:
            with self.subTest(c=c):
                a = align.is_onomatopoeia_head(c, SRC_HEADS)
                b = align_check.is_onomatopoeia_head(c)
                self.assertEqual(a, b, "ono判定不一致: %r" % c)

    def test_dialogue_decision_matches_for_ono(self):
        # 擬音頭: align 側は擬音と判定し、align_check 側も会話としない
        self.assertTrue(align.is_onomatopoeia_head(ONO_HEAD, SRC_HEADS))
        self.assertFalse(align_check.is_dialogue(ONO_HEAD))
        # 通常の会話頭: どちらも会話扱い（align 側は擬音ではない）
        self.assertFalse(align.is_onomatopoeia_head(DIALOGUE_HEAD, SRC_HEADS))
        self.assertTrue(align_check.is_dialogue(DIALOGUE_HEAD))


if __name__ == "__main__":
    unittest.main()