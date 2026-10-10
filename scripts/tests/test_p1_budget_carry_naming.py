"""P1 改善（あらすじ予算 / 用語集の出現語限定 / carry 連鎖 / コードネーム）のテスト。

対象（G20・G21・G22・G12〜G15・G18）:
  - build_context: あらすじの文字数予算（超過分は古い側を1行に省略）と
    用語集の「対象本文の出現語」限定
  - carry_analysis: series_from 再帰の連鎖和集合・欠落の名指し・
    ruby_applied の非引き継ぎ・at_block="carry" の受け入れ
  - lock_prep: コードネーム候補節・[[codenames]]・中国語ストップリスト・
    [naming_policy].codename の表示
  - build_context/_fmt_house_style: [naming_policy] の描画と scope=series の適用
  - house_style.py validate: codename の値検査
  - lang pack: ジャンル別命名慣行節と kind="naming" 登録例

合成データのみ使用。作品固有情報は含まない。
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import build_context  # noqa: E402
import house_style  # noqa: E402
import lock_prep  # noqa: E402

CARRY = SCRIPTS / "carry_analysis.py"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _run(script: Path, args: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(SCRIPTS))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _make_ctx_work(base: Path, body: list[str] | None = None) -> Path:
    """build_context 用の最小 work（zh→ja。1章1ブロック）。"""
    work = base / "_saga" / "99999"
    body = body or ["原文A。", "原文B。"]
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml",
           '[[chapters]]\nid = "ch001"\nblocks = [{id="b1", paras=[1, %d]}]\n'
           % len(body))
    _write(work / "chapters" / "ch001.paras.toml",
           "".join(f'[[paras]]\nn = {i + 1}\ntext = "{t}"\n\n'
                   for i, t in enumerate(body)))
    (work / "output" / "ch001").mkdir(parents=True, exist_ok=True)
    (work / "analysis").mkdir(parents=True, exist_ok=True)
    return work


def _stage(work: Path, mode: str = "translation", *, budget=None,
           style: str = "compact") -> str:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        build_context._stage_impl(work, "ch001", "b1", mode, style,
                                  plot_budget=budget)
    return (work / "translation_input" / f"ch001_b1_{mode}.md").read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------------
# G20: あらすじの文字数予算
# ---------------------------------------------------------------------------


class TestPlotBudget(unittest.TestCase):
    def test_condense_keeps_newest_and_summarizes_old(self):
        texts = ["a" * 100, "b" * 100, "c" * 100]
        disp, dropped = build_context._condense_to_budget(texts, 210)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(disp), 3)
        self.assertTrue(disp[0].startswith("（以前の経過1件を1行に省略: 冒頭「"))
        self.assertIn("aaa", disp[0])
        self.assertEqual(disp[1], "b" * 100)
        self.assertEqual(disp[2], "c" * 100)

    def test_condense_unlimited_is_passthrough(self):
        texts = ["x", "y"]
        self.assertEqual(build_context._condense_to_budget(texts, 0), (texts, 0))
        self.assertEqual(build_context._condense_to_budget(texts, -1), (texts, 0))

    def test_condense_keeps_newest_even_over_budget(self):
        disp, dropped = build_context._condense_to_budget(["a" * 5000], 100)
        self.assertEqual(dropped, 0)
        self.assertEqual(disp, ["a" * 5000])

    def test_compact_plot_budget(self):
        plot = {"entries": [
            {"text": "old_event_" + "x" * 100, "block": "ch001/b1"},
            {"text": "mid_event_" + "y" * 100, "block": "ch001/b2"},
            {"text": "new_event", "block": "ch001/b3"},
        ]}
        texts, resolved = build_context._compact_plot(
            plot, "ch001", "b4", budget=150)
        self.assertTrue(resolved)
        self.assertTrue(any("以前の経過" in t for t in texts))
        self.assertIn("new_event", texts)
        self.assertNotIn("old_event_" + "x" * 100, texts)

    def test_fmt_plot_budget(self):
        plot = {"entries": [{"text": "old" + "x" * 100},
                            {"text": "new"}]}
        out = build_context.fmt_plot(plot, budget=50)
        self.assertIn("以前の経過1件を1行に省略", out)
        self.assertIn("- new", out)
        self.assertNotIn("old" + "x" * 100, out)

    def test_stage_impl_context_budget_wiring(self):
        """_stage_impl が予算を受け、古いあらすじを1行に省略する。"""
        with tempfile.TemporaryDirectory() as td:
            work = _make_ctx_work(Path(td))
            _write(work / "analysis" / "plot-[ネタバレあり].toml",
                   '[[entries]]\ntext = "old_' + "x" * 200 + '"\n\n'
                   '[[entries]]\ntext = "new_event"\n')
            ctx = _stage(work, budget=120)
            self.assertIn("以前の経過1件を1行に省略", ctx)
            self.assertIn("new_event", ctx)
            self.assertNotIn("old_" + "x" * 200, ctx)

    def test_stage_impl_budget_from_config_and_zero_disables(self):
        with tempfile.TemporaryDirectory() as td:
            work = _make_ctx_work(Path(td))
            _write(work / "analysis" / "plot-[ネタバレあり].toml",
                   '[[entries]]\ntext = "old_' + "x" * 200 + '"\n\n'
                   '[[entries]]\ntext = "new_event"\n')
            with mock.patch("build_context.load_config",
                            return_value={"context": {"plot_budget_chars": 100}}):
                ctx = _stage(work, budget=None)
            self.assertIn("以前の経過1件を1行に省略", ctx)
            # 0 = 無制限（--plot-budget 0 相当）
            with mock.patch("build_context.load_config",
                            return_value={"context": {"plot_budget_chars": 100}}):
                ctx0 = _stage(work, budget=0)
            self.assertNotIn("以前の経過", ctx0)
            self.assertIn("old_" + "x" * 200, ctx0)


# ---------------------------------------------------------------------------
# G21: 用語集を対象本文の出現語に限定
# ---------------------------------------------------------------------------


class TestTermsNarrowing(unittest.TestCase):
    def test_compact_terms_keeps_only_appearing_and_core(self):
        terms = {"terms": [
            {"term_src": "magic", "term_ja": "魔法"},
            {"term_src": "sword", "term_ja": "剣"},
            {"term_src": "kingdom", "term_ja": "王国", "core": True},
        ]}
        result = build_context._compact_terms(terms, ["He used magic."],
                                              "彼は剣を抜いた。")
        srcs = [t["term_src"] for t in result]
        self.assertIn("magic", srcs)
        self.assertNotIn("sword", srcs)      # 前ブロック訳文にあるだけでは載らない
        self.assertIn("kingdom", srcs)       # core は常に載る

    def test_stage_impl_context_terms_narrowed(self):
        """前ブロック訳文にあるだけの用語は、コンテキストの用語集に出ない。"""
        with tempfile.TemporaryDirectory() as td:
            work = _make_ctx_work(Path(td), body=["原文magic。", "原文B。"])
            _write(work / "analysis" / "terms.toml",
                   '[[terms]]\nterm_src = "magic"\nterm_ja = "魔法"\n\n'
                   '[[terms]]\nterm_src = "sword"\nterm_ja = "剣"\n')
            # 前ブロックは無い（b1）ので、前作訳文の代わりにダミー出力を置く
            ctx = _stage(work)
            terms_section = ctx.split("## 用語集", 1)[1].split("\n## ", 1)[0]
            self.assertIn("magic", terms_section)
            self.assertNotIn("sword", terms_section)


# ---------------------------------------------------------------------------
# G22 / CA-1 / CA-3: carry の連鎖和集合・欠落の名指し・台帳
# ---------------------------------------------------------------------------


def _mk_carry_work(root: Path, name: str, series_from: str | None = None) -> Path:
    work = root / name
    work.mkdir(parents=True, exist_ok=True)
    manifest = 'lang = "zh"\ndst = "ja"\n'
    if series_from:
        manifest += f'series_from = "{series_from}"\n'
    _write(work / "manifest.toml", manifest)
    return work


class TestCarryChain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1_carry_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.gp = _mk_carry_work(self.tmp, "A")
        self.prev = _mk_carry_work(self.tmp, "B", series_from=self.gp.as_posix())
        self.cur = _mk_carry_work(self.tmp, "C")
        _write(self.gp / "analysis" / "characters-[ネタバレあり].toml",
               '[[characters]]\nname_src = "汐月实"\nname_ja = "シオツキ"\n'
               'speech_style = "敬体"\n\n'
               '[[characters]]\nname_src = "王五"\nname_ja = "王五"\n'
               'speech_style = "敬体"\n')
        _write(self.prev / "analysis" / "characters-[ネタバレあり].toml",
               '[[characters]]\nname_src = "李四"\nname_ja = "李四"\n\n'
               '[[characters]]\nname_src = "王五"\nname_ja = "王五"\n'
               'speech_style = "タメ口"\n')
        _write(self.gp / "analysis" / "terms.toml",
               '[[terms]]\nterm_src = "灵气"\nterm_ja = "霊気"\n')

    def test_chain_union_and_missing_report(self):
        code, out = _run(CARRY, [str(self.cur), "--from", str(self.prev)])
        self.assertEqual(code, 0, out)
        # 連鎖の表示（2作を和集合）
        self.assertIn("の2作を和集合で引き継ぎ", out)
        # 直接の前作に無い項目を名指し
        self.assertIn("直接の前作に無く、連鎖の過去作から引き継いだ項目", out)
        self.assertIn("汐月实", out)
        self.assertIn("欠落: なし", out)

        import _lib
        chars = _lib.load_toml(
            self.cur / "analysis" / "characters-[ネタバレあり].toml")["characters"]
        by_name = {c["name_src"]: c for c in chars}
        self.assertIn("汐月实", by_name)          # 祖父母作から伝播
        self.assertIn("李四", by_name)
        self.assertEqual(by_name["汐月实"]["arcs"][0]["at_block"], "carry")
        self.assertIn("連鎖の過去作『A』", by_name["汐月实"]["arcs"][0]["note"])
        # 近い側優先: 王五 は前作（B）の値を採る
        self.assertEqual(by_name["王五"]["speech_style"], "タメ口")
        # 用語も連鎖の和集合
        terms = _lib.load_toml(self.cur / "analysis" / "terms.toml")["terms"]
        self.assertIn("灵气", [t["term_src"] for t in terms])

    def test_broken_link_is_named(self):
        """series_from のリンク切れは欠落として名指しし、そこで打ち切る。"""
        broken = _mk_carry_work(self.tmp, "D",
                                series_from=(self.tmp / "does_not_exist").as_posix())
        cur = _mk_carry_work(self.tmp, "E")
        _write(broken / "analysis" / "characters-[ネタバレあり].toml",
               '[[characters]]\nname_src = "赵六"\nname_ja = "チョウロク"\n')
        code, out = _run(CARRY, [str(cur), "--from", str(broken)])
        self.assertEqual(code, 0, out)
        self.assertIn("連鎖の欠落", out)
        self.assertIn("does_not_exist", out)
        import _lib
        chars = _lib.load_toml(
            cur / "analysis" / "characters-[ネタバレあり].toml")["characters"]
        self.assertIn("赵六", [c["name_src"] for c in chars])  # 手前までは引き継ぐ

    def test_single_prev_chain_line(self):
        code, out = _run(CARRY, [str(self.cur), "--from", str(self.gp)])
        self.assertEqual(code, 0, out)
        self.assertIn("連鎖: 前作1作のみ", out)

    def test_carry_arc_has_no_invalid_warning_in_context(self):
        """at_block="carry" はコンテキスト生成で不正値警告にならない（G22）。"""
        code, out = _run(CARRY, [str(self.cur), "--from", str(self.prev)])
        self.assertEqual(code, 0, out)
        _write(self.cur / "chapters_meta.toml",
               '[[chapters]]\nid = "ch001"\nblocks = [{id="b1", paras=[1, 1]}]\n')
        _write(self.cur / "chapters" / "ch001.paras.toml",
               '[[paras]]\nn = 1\ntext = "汐月实来了。"\n')
        (self.cur / "output" / "ch001").mkdir(parents=True, exist_ok=True)
        ctx = _stage(self.cur)
        self.assertNotIn("不正な at_block", ctx)
        self.assertIn("carry", ctx)
        self.assertIn("シオツキ", ctx)

    def test_compact_arcs_accepts_carry_marker(self):
        chars = {"characters": [
            {"name_ja": "アリス",
             "arcs": [{"at_block": "carry", "note": "前作の状態を引き継ぎ"}]},
        ]}
        result, warns = build_context._compact_arcs(chars, "ch001", "b1")
        self.assertEqual(warns, [])
        self.assertIn("アリス", result)
        self.assertEqual(result["アリス"][0]["arc_notes"],
                         [("carry", "前作の状態を引き継ぎ")])
        text = build_context.fmt_arcs_resolved(result)
        self.assertIn("注記: carry 前作の状態を引き継ぎ", text)


class TestCarryLedgerPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1_carry_led_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 前作と現作は別の親に置く（house_style の探索先が親側 _house のため、
        # 同じ親だと前作の方針が「現作で決定済み」に見えてコピーを検証できない）
        self.prev = _mk_carry_work(self.tmp / "prev_root", "B")
        self.cur = _mk_carry_work(self.tmp / "cur_root", "C")

    def test_ruby_applied_not_carried(self):
        from ledger import load_ledger, save_ledger
        save_ledger(self.prev, {
            "ruby_applied": [{"term": "剣", "reading": "つるぎ",
                              "block": "ch001/b1", "paragraph": 1}],
            "term_first_use": [{"term": "雪", "block": "ch001/b1", "paragraph": 2}],
        })
        code, out = _run(CARRY, [str(self.cur), "--from", str(self.prev)])
        self.assertEqual(code, 0, out)
        self.assertIn("ruby_applied は作品単位のため引き継がず", out)
        d = load_ledger(self.cur)
        self.assertEqual(d["ruby_applied"], [])
        self.assertEqual([t["term"] for t in d["term_first_use"]], ["雪"])

    def test_naming_policy_codename_carried_and_shown(self):
        _write(self.prev.parent / "_house" / "house_style.toml",
               '[naming_policy]\nstyle = "mixed"\ncodename = "katakana"\n'
               'scope = "series"\ndecided_at = "2026-10-10T00:00:00Z"\n')
        code, out = _run(CARRY, [str(self.cur), "--from", str(self.prev)])
        self.assertEqual(code, 0, out)
        self.assertIn("codename=katakana", out)
        import _lib
        pol = _lib.load_toml(self.cur.parent / "_house" / "house_style.toml")
        self.assertEqual(pol["naming_policy"]["codename"], "katakana")


# ---------------------------------------------------------------------------
# G13〜G15: コードネーム（lock_prep / naming_policy / 描画）
# ---------------------------------------------------------------------------


def _make_lock_work(base: Path, texts: list[str]) -> Path:
    work = base / "_saga" / "99999"
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", '[[chapters]]\nid = "ch001"\nblocks = ["b1"]\n')
    _write(work / "chapters" / "ch001.paras.toml",
           "".join(f'[[paras]]\nn = {i + 1}\ntext = "{t}"\n\n'
                   for i, t in enumerate(texts)))
    return work


class TestLockPrepCodenames(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1_codename_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_codename_section_and_toml(self):
        work = _make_lock_work(self.tmp, [
            '代号“猩红”出现了。',
            "代号：零 站在屋顶。代号是“白夜”。",
            "猩红笑了。",
        ])
        _write(work / "analysis" / "terms.toml",
               '[[terms]]\nterm_src = "猩红"\nterm_ja = "スカーレット"\n')
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            rc = lock_prep.main([str(work)])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("コードネーム候補: 3件", buf.getvalue())

        md = (work / "_work" / "lock_prep.md").read_text(encoding="utf-8")
        self.assertIn("## コードネーム候補", md)
        self.assertIn("| 猩红 |", md)
        self.assertIn("| 零 |", md)       # 1字も拾う
        self.assertIn("| 白夜 |", md)
        self.assertIn("既訳: スカーレット", md)  # terms の既訳を注記

        import _lib
        d = _lib.load_toml(work / "_work" / "lock_prep.toml")
        words = [c["word"] for c in d.get("codenames", [])]
        self.assertEqual(words, ["猩红", "零", "白夜"])
        self.assertEqual(d["codenames"][0]["first"], "ch001 [P1]")

    def test_gather_codenames_patterns(self):
        paras = [("ch001", 1, '代号为"白夜"，代号：零。代號「银月」')]
        got = lock_prep._gather_codenames(paras)
        self.assertEqual([g["word"] for g in got], ["白夜", "零", "银月"])

    def test_stoplist_excludes_function_words(self):
        paras = [("ch001", 1, "我的心情。我的书。我的天。张三笑了。"),
                 ("ch001", 2, "一个一个人。她的刀。张三来了。张三走了。")]
        words = [r["word"] for r in lock_prep._gather_proper_nouns(paras, "zh", 50)]
        self.assertNotIn("我的", words)
        self.assertNotIn("一个", words)
        self.assertNotIn("她的", words)
        self.assertNotIn("代号", words)
        self.assertIn("张三", words)

    def test_is_stopword_units(self):
        self.assertTrue(lock_prep._is_stopword("我的"))
        self.assertTrue(lock_prep._is_stopword("一个"))
        self.assertTrue(lock_prep._is_stopword("了一"))
        self.assertTrue(lock_prep._is_stopword("什么"))
        self.assertTrue(lock_prep._is_stopword("代号"))
        self.assertFalse(lock_prep._is_stopword("张三"))
        self.assertFalse(lock_prep._is_stopword("小白"))   # 一部でも非機能語字なら残す

    def test_policy_codename_shown_in_lock_prep(self):
        work = _make_lock_work(self.tmp, ["张三笑了。"])
        _write(work.parent / "_house" / "house_style.toml",
               '[naming_policy]\nstyle = "mixed"\ncodename = "katakana"\n'
               'scope = "series"\n')
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            rc = lock_prep.main([str(work)])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("codename=katakana", buf.getvalue())
        md = (work / "_work" / "lock_prep.md").read_text(encoding="utf-8")
        self.assertIn("- コードネーム: カタカナ英語（codename=katakana", md)


class TestHouseStyleNamingPolicy(unittest.TestCase):
    def test_fmt_house_style_renders_policy_and_codename(self):
        text = build_context._fmt_house_style(
            [], "001",
            {"style": "mixed", "scope": "series", "codename": "katakana",
             "decided_at": "2026-10-10T00:00:00Z", "note": "魔法少女もの"})
        self.assertIn("- 命名方針: mixed（scope=series", text)
        self.assertIn("- コードネーム: カタカナ英語（codename=katakana）", text)
        self.assertIn("魔法少女もの", text)

    def test_fmt_house_style_policy_only_no_rules(self):
        """rules が空でも [naming_policy] だけで節が出る（dead config の解消）。"""
        text = build_context._fmt_house_style([], "001",
                                              {"style": "sinitic"})
        self.assertEqual(text, "- 命名方針: sinitic")

    def test_fmt_house_style_series_rule_applied(self):
        rules = [{"id": "r1", "kind": "naming", "scope": "series",
                  "target": "series-id", "pattern": "代号",
                  "preference": "カタカナ英語", "status": "approved"}]
        text = build_context._fmt_house_style(rules, "001")
        self.assertIn("命名: 代号 → カタカナ英語", text)

    def test_load_house_style_policy(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "_saga" / "001"
            work.mkdir(parents=True)
            _write(work.parent / "_house" / "house_style.toml",
                   '[naming_policy]\ncodename = "katakana"\n')
            pol = build_context._load_house_style_policy(work)
            self.assertEqual(pol.get("codename"), "katakana")

    def test_stage_impl_renders_policy_and_series_rule(self):
        with tempfile.TemporaryDirectory() as td:
            work = _make_ctx_work(Path(td))
            _write(work.parent / "_house" / "house_style.toml",
                   '[naming_policy]\nstyle = "mixed"\ncodename = "katakana"\n'
                   'scope = "series"\n\n'
                   '[[rules]]\nid = "r1"\nkind = "naming"\nscope = "series"\n'
                   'target = "series-id"\npattern = "代号"\n'
                   'preference = "カタカナ英語"\nstatus = "approved"\nversion = 1\n')
            ctx = _stage(work)
            self.assertIn("## ハウススタイル(既決事項)", ctx)
            self.assertIn("命名方針: mixed", ctx)
            self.assertIn("コードネーム: カタカナ英語", ctx)
            self.assertIn("命名: 代号 → カタカナ英語", ctx)

    def test_house_style_validate_codename(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            good = td / "good.toml"
            _write(good,
                   'version = 1\ncreated_at = "x"\nupdated_at = "x"\n'
                   'rules = []\n\n[naming_policy]\nstyle = "mixed"\n'
                   'codename = "katakana"\nscope = "series"\n')
            args = type("Args", (), {"toml_path": str(good)})()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(house_style.cmd_validate(args), 0)
            bad = td / "bad.toml"
            _write(bad,
                   'version = 1\ncreated_at = "x"\nupdated_at = "x"\n'
                   'rules = []\n\n[naming_policy]\nstyle = "mixed"\n'
                   'codename = "english"\n')
            args = type("Args", (), {"toml_path": str(bad)})()
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = house_style.cmd_validate(args)
            self.assertEqual(rc, 1)
            self.assertIn("codename", buf.getvalue())


# ---------------------------------------------------------------------------
# G10 / T13: 台帳の見出しと lang pack の命名慣行節
# ---------------------------------------------------------------------------


class TestLedgerHeadingAndLangPacks(unittest.TestCase):
    def test_ledger_heading_says_this_work(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "work"
            work.mkdir(parents=True)
            from ledger import save_ledger
            save_ledger(work, {"ruby_applied": [
                {"term": "剣", "reading": "つるぎ",
                 "block": "ch001/b1", "paragraph": 1}]})
            out = "\n".join(build_context._fmt_ledger_consumed(work))
            self.assertIn("## この作品で消費済みルビ（再付与不可）", out)

    def test_lang_pack_zh2ja_has_genre_naming_section(self):
        text = (SCRIPTS.parent / "references" / "lang_zh2ja.md").read_text(
            encoding="utf-8")
        self.assertIn("## ジャンル別命名慣行", text)
        self.assertIn("カタカナ英語", text)
        self.assertIn("コードネームにはルビを振らない", text)
        self.assertIn('kind = "naming"', text)
        self.assertIn("codename", text)

    def test_lang_pack_ja2zh_has_genre_naming_section(self):
        text = (SCRIPTS.parent / "references" / "lang_ja2zh.md").read_text(
            encoding="utf-8")
        self.assertIn("## ジャンル別命名慣行", text)
        self.assertIn("codename", text)

    def test_naming_kind_registrable(self):
        """rules kind="naming" は validate の許容 kind（登録経路が開いている）。"""
        self.assertIn("naming", house_style.VALID_KINDS)
        self.assertIn("series", house_style.VALID_SCOPES)


if __name__ == "__main__":
    unittest.main()
