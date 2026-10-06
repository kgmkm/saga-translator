"""分析ゲート・分析 prepare の修正・分析側残渣検査のテスト。

spec: docs/phase5-taskN1-gates.md（P0-2 / #4b / P0-4・#8）。

CLI をサブプロセスで実行し、終了コード・出力・chapters_meta の実データを検査する。
通常フローの work（ingest → split → lock_prep）を合成データで組み立てて使う。
作品固有情報は含めない。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
SAGA = SCRIPTS / "saga.py"
PYTHON = sys.executable

# 合成作品の原文（架空・2章）。各章は見出し＋本文2段落＝1ブロック。
_SYNTH_SRC = (
    "测试作品\n"                     # 題（合成）
    "作者名\n"                       # 著者（合成）
    "--------以下正文--------\n\n"   # ヘッダ区切り（ingest の検出形式）
    "第一章 开始\n\n"
    "天空阴沉沉的。张三站在山门前，望着远方。风里带着寒意。\n\n"
    "低沉的钟声从远处传来，他握紧了手中的剑。雪开始落下。\n\n"
    "第二章 相遇\n\n"
    "第二天清晨，李四出现在客栈门口。她看起来有些疲惫。\n\n"
    "她对张三微微一笑，说：你终于来了。我等你很久了。\n")

# 確定まで通る合成訳（段落数・文字数比を満たす）。
_DRAFT_CH001 = [
    "第一章　始まり",
    "空はどんよりと曇っていた。张三は山門の前に立ち、遠くを見つめていた。風が冷たい。",
    "低い鐘の音が遠くから聞こえてきて、彼は手にした剣を強く握りしめた。雪が降り始める。",
]


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _saga(*args: str) -> subprocess.CompletedProcess:
    return _run(str(SAGA), *args)


class _GateFixture:
    """合成作品の work を通常フローで組み立てる（ingest → split → lock_prep）。"""

    def __init__(self, td: Path):
        self.td = td
        self.work: Path = self._build()

    def _build(self) -> Path:
        src = self.td / "book.txt"
        src.write_text(_SYNTH_SRC, encoding="utf-8")
        out_parent = self.td / "_saga"
        p = _run(str(SCRIPTS / "ingest.py"), str(src), "-o", str(out_parent), "--dst", "ja")
        if p.returncode != 0:
            raise AssertionError(f"ingest 失敗 rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        work = next(out_parent.iterdir())
        for tool in ("split_chapters.py", "lock_prep.py"):
            p = _run(str(SCRIPTS / tool), str(work))
            if p.returncode != 0:
                raise AssertionError(f"{tool} 失敗 rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        return work

    def analyze_commit(self, cid: str, bid: str, delta_text: str) -> subprocess.CompletedProcess:
        d = self.td / f"delta_{cid}_{bid}.toml"
        d.write_text(delta_text, encoding="utf-8")
        return _saga("analyze-commit", str(self.work), "--delta", str(d))

    def analyze(self, cid: str, bid: str) -> subprocess.CompletedProcess:
        """clean な分析（plot 1件）を commit する。"""
        return self.analyze_commit(
            cid, bid,
            f'block = "{cid}/{bid}"\n\n[[plot_delta]]\nblock = "{cid}/{bid}"\ntext = "要点。"\n')

    def analyze_no_changes(self, cid: str, bid: str) -> subprocess.CompletedProcess:
        return self.analyze_commit(cid, bid, f'block = "{cid}/{bid}"\nno_changes = true\n')

    def place_draft(self, cid: str, bid: str, paras: list[str]) -> Path:
        draft = self.work / "output" / "_work" / f"{cid}_{bid}_ja_numbered.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"[P{i + 1:03d}] {t}" for i, t in enumerate(paras)]
        draft.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
        return draft

    def prepare(self, *extra: str) -> subprocess.CompletedProcess:
        return _saga("prepare", str(self.work), *extra)

    def commit(self, cid: str, bid: str, *extra: str) -> subprocess.CompletedProcess:
        return _saga("commit", str(self.work), "--chapter", cid, "--block", bid, *extra)

    def meta(self) -> dict:
        with open(self.work / "chapters_meta.toml", "rb") as f:
            return tomllib.load(f)

    def block(self, cid: str, bid: str) -> dict:
        for ch in self.meta().get("chapters", []):
            for b in ch.get("blocks", []):
                if f"{ch['id']}/{b['id']}" == f"{cid}/{bid}":
                    return b
        raise AssertionError(f"block {cid}/{bid} not found")

    def set_undecided(self) -> None:
        """manifest に series_decision = undecided（トップレベル）を記録する。"""
        man = self.work / "manifest.toml"
        man.write_text('series_decision = "undecided"\n' + man.read_text(encoding="utf-8"),
                       encoding="utf-8")

    def analysis_file(self, name: str) -> Path:
        return self.work / "analysis" / name


class _GateTestBase(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="analysis_gate_"))
        self.fx = _GateFixture(self.td)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)


class TestTranslationGate(_GateTestBase):
    """翻訳 prepare / commit の分析ゲート（P0-2）。"""

    def test_prepare_blocked_without_analysis(self):
        """未分析ブロックの prepare は止まる（exit 非ゼロ・案内つき）。"""
        p = self.fx.prepare("--chapter", "ch001", "--block", "b1")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("未分析", out)
        self.assertIn("--no-analysis", out)
        # 記録は何も足さない
        self.assertNotIn("analysis_skipped", self.fx.block("ch001", "b1"))

    def test_prepare_no_analysis_passes_and_records_skip(self):
        """--no-analysis 明示で prepare が通り、analysis_skipped を記録する。"""
        p = self.fx.prepare("--chapter", "ch001", "--block", "b1", "--no-analysis")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        ctx = self.fx.work / "translation_input" / "ch001_b1_translation.md"
        self.assertTrue(ctx.exists())
        self.assertIs(self.fx.block("ch001", "b1").get("analysis_skipped"), True)

    def test_commit_blocked_without_analysis(self):
        """未分析ブロックの commit はドラフトがあっても止まる。"""
        self.fx.place_draft("ch001", "b1", _DRAFT_CH001)
        p = self.fx.commit("ch001", "b1")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("未分析", out)
        self.assertFalse((self.fx.work / "output" / "ch001" / "b1_ja.md").exists())
        self.assertNotIn("analysis_skipped", self.fx.block("ch001", "b1"))

    def test_commit_no_analysis_passes_and_records_skip(self):
        """--no-analysis 明示で commit が通り、analysis_skipped を記録する。"""
        self.fx.place_draft("ch001", "b1", _DRAFT_CH001)
        p = self.fx.commit("ch001", "b1", "--no-analysis")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertTrue((self.fx.work / "output" / "ch001" / "b1_ja.md").exists())
        self.assertIs(self.fx.block("ch001", "b1").get("analysis_skipped"), True)

    def test_analyzed_block_passes_prepare_and_commit(self):
        """分析済み（committed）ブロックは prepare / commit が通る。"""
        p = self.fx.analyze("ch001", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.fx.prepare("--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.fx.place_draft("ch001", "b1", _DRAFT_CH001)
        p = self.fx.commit("ch001", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.fx.work / "output" / "ch001" / "b1_ja.md").exists())

    def test_no_changes_record_counts_as_analyzed(self):
        """no_changes の明示記録も「分析済み」として扱う。"""
        p = self.fx.analyze_no_changes("ch001", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIs(self.fx.block("ch001", "b1").get("analysis_no_changes"), True)
        p = self.fx.prepare("--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_skip_is_required_at_commit_too(self):
        """prepare で省略しても、commit でも --no-analysis の明示が必要。"""
        p = self.fx.prepare("--chapter", "ch001", "--block", "b1", "--no-analysis")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.fx.place_draft("ch001", "b1", _DRAFT_CH001)
        p = self.fx.commit("ch001", "b1")
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("未分析", p.stdout + p.stderr)
        p = self.fx.commit("ch001", "b1", "--no-analysis")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_stale_analysis_hash_warns_but_continues(self):
        """原文が変わった分析記録は警告のみ（prepare / commit とも続行）。"""
        p = self.fx.analyze("ch001", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # 対象ブロックの原文を書き換える
        paras = self.fx.work / "chapters" / "ch001.paras.toml"
        paras.write_text(
            paras.read_text(encoding="utf-8").replace("天空阴沉沉的", "天空阴沉沉"),
            encoding="utf-8")
        p = self.fx.prepare("--chapter", "ch001", "--block", "b1")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("analysis_src_hash", out)
        self.assertIn("警告", out)
        # commit 側でも警告が出て、続行する
        self.fx.place_draft("ch001", "b1", _DRAFT_CH001)
        p = self.fx.commit("ch001", "b1")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("analysis_src_hash", out)


class TestUndecidedGate(_GateTestBase):
    """series_decision = undecided は分析も翻訳も止める（P0-2）。"""

    def test_prepare_stopped_when_undecided(self):
        self.fx.set_undecided()
        p = self.fx.prepare()
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("undecided", out)
        self.assertIn("carry_analysis", out)
        self.assertIn("--standalone", out)

    def test_commit_stopped_when_undecided(self):
        self.fx.set_undecided()
        self.fx.place_draft("ch001", "b1", _DRAFT_CH001)
        p = self.fx.commit("ch001", "b1")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("undecided", out)
        self.assertFalse((self.fx.work / "output" / "ch001" / "b1_ja.md").exists())

    def test_analyze_commit_stopped_when_undecided(self):
        self.fx.set_undecided()
        p = self.fx.analyze("ch001", "b1")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("undecided", out)
        self.assertNotIn("analysis_committed_at", self.fx.block("ch001", "b1"))


class TestAnalysisPrepareSelection(_GateTestBase):
    """分析モードの prepare は「未分析の先頭ブロック」を選ぶ（#4b）。"""

    def test_selects_first_unanalyzed_and_uses_analysis_context_name(self):
        p = self.fx.analyze("ch001", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.fx.prepare("--mode", "analysis")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("ch002_b1_analysis.md", out)
        ctx = self.fx.work / "translation_input" / "ch002_b1_analysis.md"
        self.assertTrue(ctx.exists())
        # translation 名のパスを返さない
        self.assertFalse((self.fx.work / "translation_input" / "ch002_b1_translation.md").exists())
        self.assertIn("モード: analysis", ctx.read_text(encoding="utf-8"))

    def test_reports_completion_when_all_analyzed(self):
        for cid in ("ch001", "ch002"):
            p = self.fx.analyze(cid, "b1")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.fx.prepare("--mode", "analysis")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("分析は完了", out)

    def test_comment_only_record_is_unanalyzed(self):
        """コメントのみの分析 TOML は「未分析」扱いのまま選択される。"""
        f = self.fx.analysis_file("plot-[ネタバレあり].toml")
        f.write_text("# まだ記録なし\n", encoding="utf-8")
        p = self.fx.prepare("--mode", "analysis")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("ch001_b1_analysis.md", out)
        self.assertTrue((self.fx.work / "translation_input" / "ch001_b1_analysis.md").exists())


class TestAnalysisResidueCheck(_GateTestBase):
    """analyze-commit の保存前残渣検査（P0-4 / #8）。"""

    def _dirty_delta(self) -> str:
        return ('block = "ch001/b1"\n\n[[characters_delta]]\n'
                'match_key = "name_src"\nmatch_value = "测试甲"\n'
                'name_ja = "テスト甲"\nrole = "测试角色"\n')

    def test_residue_in_target_field_rejected(self):
        """対象言語の欄に中国語が残れば拒否（保存しない・記録もしない）。"""
        chars = self.fx.analysis_file("characters-[ネタバレあり].toml")
        before = chars.read_bytes() if chars.exists() else None
        p = self.fx.analyze_commit("ch001", "b1", self._dirty_delta())
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("残渣", out)
        self.assertIn("非JIS字", out)
        self.assertIn("role", out)  # どの欄かを特定して報告する
        self.assertIn("--force-residue", out)
        after = chars.read_bytes() if chars.exists() else None
        self.assertEqual(after, before, "拒否時に分析ファイルを書いてはいけない")
        self.assertNotIn("analysis_committed_at", self.fx.block("ch001", "b1"))

    def test_src_fields_excluded_structurally(self):
        """*_src（任意の接尾辞形を含む）の値は原文側として除外する。"""
        delta = ('block = "ch001/b1"\n\n[[characters_delta]]\n'
                 'match_key = "name_src"\nmatch_value = "测试甲"\n'
                 'alias_src = "测试乙"\nname_ja = "テスト甲"\nrole = "主人公"\n')
        p = self.fx.analyze_commit("ch001", "b1", delta)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        chars = tomllib.loads(self.fx.analysis_file("characters-[ネタバレあり].toml")
                              .read_text(encoding="utf-8"))
        entry = chars["characters"][0]
        self.assertEqual(entry.get("alias_src"), "测试乙")
        self.assertEqual(entry.get("name_ja"), "テスト甲")

    def test_force_residue_passes_and_records(self):
        """--force-residue は警告つきで通し、block 記録に残す。"""
        p = self.fx.analyze_commit("ch001", "b1", self._dirty_delta())
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        d = self.td / "delta_force.toml"
        d.write_text(self._dirty_delta(), encoding="utf-8")
        p = _saga("analyze-commit", str(self.fx.work), "--delta", str(d), "--force-residue")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("--force-residue", out)
        self.assertIs(self.fx.block("ch001", "b1").get("analysis_forced_residue"), True)
        self.assertIsNotNone(self.fx.block("ch001", "b1").get("analysis_committed_at"))

    def test_whole_provisional_checked_not_only_delta(self):
        """差分だけでなく、反映後の仮の全体（既存の欄）も検査する。"""
        chars = self.fx.analysis_file("characters-[ネタバレあり].toml")
        chars.write_text('[[characters]]\nname_src = "测试甲"\nrole = "测试角色"\n',
                         encoding="utf-8")
        p = self.fx.analyze("ch001", "b1")  # plot のみの clean な delta
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn("残渣", out)


class TestProtectedOverwrite(_GateTestBase):
    """analyze-commit --allow-protected-overwrite と監査記録（P1-5）。"""

    def _seed(self) -> None:
        """既存エントリ（role / name_ja つき）を作る。"""
        p = self.fx.analyze_commit(
            "ch001", "b1",
            'block = "ch001/b1"\n\n[[characters_delta]]\n'
            'match_key = "name_src"\nmatch_value = "テスト甲"\n'
            'name_ja = "テスト甲"\nrole = "主人公"\n')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def _entry(self) -> dict:
        chars = tomllib.loads(self.fx.analysis_file("characters-[ネタバレあり].toml")
                              .read_text(encoding="utf-8"))
        return chars["characters"][0]

    def _edits(self) -> list[dict]:
        p = self.fx.work / "_work" / "analysis_edits.jsonl"
        if not p.is_file():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _delta(self, body: str, name: str = "delta_po.toml") -> Path:
        d = self.td / name
        d.write_text('block = "ch001/b1"\n\n[[characters_delta]]\n'
                     'match_key = "name_src"\nmatch_value = "テスト甲"\n' + body,
                     encoding="utf-8")
        return d

    def test_flag_overwrites_and_records_audit(self):
        """フラグ指定時は記述系キー（role）を本体へ上書きし、監査記録を1行残す。"""
        self._seed()
        d = self._delta('role = "語り手"\n')
        p = _saga("analyze-commit", str(self.fx.work), "--delta", str(d),
                  "--allow-protected-overwrite")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        entry = self._entry()
        self.assertEqual(entry["role"], "語り手")
        self.assertNotIn("proposed", entry)
        edits = self._edits()
        self.assertEqual(len(edits), 1, edits)
        rec = edits[0]
        self.assertEqual(rec["key"], "role")
        self.assertEqual(rec["old"], "主人公")
        self.assertEqual(rec["new"], "語り手")
        self.assertTrue(rec["ts"])
        self.assertEqual(rec["file"], "characters-[ネタバレあり].toml")

    def test_without_flag_role_goes_to_proposed(self):
        """フラグ無しは従来どおり proposed に退避（回帰）。監査記録も作らない。"""
        self._seed()
        d = self._delta('role = "語り手"\n', "delta_noflag.toml")
        p = _saga("analyze-commit", str(self.fx.work), "--delta", str(d))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        entry = self._entry()
        self.assertEqual(entry["role"], "主人公")
        self.assertIn("role", entry.get("proposed", ""))
        self.assertEqual(self._edits(), [])

    def test_flag_does_not_touch_name_ja(self):
        """フラグ指定でも name_ja は proposed のまま（rename_terms の領分。上書き不可）。"""
        self._seed()
        d = self._delta('name_ja = "テスト甲改"\n', "delta_name.toml")
        p = _saga("analyze-commit", str(self.fx.work), "--delta", str(d),
                  "--allow-protected-overwrite")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        entry = self._entry()
        self.assertEqual(entry["name_ja"], "テスト甲")
        self.assertIn("name_ja", entry.get("proposed", ""))
        self.assertEqual(self._edits(), [], "name_ja は監査記録の対象にもしない")


class TestAnalysisPrepareContextStyle(_GateTestBase):
    """分析 prepare のコンテキストは full 固定（P1-4）。"""

    def test_analysis_prepare_includes_character_not_in_body(self):
        """本文に名前が出ない人物も分析コンテキストには載る（compact で落ちない）。"""
        chars = self.fx.analysis_file("characters-[ネタバレあり].toml")
        chars.write_text('[[characters]]\nname_src = "隠れ甲"\nname_ja = "隠れ甲"\n'
                         'role = "脇役"\n', encoding="utf-8")
        p = self.fx.prepare("--mode", "analysis")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        ctx = (self.fx.work / "translation_input" / "ch001_b1_analysis.md"
               ).read_text(encoding="utf-8")
        self.assertIn("隠れ甲", ctx)
        # compact の出力ではない（full 固定）
        self.assertNotIn("[context] compact:", p.stderr)


class TestPrepareResidueWarning(_GateTestBase):
    """prepare --mode analysis は既存 analysis の残渣を stderr 1行で警告する（P0-3）。"""

    def test_warning_one_line_with_residue(self):
        chars = self.fx.analysis_file("characters-[ネタバレあり].toml")
        chars.write_text('[[characters]]\nname_src = "甲"\nrole = "测试角色"\n',
                         encoding="utf-8")
        p = self.fx.prepare("--mode", "analysis")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = [l for l in p.stderr.splitlines()
                 if l.startswith("[prepare]") and "残渣" in l]
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertIn("lint", lines[0])

    def test_no_warning_without_residue(self):
        chars = self.fx.analysis_file("characters-[ネタバレあり].toml")
        chars.write_text('[[characters]]\nname_src = "甲"\nrole = "主人公"\n',
                         encoding="utf-8")
        p = self.fx.prepare("--mode", "analysis")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("残渣", p.stderr)


if __name__ == "__main__":
    unittest.main()
