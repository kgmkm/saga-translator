"""analyze-commit の受入検証と「分析済み」記録の定義（spec: docs/phase3-taskG-analysis-schema.md）。

CLI をサブプロセスで実行し、終了コード・stdout/stderr・chapters_meta の実データを検査する。
合成データのみを使用する。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tomllib

SCRIPTS = Path(__file__).resolve().parent.parent
SAGA = SCRIPTS / "saga.py"


def _write_meta_new(work: Path, ch_count: int = 1, blk_count: int = 3) -> dict:
    """最小 work フィクスチャを TOML で直接作成（作品固有情報なし）。"""
    (work / "chapters").mkdir(parents=True)
    (work / "analysis").mkdir()
    (work / "_work").mkdir(parents=True)
    (work / "manifest.toml").write_text('title = "T"\nlang = "zh"\n', encoding="utf-8")

    lines = ['[work]', 'title = "T"', 'lang = "zh"', '']
    chapters = []
    for ci in range(1, ch_count + 1):
        cid = f"ch{ci:03d}"
        blocks = []
        for bi in range(1, blk_count + 1):
            bid = f"b{bi}"
            blocks.append({"id": bid, "paras": [(bi - 1) * 4 + 1, bi * 4], "n_chars": 40})
        chapters.append({"id": cid, "title": "", "status": "pending", "blocks": blocks})

    for ch in chapters:
        lines.append("[[chapters]]")
        lines.append(f'id = "{ch["id"]}"')
        lines.append('title = ""')
        lines.append('status = "pending"')
        for b in ch["blocks"]:
            lines.append(f"[[chapters.blocks]]")
            lines.append(f'id = "{b["id"]}"')
            lines.append(f'paras = [{b["paras"][0]}, {b["paras"][1]}]')
            lines.append(f'n_chars = {b["n_chars"]}')
        lines.append("")
    (work / "chapters_meta.toml").write_text("\n".join(lines), encoding="utf-8")

    for ci in range(1, ch_count + 1):
        cid = f"ch{ci:03d}"
        plines = []
        for i in range(blk_count * 4):
            plines.append("[[paras]]")
            plines.append(f"n = {i + 1}")
            plines.append(f'text = "原文{i + 1}。"')
            plines.append("")
        (work / "chapters" / f"{cid}.paras.toml").write_text("\n".join(plines), encoding="utf-8")

    return chapters[0]


def _ensure_analysis_files(work: Path) -> None:
    (work / "analysis" / "characters-[ネタバレあり].toml").write_text(
        "characters = []\n", encoding="utf-8")
    (work / "analysis" / "terms.toml").write_text("terms = []\n", encoding="utf-8")
    (work / "analysis" / "plot-[ネタバレあり].toml").write_text("entries = []\n", encoding="utf-8")


def _make_work(tmp: Path) -> Path:
    work = tmp / "work"
    _write_meta_new(work)
    _ensure_analysis_files(work)
    return work


def _write_delta(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _run_ac(work: Path, delta: Path) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(SAGA), "analyze-commit", str(work), "--delta", str(delta)],
        capture_output=True, text=True, cwd=str(SCRIPTS))
    return r.returncode, r.stdout + r.stderr


def _load_meta(work: Path) -> dict:
    with open(work / "chapters_meta.toml", "rb") as f:
        return tomllib.load(f)


def _block(meta: dict, key: str) -> dict:
    for ch in meta["chapters"]:
        for b in ch.get("blocks", []):
            if f'{ch["id"]}/{b["id"]}' == key:
                return b
    raise AssertionError(f"block {key} not found")


def _meta_bytes(work: Path) -> bytes:
    return (work / "chapters_meta.toml").read_bytes()


class AnalysisSchemaBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ac_schema_"))
        self.work = _make_work(self.tmp)

    def tearDown(self):
        import shutil
        lock = self.work / "_work" / ".lock"
        if lock.exists():
            lock.unlink()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _delta(self, name: str = "delta.toml") -> Path:
        return self.tmp / name


class TestRejections(AnalysisSchemaBase):
    """受入検証の拒否5ケース → exit 1 かつ chapters_meta 無変更。"""

    def _assert_rejected(self, delta_text: str) -> str:
        d = self._delta()
        _write_delta(d, delta_text)
        before = _meta_bytes(self.work)
        code, out = _run_ac(self.work, d)
        self.assertEqual(code, 1, f"exit 1 になるべき:\n{out}")
        self.assertEqual(before, _meta_bytes(self.work),
                         "拒否時は chapters_meta を一切変更してはいけない")
        return out

    def test_reject_missing_block(self):
        """1. block 欠落 → 拒否。"""
        out = self._assert_rejected('[[plot_delta]]\ntext = "要点。"\n')
        self.assertIn("block", out)

    def test_reject_unknown_block(self):
        """2. 未知 block（chapters_meta に無い）→ 拒否。"""
        out = self._assert_rejected('block = "ch999/b9"\n[[plot_delta]]\nblock = "ch999/b9"\ntext = "x"\n')
        self.assertIn("存在しません", out)

    def test_reject_schema_violation(self):
        """3. スキーマ違反（text が文字列でない / match_key 欠落）→ 拒否。"""
        out = self._assert_rejected('block = "ch001/b1"\n[[plot_delta]]\nblock = "ch001/b1"\ntext = 3\n')
        self.assertIn("text", out)
        # match_value 欠落
        out2 = self._assert_rejected('block = "ch001/b1"\n[[characters_delta]]\nmatch_key = "name_src"\n')
        self.assertIn("match_value", out2)

    def test_reject_out_of_scope_plot(self):
        """4. plot の block が delta の block と不一致 → 拒否。"""
        out = self._assert_rejected('block = "ch001/b1"\n[[plot_delta]]\nblock = "ch001/b2"\ntext = "x"\n')
        self.assertIn("一致しません", out)

    def test_reject_empty_delta_without_no_changes(self):
        """5. 差分が空で no_changes の明示が無い → 拒否。"""
        out = self._assert_rejected('block = "ch001/b1"\n')
        self.assertIn("no_changes", out)


class TestAcceptance(AnalysisSchemaBase):
    """正常系・no_changes 明示・ハッシュ記録。"""

    def test_accept_success_records_hash(self):
        """正常 delta → hash 付きで「分析済み」を記録。"""
        d = self._delta()
        _write_delta(d, 'block = "ch001/b1"\n'
                        '[[plot_delta]]\nblock = "ch001/b1"\ntext = "要点。"\n')
        code, out = _run_ac(self.work, d)
        self.assertEqual(code, 0, out)
        b = _block(_load_meta(self.work), "ch001/b1")
        self.assertTrue(b.get("analysis_committed_at"))
        self.assertIn("analysis_src_hash", b)
        self.assertEqual(len(str(b["analysis_src_hash"])), 12)
        self.assertNotIn("analysis_no_changes", b)

    def test_accept_no_changes_flag(self):
        """空差分 + no_changes = true → committed を立て、フラグを記録。"""
        d = self._delta()
        _write_delta(d, 'block = "ch001/b2"\nno_changes = true\n')
        code, out = _run_ac(self.work, d)
        self.assertEqual(code, 0, out)
        b = _block(_load_meta(self.work), "ch001/b2")
        self.assertTrue(b.get("analysis_committed_at"))
        self.assertIs(b.get("analysis_no_changes"), True)
        self.assertIn("analysis_src_hash", b)

    def test_hash_changes_when_source_changes(self):
        """ブロック原文が変わると analysis_src_hash が変わる。"""
        d = self._delta()
        _write_delta(d, 'block = "ch001/b1"\n[[plot_delta]]\nblock = "ch001/b1"\ntext = "要点。"\n')
        code, _ = _run_ac(self.work, d)
        self.assertEqual(code, 0)
        h1 = _block(_load_meta(self.work), "ch001/b1")["analysis_src_hash"]

        # 元段落2を書き換える（b1 は段落 1..4 を範囲に持つ）
        paras_path = self.work / "chapters" / "ch001.paras.toml"
        txt = paras_path.read_text(encoding="utf-8")
        txt = txt.replace('text = "原文2。"', 'text = "原文2改。"')
        paras_path.write_text(txt, encoding="utf-8")

        # no_changes 再コミットで hash を更新
        _write_delta(d, 'block = "ch001/b1"\nno_changes = true\n')
        code, out = _run_ac(self.work, d)
        self.assertEqual(code, 0, out)
        h2 = _block(_load_meta(self.work), "ch001/b1")["analysis_src_hash"]
        self.assertNotEqual(h1, h2, "原文が変われば hash も変わる")


if __name__ == "__main__":
    unittest.main()