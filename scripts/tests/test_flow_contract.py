"""フロー契約の整合テストと実動作シナリオ契約テスト。

references/flow-contract.toml を正本として、
  (A) 契約と SKILL.md・実在スクリプトの整合を検査する（今すぐグリーン）。
  (B) 通常フロー完走（S7）は今すぐグリーン。
  (C) 未実装のゲート/挙動は expectedFailure で段階タグ付きで置く。
      （refactor-plan-2.md「expectedFailure の運用」に従う。実装段階で通常テストへ戻す）

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent  # リポジトリルート
SCRIPTS = BASE / "scripts"
SKILL_MD = BASE / "SKILL.md"
CONTRACT = BASE / "references" / "flow-contract.toml"
PYTHON = sys.executable

# 段階番号 → 実装予定の段階タグ（refactor-plan-2.md の段階表に対応）
STAGE_TAGS = {
    3: "stage3 実装で通常テストへ",
    4: "stage4 実装で通常テストへ",
    5: "stage5 実装で通常テストへ",
    6: "stage6 実装で通常テストへ",
}

# 各 missing 工程に対応する expectedFailure シナリオの ID を、テストソース内の
# コメント（# scenario: <id>）で照合するために使う。テスト側はこの接頭辞で
# シナリオ ID を埋め込む。
SCENARIO_TAG = "scenario:"

# 合成作品の原文（フロー完走用。架空・短い2章）。
_SYNTH_SRC = (
    "测试作品\n"                     # 題（合成）
    "作者名\n"                       # 著者（合成）
    "--------以下正文--------\n\n"   # ヘッダ区切り（ingest の検出形式）
    "第一章 开始\n\n"
    "天空阴沉沉的。林霖站在山门前，望着远方。风里带着寒意。\n\n"
    "低沉的钟声从远处传来，他握紧了手中的剑。雪开始落下。\n\n"
    "第二章 相遇\n\n"
    "第二天清晨，苏婉出现在客栈门口。她看起来有些疲惫。\n\n"
    "她对林霖微微一笑，说：你终于来了。我等你很久了。\n")

# 合成作品の確定訳（原文段落数と一致させ、align の文字数比を満たす）。
_SYNTH_JA = {
    "ch001": [
        "第一章　始まり",
        "空はどんよりと曇っていた。林霖は山門の前に立ち、遠くを見つめていた。風が冷たい。",
        "低い鐘の音が遠くから聞こえてきて、彼は手にした剣を強く握りしめた。雪が降り始める。",
    ],
    "ch002": [
        "第二章　出会い",
        "翌日の早朝、蘇婉（ソワン）が宿の門口に現れた。彼女はどこか疲れているようだった。",
        "彼女は林霖にほほえみかけて言った。「やっと来たね。ずっと待っていたんだよ。」",
    ],
}


def _run(*args: str) -> subprocess.CompletedProcess:
    """CLI をサブプロセスで起動する。"""
    return subprocess.run(
        [PYTHON, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _saga(*args: str) -> subprocess.CompletedProcess:
    return _run(str(SCRIPTS / "saga.py"), *args)


class _FlowFixture:
    """合成作品の作業ディレクトリを組み立てる共通フィクスチャ。"""

    def __init__(self, td: Path):
        self.td = td
        self.work: Path | None = None

    def ingest(self) -> Path:
        src = self.td / "book.txt"
        src.write_text(_SYNTH_SRC, encoding="utf-8")
        out_parent = self.td / "_saga"
        p = _run(str(SCRIPTS / "ingest.py"), str(src), "-o", str(out_parent), "--dst", "ja")
        if p.returncode != 0:
            raise AssertionError(f"ingest 失敗 rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        self.work = next(out_parent.iterdir())
        return self.work

    def split(self) -> None:
        p = _run(str(SCRIPTS / "split_chapters.py"), str(self.work))
        if p.returncode != 0:
            raise AssertionError(f"split_chapters 失敗 rc={p.returncode}\n{p.stderr}")

    def lock_prep(self) -> None:
        p = _run(str(SCRIPTS / "lock_prep.py"), str(self.work))
        if p.returncode != 0:
            raise AssertionError(f"lock_prep 失敗 rc={p.returncode}\n{p.stderr}")

    def analyze_commit(self, cid: str, bid: str, text: str) -> subprocess.CompletedProcess:
        d = self.td / f"delta_{cid}_{bid}.toml"
        d.write_text(
            f'block = "{cid}/{bid}"\n\n[[plot_delta]]\nblock = "{cid}/{bid}"\ntext = "{text}"\n',
            encoding="utf-8")
        return _saga("analyze-commit", str(self.work), "--delta", str(d))

    def place_draft(self, cid: str, paras: list[str], block: str = "b1") -> Path:
        draft = self.work / "output" / "_work" / f"{cid}_{block}_ja_numbered.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"[P{i + 1:03d}] {t}" for i, t in enumerate(paras)]
        draft.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
        return draft


class TestFlowContractIntegrity(unittest.TestCase):
    """(A) flow-contract.toml と SKILL.md・実在スクリプトの整合（今すぐグリーン）。"""

    @classmethod
    def setUpClass(cls):
        cls.contract_text = CONTRACT.read_text(encoding="utf-8")
        cls.contract = tomllib.loads(cls.contract_text)
        cls.flows = cls.contract.get("flow", [])
        cls.skill = SKILL_MD.read_text(encoding="utf-8")

    def test_contract_parses_and_has_flows(self):
        """契約がパースでき、工程が存在する。"""
        self.assertIsInstance(self.flows, list)
        self.assertGreater(len(self.flows), 0)

    def test_required_fields_present(self):
        """各エントリに必須フィールドが揃う。"""
        required = {"id", "skill_md", "requires", "ensures", "status"}
        ids = set()
        for f in self.flows:
            missing = required - set(f)
            self.assertFalse(missing, f"{f.get('id')}: 必須フィールド欠落 {sorted(missing)}")
            self.assertIn(f["status"], ("documented", "missing"))
            # script か scripts のどちらか、または手作業工程の marker が必要
            has_script = "script" in f or "scripts" in f
            has_marker = "marker" in f
            self.assertTrue(has_script or has_marker,
                            f"{f['id']}: script/marker のいずれも無い")
            self.assertNotIn(f["id"], ids, f"id 重複: {f['id']}")
            ids.add(f["id"])

    def _entry_scripts(self, f: dict) -> list[str]:
        if "scripts" in f:
            return list(f["scripts"])
        if f.get("script"):
            return [f["script"]]
        return []

    def test_script_paths_exist(self):
        """各エントリの script パスが実在する。"""
        for f in self.flows:
            for rel in self._entry_scripts(f):
                self.assertTrue((BASE / rel).is_file(),
                                f"{f['id']}: スクリプトが実在しない: {rel}")

    def test_skill_md_integrity(self):
        """documented の script 名は SKILL.md に出現し、missing は出現しない。"""
        missing_lines = []
        for f in self.flows:
            scripts = self._entry_scripts(f)
            # 手作業工程は marker を、スクリプト工程はファイル名を使う
            tokens = [s.split("/")[-1] for s in scripts] or (
                [f["marker"]] if "marker" in f else [])
            for tok in tokens:
                present = tok in self.skill
                if f["status"] == "documented" and not present:
                    missing_lines.append(f"{f['id']}: documented だが SKILL.md に {tok} が無い")
                if f["status"] == "missing" and present:
                    missing_lines.append(
                        f"{f['id']}: missing だが SKILL.md に {tok} が出現（status 更新漏れ）")
        self.assertEqual(missing_lines, [], "\n".join(missing_lines))

    def test_missing_entries_have_expected_failure_scenario(self):
        """missing の各工程に対応する expectedFailure シナリオがテスト内に存在する。"""
        src = Path(__file__).read_text(encoding="utf-8")
        missing_ids = [f["id"] for f in self.flows if f["status"] == "missing"]
        self.assertTrue(missing_ids, "missing 工程が無い（契約の想定と不一致）")
        absent = []
        for eid in missing_ids:
            tag = f"{SCENARIO_TAG} {eid}"
            if tag not in src:
                absent.append(eid)
        self.assertEqual(absent, [], f"expectedFailure シナリオ欠落: {absent}")

    def test_contract_lists_required_pipeline_entries(self):
        """最低限収録すべき工程（P0-1/P0-2 対象）を漏らしていない。"""
        expected = {
            "ingest", "normalize_images", "split_chapters", "lock_prep",
            "carry_analysis", "analysis", "titles", "translate",
            "status", "review", "assemble", "build_series_edition",
        }
        present = {f["id"] for f in self.flows}
        self.assertEqual(expected - present, set(),
                         f"契約から漏れた工程: {sorted(expected - present)}")

    def test_missing_entries_marked_stage(self):
        """missing の工程には復活予定の段階番号が付く。"""
        for f in self.flows:
            if f["status"] == "missing":
                self.assertIn("stage", f, f"{f['id']}: stage が無い")
                self.assertIsInstance(f["stage"], int)

    def test_documented_entry_requires_ensures(self):
        """documented の工程には終了条件（ensures）が記録されている。"""
        for f in self.flows:
            if f["status"] == "documented":
                self.assertTrue(f["ensures"], f"{f['id']}: ensures が空")


if __name__ == "__main__":
    unittest.main()


class _ScenarioBase(unittest.TestCase):
    """シナリオ共通の作業ディレクトリ管理。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.fx = _FlowFixture(self.td)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)


class TestFlowScenarioNormal(_ScenarioBase):
    """S7: 通常フロー完走（今すぐグリーン）。 # scenario: S7"""

    def test_normal_flow_pipeline(self):
        """ingest→split→lock_prep→analysis→translate→status→review→assemble。"""
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()

        # 分析: 各ブロックの plot delta を commit
        for cid in ("ch001", "ch002"):
            p = self.fx.analyze_commit(cid, "b1", "要点。")
            self.assertEqual(p.returncode, 0, p.stderr)

        # 翻訳: prepare → ドラフト配置 → commit
        for cid in ("ch001", "ch002"):
            p = _saga("prepare", str(self.fx.work), "--chapter", cid, "--block", "b1")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.fx.place_draft(cid, _SYNTH_JA[cid])
            p = _saga("commit", str(self.fx.work), "--chapter", cid, "--block", "b1")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue((self.fx.work / "output" / cid / "b1_ja.md").exists())

        # 進捗確認
        p = _saga("status", str(self.fx.work))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("翻訳 2/2 ブロック", p.stdout)

        # 検収
        p = _saga("review", str(self.fx.work))
        self.assertEqual(p.returncode, 0, p.stderr)

        # 結合（章題は分析で確定済みの前提——titles.toml を置く）
        tdir = self.fx.work / "analysis"
        tdir.mkdir(exist_ok=True)
        (tdir / "titles.toml").write_text(
            'title_src = "合成作品"\ntitle_ja = "合成作品（改題）"\n\n'
            '[[chapters]]\nid = "ch001"\nsrc = "第一章"\nja = "第一章　始まり"\n\n'
            '[[chapters]]\nid = "ch002"\nsrc = "第二章"\nja = "第二章　続き"\n', encoding="utf-8")
        p = _run(str(SCRIPTS / "assemble.py"), str(self.fx.work))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.fx.work / "output" / "全文_訳文.md").exists())
        self.assertTrue((self.fx.work / "output" / "目次.md").exists())


class TestFlowScenarioGates(_ScenarioBase):
    """未実装ゲートのシナリオ（expectedFailure＋段階タグ）。"""

    def test_s1_translate_blocked_without_analysis(self):
        """S1: 分析未実施のブロックで翻訳 prepare/commit が止まる。

        # scenario: S1 / stage5（P0-2）実装で通常テストへ
        """
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()
        # 分析は実行しない（committed にしない）
        p = _saga("prepare", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        self.assertNotEqual(p.returncode, 0, f"prepare が止まらない:\n{p.stdout}")
        # commit もドラフトを置いても止まる
        self.fx.place_draft("ch001", _SYNTH_JA["ch001"])
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        self.assertNotEqual(p.returncode, 0, f"commit が止まらない:\n{p.stdout}")

    def test_s2_invalid_delta_rejected(self):
        """S2: 無効な分析 delta（block 欠落・未知 block・スキーマ違反）で止まる。

        # scenario: S2 / stage3 実装で通常テストへ
        """
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()
        # block 欠落
        d1 = self.td / "d_noblock.toml"
        d1.write_text('[[plot_delta]]\ntext = "x"\n', encoding="utf-8")
        p = _saga("analyze-commit", str(self.fx.work), "--delta", str(d1))
        self.assertNotEqual(p.returncode, 0, f"block 欠落を通した:\n{p.stdout}")
        # 未知 block
        d2 = self.td / "d_unknown.toml"
        d2.write_text('block = "zzz/b9"\n\n[[plot_delta]]\nblock = "zzz/b9"\ntext = "x"\n',
                      encoding="utf-8")
        p = _saga("analyze-commit", str(self.fx.work), "--delta", str(d2))
        self.assertNotEqual(p.returncode, 0, f"未知 block を通した:\n{p.stdout}")

    @unittest.expectedFailure
    def test_s3_unresolved_blocks_commit_and_resolution_releases(self):
        """S3: 未裁定（残渣候補・要確認）では確定せず、裁定の記録で通る。

        # scenario: S3 / stage3-4 実装で通常テストへ
        """
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()
        self.fx.analyze_commit("ch001", "b1", "要点。")
        # 残渣候補（合成語）を含むドラフト → 未裁定では確定しない
        draft = self.fx.place_draft("ch001", [
            "第一章　始まり",
            "夜の星辰が輝く。どんよりと曇った空。",
            "低い鐘の音が響く。雪が降る。",
        ])
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        self.assertNotEqual(p.returncode, 0, "残渣候補が残るのに確定した")
        # 裁定の記録（段落ハッシュに紐づく）で通せること
        rpath = self.fx.work / "_work" / "residue_acks.jsonl"
        rpath.parent.mkdir(parents=True, exist_ok=True)
        import json
        rec = {
            "block": "ch001/b1",
            "draft_hash": hashlib.sha256(draft.read_bytes()).hexdigest()[:12],
            "paras": [2],
            "reason": "合成テストの裁定",
        }
        with open(rpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 0, f"裁定記録で通らない:\n{p.stdout}{p.stderr}")


class TestFlowScenarioIntegrity(_ScenarioBase):
    """データ整合・復旧・改訂のシナリオ（expectedFailure＋段階タグ）。"""

    @unittest.expectedFailure
    def test_s4_missing_image_detected_before_assemble(self):
        """S4: 画像参照の欠落が assemble 前に検出されて止まる。

        # scenario: S4 / stage6（P0-1）実装で通常テストへ
        """
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()
        self.fx.analyze_commit("ch001", "b1", "要点。")
        self.fx.place_draft("ch001", _SYNTH_JA["ch001"])
        _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        # 実在しない画像参照を確定出力へ注入
        out = self.fx.work / "output" / "ch001" / "b1_ja.md"
        out.write_text(out.read_text(encoding="utf-8") + "\n![img](../images/missing-9999.png)\n",
                       encoding="utf-8")
        # 章題は確定済みにしておく（画像欠落だけを見る）
        tdir = self.fx.work / "analysis"
        tdir.mkdir(exist_ok=True)
        (tdir / "titles.toml").write_text(
            'title_src = "合成作品"\ntitle_ja = "合成作品（改題）"\n\n'
            '[[chapters]]\nid = "ch001"\nsrc = "第一章"\nja = "第一章　始まり"\n', encoding="utf-8")
        p = _run(str(SCRIPTS / "assemble.py"), str(self.fx.work))
        self.assertNotEqual(p.returncode, 0, f"画像欠落なのに assemble が通った:\n{p.stdout}")

    def test_s5_interrupted_commit_recovers_record_only(self):
        """S5: 保存失敗・中断後の復旧が「記録だけ補完」で完結する。

        # scenario: S5 / stage4 実装で通常テストへ
        """
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()
        self.fx.analyze_commit("ch001", "b1", "要点。")
        self.fx.place_draft("ch001", _SYNTH_JA["ch001"])
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = self.fx.work / "output" / "ch001" / "b1_ja.md"
        h1 = hashlib.sha256(out.read_bytes()).hexdigest()

        # 中断を模擬: 出力は在るが meta の status が emitted でない状態へ戻す
        meta_txt = (self.fx.work / "chapters_meta.toml").read_text(encoding="utf-8")
        (self.fx.work / "chapters_meta.toml").write_text(
            meta_txt.replace('status = "emitted"', 'status = "pending"'), encoding="utf-8")
        # 翻訳 meta 記録だけを補完して exit 0 で完結すること
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 0, f"復旧が記録補完で完結しない:\n{p.stdout}{p.stderr}")
        # 確定出力を壊さない
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), h1)

    def test_s6_recommit_splits_recovery_and_revision(self):
        """S6: 確定済みブロックの再実行が復旧/改訂に分かれ、由来を上書きしない。

        # scenario: S6 / stage4 実装で通常テストへ
        """
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()
        self.fx.analyze_commit("ch001", "b1", "要点。")
        self.fx.place_draft("ch001", _SYNTH_JA["ch001"])
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1",
                  "--model", "modelA", "--prompt-version", "v1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        meta = tomllib.loads((self.fx.work / "chapters_meta.toml").read_text(encoding="utf-8"))
        first = meta["chapters"][0]["blocks"][0].get("translation", {})
        self.assertEqual(first.get("model"), "modelA")

        # 新しいドラフトを置いて --revise 無しで再実行 → 復旧:
        # 由来の記録は上書きされず、確定出力も変わらず、exit 0 で --revise を案内する
        self.fx.place_draft("ch001", [
            "第一章　始まり",
            "空は曇っていた。風が冷たい。",
            "低い鐘が響く。雪が降る。",
        ])
        out = self.fx.work / "output" / "ch001" / "b1_ja.md"
        out_h1 = hashlib.sha256(out.read_bytes()).hexdigest()
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1",
                  "--model", "modelB", "--prompt-version", "v2")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        meta2 = tomllib.loads((self.fx.work / "chapters_meta.toml").read_text(encoding="utf-8"))
        blk2 = meta2["chapters"][0]["blocks"][0]
        # 由来の記録（初回の modelA）が残っていること
        self.assertEqual(blk2.get("translation", {}).get("model"), "modelA",
                         "初回の由来が上書きされた")
        # 未使用の新ドラフトで確定出力を上書きしないこと
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), out_h1,
                         "--revise 無しで確定出力が上書きされた")
        # 改訂の案内が出ること
        self.assertIn("--revise", p.stdout + p.stderr)

        # --revise を付けた再実行 → 改訂として記録され、旧記録は履歴に残る
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1",
                  "--model", "modelB", "--prompt-version", "v2", "--revise")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        meta3 = tomllib.loads((self.fx.work / "chapters_meta.toml").read_text(encoding="utf-8"))
        blk = meta3["chapters"][0]["blocks"][0]
        # 由来が改訂後の記録へ更新される
        self.assertEqual(blk.get("translation", {}).get("model"), "modelB")
        # 改訂の履歴が残ること（初回の記録は上書きされず保持される）
        self.assertTrue(
            blk.get("translation_history") or blk.get("revision"),
            "改訂の履歴が記録されていない")


class TestFlowScenarioMissingSteps(_ScenarioBase):
    """SKILL.md から消えた工程（missing）の復活シナリオ（expectedFailure＋段階タグ）。"""

    @unittest.expectedFailure
    def test_normalize_images_step_in_flow(self):
        """S8: 取り込み後の画像正規化が通常フローで機能する（多章・参照更新一体）。

        # scenario: normalize_images / stage6（P0-1）実装で通常テストへ
        """
        self.fx.ingest()
        self.fx.split()
        work = self.fx.work
        # 非JIS漢字名の画像を置き、複数章から参照する
        img = work / "output" / "images"
        img.mkdir(parents=True, exist_ok=True)
        (img / "插图一.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        for cid in ("ch001", "ch002"):
            d = work / "output" / cid
            d.mkdir(parents=True, exist_ok=True)
            (d / "b1_ja.md").write_text("![插图](../images/插图一.png)\n", encoding="utf-8")
        p = _run(str(SCRIPTS / "normalize_images.py"), str(work), "--apply")
        self.assertEqual(p.returncode, 0, p.stderr)
        # 正規化後: 非JIS漢字を含むファイル名が残らない
        leftovers = [f.name for f in img.iterdir() if any("\u4e00" <= c <= "\u9fff" for c in f.name)]
        self.assertEqual(leftovers, [], f"非JIS漢字名の画像が残っている: {leftovers}")
        # 全章の参照が一括で更新されている
        for cid in ("ch001", "ch002"):
            body = (work / "output" / cid / "b1_ja.md").read_text(encoding="utf-8")
            self.assertNotIn("插图一.png", body, f"{cid}: 参照が更新されていない")

    def test_titles_gate_before_assemble(self):
        """S9: titles.toml が無ければ assemble は止まる（原題のまま黙って出さない）。

        # scenario: titles / stage5 で titles ゲート実装済み（通常テスト）。
        #   SKILL.md への手順復活と flow-contract の documented 化も同時に行う
        """
        self.fx.ingest()
        self.fx.split()
        self.fx.lock_prep()
        self.fx.analyze_commit("ch001", "b1", "要点。")
        self.fx.place_draft("ch001", _SYNTH_JA["ch001"])
        p = _saga("commit", str(self.fx.work), "--chapter", "ch001", "--block", "b1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # titles.toml を作らない状態で assemble
        self.assertFalse((self.fx.work / "analysis" / "titles.toml").exists())
        p = _run(str(SCRIPTS / "assemble.py"), str(self.fx.work))
        self.assertNotEqual(p.returncode, 0,
                            f"titles 無しで assemble が通った:\n{p.stdout}")
        # 回避フラグでだけ通せること
        p = _run(str(SCRIPTS / "assemble.py"), str(self.fx.work), "--allow-src-title")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()