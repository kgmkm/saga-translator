"""fixed の規定訳検査の段落単位化（S-C強化）のテスト。

- ``src_items``（原文の (段落番号, 本文) 列）を渡すと、原文段落ごとに規定訳の
  出現を強制する（原文段落 n に語があり、訳文段落 n に dst が無ければ違反）
- 省略時（None）は従来どおりブロック全体検査（互換）
- 画像リンク・known_terms・dst スパン除外の既存保護は段落検査でも維持
- commit 経路（saga.py）が src_items を渡して実際に停止すること
- 残渣（原語相当の語が残る）の detail に rename_terms の誘導が入ること

合成データのみ使用（実在の語彙表エントリ・作品情報は使わない）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import vocab  # noqa: E402


def _entry(src: str, dst: str = "", kind: str = "fixed",
           variants: tuple[str, ...] = (),
           dst_variants: tuple[str, ...] = ()) -> vocab.VocabEntry:
    return vocab.VocabEntry(src=src, dst=dst, kind=kind, variants=tuple(variants),
                            dst_variants=tuple(dst_variants))


def _vocab(*entries: vocab.VocabEntry) -> vocab.Vocab:
    return vocab.Vocab(list(entries))


# ── 段落単位の規定訳検査（check_draft） ─────────────────────────────────


class TestParagraphDstCheck(unittest.TestCase):
    def test_paragraph_violation_detected(self):
        """原文段落に語があり、その訳文段落に規定訳が無ければ違反。

        dst がブロック内の別段落に現れていても通さない（ブロック全体検査との差）。
        """
        v = _vocab(_entry("合成語B", "合成訳B"))
        src_items = [(1, "静かな夜。"), (2, "合成語B闪着光。")]
        items = [(1, "静かな夜。"), (2, "粘つく光がある。"), (3, "別の段落に合成訳Bがある。")]
        findings = vocab.check_draft(v, items, "合成語B闪着光。", src_items=src_items)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.kind, "fixed")
        self.assertEqual(f.paragraph, 2)
        self.assertEqual(f.form, "合成語B")
        self.assertEqual(f.detail,
                         "原文段落2の『合成語B』は規定訳『合成訳B』で訳されていません")

    def test_dst_in_matching_paragraph_passes(self):
        v = _vocab(_entry("合成語B", "合成訳B"))
        findings = vocab.check_draft(
            v, [(1, "合成訳Bが光る。")], "合成語B闪着光。",
            src_items=[(1, "合成語B闪着光。")])
        self.assertEqual(findings, [])

    def test_paragraph_without_src_not_checked(self):
        """原文段落に語が無ければ、その訳文段落に規定訳が無くても違反にしない。"""
        v = _vocab(_entry("合成語B", "合成訳B"))
        findings = vocab.check_draft(
            v, [(1, "光る石がある。"), (2, "合成訳Bが光る。")], "合成語B闪着光。",
            src_items=[(1, "静かな夜。"), (2, "合成語B闪着光。")])
        self.assertEqual(findings, [])

    def test_multiple_occurrences_in_one_paragraph_single_finding(self):
        """1段落に複数回出現しても検査は1回（段落ごとに高々1件）。"""
        v = _vocab(_entry("合成語B", "合成訳B"))
        findings = vocab.check_draft(
            v, [(2, "光る石がある。")], "",
            src_items=[(2, "合成語Bと合成語Bが並ぶ。合成語B。")])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].paragraph, 2)

    def test_each_paragraph_reported(self):
        """出現する原文段落ごとに1件ずつ報告する。"""
        v = _vocab(_entry("合成語B", "合成訳B"))
        findings = vocab.check_draft(
            v, [(1, "光る石がある。"), (2, "夜が明ける。")], "",
            src_items=[(1, "合成語Bが光る。"), (2, "合成語Bが消える。")])
        self.assertEqual([f.paragraph for f in findings], [1, 2])

    def test_src_items_none_keeps_block_behavior(self):
        """省略時（None）は従来どおりブロック全体検査（dst が別段落にあれば通過）。"""
        v = _vocab(_entry("合成語B", "合成訳B"))
        items = [(1, "光る石がある。"), (2, "合成訳Bが光る。")]
        self.assertEqual(vocab.check_draft(v, items, "合成語B闪着光。"), [])
        # ブロック全体に dst が無ければ従来の違反（paragraph=0）
        findings = vocab.check_draft(v, [(1, "光る石がある。")], "合成語B闪着光。")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].paragraph, 0)
        self.assertIn("訳文に規定訳が見つかりません", findings[0].detail)

    def test_image_link_protection(self):
        """画像リンク（原語ファイル名）は原文・訳文のどちらのビューからも除く。"""
        v = _vocab(_entry("合成語B", "合成訳B"))
        img_src = "images/合成語Bの写真.png"
        # (a) 訳文の画像リンクは残渣として数えない
        findings = vocab.check_draft(
            v, [(1, f"![]({img_src})")], "合成語B闪着光。",
            src_items=[(1, "画像だけの段落。")])
        self.assertEqual(findings, [])
        # (b) 原文段落の画像リンクも語の出現として数えない
        findings = vocab.check_draft(
            v, [(1, "光る石がある。")], "合成語B闪着光。",
            src_items=[(1, f"![]({img_src})")])
        self.assertEqual(findings, [])
        # (c) 規定訳が画像リンクのパスにしか無い場合は「訳文段落に無い」扱い
        img_dst = "images/合成訳B.png"
        findings = vocab.check_draft(
            v, [(1, f"![]({img_dst}) 光る石がある。")], "合成語B闪着光。",
            src_items=[(1, "合成語B闪着光。")])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].paragraph, 1)

    def test_known_terms_waives_paragraph_check(self):
        """known_terms（作品全体で残す語）は段落検査でも除外する。"""
        v = _vocab(_entry("合成語B", "合成訳B"))
        findings = vocab.check_draft(
            v, [(1, "光る石がある。")], "合成語B闪着光。",
            known_terms=["合成語B"], src_items=[(1, "合成語B闪着光。")])
        self.assertEqual(findings, [])
        # 別字形（src_variants）が known_terms にある場合も同様
        v2 = _vocab(_entry("合成語C", "合成訳C", variants=("魅镜",)))
        findings = vocab.check_draft(
            v2, [(1, "光る石がある。")], "魅镜发光。",
            known_terms=["魅镜"], src_items=[(1, "魅镜发光。")])
        self.assertEqual(findings, [])

    def test_src_variants_trigger_paragraph_check(self):
        """別字形（src_variants）の出現でも対応段落に規定訳を要求する。"""
        v = _vocab(_entry("合成語C", "合成訳C", variants=("魅镜",)))
        findings = vocab.check_draft(
            v, [(1, "静かな夜。"), (2, "光る石がある。")], "",
            src_items=[(1, "静かな夜。"), (2, "魅镜发光。")])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].form, "魅镜")
        self.assertEqual(findings[0].paragraph, 2)
        self.assertIn("原文段落2の『魅镜』", findings[0].detail)
        # 対応する訳文段落に規定訳があれば通過
        findings = vocab.check_draft(
            v, [(2, "合成訳Cが光る。")], "", src_items=[(2, "魅镜发光。")])
        self.assertEqual(findings, [])

    def test_ja_ok_not_checked(self):
        v = _vocab(_entry("合成語D", "合成語D", kind="ja_ok"))
        findings = vocab.check_draft(
            v, [(1, "伸びている。")], "合成語D伸长。",
            src_items=[(1, "合成語D伸长。")])
        self.assertEqual(findings, [])

    def test_dst_span_exclusion_maintained(self):
        """規定訳が原語形を内部に含む語でも、正しい訳語を残渣と誤検知しない。"""
        v = _vocab(_entry("合成語B", "合成語Bの訳"))
        findings = vocab.check_draft(
            v, [(1, "合成語Bの訳が光る。")], "合成語B闪着光。",
            src_items=[(1, "合成語B闪着光。")])
        self.assertEqual(findings, [])

    def test_candidates_residue_detail_has_rename_guidance(self):
        """残渣（原語相当の語が残る）の detail に rename_terms の誘導が入る。"""
        v = _vocab(_entry("合成語C", "合成訳C", kind="candidates"))
        findings = vocab.check_draft(v, [(2, "合成語Cを拭く。")], "合成語C发光。")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "candidates")
        self.assertIn("原語相当の語が残っています", findings[0].detail)
        self.assertIn("既訳が混在する作品は rename_terms.py で統一してから",
                      findings[0].detail)


# ── 許容別表記 dst_variants ─────────────────────────────────────────────


class TestDstVariants(unittest.TestCase):
    """dst か dst_variants のどれか1つでも該当段落・ブロックにあれば合格（完全一致）。"""

    def test_paragraph_check_accepts_variant(self):
        """規定訳が無くても許容別表記が該当段落にあれば合格。どれも無ければ違反。"""
        v = _vocab(_entry("合成語B", "合成訳B", dst_variants=("合成訳B状",)))
        findings = vocab.check_draft(
            v, [(1, "合成訳B状の光。")], "合成語B闪着光。",
            src_items=[(1, "合成語B闪着光。")])
        self.assertEqual(findings, [])
        findings = vocab.check_draft(
            v, [(1, "光る石がある。")], "合成語B闪着光。",
            src_items=[(1, "合成語B闪着光。")])
        self.assertEqual(len(findings), 1)
        self.assertIn("別表記『合成訳B状』も可", findings[0].detail)

    def test_block_check_accepts_variant(self):
        """ブロック全体検査（src_items 省略）でも許容別表記で合格する。"""
        v = _vocab(_entry("合成語B", "合成訳B", dst_variants=("合成訳B状",)))
        items = [(1, "合成訳B状が光る。")]
        self.assertEqual(vocab.check_draft(v, items, "合成語B闪着光。"), [])

    def test_variant_containing_src_form_is_not_residue(self):
        """許容別表記が原語形を内部に含んでも残渣と誤検知しない（dst スパン除外）。"""
        v = _vocab(_entry("合成語B", "合成訳B", dst_variants=("合成語B風訳",)))
        findings = vocab.check_draft(
            v, [(1, "合成語B風訳が光る。")], "合成語B闪着光。",
            src_items=[(1, "合成語B闪着光。")])
        self.assertEqual(findings, [])

    def test_dst_variants_loaded_from_vocab_toml(self):
        """拡張 vocab.toml の dst_variants を読み込む（空・dst 自身・重複は除く）。"""
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            ext = td / "ext"
            _write(ext / "vocab.toml", """\
version = "1"

[[vocab]]
src = "合成語B"
dst = "合成訳B"
dst_variants = ["合成訳B状", "", "合成訳B", "合成訳B状"]
kind = "fixed"
""")
            work = td / "work"
            work.mkdir()
            with _vocab_env(ext):
                v = vocab.load_vocab(work)
            e = v.lookup("合成語B")
            self.assertIsNotNone(e)
            self.assertEqual(e.dst_variants, ("合成訳B状",))
            self.assertEqual(e.dst_forms, ("合成訳B", "合成訳B状"))


# ── commit 経路の接続（saga.py が src_items を渡す） ────────────────────


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@contextlib.contextmanager
def _vocab_env(*paths: Path):
    """SAGA_VOCAB_PATH を一時的に設定する（終了時に元へ戻す）。"""
    old = os.environ.get("SAGA_VOCAB_PATH")
    os.environ["SAGA_VOCAB_PATH"] = os.pathsep.join(str(p) for p in paths)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("SAGA_VOCAB_PATH", None)
        else:
            os.environ["SAGA_VOCAB_PATH"] = old


class TestCommitParagraphWiring(unittest.TestCase):
    """commit（saga.py）が src_items を渡し、段落単位で規定訳を強制する。"""

    def setUp(self) -> None:
        self.td = Path(tempfile.mkdtemp())
        self.work = self.td / "test_work"
        _write(self.work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
        _write(self.work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}]
""")
        _write(self.work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "合成語B闪着光。"

[[paras]]
n = 2
text = "静かな夜。"

[[paras]]
n = 3
text = "普通の文。"
""")
        self.ext = self.td / "ext"
        _write(self.ext / "vocab.toml", """\
version = "1"

[[vocab]]
src = "合成語B"
dst = "合成訳B"
kind = "fixed"
""")

    def tearDown(self) -> None:
        import shutil
        os.environ.pop("SAGA_VOCAB_PATH", None)
        shutil.rmtree(self.td, ignore_errors=True)

    def _draft(self, paras: list[tuple[int, str]]) -> None:
        draft = self.work / "output" / "_work" / "ch001_b1_ja_numbered.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("\n\n".join(f"[P{n:03d}] {t}" for n, t in paras),
                         encoding="utf-8", newline="\n")

    def _commit_capture(self) -> tuple[int, str]:
        from saga import cmd_commit
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cmd_commit(self.work, "ch001", "b1", "test-model", "v1")
        return rc, out.getvalue() + err.getvalue()

    def test_commit_stops_when_dst_in_wrong_paragraph(self):
        """dst がブロック内の別段落にあっても、対応段落に無ければ commit は止まる。"""
        self._draft([(1, "光る石がある。"), (2, "合成訳Bを拭く。"), (3, "普通の文。")])
        with _vocab_env(self.ext):
            rc, text = self._commit_capture()
        self.assertEqual(rc, 1)
        self.assertIn("原文段落1", text)
        self.assertIn("合成訳B", text)
        self.assertFalse((self.work / "output" / "ch001" / "b1_ja.md").exists())

    def test_commit_passes_when_dst_in_matching_paragraph(self):
        self._draft([(1, "合成訳Bが光る。"), (2, "静かな夜。"), (3, "普通の文。")])
        with _vocab_env(self.ext):
            rc = self._commit_capture()[0]
        self.assertEqual(rc, 0)
        self.assertTrue((self.work / "output" / "ch001" / "b1_ja.md").exists())


if __name__ == "__main__":
    unittest.main()
