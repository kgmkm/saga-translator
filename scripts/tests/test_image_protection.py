# -*- coding: utf-8 -*-
"""画像リンク保護の回帰テスト（BUG-01 / BUG-02 / インデント事故）

翻訳運用（画像リンク内の原語ファイル名で QA・字形整形が誤爆）で発見した
不具合を回帰防止として固定する。テスト・コメントには作品固有の情報
（作品名・パス・キャラ名・実ファイル名）を含めない。例は合成データ。

1. BUG-01: run_qa は画像リンク ![alt](path) のパス・alt を
   用字チェック（non_jis_han / leftover_source_names）の対象にしない。
   パスは実ファイル名（＝原語表記）と一致させる必要があるため。
2. BUG-02: shape_glyph は画像リンクのパスを書き換えない。
   恶→悪 等の字形整形は本文のみに適用され、リンクは無傷。
3. インデント事故の回帰: align.py の会話頭不一致ループは
   check_block のトップレベルで実行されること（一時パッチ適用時に
   is_acked の関数本体にネストし、検出が無効化されていた実例あり）。
   「原文が会話頭なのに訳が地の文」が fatal で出ることを固定する。

sentinel には NUL (\\x00) を使わないこと（ソースに実NULが混入し
ast.parse が落ちる実例あり）。PUA 文字を使用。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

# 合成データ。字形整形の対象になる簡体字（馈・恶）を含む。
IMAGE_PATH = "images/example-10000000-馈字测试——样例.png"

try:  # opencc があれば 恶→悪 の字形変換結果を検査できる（導入環境でのみ実行）
    import opencc  # noqa: F401
    OPENCC_AVAILABLE = True
except Exception:  # pragma: no cover - 環境依存
    OPENCC_AVAILABLE = False


class TestShapeGlyphImageProtection(unittest.TestCase):
    """BUG-02: 画像リンクのパスは字形整形から保護される。"""

    def test_image_path_preserved(self):
        """画像リンクのパスは字形整形の対象にならない（opencc の有無に依らない）。"""
        from normalize.zh2ja import shape_glyph
        src = f"本文。![]({IMAGE_PATH}) 恶梦"
        out, applied = shape_glyph(src)
        # 画像リンクは実ファイル名のまま（opencc 無しでも保たれる）
        self.assertIn(IMAGE_PATH, out)
        # 適用記録に画像パス内の字（馈）が入っていないこと
        items = [a["item"] for a in applied]
        self.assertNotIn("馈", items)

    def test_image_path_protected_glyph_shaping_handtable(self):
        """本文側の字形整形（手当て表で解決できる字）は opencc 無しでも効く。"""
        from normalize.zh2ja import shape_glyph
        # 沉→沈 は手当て表（MANUAL_CHAR）にある glyph 単字 → opencc 非依存
        src = f"本文。![]({IMAGE_PATH}) 沉む"
        out, _ = shape_glyph(src)
        self.assertIn(IMAGE_PATH, out)   # パスは無傷
        self.assertIn("沈", out)         # 本文は整形される

    @unittest.skipUnless(
        OPENCC_AVAILABLE,
        "opencc 任意依存（P1-4）: 恶→悪 は opencc による字形変換のため未導入環境ではスキップ",
    )
    @unittest.skipUnless(
        __import__("normalize.zh2ja", fromlist=["x"]).opencc_available(),
        "opencc 任意依存（P1-4）: カスタム設定が無い環境ではスキップする",
    )
    def test_image_path_preserved_body_glyph_via_opencc(self):
        """opencc あり: 本文中の簡体字（恶→悪）が整形され、パスは無傷。"""
        from normalize.zh2ja import shape_glyph
        src = f"本文。![]({IMAGE_PATH}) 恶梦"
        out, _ = shape_glyph(src)
        self.assertIn(IMAGE_PATH, out)
        self.assertIn("悪梦", out)

    def test_no_image_unchanged(self):
        from normalize.zh2ja import shape_glyph
        out, applied = shape_glyph("普通の本文。")
        self.assertEqual(out, "普通の本文。")
        self.assertEqual(applied, [])


class TestRunQaImageExclusion(unittest.TestCase):
    """BUG-01: QA の用字チェックは画像タグを除外したビューで行う。"""

    def _make_work(self, tmp: Path) -> Path:
        import _lib
        work = tmp / "w1"
        work.mkdir(parents=True)
        (work / "manifest.toml").write_text(
            'lang = "zh"\ndst = "ja"\n', encoding="utf-8")
        (work / "chapters").mkdir()
        _lib.atomic_write_text(
            work / "chapters_meta.toml",
            '[work]\nwork_id = "w1"\n\n[[chapters]]\nid = "ch001"\ntitle = ""\n'
            'n_paras = 2\nn_chars = 40\n'
            'blocks = [{ id = "b1", paras = [1, 2], n_chars = 40, '
            'scene_break_cut = false }]\nstatus = "pending"\n')
        _lib.atomic_write_text(
            work / "chapters" / "ch001.paras.toml",
            'chapter = "ch001"\n\n[[paras]]\nn = 1\ntext = "“是啊”她说。"\n\n'
            '[[paras]]\nn = 2\ntext = "他走了。"\n')
        drafts = work / "output" / "_work"
        drafts.mkdir(parents=True)
        # 訳文に原語ファイル名を含む画像リンクがあっても QA は通る
        (drafts / "ch001_b1_ja_numbered.md").write_text(
            "[P1] 「そうだね」と彼女は言った。\n\n"
            f"[P2] ![]({IMAGE_PATH}) 彼は去った。\n",
            encoding="utf-8")
        return work

    def test_qa_passes_with_image_link(self):
        from qa_translation import run_qa
        with tempfile.TemporaryDirectory() as tmp:
            work = self._make_work(Path(tmp))
            draft = work / "output" / "_work" / "ch001_b1_ja_numbered.md"
            report, items = run_qa(work, "ch001", "b1", draft, strict=True)
            checks = report["checks"]
            # 画像パス内の簡体字は non_jis_han で検出されない
            self.assertTrue(checks["non_jis_han"]["pass"],
                            f"non_jis_han が画像パスを拾った: {checks['non_jis_han']}")
            self.assertTrue(report["pass"], f"QA が落ちた: {checks}")
            # 確定出力には画像リンクが残る
            self.assertTrue(any("馈字测试" in t for _, t in items))


class TestAlignDialogueHeadRegression(unittest.TestCase):
    """インデント事故の回帰: 会話頭不一致は fatal で出る。"""

    def test_dialogue_head_fatal_toplevel(self):
        import align
        import _lib
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w1"
            work.mkdir(parents=True)
            (work / "manifest.toml").write_text(
                'lang = "zh"\ndst = "ja"\n', encoding="utf-8")
            (work / "chapters").mkdir()
            _lib.atomic_write_text(
                work / "chapters_meta.toml",
                '[work]\nwork_id = "w1"\n\n[[chapters]]\nid = "ch001"\ntitle = ""\n'
                'n_paras = 1\nn_chars = 20\n'
                'blocks = [{ id = "b1", paras = [1, 1], n_chars = 20, '
                'scene_break_cut = false }]\nstatus = "pending"\n')
            _lib.atomic_write_text(
                work / "chapters" / "ch001.paras.toml",
                'chapter = "ch001"\n\n[[paras]]\nn = 1\ntext = "“是啊”她说。"\n')
            drafts = work / "output" / "_work"
            drafts.mkdir(parents=True)
            # 原文は会話頭なのに訳は地の文 → fatal のはず
            (drafts / "ch001_b1_ja_numbered.md").write_text(
                "[P1] 彼女はそう言った。\n", encoding="utf-8")
            findings = align.check_block(work, "ch001", "b1")
            self.assertTrue(
                any(f.severity == "fatal" and f.kind == "dialogue_head"
                    for f in findings),
                f"dialogue_head が検出されない: {[(f.severity, f.kind) for f in findings]}",
            )


class TestIngestImageReferenceProtection(unittest.TestCase):
    """P0-2: ingest の画像名正規化後も参照は images/ 相対のまま保たれ、実ファイルと一致する。

    正規化はファイル名と参照を一体で行う（リンクの alt は無傷・旧名は残らない）。
    """

    def test_md_refs_rewritten_to_normalized_relative_paths(self):
        import shutil
        import subprocess
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        src = td / "src"
        (src / "images").mkdir(parents=True)
        (src / "images" / "10000000-10000001-原語題名.png").write_bytes(b"\x89PNG one")
        (src / "images" / "10000000-封面.png").write_bytes(b"\x89PNG cover")
        md = src / "10000000-原語題名.md"
        md.write_text(
            "---\ntitle: 原語題名\nauthor: 作者名\n---\n\n"
            "第一段落。\n\n"
            "![挿絵](images/10000000-10000001-原語題名.png)\n\n"
            "![表紙](images/10000000-封面.png)\n\n"
            "第四段落。\n\n"
            "第五段落。\n",
            encoding="utf-8",
        )
        out = td / "_saga"
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "ingest.py"), str(md), "-o", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        work = next(x for x in out.iterdir() if x.is_dir())
        body = (work / "originals" / "source.md").read_text(encoding="utf-8")
        img = work / "output" / "images"
        # 参照は images/ 相対のまま、正規名（実ファイルと一致）を指す
        self.assertIn("![挿絵](images/10000000-10000001.png)", body)
        self.assertIn("![表紙](images/10000000-cover.png)", body)
        for name in ("10000000-10000001.png", "10000000-cover.png"):
            self.assertTrue((img / name).is_file(), f"参照先が実在しない: {name}")
        # 旧名は参照に残らない（リンクの alt は無傷）
        self.assertNotIn("原語題名.png", body)
        self.assertNotIn("封面", body)


if __name__ == "__main__":
    unittest.main()