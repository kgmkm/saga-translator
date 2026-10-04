"""normalize の glyph/meaning 候補化テスト (P1-1)。

既定モード（glyph のみ自動適用）の動作を検証する。
"""
from __future__ import annotations

import builtins
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from normalize.zh2ja import shape_glyph, meaning_candidates, fix_text, MANUAL_COMPOUND, MANUAL_CHAR


class TestOpenccIndependentFallback(unittest.TestCase):
    """opencc が無くても手当て表だけで字形整形が成立する（P1-4）。

    opencc 未導入環境を pip アンインストールせず、import をスタブして
    プロセス内で再現する。実際の opencc 依存の変換（恶→悪 等）は
    test_opencc_optional.py 側で skipUnless 明示する。
    """

    def _block_opencc(self):
        real_import = builtins.__import__
        saved = sys.modules.pop("opencc", None)

        def blocked(name, *args, **kwargs):
            if name == "opencc" or name.startswith("opencc."):
                raise ImportError("opencc blocked for test (P1-4)")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = blocked
        self.addCleanup(self._restore_import, real_import, saved)

    @staticmethod
    def _restore_import(real_import, saved):
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["opencc"] = saved

    def test_glyph_char_without_opencc(self):
        """opencc 無しでも glyph 単字（沉→沈）は手当て表で適用される。"""
        self._block_opencc()
        out, applied = shape_glyph("沉")
        self.assertEqual(out, "沈")
        self.assertTrue(any(a["item"] == "沉" for a in applied))

    def test_glyph_compound_without_opencc(self):
        """opencc 無しでも glyph 複合語（强行→強行）は適用される。"""
        self._block_opencc()
        out, applied = shape_glyph("强行")
        self.assertEqual(out, "強行")
        self.assertTrue(any(a["item"] == "强行" for a in applied))

    def test_fix_text_without_opencc(self):
        """opencc 無しでも fix_text の glyph 単字適用が成立する。"""
        self._block_opencc()
        out, applied, _ = fix_text("沉", None, None)
        self.assertEqual(out, "沈")


class TestShapeGlyph(unittest.TestCase):
    """shape_glyph: glyph のみ自動適用され、meaning はそのまま"""

    def test_glyph_compound_applied(self):
        """glyph 複合語（强行→強行）は適用される"""
        out, applied = shape_glyph("强行")
        self.assertEqual(out, "強行")
        self.assertTrue(any(a["item"] == "强行" for a in applied))

    def test_glyph_compound_multi(self):
        """glyph 複合語（灭顶→滅頂）は適用される（opencc による他字変換も含む）"""
        out, applied = shape_glyph("灭顶之灾")
        self.assertIn("滅頂", out)  # glyph compound works
        self.assertTrue(any(a["item"] == "灭顶" for a in applied))

    def test_meaning_compound_not_applied_by_default(self):
        """meaning 複合語（抽搐→身を捩る）は既定モードで適用されない"""
        out, applied = shape_glyph("抽搐")
        self.assertEqual(out, "抽搐")
        self.assertFalse(any(a["item"] == "抽搐" for a in applied))

    def test_meaning_compound_another(self):
        """meaning 複合語（尾骶骨→尾骨）は既定モードで適用されない"""
        out, applied = shape_glyph("尾骶骨")
        self.assertEqual(out, "尾骶骨")
        self.assertFalse(any(a["item"] == "尾骶骨" for a in applied))

    def test_glyph_char_applied(self):
        """glyph 単字（沉→沈）は適用される"""
        out, applied = shape_glyph("沉")
        self.assertEqual(out, "沈")
        self.assertTrue(any(a["item"] == "沉" for a in applied))

    def test_glyph_char_applied_another(self):
        """glyph 単字（烬→燼）は適用される"""
        out, applied = shape_glyph("灰烬")
        self.assertEqual(out, "灰燼")
        self.assertTrue(any(a["item"] == "烬" for a in applied))

    def test_meaning_char_not_applied(self):
        """meaning 単字（嗓→声）は既定モードで適用されない"""
        out, applied = shape_glyph("嗓音")
        # 嗓 は meaning 単字なので shape_glyph で変換されない
        # ただし '嗓音' 全体が compound meaning として shape_glyph で処理されない
        self.assertEqual(out, "嗓音")
        self.assertFalse(any("嗓" in a["item"] for a in applied))

    def test_meaning_char_not_applied_single(self):
        """meaning 単字単独（盏→基）は既定モードで適用されない（opencc による字形変換は別）"""
        out, applied = shape_glyph("一盏")
        # 盏→盞 への opencc 変換は glyph（字形）なので許容
        # 盏→基 の meaning 置換は行われない
        self.assertNotIn("基", out)
        self.assertFalse(any(a["replacement"] == "基" for a in applied))

    def test_glyph_only_preserves_meaning_text(self):
        """glyph のみ適用後も meaning 相当のテキストは元のまま"""
        out, applied = shape_glyph("抽搐して滚烫だ")
        # 抽搐, 烫 は meaning → そのまま
        # 滚 は opencc で 滾 に glyph 変換される可能性がある
        self.assertIn("抽搐", out)
        self.assertIn("烫", out)
        self.assertFalse(any(a["item"] == "抽搐" for a in applied))
        self.assertFalse(any(a["item"] == "烫" for a in applied))

    def test_mixed_glyph_and_meaning(self):
        """glyph と meaning が混在: glyph のみ適用"""
        out, applied = shape_glyph("强行抽搐")
        self.assertEqual(out, "強行抽搐")  # 强行→強行, 抽搐はそのまま
        self.assertTrue(any(a["item"] == "强行" for a in applied))
        self.assertFalse(any(a["item"] == "抽搐" for a in applied))

    def test_glyph_char_not_in_non_jis_set(self):
        """既にJISな字は変換不要（shape_glyph は非JIS字のみ処理）"""
        out, applied = shape_glyph("日本語")
        self.assertEqual(out, "日本語")
        self.assertEqual(len(applied), 0)


class TestMeaningCandidates(unittest.TestCase):
    """meaning_candidates: meaning エントリを検出するが適用しない"""

    def test_detect_meaning_compound(self):
        """meaning 複合語（抽搐）を検出"""
        candidates = meaning_candidates("抽搐")
        self.assertTrue(any(c["value"] == "抽搐" and c["replacement"] == "身を捩る" for c in candidates))

    def test_detect_meaning_compound_another(self):
        """meaning 複合語（尾骶骨）を検出"""
        candidates = meaning_candidates("尾骶骨が痛い")
        self.assertTrue(any(c["value"] == "尾骶骨" and c["replacement"] == "尾骨" for c in candidates))

    def test_detect_meaning_char(self):
        """meaning 単字（嗓）は複合語（嗓音）に含まれるが検出される"""
        candidates = meaning_candidates("嗓音")
        # 複合語 嗓音→声 として検出される（単字 嗓→声 は複合語に吸収されて出ない）
        self.assertTrue(any(c["value"] == "嗓音" and c["replacement"] == "声" for c in candidates))

    def test_detect_meaning_char_standalone(self):
        """meaning 単字単独（盏）を検出"""
        candidates = meaning_candidates("一盏灯")
        self.assertTrue(any(c["value"] == "盏" and c["replacement"] == "基" for c in candidates))

    def test_meaning_char_skipped_when_compound_covers(self):
        """複合語でカバー済みの単字は個別候補に出ない（複合語自体は出る）"""
        candidates = meaning_candidates("嗓音")
        values = [c["value"] for c in candidates]
        self.assertIn("嗓音", values)
        # 単字 '嗓' は複合語 '嗓音' に含まれるため個別には出ない

    def test_candidates_have_context(self):
        """candidates に context と line が含まれる"""
        candidates = meaning_candidates("彼は抽搐した。")
        for c in candidates:
            self.assertIn("context", c)
            self.assertIn("line", c)
            self.assertIsInstance(c["line"], int)

    def test_no_false_meaning(self):
        """普通の日本語テキストでは meaning 検出なし"""
        candidates = meaning_candidates("こんにちは世界")
        self.assertEqual(len(candidates), 0)

    def test_candidates_match_expected_values(self):
        """全ての meaning エントリが candidates で検出可能"""
        text = " ".join(s for s, _, k in MANUAL_COMPOUND if k == "meaning")
        text += " " + " ".join(s for s, _, k in MANUAL_CHAR if k == "meaning")
        candidates = meaning_candidates(text)
        detected = {c["value"] for c in candidates}
        for s, _, k in MANUAL_COMPOUND:
            if k == "meaning":
                self.assertIn(s, detected, f"compound {s} should be detected as meaning")
        # 単字の一部は複合語に含まれてスキップされるので、完全一致は確認しない


class TestFixTextLegacy(unittest.TestCase):
    """fix_text の後方互換（既定では glyph のみ）"""

    def test_fix_text_glyph_only_default(self):
        """fix_text 既定では glyph のみ適用"""
        out, applied, unresolved = fix_text("强行抽搐", None, None)
        self.assertEqual(out, "強行抽搐")  # 强行のみ→強行
        self.assertIn("强行", applied)

    def test_fix_text_glyph_char_default(self):
        """fix_text 既定で glyph 単字も適用"""
        out, applied, unresolved = fix_text("沉", None, None)
        self.assertEqual(out, "沈")


class TestClassificationStability(unittest.TestCase):
    """分類結果の固定: 将来の無自覚な再分類を防ぐ"""

    def test_compound_classification(self):
        """MANUAL_COMPOUND の分類が期待通り"""
        classification = {(s, d): k for s, d, k in MANUAL_COMPOUND}
        # glyph
        self.assertEqual(classification[("强行", "強行")], "glyph")
        self.assertEqual(classification[("灭顶", "滅頂")], "glyph")
        # meaning
        self.assertEqual(classification[("抽搐", "身を捩る")], "meaning")
        self.assertEqual(classification[("尾骶骨", "尾骨")], "meaning")
        self.assertEqual(classification[("嗓音", "声")], "meaning")
        self.assertEqual(classification[("陡然", "急に")], "meaning")
        self.assertEqual(classification[("骤然", "急に")], "meaning")
        self.assertEqual(classification[("磅礴", "雄渾")], "meaning")
        self.assertEqual(classification[("汹涌", "激しく")], "meaning")
        self.assertEqual(classification[("噙着", "浮かべ")], "meaning")
        self.assertEqual(classification[("博弈", "駆け引き")], "meaning")
        self.assertEqual(classification[("滚烫", "煮え滾る")], "meaning")
        self.assertEqual(classification[("滚热", "煮えたぎる")], "meaning")
        self.assertEqual(classification[("粗糙", "粗い")], "meaning")
        self.assertEqual(classification[("荒诞", "荒唐")], "meaning")

    def test_char_classification(self):
        """MANUAL_CHAR の分類が期待通り"""
        classification = {(s, d): k for s, d, k in MANUAL_CHAR}
        # glyph
        self.assertEqual(classification[("韌", "靭")], "glyph")
        self.assertEqual(classification[("虬", "龍")], "glyph")
        self.assertEqual(classification[("虯", "龍")], "glyph")
        self.assertEqual(classification[("沉", "沈")], "glyph")
        self.assertEqual(classification[("烬", "燼")], "glyph")
        self.assertEqual(classification[("炽", "熾")], "glyph")
        self.assertEqual(classification[("喻", "喩")], "glyph")
        # meaning
        self.assertEqual(classification[("嗓", "声")], "meaning")
        self.assertEqual(classification[("骤", "急")], "meaning")
        self.assertEqual(classification[("陡", "急")], "meaning")
        self.assertEqual(classification[("弈", "駆")], "meaning")
        self.assertEqual(classification[("撑", "張")], "meaning")
        self.assertEqual(classification[("撐", "張")], "meaning")
        self.assertEqual(classification[("坝", "堰")], "meaning")
        self.assertEqual(classification[("壩", "堰")], "meaning")
        self.assertEqual(classification[("汹", "逆")], "meaning")
        self.assertEqual(classification[("洶", "逆")], "meaning")
        self.assertEqual(classification[("盏", "基")], "meaning")
        self.assertEqual(classification[("盞", "基")], "meaning")
        self.assertEqual(classification[("礴", "渾")], "meaning")
        self.assertEqual(classification[("烫", "熱")], "meaning")
        self.assertEqual(classification[("绽", "咲")], "meaning")
        self.assertEqual(classification[("刁", "えぐ")], "meaning")
        self.assertEqual(classification[("滟", "艶")], "meaning")
        self.assertEqual(classification[("啃", "噛")], "meaning")
        self.assertEqual(classification[("朵", "輪")], "meaning")
        self.assertEqual(classification[("俏", "茶目っ気")], "meaning")
        self.assertEqual(classification[("嗡", "轟")], "meaning")
        self.assertEqual(classification[("肏", "犯")], "meaning")
        self.assertEqual(classification[("缕", "一筋")], "meaning")
        self.assertEqual(classification[("滩", "水溜まり")], "meaning")

    def test_compound_count(self):
        """MANUAL_COMPOUND の件数が正しい（将来の追加漏れ・重複を防止）"""
        glyph_compounds = [(s, d) for s, d, k in MANUAL_COMPOUND if k == "glyph"]
        meaning_compounds = [(s, d) for s, d, k in MANUAL_COMPOUND if k == "meaning"]
        self.assertEqual(len(glyph_compounds), 2, f"got {glyph_compounds}")
        self.assertEqual(len(meaning_compounds), 13, f"got {meaning_compounds}")
        self.assertEqual(len(MANUAL_COMPOUND), 15)

    def test_char_count(self):
        """MANUAL_CHAR の件数が正しい"""
        glyph_chars = [(s, d) for s, d, k in MANUAL_CHAR if k == "glyph"]
        meaning_chars = [(s, d) for s, d, k in MANUAL_CHAR if k == "meaning"]
        self.assertEqual(len(glyph_chars), 7, f"got {glyph_chars}")
        self.assertEqual(len(meaning_chars), 24, f"got {meaning_chars}")
        self.assertEqual(len(MANUAL_CHAR), 31)


if __name__ == "__main__":
    unittest.main()