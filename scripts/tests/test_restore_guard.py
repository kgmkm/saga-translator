# -*- coding: utf-8 -*-
"""画像退避 sentinel の安全化（#25）と restore_parallel の照合強化（#26）の回帰テスト。

テスト・コメントには作品固有の情報（作品名・実ファイル名・キャラ名）を含めない。
例は合成データのみ。

#25: 画像リンクの退避で開きと閉じに同じ1文字（PUA）を使っていると、本文に
     その文字が含まれるときに退避したリンクと本文を取り違える。開き・閉じを
     別の文字にし、対象テキストに含まれないことを実行時に確認する。

#26: restore_parallel は行の位置でバックアップを写すため、境界がずれていても
     総行数が同じなら誤って復元する。段落 ID（[Pn]）と原文/訳文の役割で照合し、
     一致が取れない・曖昧な場合は復元せず停止する。read_text(newline=...) は
     Python 3.12 で使えないため open(newline="") を使う。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

REPO_ROOT = SCRIPTS.parent
RESTORE = SCRIPTS / "restore_parallel.py"

# 画像退避に使われていた PUA 文字（本文に含めて取り違えを誘発する）
PUA = "\ue000"


def _write(p: Path, text: str, newline: str = "\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\r\n", "\n").replace("\n", newline))


class TestSentinelSafety(unittest.TestCase):
    """#25: 開き・閉じの別文字化と本文混入時の取り違え防止。"""

    def test_sentinel_pair_distinct_and_absent(self):
        """開きと閉じは別文字で、どちらも対象テキストに含まれない。"""
        from normalize.zh2ja import _pick_sentinels
        text = "本文" + PUA + "。"
        o, c = _pick_sentinels(text)
        self.assertNotEqual(o, c)
        self.assertNotIn(o, text)
        self.assertNotIn(c, text)
        for ch in (o, c):
            self.assertTrue(0xE000 <= ord(ch) <= 0xF8FF, "PUA 以外が選ばれた")

    def test_pick_sentinels_avoids_chars_in_text(self):
        """本文に PUA 文字が含まれても、それを避けて別の文字を選ぶ。"""
        from normalize.zh2ja import _pick_sentinels
        text = "\ue000本文\ue001あり。"
        o, c = _pick_sentinels(text)
        self.assertNotIn(o, ("\ue000", "\ue001"))
        self.assertNotIn(c, ("\ue000", "\ue001"))

    def test_pick_sentinels_raises_when_pool_exhausted(self):
        """使える PUA 候補が無ければ推測せずエラーで止まる。"""
        from normalize.zh2ja import _pick_sentinels
        all_pua = "".join(chr(c) for c in range(0xE000, 0xF8FF + 1))
        with self.assertRaises(ValueError):
            _pick_sentinels(all_pua)

    def test_shape_glyph_roundtrip_with_sentinel_char_in_body(self):
        """本文に sentinel 文字そのものを含んでも、退避→整形→復元が正確に完走する。

        旧実装は開き=閉じ=PUA だったため、本文の同文字と退避リンクを
        取り違えていた（回帰）。
        """
        from normalize.zh2ja import shape_glyph
        src = f"本文{PUA}に同文字。![]({PUA}.png) 沉む"
        out, applied = shape_glyph(src)
        # 本文中の sentinel 文字はそのまま残る
        self.assertIn(f"本文{PUA}に同文字。", out)
        # 画像リンクは元のパスのまま復元される（PUA 取り違えなし）
        self.assertIn(f"![]({PUA}.png)", out)
        # 本文側の字形整形（沉→沈）は効く
        self.assertIn("沈", out)
        self.assertTrue(any(a["item"] == "沉" for a in applied))

    def test_shape_glyph_two_images_preserved(self):
        """複数の画像リンクを退避しても、索引が正しく復元される。"""
        from normalize.zh2ja import shape_glyph
        a = "images/one.png"
        b = "images/two.png"
        src = f"![]( {a} ) と ![]({b}) 沉"
        out, _ = shape_glyph(src)
        self.assertIn(a, out)
        self.assertIn(b, out)


class TestRestoreParallelGuard(unittest.TestCase):
    """#26: 段落 ID・役割で照合し、曖昧なら停止する。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="restore_guard_"))
        self.work = self.tmp / "work"
        self.backup = self.tmp / "backup"
        # 作品IDサブディレクトリを持つ形（work/1001/output/ch001_parallel.md）
        self.work_id = "1001"
        (self.work / self.work_id / "output").mkdir(parents=True, exist_ok=True)
        self.par = self.work / self.work_id / "output" / "ch001_parallel.md"
        self.bak = self.backup / self.work_id / "output" / "ch001_parallel.md"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, apply_: bool = False) -> subprocess.CompletedProcess:
        args = [sys.executable, str(RESTORE), str(self.work), str(self.backup)]
        if apply_:
            args.append("--apply")
        return subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", cwd=str(REPO_ROOT))

    # 合成データ: 原文ブロック（> 始まり）と訳文ブロックの交互構造
    NORMAL = (
        "> [P1] 原文一。\n"
        "> 折り返し行一。\n"
        "\n"
        "[P1] 訳文一。\n"
        "\n"
        "> [P2] 原文二。\n"
        "\n"
        "[P2] 訳文二。\n"
    )

    def test_normal_restore_apply(self):
        """①正常復元: 原文ブロックの改変がバックアップから戻る。"""
        _write(self.bak, self.NORMAL)
        _write(self.par, self.NORMAL.replace("折り返し行一。", "改変された行。"))
        r = self._run(apply_=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.par.read_text(encoding="utf-8"), self.NORMAL)

    def test_boundary_shift_stops(self):
        """②境界ずれ（総行数同じ・段落境界違い）→ 誤復元せず停止。"""
        _write(self.bak, self.NORMAL)
        # P1 の折り返しを原文に吸収し、空行位置をずらす＝総行数は同じ
        shifted = (
            "> [P1] 原文一。\n"
            "> 折り返し行一。\n"
            "[P1] 訳文一。\n"
            "\n"
            "> [P2] 原文二。\n"
            "\n"
            "\n"
            "[P2] 訳文二。\n"
        )
        _write(self.par, shifted)
        r = self._run(apply_=True)
        self.assertNotEqual(r.returncode, 0, "停止すべきところ通った")
        self.assertEqual(self.par.read_text(encoding="utf-8"), shifted,
                         "誤復元して書き換えた")

    def test_role_swap_stops(self):
        """③役割（原文/訳文）の取り違え → 停止。"""
        _write(self.bak, self.NORMAL)
        # 訳文ブロックを '>' 始まりに変える（役割が食い違う）
        swapped = self.NORMAL.replace("[P1] 訳文一。", "> [P1] 訳文一。")
        _write(self.par, swapped)
        r = self._run(apply_=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.par.read_text(encoding="utf-8"), swapped)

    def test_id_mismatch_stops(self):
        """段落 ID が食い違えば停止する。"""
        _write(self.bak, self.NORMAL)
        _write(self.par, self.NORMAL.replace("> [P2] 原文二。", "> [P3] 原文二。"))
        r = self._run(apply_=True)
        self.assertNotEqual(r.returncode, 0)

    def test_crlf_difference_restores(self):
        """④行末コード差（CRLF/LF）があっても段落 ID 一致なら復元できる。"""
        _write(self.bak, self.NORMAL, newline="\r\n")
        # 現在側を改変しつつ LF で保存
        modified = self.NORMAL.replace("折り返し行一。", "改変された行。")
        _write(self.par, modified, newline="\n")
        r = self._run(apply_=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.par.read_text(encoding="utf-8")
        self.assertIn("折り返し行一。", got)
        self.assertNotIn("改変された行。", got)

    def test_missing_backup_stops(self):
        """バックアップが無ければ停止する（NO-BACKUP）。"""
        _write(self.par, self.NORMAL)
        r = self._run(apply_=True)
        self.assertNotEqual(r.returncode, 0)

    def test_no_drift_is_clean_success(self):
        """差分が無ければ成功（exit 0）。"""
        _write(self.bak, self.NORMAL)
        _write(self.par, self.NORMAL)
        r = self._run(apply_=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestReadDoesNotUseNewlineKwarg(unittest.TestCase):
    """#26: Python 3.12 でも動く読み方（read_text(newline=...) を使わない）。"""

    def test_no_read_text_newline_kwarg(self):
        src = RESTORE.read_text(encoding="utf-8")
        self.assertNotIn("read_text(encoding=\"utf-8\", newline=", src)
        self.assertIn("newline=\"\"", src)

    def test_read_helper_preserves_crlf(self):
        """read() は改行コードを変換せずに読む。"""
        sys.path.insert(0, str(SCRIPTS))
        import importlib
        rp = importlib.import_module("restore_parallel")
        importlib.reload(rp)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "crlf.md"
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write("a\r\nb\r\n")
            self.assertEqual(rp.read(p), "a\r\nb\r\n")


if __name__ == "__main__":
    unittest.main()