"""saga-translator build_context compact モードの単体テスト。"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from build_context import (
    _block_address,
    _parse_at_block,
    _compact_characters,
    _compact_terms,
    _compact_arcs,
    _compact_plot,
    _load_house_style_rules,
    _fmt_house_style,
    fmt_characters,
    fmt_arcs,
    fmt_terms,
    get_work_id,
)


class TestBlockAddress(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_block_address("ch001", "b1"), (1, 1))
        self.assertEqual(_block_address("ch012", "b5"), (12, 5))
        self.assertEqual(_block_address("ch100", "b99"), (100, 99))

    def test_invalid(self):
        self.assertEqual(_block_address("", ""), (0, 0))
        self.assertEqual(_block_address("ch001", ""), (0, 0))


class TestParseAtBlock(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_parse_at_block("ch001/b3"), (1, 3))
        self.assertEqual(_parse_at_block("ch010/b1"), (10, 1))

    def test_invalid(self):
        self.assertIsNone(_parse_at_block(""))
        self.assertIsNone(_parse_at_block("invalid"))
        self.assertIsNone(_parse_at_block("ch001"))  # 不完全


class TestCompactCharacters(unittest.TestCase):
    def setUp(self):
        self.chars = {
            "characters": [
                {"name_src": "Alice", "name_ja": "アリス", "first_person": "私"},
                {"name_src": "Bob", "name_ja": "ボブ", "first_person": "俺"},
                {"name_src": "Carol", "name_ja": "キャロル", "core": True, "first_person": "私"},
            ]
        }

    def test_name_src_in_body(self):
        """name_src が原文に出る人物だけ載る"""
        body = ["Hello Alice!"]
        result = _compact_characters(self.chars, body, "(なし: 冒頭)")
        names = [c["name_src"] for c in result]
        self.assertIn("Alice", names)
        self.assertNotIn("Bob", names)

    def test_core_always_included(self):
        """core=True の人物は常に載る"""
        body = ["No names match here"]
        result = _compact_characters(self.chars, body, "(なし: 冒頭)")
        names = [c["name_src"] for c in result]
        self.assertIn("Carol", names)

    def test_name_ja_in_prev_translation(self):
        """前ブロック訳文に name_ja が出る人物も載る"""
        body = ["Monologue only"]
        prev = "これはアリスの物語だ。"
        result = _compact_characters(self.chars, body, prev)
        names = [c["name_src"] for c in result]
        self.assertIn("Alice", names)

    def test_no_match_shows_core_only(self):
        """該当なしの場合は core のみ"""
        body = ["Irrelevant text"]
        prev = "(なし: 冒頭)"
        result = _compact_characters(self.chars, body, prev)
        names = [c["name_src"] for c in result]
        self.assertEqual(names, ["Carol"])

    def test_address_target_src_in_body(self):
        """address の target_src が原文に出る人物も載る"""
        chars = {
            "characters": [
                {
                    "name_src": "Alice",
                    "name_ja": "アリス",
                    "address": [{"target_src": "Bob", "target_ja": "ボブ", "form": "ボブ"}],
                },
            ]
        }
        body = ["Bob is mentioned"]
        result = _compact_characters(chars, body, "(なし: 冒頭)")
        self.assertEqual(len(result), 1)

    def test_address_target_ja_in_prev(self):
        """address の target_ja が前ブロック訳文に出る人物も載る"""
        chars = {
            "characters": [
                {
                    "name_src": "Alice",
                    "name_ja": "アリス",
                    "address": [{"target_src": "Bob", "target_ja": "ボブ", "form": "ボブ"}],
                },
            ]
        }
        body = ["Monologue"]
        prev = "ボブが走る。"
        result = _compact_characters(chars, body, prev)
        self.assertEqual(len(result), 1)

    def test_ruby_in_body(self):
        """ruby が原文に出る人物も載る"""
        chars = {
            "characters": [
                {"name_src": "靈", "name_ja": "霊", "ruby": "たま"},
            ]
        }
        body = ["たまが現れた"]
        result = _compact_characters(chars, body, "(なし: 冒頭)")
        self.assertEqual(len(result), 1)

    def test_empty_characters(self):
        """空リストは空リストを返す"""
        result = _compact_characters({"characters": []}, ["text"], "(なし: 冒頭)")
        self.assertEqual(result, [])


class TestCompactTerms(unittest.TestCase):
    def setUp(self):
        self.terms = {
            "terms": [
                {"term_src": "magic", "term_ja": "魔法"},
                {"term_src": "sword", "term_ja": "剣"},
                {"term_src": "kingdom", "term_ja": "王国", "core": True},
            ]
        }

    def test_term_src_in_body(self):
        """term_src が原文に出る用語だけ載る"""
        body = ["He used magic."]
        result = _compact_terms(self.terms, body, "(なし: 冒頭)")
        srcs = [t["term_src"] for t in result]
        self.assertIn("magic", srcs)
        self.assertNotIn("sword", srcs)

    def test_term_ja_only_in_prev_is_excluded(self):
        """前ブロック訳文に term_ja が出るだけの用語は載せない（G21: 対象本文の出現語に限定）"""
        body = ["Some scene."]
        prev = "彼は剣を抜いた。"
        result = _compact_terms(self.terms, body, prev)
        srcs = [t["term_src"] for t in result]
        self.assertNotIn("sword", srcs)
        self.assertNotIn("magic", srcs)

    def test_core_always_included(self):
        """core=True の用語は常に載る"""
        body = ["No terms here."]
        result = _compact_terms(self.terms, body, "(なし: 冒頭)")
        srcs = [t["term_src"] for t in result]
        self.assertIn("kingdom", srcs)
        self.assertNotIn("magic", srcs)
        self.assertNotIn("sword", srcs)

    def test_empty_terms(self):
        result = _compact_terms({"terms": []}, ["text"], "(なし: 冒頭)")
        self.assertEqual(result, [])


class TestCompactArcs(unittest.TestCase):
    def setUp(self):
        self.chars = {
            "characters": [
                {
                    "name_ja": "アリス",
                    "arcs": [
                        {"at_block": "ch001/b1", "first_person": "私", "speech_style": "丁寧"},
                        {"at_block": "ch001/b3", "first_person": "あたし", "speech_style": "タメ口"},
                        {"at_block": "ch002/b1", "first_person": "俺", "speech_style": "乱暴"},
                    ],
                },
            ]
        }

    def test_only_before_target(self):
        """対象ブロックより後の arc は出ない"""
        result, warns = _compact_arcs(self.chars, "ch001", "b2")  # ch001/b2 時点
        self.assertIn("アリス", result)
        self.assertEqual(len(result["アリス"]), 1)
        arc = result["アリス"][0]
        self.assertEqual(arc["first_person"], "私")  # ch001/b1 が最新

    def test_no_arcs_before_target(self):
        """対象より前に arc が無い場合は空"""
        result, warns = _compact_arcs(self.chars, "ch001", "b1")  # ch001/b1 時点
        self.assertEqual(result, {})

    def test_latest_only(self):
        """最新の arc だけが出る"""
        result, warns = _compact_arcs(self.chars, "ch002", "b1")  # ch002/b1 時点
        self.assertIn("アリス", result)
        self.assertEqual(len(result["アリス"]), 1)
        arc = result["アリス"][0]
        self.assertEqual(arc["first_person"], "あたし")

    def test_invalid_at_block_warns(self):
        chars = {
            "characters": [
                {
                    "name_ja": "ボブ",
                    "arcs": [
                        {"at_block": "invalid", "note": "bad entry"},
                    ],
                },
            ]
        }
        result, warns = _compact_arcs(chars, "ch099", "b1")
        self.assertEqual(result, {})
        self.assertTrue(any("WARNING" in w for w in warns))
        self.assertTrue(any("invalid" in w for w in warns))


class TestCompactPlot(unittest.TestCase):
    def test_entries_after_target_excluded(self):
        """対象ブロックより後の block エントリは除外"""
        plot = {
            "entries": [
                {"text": "event_1", "block": "ch001/b1"},
                {"text": "event_2", "block": "ch001/b3"},
                {"text": "event_3", "block": "ch002/b1"},
            ]
        }
        texts, resolved = _compact_plot(plot, "ch001", "b2")  # ch001/b2 時点
        self.assertIn("event_1", texts)
        self.assertNotIn("event_2", texts)
        self.assertNotIn("event_3", texts)
        self.assertTrue(resolved)

    def test_no_block_field_fallback(self):
        """block フィールドが無いエントリが混ざるとフォールバック"""
        plot = {
            "entries": [
                {"text": "old_event"},
                {"text": "future_event", "block": "ch010/b1"},
            ]
        }
        texts, resolved = _compact_plot(plot, "ch001", "b1")
        # フォールバック: 全体から末尾20件
        self.assertEqual(len(texts), 2)
        self.assertFalse(resolved)

    def test_empty_entries(self):
        texts, resolved = _compact_plot({"entries": []}, "ch001", "b1")
        self.assertEqual(texts, [])
        self.assertFalse(resolved)

    def test_tail_limit(self):
        """末尾20件制限"""
        entries = []
        for i in range(1, 31):
            entries.append({"text": f"event_{i}", "block": f"ch001/b{i}"})
        plot = {"entries": entries}
        texts, resolved = _compact_plot(plot, "ch099", "b1")  # 全部対象
        self.assertEqual(len(texts), 20)  # 末尾20件
        self.assertTrue(resolved)


class TestHouseStyle(unittest.TestCase):
    def test_fmt_house_style_approved_only(self):
        rules = [
            {"id": "r1", "kind": "naming", "scope": "work", "target": "001",
             "pattern": "Magic", "replacement": "魔法", "status": "approved", "rationale": "統一"},
            {"id": "r2", "kind": "goi", "scope": "global",
             "pattern": "Sword", "replacement": "剣", "status": "candidate"},
            {"id": "r3", "kind": "ruby", "scope": "global",
             "pattern": "Castle", "replacement": "城", "status": "locked"},
        ]
        text = _fmt_house_style(rules, "001")
        self.assertIn("命名: Magic → 魔法", text)
        self.assertIn("ルビ: Castle → 城", text)  # locked は出る
        # candidate は出ない
        self.assertNotIn("Sword", text)

    def test_fmt_house_style_scope_filter(self):
        """scope=work で target 不一致は出ない"""
        rules = [
            {"id": "r1", "kind": "naming", "scope": "work", "target": "002",
             "pattern": "X", "replacement": "Y", "status": "approved"},
        ]
        text = _fmt_house_style(rules, "001")
        self.assertEqual(text, "")

    def test_fmt_house_style_no_file(self):
        """ファイルが無ければ空文字"""
        text = _fmt_house_style([], "001")
        self.assertEqual(text, "")

    def test_fmt_house_style_preference(self):
        """preference がある場合はそれを使う"""
        rules = [
            {"id": "r1", "kind": "jishou", "scope": "global",
             "pattern": "Me", "preference": "私", "status": "approved"},
        ]
        text = _fmt_house_style(rules, "001")
        self.assertIn("自称: Me → 私", text)


class TestGetWorkId(unittest.TestCase):
    def test_work_id_from_path(self):
        wid = get_work_id(Path("/some/path/001"))
        self.assertEqual(wid, "001")


class TestFullModePreserved(unittest.TestCase):
    """fmt_characters/fmt_arcs/fmt_terms/fmt_plot はシグネチャ不変で full モード互換"""

    def test_fmt_characters_full(self):
        text = fmt_characters({"characters": [
            {"name_src": "张三", "name_ja": "チョウサン", "first_person": "私"},
        ]})
        self.assertIn("チョウサン", text)
        self.assertIn("一人称: 私", text)

    def test_fmt_arcs_full(self):
        text = fmt_arcs({"characters": [
            {"name_ja": "アリス", "arcs": [{"at_block": "ch001/b3", "speech_style": "タメ口"}]},
        ]})
        self.assertIn("アリス", text)
        self.assertIn("ch001/b3", text)

    def test_fmt_terms_full(self):
        text = fmt_terms({"terms": [{"term_src": "magic", "term_ja": "魔法"}]})
        self.assertIn("magic", text)
        self.assertIn("魔法", text)


class TestEndToEndCompactOutput(unittest.TestCase):
    """_stage_impl の compact モードで生成される出力の確認"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        work = self.tmp / "test_work"
        work.mkdir(parents=True)

        # chapters_meta
        meta = {
            "chapters": [
                {
                    "id": "ch001",
                    "title": None,
                    "blocks": [
                        {"id": "b1", "paras": [1, 2]},
                    ],
                }
            ],
            "work": {"title": "Test", "author": "Author", "lang": "zh"},
        }
        (work / "chapters_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        # manifest
        (work / "manifest").write_text(json.dumps({"lang": "zh"}, ensure_ascii=False), encoding="utf-8")

        # chapters dir + paras
        chdir = work / "chapters"
        chdir.mkdir()
        paras = [
            {"n": 1, "text": "原文Alice。"},
            {"n": 2, "text": "原文Bob。"},
        ]
        (chdir / "ch001.paras.json").write_text(
            json.dumps({"chapter": "ch001", "title": None, "paras": paras}, ensure_ascii=False),
            encoding="utf-8",
        )

        # output dir（前ブロック訳文用）
        outdir = work / "output" / "ch001"
        outdir.mkdir(parents=True)

        # translation_input dir
        (work / "translation_input").mkdir(parents=True)
        # 前ブロックは無し（b1 が先頭のため）

        # analysis: characters
        analysis = work / "analysis"
        analysis.mkdir()
        chars = {
            "characters": [
                {"name_src": "Alice", "name_ja": "アリス", "first_person": "私"},
                {"name_src": "Bob", "name_ja": "ボブ", "first_person": "俺"},
                {"name_src": "Carol", "name_ja": "キャロル", "core": True, "first_person": "僕"},
            ]
        }
        (analysis / "characters.json").write_text(json.dumps(chars, ensure_ascii=False), encoding="utf-8")

        # analysis: terms
        terms_data = {
            "terms": [
                {"term_src": "magic", "term_ja": "魔法"},
                {"term_src": "sword", "term_ja": "剣"},
            ]
        }
        (analysis / "terms.json").write_text(json.dumps(terms_data, ensure_ascii=False), encoding="utf-8")

        # analysis: plot
        plot = {
            "entries": [
                {"text": "Alice meets Bob.", "block": "ch001/b1"},
            ]
        }
        (analysis / "plot.json").write_text(json.dumps(plot, ensure_ascii=False), encoding="utf-8")

        # config
        import _lib
        cfg_dir = Path.home() / ".config" / "saga-translator"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = cfg_dir / "config.toml"
        if not cfg.exists():
            cfg.write_text("[context]\nprev_tail_paras = 3\nnext_head_paras = 2\n", encoding="utf-8")

        self.work = work

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compact_analysis_mode_no_raw_toml(self):
        """分析モードで生 TOML が含まれない"""
        from build_context import _stage_impl
        # analysis は full 固定 → 生 TOML は既に廃止
        _stage_impl(self.work, "ch001", "b1", "analysis")
        out = self.work / "translation_input" / "ch001_b1_analysis.md"
        self.assertTrue(out.exists())
        text = out.read_text(encoding="utf-8")
        # 生 TOML ブロックが無い
        self.assertNotIn("```toml", text)
        # 整形済み人物表がある
        self.assertIn("登場人物表", text)

    def test_compact_characters_filtered(self):
        """compact モードで Alice (原文登場) + Carol (core) だけ"""
        from build_context import _stage_impl
        _stage_impl(self.work, "ch001", "b1", "translation")
        out = self.work / "translation_input" / "ch001_b1_translation.md"
        text = out.read_text(encoding="utf-8")
        self.assertIn("Alice", text)
        self.assertIn("キャロル", text)
        # Bob は "原文Bob。" に出ているので compact でも載る（正しい挙動）
        self.assertIn("ボブ", text)

    def test_full_mode_shows_all(self):
        """--full 相当 (context_style='full') で全件出力"""
        from build_context import _stage_impl
        _stage_impl(self.work, "ch001", "b1", "translation", context_style="full")
        out = self.work / "translation_input" / "ch001_b1_translation.md"
        text = out.read_text(encoding="utf-8")
        self.assertIn("Alice", text)
        self.assertIn("ボブ", text)  # full は全件

    def test_compact_shows_core_characters(self):
        """core マークされたキャラは原文に無くても載る"""
        from build_context import _stage_impl
        # 原文に Alice も Bob も出ない原文に変える
        chdir = self.work / "chapters"
        paras = [
            {"n": 1, "text": "天気が良い。"},
            {"n": 2, "text": "散歩しよう。"},
        ]
        (chdir / "ch001.paras.json").write_text(
            json.dumps({"chapter": "ch001", "title": None, "paras": paras}, ensure_ascii=False),
            encoding="utf-8",
        )
        _stage_impl(self.work, "ch001", "b1", "translation")
        out = self.work / "translation_input" / "ch001_b1_translation.md"
        text = out.read_text(encoding="utf-8")
        self.assertIn("キャロル", text)  # core
        self.assertNotIn("Alice", text)
        self.assertNotIn("ボブ", text)


# ── P1-4 の可視化（compact の省略人物）と P0-2 の保険（analysis の未正規化警告） ──


def _make_work(base: Path, characters: list[dict], body: list[str]) -> Path:
    """compact / analysis テスト用の最小 work（合成データのみ）。"""
    work = base / "test_work"
    work.mkdir(parents=True)
    meta = {
        "chapters": [
            {"id": "ch001", "title": None, "blocks": [{"id": "b1", "paras": [1, len(body)]}]},
        ],
        "work": {"title": "Test", "author": "Author", "lang": "zh"},
    }
    (work / "chapters_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (work / "manifest").write_text(json.dumps({"lang": "zh"}, ensure_ascii=False), encoding="utf-8")
    chdir = work / "chapters"
    chdir.mkdir()
    paras = [{"n": i + 1, "text": t} for i, t in enumerate(body)]
    (chdir / "ch001.paras.json").write_text(
        json.dumps({"chapter": "ch001", "title": None, "paras": paras}, ensure_ascii=False),
        encoding="utf-8",
    )
    (work / "output" / "ch001").mkdir(parents=True)
    (work / "translation_input").mkdir(parents=True)
    analysis = work / "analysis"
    analysis.mkdir()
    (analysis / "characters.json").write_text(
        json.dumps({"characters": characters}, ensure_ascii=False), encoding="utf-8")
    (analysis / "terms.json").write_text(json.dumps({"terms": []}, ensure_ascii=False), encoding="utf-8")
    (analysis / "plot.json").write_text(json.dumps({"entries": []}, ensure_ascii=False), encoding="utf-8")
    return work


class TestCompactOmittedCharacters(unittest.TestCase):
    """compact が人物を落としたとき、落ちた名前を stderr に出す（P1-4 の可視化）。"""

    def setUp(self):
        import shutil
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = _make_work(self.tmp, characters=[
            {"name_src": "Alice", "name_ja": "アリス", "first_person": "私"},
            {"name_src": "Bob", "name_ja": "ボブ", "core": True},
            {"name_src": "Dave", "name_ja": "デイブ", "first_person": "僕"},
        ], body=["原文Alice。", "天気が良い。"])

    def _stderr(self) -> str:
        from build_context import _stage_impl
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _stage_impl(self.work, "ch001", "b1", "translation")
        return buf.getvalue()

    def test_omitted_names_are_reported(self):
        err = self._stderr()
        self.assertIn("[context] compact: 人物 2/3（省略: デイブ）", err)

    def test_no_omitted_part_when_all_shown(self):
        # 本文に Dave も出るようにして全員載る状態にする
        paras = [
            {"n": 1, "text": "原文Alice。"},
            {"n": 2, "text": "原文Dave。"},
        ]
        (self.work / "chapters" / "ch001.paras.json").write_text(
            json.dumps({"chapter": "ch001", "title": None, "paras": paras}, ensure_ascii=False),
            encoding="utf-8",
        )
        err = self._stderr()
        self.assertIn("人物 3/3", err)
        self.assertNotIn("省略", err)


class TestOmittedCharacterNames(unittest.TestCase):
    """_omitted_character_names の単体（name_ja 優先・name_src フォールバック）。"""

    def test_prefers_ja_then_src(self):
        from build_context import _omitted_character_names
        chars = {"characters": [
            {"name_src": "A", "name_ja": "エー"},
            {"name_src": "B"},
            {"name_ja": "シー"},
        ]}
        shown = [chars["characters"][0]]
        self.assertEqual(_omitted_character_names(chars, shown), ["B", "シー"])


class TestAnalysisUnnormalizedWarning(unittest.TestCase):
    """analysis モードは未正規化画像を stderr に1行で警告する（P0-2 の保険。停止はしない）。"""

    def setUp(self):
        import shutil
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = _make_work(self.tmp, characters=[
            {"name_src": "Alice", "name_ja": "アリス"},
        ], body=["原文Alice。", "天気が良い。"])
        self.img = self.work / "output" / "images"

    def _stderr(self) -> str:
        from build_context import _stage_impl
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _stage_impl(self.work, "ch001", "b1", "analysis")
        return buf.getvalue()

    def test_warns_once_and_does_not_stop(self):
        self.img.mkdir(parents=True)
        (self.img / "插图一.png").write_bytes(b"\x89PNG")
        err = self._stderr()
        lines = [l for l in err.splitlines() if "未正規化" in l]
        self.assertEqual(len(lines), 1, f"警告は1行のはず: {err!r}")
        self.assertIn("未正規化の画像が 1 件あります", lines[0])
        self.assertIn("analysis_src_hash", lines[0])
        self.assertIn("插图一.png", lines[0])
        # 警告のみ（停止しない）: コンテキストは生成される
        self.assertTrue((self.work / "translation_input" / "ch001_b1_analysis.md").exists())

    def test_no_warning_when_images_are_normalized(self):
        self.img.mkdir(parents=True)
        (self.img / "10000000-001.png").write_bytes(b"\x89PNG")
        err = self._stderr()
        self.assertNotIn("未正規化", err)