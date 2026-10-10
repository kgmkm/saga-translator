"""P0積み残し分の回帰テスト（G38/G42/G43・命名codenameのみ記録）。

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import saga  # noqa: E402
from qa_translation import run_qa  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


class TestNoiseVariant(unittest.TestCase):
    def test_reported_fragments_are_noise(self):
        for f in ("见时", "小腹", "小心", "的时"):
            self.assertTrue(saga._is_noise_variant(f), f)

    def test_function_word_only_is_noise(self):
        self.assertTrue(saga._is_noise_variant("我的"))
        self.assertTrue(saga._is_noise_variant("了一个"))

    def test_real_names_are_not_noise(self):
        for n in ("张三", "白狼", "阿雪", "Alice"):
            self.assertFalse(saga._is_noise_variant(n), n)

    def test_single_char_is_not_noise(self):
        self.assertFalse(saga._is_noise_variant("的"))


class TestWorkLevelNoiseGuard(unittest.TestCase):
    def _work(self, base: Path) -> tuple[Path, dict, list]:
        work = base / "work"
        _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
        _write(work / "chapters_meta.toml",
               '[[chapters]]\nid = "ch001"\nblocks = [{id="b1", paras=[1,2]}]\n')
        # 小心(登録名)と小腹(ノイズ片)・阿雨(正規の類似形)を本文に置く
        _write(work / "chapters" / "ch001.paras.toml",
               '[[paras]]\nn = 1\ntext = "小心说小腹疼，小腹真的很疼。"\n\n'
               '[[paras]]\nn = 2\ntext = "阿雪和阿雨来了，阿雨笑了。"\n\n')
        chars = [{"name_src": "小心", "name_ja": "慎重",
                  "aliases": [], "variant_candidates": []},
                 {"name_src": "阿雪", "name_ja": "阿雪",
                  "aliases": [], "variant_candidates": []}]
        chars_data: dict = {"characters": chars}
        meta = {"chapters": [{"id": "ch001",
                              "blocks": [{"id": "b1", "paras": [1, 2]}]}]}
        return work, meta, chars_data

    def test_noise_fragment_not_recorded_but_real_variant_is(self):
        with tempfile.TemporaryDirectory() as td:
            work, meta, chars_data = self._work(Path(td))
            logs, changed = saga._record_voice_candidates(
                work, "ch001/b1", meta, [(Path("characters-x.toml"), chars_data)])
            from _lib import variant_candidates_of
            got = [cand.get("name_src")
                   for entry in chars_data["characters"]
                   for cand in variant_candidates_of(entry)]
            got += [cand.get("name_src")
                    for cand in chars_data.get("variant_candidates", [])]
            self.assertNotIn("小腹", got, logs)
            # 正規の類似形（阿雨⇔阿雪）は残る＝除外が選択的であること
            self.assertTrue(any("阿雨" in (g or "") or "阿雪" in (g or "")
                                for g in got),
                            f"正規候補まで消えたら過剰除外: {got}")


class TestDeltaWorkRelative(unittest.TestCase):
    def test_relative_resolves_under_work(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "w"
            delta = work / "_work" / "ch001_b1_delta.toml"
            _write(delta, "no_changes = true\n")
            got = saga.resolve_delta_path(work, "_work/ch001_b1_delta.toml")
            self.assertEqual(got, delta)

    def test_absolute_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "w"
            target = Path(td) / "else.toml"
            _write(target, "no_changes = true\n")
            self.assertEqual(saga.resolve_delta_path(work, str(target)), target)


class TestNamingCodenameOnly(unittest.TestCase):
    def test_codename_only_counts_as_decided(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "w"
            _write(work / "_house" / "house_style.toml",
                   '[naming_policy]\ncodename = "katakana"\n')
            pol = saga._naming_policy_of(work)
            self.assertIsNotNone(pol)
            self.assertEqual(pol.get("codename"), "katakana")

    def test_absent_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(saga._naming_policy_of(Path(td) / "w"))


def _qa_work(base: Path, aliases_line: str) -> Path:
    work = base / "work"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml",
           '[[chapters]]\nid = "ch001"\nblocks = [{id="b1", paras=[1,1]}]\n')
    _write(work / "chapters" / "ch001.paras.toml",
           '[[paras]]\nn = 1\ntext = "阿雪笑了。"\n')
    _write(work / "analysis" / "terms.toml", "terms = []\n")
    _write(work / "analysis" / "characters-[ネタバレあり].toml",
           '[[characters]]\nname_src = "阿雪"\nname_ja = "アセツ"\n'
           f"{aliases_line}\n")
    _write(work / "analysis" / "plot-[ネタバレあり].toml", "entries = []\n")
    return work


def _draft(work: Path, text: str) -> Path:
    d = work / "output" / "_work" / "ch001_b1_ja_numbered.md"
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(f"[P001] {text}\n", encoding="utf-8", newline="\n")
    return d


class TestQaKanaAliasGuard(unittest.TestCase):
    """G42: かな別名の誤登録が QA FAIL にしないこと。漢字別名の検出は残す。"""

    def test_kana_alias_does_not_fail(self):
        with tempfile.TemporaryDirectory() as td:
            work = _qa_work(Path(td), 'aliases = ["アリス"]')
            report, _ = run_qa(work, "ch001", "b1", _draft(work, "アリスは笑った。"))
            check = report["checks"]["leftover_source_names"]
            self.assertTrue(check["pass"], check)

    def test_chinese_alias_still_detected(self):
        with tempfile.TemporaryDirectory() as td:
            work = _qa_work(Path(td), 'aliases = ["阿雪妹"]')
            report, _ = run_qa(work, "ch001", "b1", _draft(work, "阿雪妹は笑った。"))
            check = report["checks"]["leftover_source_names"]
            self.assertFalse(check["pass"])


if __name__ == "__main__":
    unittest.main()
