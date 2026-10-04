"""命名方針の工程化（P0-5）のテスト。

spec: docs/phase10-taskS1-naming.md（統合レビュー §3 P0-5）。

① lock_prep の固有名の扱い一覧（分類と混在の要確認）
② [naming_policy] 記録後に要確認が消える
③ carry_analysis の [naming_policy] 引き継ぎ
④ saga.py status の未決定警告（未訳あり時のみ）
⑤ 混在を「誤り」と断定しない（出力文言は「要確認」止まり）

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402
import lock_prep  # noqa: E402

CARRY = SCRIPTS / "carry_analysis.py"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _run(script: Path, args: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, cwd=str(SCRIPTS))
    return r.returncode, r.stdout + r.stderr


def _make_naming_work(base: Path) -> Path:
    """音訳名（既訳カタカナ）と漢語名（漢字のまま）が混在する最小の作品（zh→ja）。"""
    work = base / "_saga" / "99999"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml",
           '[[chapters]]\nid = "ch001"\nblocks = ["b1"]\n')
    _write(work / "chapters" / "ch001.paras.toml", """[[paras]]
n = 1
text = "张三笑了。李四也笑了。张三说：「走吧。」"

[[paras]]
n = 2
text = "李四和张三去了图书馆。图书馆很安静。"

[[paras]]
n = 3
text = "李四又想起张三的话。张三说过：「明天见。」"
""")
    _write(work / "analysis" / "characters-[ネタバレあり].toml", """[[characters]]
name_src = "张三"
name_ja = "チョウサン"
first_person = "私"

[[characters]]
name_src = "李四"
name_ja = "李四"
""")
    return work


def _run_lock_prep(work: Path) -> tuple[int, str, str]:
    """lock_prep を実行し (終了コード, stdout, lock_prep.md) を返す。"""
    buf = io.StringIO()
    old_out = sys.stdout
    try:
        sys.stdout = buf
        rc = lock_prep.main([str(work)])
    finally:
        sys.stdout = old_out
    md = (work / "_work" / "lock_prep.md").read_text(encoding="utf-8")
    return rc, buf.getvalue(), md


def _write_policy(work: Path, style: str = "mixed", scope: str = "series",
                  note: str = "テスト用の方針") -> Path:
    """house_style.toml（親側）に [naming_policy] を書く。"""
    path = work.parent / "_house" / "house_style.toml"
    _write(path, f'[naming_policy]\nstyle = "{style}"\nnote = "{note}"\n'
                 f'scope = "{scope}"\ndecided_at = "2026-10-05T00:00:00Z"\n')
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s1_naming_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# ① lock_prep の固有名の扱い一覧（分類と混在の要確認）
# ---------------------------------------------------------------------------

class TestNamingList(_Base):
    def test_category_rule(self):
        """分類の単純規則（カタカナ=音訳 / 漢字=漢語 / それ以外）。"""
        self.assertEqual(lock_prep._naming_category("チョウサン"), "音訳（カタカナ）")
        self.assertEqual(lock_prep._naming_category("李四"), "漢語のまま")
        self.assertEqual(lock_prep._naming_category("イ音"), "それ以外")
        self.assertEqual(lock_prep._naming_category("A1"), "それ以外")
        self.assertEqual(lock_prep._naming_category(""), "それ以外")

    def test_list_groups_and_mixed_confirm(self):
        """一覧が区分ごとに分かれ、混在なら「要確認」が出る（policy 未決定）。"""
        work = _make_naming_work(self.tmp)
        rc, out, md = _run_lock_prep(work)
        self.assertEqual(rc, 0, out)
        # md: 区分ごとに分けて並ぶ（既訳の表記で判定）
        self.assertIn("## 固有名の扱い（命名方針）", md)
        self.assertIn("### 音訳（カタカナ）（1件）", md)
        self.assertIn("| 张三 | チョウサン |", md)
        self.assertIn("### 漢語のまま（1件）", md)
        self.assertIn("| 李四 | 李四 |", md)
        # 混在 → 要確認（方針は未決定として明示）
        self.assertIn("命名方針: 未決定", md)
        self.assertIn("混在: 要確認", md)
        self.assertIn("混在: 要確認", out)
        # toml: 扱い区分つきの一覧。方針はまだ無い
        data = _lib.load_toml(work / "_work" / "lock_prep.toml")
        by_word = {e["word"]: e for e in data.get("naming", [])}
        self.assertEqual(by_word["张三"]["category"], "音訳（カタカナ）")
        self.assertEqual(by_word["李四"]["category"], "漢語のまま")
        self.assertNotIn("naming_policy", data)


# ---------------------------------------------------------------------------
# ② [naming_policy] 記録後に要確認が消える
# ---------------------------------------------------------------------------

class TestPolicyRecorded(_Base):
    def test_recorded_policy_suppresses_confirm(self):
        """方針を記録すると、混在の「要確認」は出なくなる（一覧は残る）。"""
        work = _make_naming_work(self.tmp)
        _write_policy(work, style="mixed")
        rc, out, md = _run_lock_prep(work)
        self.assertEqual(rc, 0, out)
        self.assertIn("命名方針: mixed", md)
        self.assertIn("混在: あり", md)
        self.assertNotIn("要確認", md)
        self.assertNotIn("要確認", out)
        data = _lib.load_toml(work / "_work" / "lock_prep.toml")
        self.assertEqual(data.get("naming_policy", {}).get("style"), "mixed")

    def test_policy_from_work_side_house_style(self):
        """作品側（work/_house）の house_style でも方針として読める。"""
        work = _make_naming_work(self.tmp)
        _write(work / "_house" / "house_style.toml",
               '[naming_policy]\nstyle = "sinitic"\nscope = "work"\n'
               'decided_at = "2026-10-05T00:00:00Z"\n')
        rc, out, md = _run_lock_prep(work)
        self.assertEqual(rc, 0, out)
        self.assertIn("命名方針: sinitic", md)
        self.assertNotIn("要確認", md)


# ---------------------------------------------------------------------------
# ③ carry_analysis の [naming_policy] 引き継ぎ
# ---------------------------------------------------------------------------

class TestCarryNamingPolicy(_Base):
    def _make_pair(self, prev_policy: bool = True, cur_policy: bool = False):
        prev = self.tmp / "prev" / "_saga" / "111"
        cur = self.tmp / "cur" / "_saga" / "222"
        _write(prev / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
        _write(cur / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
        if prev_policy:
            _write(prev.parent / "_house" / "house_style.toml",
                   '[naming_policy]\nstyle = "transliterate"\n'
                   'note = "洋風のため音訳"\nscope = "series"\n'
                   'decided_at = "2026-10-04T00:00:00Z"\n')
        if cur_policy:
            _write(cur.parent / "_house" / "house_style.toml",
                   '[naming_policy]\nstyle = "sinitic"\nscope = "work"\n'
                   'decided_at = "2026-10-05T00:00:00Z"\n')
        return prev, cur

    def test_carry_copies_policy_and_shows_confirm(self):
        """未決定の現作へ前作の方針をコピーし、「このまま進めるか確認」を出す。"""
        prev, cur = self._make_pair()
        code, out = _run(CARRY, [str(cur), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        self.assertIn("前作の方針: transliterate", out)
        self.assertIn("このまま進めるか確認", out)
        data = _lib.load_toml(cur.parent / "_house" / "house_style.toml")
        self.assertEqual(data.get("naming_policy", {}).get("style"), "transliterate")
        self.assertEqual(data.get("naming_policy", {}).get("scope"), "series")

    def test_carry_does_not_override_decided(self):
        """現作で決定済みなら前作の記録で上書きしない。"""
        prev, cur = self._make_pair(cur_policy=True)
        code, out = _run(CARRY, [str(cur), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        self.assertIn("現作で決定済み", out)
        data = _lib.load_toml(cur.parent / "_house" / "house_style.toml")
        self.assertEqual(data.get("naming_policy", {}).get("style"), "sinitic")

    def test_carry_without_prev_policy(self):
        """前作に記録が無ければ何も作らない。"""
        prev, cur = self._make_pair(prev_policy=False)
        code, out = _run(CARRY, [str(cur), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        self.assertIn("前作に [naming_policy] の記録はありません", out)
        self.assertFalse((cur.parent / "_house" / "house_style.toml").exists())

    def test_carry_appends_to_existing_work_side_house_style(self):
        """作品側 house_style が既にあれば、本文を保ったまま追記する。"""
        prev, cur = self._make_pair()
        _write(cur / "_house" / "house_style.toml",
               'version = 1\ncreated_at = "2026-10-01T00:00:00Z"\n'
               'updated_at = "2026-10-01T00:00:00Z"\n\n'
               '[[rules]]\nid = "r1"\nkind = "kigou"\nscope = "work"\n'
               'target = "222"\npattern = "…"\nreplacement = "…"\n'
               'status = "approved"\nversion = 1\n')
        code, out = _run(CARRY, [str(cur), "--from", str(prev)])
        self.assertEqual(code, 0, out)
        text = (cur / "_house" / "house_style.toml").read_text(encoding="utf-8")
        self.assertIn("[[rules]]", text)        # 既存の本文を保つ
        self.assertIn("[naming_policy]", text)  # 追記される
        data = _lib.load_toml(cur / "_house" / "house_style.toml")
        self.assertEqual(data["naming_policy"]["style"], "transliterate")
        self.assertEqual(data["rules"][0]["id"], "r1")
        self.assertFalse((cur.parent / "_house" / "house_style.toml").exists())


# ---------------------------------------------------------------------------
# ④ saga.py status の未決定警告（未訳あり時のみ）
# ---------------------------------------------------------------------------

class TestStatusNamingWarning(_Base):
    def _make_status_work(self) -> Path:
        work = self.tmp / "_saga" / "99999"
        _write(work / "manifest.toml", 'title = "T"\nlang = "zh"\ndst = "ja"\n')
        _write(work / "chapters_meta.toml",
               '[[chapters]]\nid = "ch001"\ntitle = ""\nstatus = "pending"\n'
               '[[chapters.blocks]]\nid = "b1"\nparas = [1, 2]\nn_chars = 20\n'
               '[[chapters.blocks]]\nid = "b2"\nparas = [3, 4]\nn_chars = 20\n')
        _write(work / "chapters" / "ch001.paras.toml",
               "".join(f'[[paras]]\nn = {i}\ntext = "原文{i}。"\n\n' for i in range(1, 5)))
        (work / "analysis").mkdir(parents=True, exist_ok=True)
        (work / "_work").mkdir(parents=True, exist_ok=True)
        (work / "output" / "ch001").mkdir(parents=True, exist_ok=True)
        return work

    def _run_status(self, work: Path) -> tuple[int, str]:
        from saga import cmd_status
        buf = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = buf
            code = cmd_status(work)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code or 0, buf.getvalue()

    def _mark_translated(self, work: Path, *blocks: str) -> None:
        for b in blocks:
            _write(work / "output" / "ch001" / f"{b}_ja.md", "訳文。\n")

    def test_warns_when_undecided_and_untranslated(self):
        """lock_prep 済み・方針なし・未訳あり → 警告が出る。"""
        work = self._make_status_work()
        _write(work / "_work" / "lock_prep.toml", "naming = []\n")
        self._mark_translated(work, "b1")  # 未訳（b2）が残っている
        code, out = self._run_status(work)
        self.assertEqual(code, 0, out)
        self.assertIn("命名方針: 未決定（未訳ブロック残。次の確定前に決めてください）", out)

    def test_no_warning_when_all_translated(self):
        """全ブロック訳済みなら警告しない（未訳あり時のみ）。"""
        work = self._make_status_work()
        _write(work / "_work" / "lock_prep.toml", "naming = []\n")
        self._mark_translated(work, "b1", "b2")
        code, out = self._run_status(work)
        self.assertEqual(code, 0, out)
        self.assertNotIn("命名方針: 未決定", out)

    def test_no_warning_when_policy_decided(self):
        """方針が記録済みなら警告しない。"""
        work = self._make_status_work()
        _write(work / "_work" / "lock_prep.toml", "naming = []\n")
        self._mark_translated(work, "b1")
        _write_policy(work)
        code, out = self._run_status(work)
        self.assertEqual(code, 0, out)
        self.assertNotIn("命名方針: 未決定", out)

    def test_no_warning_without_lock_prep(self):
        """lock_prep 未実施の work では警告しない。"""
        work = self._make_status_work()
        self._mark_translated(work, "b1")
        code, out = self._run_status(work)
        self.assertEqual(code, 0, out)
        self.assertNotIn("命名方針: 未決定", out)


# ---------------------------------------------------------------------------
# ⑤ 混在を「誤り」と断定しない（出力文言は「要確認」止まり）
# ---------------------------------------------------------------------------

class TestMixedNotError(_Base):
    def test_wording_is_confirm_only(self):
        """混在の提示は「要確認」に止め、誤り・エラーとして断定しない。"""
        work = _make_naming_work(self.tmp)
        rc, out, md = _run_lock_prep(work)
        self.assertEqual(rc, 0, out)
        self.assertIn("要確認", md)
        self.assertIn("確認の候補", md)
        self.assertNotIn("誤り", md)
        self.assertNotIn("エラー", md)
        self.assertNotIn("不正", md)
        self.assertNotIn("誤り", out)
        self.assertNotIn("エラー", out)


if __name__ == "__main__":
    unittest.main()
