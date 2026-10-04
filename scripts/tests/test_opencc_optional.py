"""opencc を任意依存（推奨）として扱うことの検証（P1-4）。

opencc が未導入の環境でも:
  * `normalize/zh2ja._load_opencc()` が例外を握って (None, None) を返し、
    手当て表（MANUAL_COMPOUND / MANUAL_CHAR）だけで字形整形が動く。
  * 残渣検出（detect.*）は辞書ベースなので完全に動作する。
ことを固定する。

opencc が無い環境は pip アンインストールせず、`builtins.__import__` を
スタブして「import が失敗する」状況をプロセス内で再現する（実環境を壊さない）。
opencc の変換結果そのものを検査するテストは skipUnless で明示的にスキップし、
理由に「opencc 任意依存（P1-4）」と書く（黙って通る／黙って落ちるを避ける）。

テスト・コメントに作品固有情報は含めない（合成データのみ）。
"""
from __future__ import annotations

import builtins
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

try:  # opencc があれば字形変換結果を検査できる（導入環境でのみ実行）
    import opencc  # noqa: F401
    OPENCC_AVAILABLE = True
except Exception:  # pragma: no cover - 環境依存
    OPENCC_AVAILABLE = False

_SKIP_OPENCC = (
    "opencc 任意依存（P1-4）: このテストは opencc の変換結果を検査するため、"
    "未導入環境ではスキップする"
)


class _BlockOpencc:
    """with ブロック内で opencc の import を強制的に失敗させる。

    sys.modules からも外し、`from opencc import OpenCC`（_load_opencc の実装）が
    ImportError になる状況を再現する。ブロックを抜けると元に戻す。
    """

    def __enter__(self):
        self._real_import = builtins.__import__
        self._saved_mod = sys.modules.pop("opencc", None)

        def _blocked(name, *args, **kwargs):
            if name == "opencc" or name.startswith("opencc."):
                raise ImportError("opencc blocked for test (P1-4)")
            return self._real_import(name, *args, **kwargs)

        builtins.__import__ = _blocked
        return self

    def __exit__(self, *exc):
        builtins.__import__ = self._real_import
        if self._saved_mod is not None:
            sys.modules["opencc"] = self._saved_mod
        return False


class TestLoadOpenccFallback(unittest.TestCase):
    """opencc 未導入時は _load_opencc() が安全にフォールバックする。"""

    def test_load_opencc_returns_none_when_import_fails(self):
        """import を失敗させると (None, None) を返す（例外を漏らさない）。"""
        from normalize.zh2ja import _load_opencc
        with _BlockOpencc():
            s2t, t2jp = _load_opencc()
        self.assertIsNone(s2t)
        self.assertIsNone(t2jp)

    def test_load_opencc_restores_after_block(self):
        """ブロック解除後は opencc 導入環境なら再びロードできる（副作用なし）。"""
        from normalize.zh2ja import _load_opencc
        with _BlockOpencc():
            _load_opencc()
        if OPENCC_AVAILABLE:
            s2t, t2jp = _load_opencc()
            self.assertIsNotNone(s2t)
            self.assertIsNotNone(t2jp)


class TestFallbackGlyphPath(unittest.TestCase):
    """opencc 無しでも手当て表だけで字形整形が成立する。"""

    def test_glyph_char_via_handtable_without_opencc(self):
        """glyph 単字（沉→沈）は opencc 無しでも手当て表で変換される。"""
        from normalize.zh2ja import shape_glyph
        with _BlockOpencc():
            out, applied = shape_glyph("沉")
        self.assertEqual(out, "沈")
        self.assertTrue(any(a["item"] == "沉" for a in applied))

    def test_glyph_compound_without_opencc(self):
        """glyph 複合語（强行→強行）は opencc 無しでも適用される。"""
        from normalize.zh2ja import shape_glyph
        with _BlockOpencc():
            out, applied = shape_glyph("强行")
        self.assertEqual(out, "強行")
        self.assertTrue(any(a["item"] == "强行" for a in applied))

    def test_meaning_char_not_applied_without_opencc(self):
        """meaning 単字（烫）は opencc 無しでも既定モードでは適用されない。"""
        from normalize.zh2ja import shape_glyph
        with _BlockOpencc():
            out, _ = shape_glyph("烫")
        self.assertEqual(out, "烫")

    def test_build_map_fallback_marks_unresolved(self):
        """opencc 無しで手当て表に無い非JIS字は unresolved に落ちる。"""
        from normalize.zh2ja import build_map
        with _BlockOpencc():
            mapping, unresolved = build_map(["恶"], None, None)
        self.assertNotIn("恶", mapping)
        self.assertIn("恶", unresolved)

    def test_build_map_uses_handtable_without_opencc(self):
        """opencc 無しでも手当て表にある字は build_map で解決される。"""
        from normalize.zh2ja import build_map
        with _BlockOpencc():
            mapping, unresolved = build_map(["沉"], None, None)
        self.assertEqual(mapping.get("沉"), "沈")
        self.assertNotIn("沉", unresolved)


@unittest.skipUnless(OPENCC_AVAILABLE, _SKIP_OPENCC)
class TestOpenccPresentGlyphPath(unittest.TestCase):
    """opencc 導入環境での字形変換（悪 等）。未導入時は明示スキップ。"""

    def test_simplified_to_jis_via_opencc(self):
        """opencc あり: 恶→悪 に変換される。"""
        from normalize.zh2ja import shape_glyph
        out, _ = shape_glyph("恶")
        self.assertEqual(out, "悪")

    def test_simplified_to_jis_in_context_via_opencc(self):
        """opencc あり: 文中（恶梦）でも本文の簡体字が整形される。"""
        from normalize.zh2ja import shape_glyph
        out, _ = shape_glyph("見た恶梦")
        self.assertIn("悪梦", out)


class TestDetectWorksWithoutOpencc(unittest.TestCase):
    """残渣検出（detect）は opencc 無しでも完全に動作する（辞書ベース）。"""

    def test_find_zh_vocab_simp_without_opencc(self):
        from detect.zh2ja import find_zh_vocab
        with _BlockOpencc():
            findings = find_zh_vocab("缆を巻き取る。")
        simp = [f for f in findings if f.rule == "simp"]
        values = {f.value for f in simp}
        self.assertIn("缆", values)

    def test_find_ja_residue_without_opencc(self):
        from detect.ja2zh import find_ja_residue
        with _BlockOpencc():
            findings = find_ja_residue("这是一个テスト文本。")
        self.assertTrue(any(f.rule == "kana" for f in findings))


if __name__ == "__main__":
    unittest.main()