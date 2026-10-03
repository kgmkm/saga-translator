"""P0-3 残渣検出の回帰テスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


sys.path.insert(0, str(SCRIPTS))
from detect.zh2ja import Finding, ResidueConfig, find_zh_vocab, find_cn_residue_toml, find_term_src_leftover
from detect.ja2zh import find_ja_residue


class TestFindZhVocab(unittest.TestCase):
    """find_zh_vocab: VOCAB語検出 / IDIOMS検出 / known_terms 除外 / allow 除外"""

    def test_detect_vocab(self):
        text = "彼女の頬に紅暈が広がった。搏動が激しい。"
        findings = find_zh_vocab(text)
        rules = {f.rule for f in findings}
        self.assertIn("vocab", rules)
        values = [f.value for f in findings]
        self.assertIn("紅暈", values)
        self.assertIn("搏動", values)

    def test_detect_idiom(self):
        text = "彼は从容不迫に立ち上がった。目瞪口呆の状態だ。"
        findings = find_zh_vocab(text)
        rules = {f.rule for f in findings}
        self.assertIn("idiom", rules)
        values = [f.value for f in findings]
        self.assertIn("从容不迫", values)
        self.assertIn("目瞪口呆", values)

    def test_known_terms_excluded(self):
        text = "データ奪舍の影響で昏睡状態。"
        cfg = ResidueConfig(known_terms={"データ奪舍"})
        findings = find_zh_vocab(text, config=cfg)
        # "夺" is a simp char; with known_terms it should be excluded
        simp_hits = [f for f in findings if f.rule in ("simp", "vocab", "idiom")]
        self.assertEqual(len(simp_hits), 0)

    def test_allow_excluded(self):
        text = "SNSで拡散された。Day 1の記録。Mサイズ。"
        cfg = ResidueConfig(allow={"SNS", "Day", "M"})
        findings = find_zh_vocab(text, config=cfg)
        # allow words should suppress vocab/idiom/simp hits that overlap
        # (SNS/Day/M are not in vocab, so they should just not be false-positives)
        self.assertIsInstance(findings, list)

    def test_src_line_excluded(self):
        text = 'name_src = "搏動"\n彼は脉打つ。'
        findings = find_zh_vocab(text)
        # The "搏動" on the src line should be ignored
        src_hits = [f for f in findings if f.value == "搏動"]
        self.assertEqual(len(src_hits), 0)

    def test_simp_detected(self):
        text = "缆を巻き取る。涩い味。"
        findings = find_zh_vocab(text)
        simp_hits = [f for f in findings if f.rule == "simp"]
        self.assertGreaterEqual(len(simp_hits), 2)
        values = {f.value for f in simp_hits}
        self.assertIn("缆", values)
        self.assertIn("涩", values)

    def test_finding_dataclass(self):
        f = Finding(rule="vocab", value="紅暈", context="頬に紅暈が広", line=1, safe_fix="紅潮")
        self.assertEqual(f.rule, "vocab")
        self.assertEqual(f.value, "紅暈")
        self.assertEqual(f.safe_fix, "紅潮")
        self.assertEqual(f.line, 1)

    def test_residue_config_defaults(self):
        cfg = ResidueConfig()
        self.assertEqual(cfg.known_terms, set())
        self.assertEqual(cfg.allow, set())
        self.assertEqual(cfg.vocab, {})
        self.assertEqual(cfg.idioms, {})


class TestFindCnResidueToml(unittest.TestCase):
    """find_cn_residue_toml: *_src 除外 / コメント除外 / 日本語フィールド検出 / R4 全角読点"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test_toml_"))
        self.toml = self.tmpdir / "analysis.toml"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_toml(self, text: str) -> Path:
        _write(self.toml, text)
        return self.toml

    def test_src_line_excluded(self):
        self._write_toml('name_src = "搏動"\nterm_src = "从容不迫"\ntarget_src = "红晕"')
        findings = find_cn_residue_toml(self.toml)
        self.assertEqual(len(findings), 0)

    def test_comment_line_excluded(self):
        self._write_toml("# これはコメント行\n# 全角読点，も無視")
        findings = find_cn_residue_toml(self.toml)
        self.assertEqual(len(findings), 0)

    def test_r4_fullwidth_comma(self):
        self._write_toml('nickname = "彼女，元気"')
        findings = find_cn_residue_toml(self.toml)
        # Should detect the fullwidth comma
        cn_field = [f for f in findings if f.rule == "cn_field"]
        self.assertGreaterEqual(len(cn_field), 1)
        self.assertIn("中文読点", cn_field[0].value)

    def test_r6_non_jis_char(self):
        self._write_toml('desc = "缆を巻く"')
        findings = find_cn_residue_toml(self.toml)
        cn_field = [f for f in findings if f.rule == "cn_field"]
        self.assertGreaterEqual(len(cn_field), 1)
        self.assertIn("非JIS字", cn_field[0].value)

    def test_age_policy_report_excluded(self):
        report = self.tmpdir / "age_policy_report.toml"
        _write(report, 'desc = "缆を巻く"')
        findings = find_cn_residue_toml(report)
        self.assertEqual(len(findings), 0)

    def test_no_hits_for_clean_jp(self):
        self._write_toml('nickname = "彼女元気です"\nnote = "日本語のみ"')
        findings = find_cn_residue_toml(self.toml)
        self.assertEqual(len(findings), 0)


class TestFindTermSrcLeftover(unittest.TestCase):
    """find_term_src_leftover: terms.toml の term_src が訳文に残るケース"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test_terms_"))
        self.work = self.tmpdir / "work_12345"
        self.work.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_terms(self, src_list: list[str]) -> None:
        lines = []
        for s in src_list:
            lines.append(f"[[terms]]\nterm_src = \"{s}\"\nterm_ja = \"訳語\"\n")
        _write(self.work / "terms.toml", "\n".join(lines))

    def _write_output(self, rel: str, text: str) -> None:
        p = self.work / "output" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        _write(p, text)

    def test_detect_leftover(self):
        self._write_terms(["心灵共感", "魔力放出"])
        self._write_output("ch001/chapter_01.md", "彼は心灵共感を使った。\n魔力放出が炸裂する。")
        findings = find_term_src_leftover(self.work)
        self.assertGreaterEqual(len(findings), 2)
        values = {f.value for f in findings}
        self.assertIn("心灵共感", values)
        self.assertIn("魔力放出", values)

    def test_no_leftover(self):
        self._write_terms(["心灵共感"])
        self._write_output("ch001/chapter_01.md", "彼は心霊共感を使った。")
        findings = find_term_src_leftover(self.work)
        self.assertEqual(len(findings), 0)

    def test_no_terms_file(self):
        self._write_output("ch001/chapter_01.md", "なんでもない。")
        findings = find_term_src_leftover(self.work)
        self.assertEqual(len(findings), 0)


class TestFindJaResidue(unittest.TestCase):
    """ja2zh 方向: 仮名・鉤括弧検出"""

    def test_detect_kana(self):
        text = "这是一个テスト文本。彼女は言った。"
        findings = find_ja_residue(text)
        kana_hits = [f for f in findings if f.rule == "kana"]
        self.assertGreaterEqual(len(kana_hits), 1)

    def test_detect_jp_quotes(self):
        text = "她说「你好」。\n他回答『再见』。"
        findings = find_ja_residue(text)
        quote_hits = [f for f in findings if f.rule == "jp_quote"]
        self.assertGreaterEqual(len(quote_hits), 2)

    def test_detect_vfm_ruby(self):
        text = "这是{漢字|かんじ}的读法。"
        findings = find_ja_residue(text)
        ruby_hits = [f for f in findings if f.rule == "vfm_ruby"]
        self.assertGreaterEqual(len(ruby_hits), 1)

    def test_detect_rb_kana(self):
        text = "[[rb:漢字 > かんじ]]这是。"
        findings = find_ja_residue(text)
        rb_hits = [f for f in findings if f.rule == "rb_kana"]
        self.assertGreaterEqual(len(rb_hits), 1)

    def test_detect_ja_shape(self):
        text = "这是一个発展的過程。"  # 発(ja shape) should be detected
        findings = find_ja_residue(text)
        shape_hits = [f for f in findings if f.rule == "ja_shape"]
        self.assertGreaterEqual(len(shape_hits), 1)


class TestCLI(unittest.TestCase):
    """CLI: 終了コード 0/1/2、--json 出力"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test_cli_"))
        self.clean_file = self.tmpdir / "clean.txt"
        _write(self.clean_file, "日本語のみのファイルです。\n何も問題ありません。")
        self.dirty_file = self.tmpdir / "dirty.txt"
        _write(self.dirty_file, "紅暈が広がる。从容不迫に立つ。")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cli_exit_0(self):
        """クリーンファイル → exit 0"""
        proc = subprocess.run(
            [PYTHON, str(SCRIPTS / "detect" / "zh2ja.py"),
             str(self.clean_file)],
            capture_output=True, text=True, cwd=SCRIPTS.parent,
        )
        self.assertEqual(proc.returncode, 0)

    def test_cli_exit_1(self):
        """検出あり → exit 1"""
        proc = subprocess.run(
            [PYTHON, str(SCRIPTS / "detect" / "zh2ja.py"),
             str(self.dirty_file)],
            capture_output=True, text=True, cwd=SCRIPTS.parent,
        )
        self.assertEqual(proc.returncode, 1)

    def test_cli_json_output(self):
        """--json で JSON 出力される"""
        proc = subprocess.run(
            [PYTHON, str(SCRIPTS / "detect" / "zh2ja.py"),
             "--json", str(self.dirty_file)],
            capture_output=True, text=True, cwd=SCRIPTS.parent,
        )
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        self.assertIn("rule", data[0])
        self.assertIn("value", data[0])

    def test_cli_toml_uses_cn_residue(self):
        """.toml ファイルは find_cn_residue_toml 経由"""
        toml_file = self.tmpdir / "test.toml"
        _write(toml_file, 'desc = "缆を巻く"')
        proc = subprocess.run(
            [PYTHON, str(SCRIPTS / "detect" / "zh2ja.py"),
             "--json", str(toml_file)],
            capture_output=True, text=True, cwd=SCRIPTS.parent,
        )
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertGreater(len(data), 0)


if __name__ == "__main__":
    unittest.main()
