"""saga-translator scripts の単体テスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402


class TestLib(unittest.TestCase):
    def test_split_paras(self):
        text = "一段目。\n\n二段目。\n\r\n\r\n三段目。"
        self.assertEqual(_lib.split_paras(text), ["一段目。", "二段目。", "三段目。"])

    def test_parse_novel_header(self):
        text = "タイトル\n\n作者名\n\nhttps://example.com/n/1\n\n2023-02-23\n\n#R-18\n#オリジナル\n\n----- 以下は本文 -----\n\n本文一段目。\n\n本文二段目。"
        meta, body = _lib.parse_novel_header(text)
        self.assertEqual(meta["title"], "タイトル")
        self.assertEqual(meta["author"], "作者名")
        self.assertEqual(meta["source_url"], "https://example.com/n/1")
        self.assertIn("#R-18", meta["tags"])
        self.assertIn("本文一段目。", body)
        self.assertNotIn("以下は本文", body)

    def test_parse_novel_header_absent(self):
        meta, body = _lib.parse_novel_header("ただの本文。\n\n二段落。")
        self.assertIsNone(meta)
        self.assertIn("ただの本文。", body)

    def test_parse_filename(self):
        d = _lib.parse_filename(Path("19360197-堕魔姬奴-テスト【1】.txt"))
        self.assertEqual(d["novel_id"], "19360197")
        self.assertEqual(d["part"], "1")
        self.assertIn("堕魔姬奴", d["title"])

    def test_guess_lang_simplified(self):
        r = _lib.guess_lang("这是中文小说。他们说的话。")
        self.assertEqual(r["lang"], "zh")
        self.assertEqual(r["han_script"], "simplified")

    def test_guess_lang_japanese(self):
        r = _lib.guess_lang("これは日本語の文章です。テキスト。")
        self.assertEqual(r["lang"], "ja")

    def test_find_chapters(self):
        paras = ["第1章 開始", "本文A", "本文B", "第2章 続き", "本文C"]
        chs = _lib.find_chapters(paras)
        self.assertEqual(len(chs), 2)
        self.assertEqual(chs[0][0], "第1章 開始")
        self.assertEqual(chs[0][1], 0)
        self.assertEqual(chs[0][2], 3)
        self.assertEqual(chs[1][0], "第2章 続き")

    def test_find_chapters_none(self):
        chs = _lib.find_chapters(["本文だけ", "章立てなし"])
        self.assertEqual(len(chs), 1)
        self.assertIsNone(chs[0][0])
        self.assertEqual(chs[0][2], 2)

    def test_find_chapters_zh_pack(self):
        paras = ["第1章 開始", "本文A", "楔子", "本文B"]
        chs = _lib.find_chapters(paras, lang="zh")
        self.assertEqual(len(chs), 2)
        self.assertEqual(chs[0][0], "第1章 開始")
        self.assertEqual(chs[1][0], "楔子")

    def test_find_chapters_en_ignores_zh_heading(self):
        paras = ["第1章 開始", "本文A", "本文B"]
        chs = _lib.find_chapters(paras, lang="en")
        self.assertEqual(len(chs), 1)
        self.assertIsNone(chs[0][0])

    def test_guess_lang_korean(self):
        r = _lib.guess_lang("이것은 한국어 소설입니다. 주인공이 말했다.")
        self.assertEqual(r["lang"], "ko")

    def test_make_blocks_scene_break_priority(self):
        # 場面転換が目標文字数の範囲内にあれば、その直後で切る
        paras = ["あ" * 50] * 5 + ["＊＊＊"] + ["い" * 50] * 5
        blocks = _lib.make_blocks(paras, target_chars=300)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][1], 6)  # ＊＊＊まで含めて切る
        self.assertEqual(blocks[1][0], 6)

    def test_make_blocks_no_break(self):
        # 場面転換がなければ目標超過後の最初の段落境界で切る(わずかな超過を許す)
        paras = ["あ" * 30] * 8
        blocks = _lib.make_blocks(paras, target_chars=100)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][1], 4)
        self.assertEqual(blocks[1][1], 8)

    def test_make_blocks_covers_all(self):
        paras = ["あ" * 100] * 12
        blocks = _lib.make_blocks(paras, target_chars=250)
        self.assertEqual(blocks[0][0], 0)
        self.assertEqual(blocks[-1][1], len(paras))
        for (s1, e1), (s2, e2) in zip(blocks, blocks[1:]):
            self.assertEqual(e1, s2)

    def test_atomic_write(self):
        p = Path(SCRIPTS.parent) / "_test_atomic.tmp.md"
        _lib.atomic_write_text(p, "ok")
        self.assertEqual(p.read_text(encoding="utf-8"), "ok")
        p.unlink()


class TestContextFmt(unittest.TestCase):
    def test_fmt_characters_splits_voice_fields(self):
        from build_context import fmt_characters
        text = fmt_characters({"characters": [{
            "name_src": "张三",
            "name_ja": "チョウサン",
            "first_person": "あたし",
            "second_person": "あなた",
            "speech_style": "タメ口。語尾は「〜だよ」「〜じゃん」",
            "vocab": "擬音が多い",
            "catchphrase": "まかせて！",
            "address": [{"target_ja": "王五", "form": "王五さん"}],
        }]})
        self.assertIn("一人称: あたし", text)
        self.assertIn("二人称: あなた", text)
        self.assertIn("王五→王五さん", text)
        self.assertIn("口調: タメ口。語尾は「〜だよ」「〜じゃん」", text)
        self.assertIn("語彙: 擬音が多い", text)
        self.assertIn("口癖: まかせて！", text)
        self.assertNotIn("語調メモ", text)

    def test_fmt_characters_falls_back_to_tone(self):
        from build_context import fmt_characters
        text = fmt_characters({"characters": [{
            "name_ja": "旧キャラ",
            "first_person": "私",
            "tone": "丁寧だが誇り高い。口癖は「〜ですわ」",
        }]})
        self.assertIn("語調(旧):", text)
        self.assertNotIn("口調:", text)

    def test_fmt_arcs_new_fields(self):
        from build_context import fmt_arcs
        text = fmt_arcs({"characters": [{
            "name_ja": "チョウサン",
            "arcs": [{
                "at_block": "ch001/b3",
                "first_person": "あたし",
                "speech_style": "崩れたタメ口。語尾は「〜だよ」",
                "note": "変化後",
            }],
        }]})
        self.assertIn("ch001/b3〜", text)
        self.assertIn("口調: 崩れたタメ口", text)
        self.assertIn("一人称: あたし", text)
        self.assertIn("変化後", text)


class TestQa(unittest.TestCase):
    def setUp(self):
        import json
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "chapters").mkdir()
        (self.tmp / "analysis").mkdir()
        (self.tmp / "output").mkdir()
        paras = [{"n": i + 1, "text": t} for i, t in enumerate(["原文一。", "原文二。", "原文三。", "原文四。"])]
        (self.tmp / "chapters" / "ch001.paras.json").write_text(
            json.dumps({"chapter": "ch001", "title": None, "paras": paras}, ensure_ascii=False), encoding="utf-8")
        meta = {"chapters": [{"id": "ch001", "title": None, "blocks": [{"id": "b1", "paras": [1, 4]}]}]}
        (self.tmp / "chapters_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        (self.tmp / "analysis" / "characters.json").write_text(
            json.dumps({"characters": [{"name_src": "张三", "name_ja": "チョウサン"}]}, ensure_ascii=False), encoding="utf-8")
        self.json = json
        self.shutil = shutil

    def tearDown(self):
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_draft(self):
        from qa_translation import parse_draft
        items, _ = parse_draft("[P001] 一段目の訳。\n続きの行。\n\n[P002] 二段目。\n\n[P003] 三段目。\n\n[P004] 四段目。")
        self.assertEqual([n for n, _ in items], [1, 2, 3, 4])
        self.assertEqual(items[0][1], "一段目の訳。\n続きの行。")

    def test_qa_pass(self):
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 訳一。\n\n[P002] 訳二。\n\n[P003] 訳三。\n\n[P004] 訳四。", encoding="utf-8")
        report, items = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertTrue(report["pass"])
        self.assertEqual(len(items), 4)

    def test_qa_sequence_fail(self):
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 訳一。\n\n[P003] 訳三。\n\n[P004] 訳四。", encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertFalse(report["pass"])
        self.assertFalse(report["checks"]["paragraph_sequence"]["pass"])

    def test_qa_leftover_name_fail(self):
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 张三は走った。\n\n[P002] 訳二。\n\n[P003] 訳三。\n\n[P004] 訳四。", encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertFalse(report["pass"])
        self.assertEqual(report["checks"]["leftover_source_names"]["found"][0]["name_src"], "张三")

    def test_qa_leftover_name_in_ja_ok(self):
        # 訳語に漢字がそのまま含まれる名前(灰狼→暗影団の頭目・灰狼)は残留扱いしない
        from qa_translation import run_qa
        (self.tmp / "analysis" / "characters.json").write_text(
            self.json.dumps({"characters": [{"name_src": "灰狼", "name_ja": "暗影団の頭目・灰狼"}]},
                            ensure_ascii=False), encoding="utf-8")
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 暗影団の頭目・灰狼が現れた。\n\n[P002] 訳二。\n\n[P003] 訳三。\n\n[P004] 訳四。", encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertTrue(report["checks"]["leftover_source_names"]["pass"])

    def test_qa_non_jis_han_fail(self):
        # 日本語に無い漢字(簡体字・原文の誤字)が残ったら不合格
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 沈剑平が入ってきた。\n\n[P002] 訳二。\n\n[P003] 訳三。\n\n[P004] 訳四。", encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertFalse(report["pass"])
        self.assertEqual(report["checks"]["non_jis_han"]["found"][0]["char"], "剑")

    def test_qa_non_jis_han_ok(self):
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text("[P001] 沈見平が入ってきた。\n\n[P002] 訳二。\n\n[P003] 訳三。\n\n[P004] 訳四。", encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertTrue(report["checks"]["non_jis_han"]["pass"])

    def test_qa_merged_paragraphs_fail(self):
        # 段落をまたぐ閉じ漏れ: layout が段落の区切りを消す(3段落以上は不合格)
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text(
            "[P001] 「台詞が開いたまま\n\n[P002] 段落が続き、\n\n[P003] さらに続く。\n\n[P004] 訳四。",
            encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertFalse(report["pass"])
        found = report["checks"]["merged_paragraphs"]["found"]
        self.assertEqual(found[0]["first"], 1)
        self.assertGreaterEqual(found[0]["count"], 3)

    def test_qa_merged_two_paragraphs_warn(self):
        # 2段落の融合も境界を消すため不合格（stage6 で fatal 化。旧「警告のみ」から契約変更）
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text(
            "[P001] 「台詞が開いたまま\n\n[P002] 閉じる」\n\n[P003] 訳三。\n\n[P004] 訳四。",
            encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertTrue(report["checks"]["merged_paragraphs"]["found"])
        self.assertFalse(report["checks"]["merged_paragraphs"]["pass"])
        self.assertFalse(report["pass"])

    def test_qa_song_within_paragraph_not_merged(self):
        # 1段落内の複数行(歌・詩)は段落融合ではない
        from qa_translation import run_qa
        draft = self.tmp / "draft.md"
        draft.write_text(
            "[P001] 「うた いち\nうた に\nうた さん」\n\n[P002] 訳二。\n\n[P003] 訳三。\n\n[P004] 訳四。",
            encoding="utf-8")
        report, _ = run_qa(self.tmp, "ch001", "b1", draft)
        self.assertTrue(report["checks"]["merged_paragraphs"]["pass"])
        self.assertTrue(report["pass"])

    def test_find_non_jis_han(self):
        from _lib import find_non_jis_han
        self.assertEqual(find_non_jis_han("日本語の文章。"), [])
        hits = find_non_jis_han("沈剑平")
        self.assertEqual(hits, [("剑", 1)])
        # JIS収録だが読めない字は言語packのNON_JIS_HANで拾う
        hits = find_non_jis_han("高橋筱")
        self.assertEqual(hits, [("筱", 1)])


class TestRename(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "output" / "ch001").mkdir(parents=True)
        (self.tmp / "output" / "ch001" / "b1_ja.md").write_text("山田太郎は剣を振る。山田太郎の眼は澄む。", encoding="utf-8")
        self.shutil = shutil

    def tearDown(self):
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_and_apply(self):
        from rename_terms import scan
        hits = scan(self.tmp, "山田太郎")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["total"], 2)
        self.assertTrue(hits[0]["multi_in_para"])
        f = self.tmp / "output" / "ch001" / "b1_ja.md"
        f.write_text(f.read_text(encoding="utf-8").replace("山田太郎", "山田花子"), encoding="utf-8")
        self.assertNotIn("山田太郎は", f.read_text(encoding="utf-8"))


class TestTomlAndPurge(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.shutil = shutil

    def tearDown(self):
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paras_toml_roundtrip(self):
        obj = {"chapter": "ch001", "title": "第1章", "paras": [
            {"n": 1, "text": '彼は"そう"と言った。'},
            {"n": 2, "text": "二段落。"},
        ]}
        _lib.save_paras(self.tmp, "ch001", obj)
        self.assertTrue((self.tmp / "chapters" / "ch001.paras.toml").exists())
        self.assertFalse((self.tmp / "chapters" / "ch001.paras.json").exists())
        got = _lib.load_paras(self.tmp, "ch001")
        self.assertEqual(got["paras"][0]["text"], '彼は"そう"と言った。')
        self.assertEqual(got["chapter"], "ch001")

    def test_chapters_meta_nested_roundtrip(self):
        meta = {
            "work": {"title": "題", "author": "", "lang": "zh"},
            "chapters": [{
                "id": "ch001",
                "title": "第1章",
                "blocks": [{
                    "id": "b1",
                    "paras": [1, 4],
                    "n_chars": 10,
                    "scene_break_cut": False,
                    "translation": {"model": "x", "prompt_version": "v3", "tokens_in": 1, "tokens_out": 2},
                    "status": "done",
                }],
                "status": "done",
            }],
        }
        _lib.save_chapters_meta(self.tmp, meta)
        got = _lib.load_chapters_meta(self.tmp)
        self.assertEqual(got["chapters"][0]["blocks"][0]["paras"], [1, 4])
        self.assertEqual(got["chapters"][0]["blocks"][0]["translation"]["model"], "x")

    def test_load_json_fallback(self):
        import json
        p = self.tmp / "chapters_meta.json"
        p.write_text(json.dumps({"work": {"title": "旧"}, "chapters": []}, ensure_ascii=False), encoding="utf-8")
        got = _lib.load_chapters_meta(self.tmp)
        self.assertEqual(got["work"]["title"], "旧")

    def test_purge_source_keeps_translation(self):
        (self.tmp / "manifest.toml").write_text("title = \"x\"\n", encoding="utf-8")
        (self.tmp / "originals").mkdir()
        (self.tmp / "originals" / "source.md").write_text("原文。", encoding="utf-8")
        (self.tmp / "chapters").mkdir()
        (self.tmp / "chapters" / "ch001.paras.toml").write_text("chapter = \"ch001\"\n", encoding="utf-8")
        (self.tmp / "output" / "ch001").mkdir(parents=True)
        (self.tmp / "output" / "ch001" / "b1_ja.md").write_text("訳。", encoding="utf-8")
        (self.tmp / "output" / "ch001" / "b1_parallel.md").write_text("対訳。", encoding="utf-8")
        (self.tmp / "output" / "ch001_ja.md").write_text("章訳。", encoding="utf-8")
        (self.tmp / "output" / "全文_訳文.md").write_text("全文。", encoding="utf-8")
        (self.tmp / "analysis").mkdir()
        (self.tmp / "analysis" / "characters.toml").write_text("[[characters]]\nname_ja = \"A\"\n", encoding="utf-8")
        removed = _lib.purge_source(self.tmp)
        self.assertTrue(any("originals" in r for r in removed))
        self.assertFalse((self.tmp / "originals").exists())
        self.assertFalse((self.tmp / "chapters").exists())
        self.assertFalse((self.tmp / "output" / "ch001").exists())
        self.assertFalse((self.tmp / "output" / "ch001" / "b1_parallel.md").exists())
        self.assertTrue((self.tmp / "output" / "ch001_ja.md").exists())
        self.assertTrue((self.tmp / "output" / "全文_訳文.md").exists())
        self.assertTrue((self.tmp / "analysis" / "characters.toml").exists())

    def test_purge_source_keep_parallel(self):
        (self.tmp / "manifest.toml").write_text("title = \"x\"\n", encoding="utf-8")
        (self.tmp / "originals").mkdir()
        (self.tmp / "originals" / "source.md").write_text("原文。", encoding="utf-8")
        (self.tmp / "output").mkdir(exist_ok=True)
        (self.tmp / "output" / "全文_訳文.md").write_text("全文。", encoding="utf-8")
        (self.tmp / "output" / "全文_対訳.md").write_text("対訳。", encoding="utf-8")
        (self.tmp / "output" / "ch001_parallel.md").write_text("章対訳。", encoding="utf-8")
        removed = _lib.purge_source(self.tmp, keep_parallel=True)
        self.assertTrue((self.tmp / "output" / "全文_対訳.md").exists())
        self.assertTrue((self.tmp / "output" / "ch001_parallel.md").exists())
        self.assertTrue((self.tmp / "output" / "全文_訳文.md").exists())
        self.assertFalse((self.tmp / "originals").exists())
        self.assertNotIn("output/全文_対訳.md", removed)

    def test_load_characters_prefers_spoiler_name(self):
        (self.tmp / "analysis").mkdir(exist_ok=True)
        (self.tmp / "analysis" / "characters.toml").write_text(
            "[[characters]]\nname_ja = \"旧\"\n", encoding="utf-8")
        (self.tmp / "analysis" / "characters-[ネタバレあり].toml").write_text(
            "[[characters]]\nname_ja = \"新\"\n", encoding="utf-8")
        got = _lib.load_characters(self.tmp)
        self.assertEqual(got["characters"][0]["name_ja"], "新")

    def test_load_titles(self):
        (self.tmp / "analysis").mkdir(exist_ok=True)
        (self.tmp / "analysis" / "titles.toml").write_text(
            'title_src = "奇点计划"\ntitle_ja = "特異点計画"\n', encoding="utf-8")
        got = _lib.load_titles(self.tmp)
        self.assertEqual(got["title_ja"], "特異点計画")

    def test_load_toml_broken_raises(self):
        (self.tmp / "analysis").mkdir(exist_ok=True)
        bad = self.tmp / "analysis" / "titles.toml"
        bad.write_text('title_ja = "壊れ\nchapters = [あ]\n', encoding="utf-8")
        with self.assertRaises(_lib.TomlSyntaxError):
            _lib.load_titles(self.tmp)

    def test_validate_expected_missing_key(self):
        with self.assertRaises(_lib.TomlSyntaxError):
            _lib.validate_expected(self.tmp / "x.toml", {}, {"title_ja": "str"})

    def test_validate_expected_characters(self):
        _lib.validate_expected(self.tmp / "x.toml", {"characters": [{"name_ja": "A"}]}, {"characters": "list_of_dicts"})
        with self.assertRaises(_lib.TomlSyntaxError):
            _lib.validate_expected(self.tmp / "x.toml", {"characters": "broken"}, {"characters": "list_of_dicts"})
        with self.assertRaises(_lib.TomlSyntaxError):
            _lib.validate_expected(self.tmp / "x.toml", {"characters": ["str"]}, {"characters": "list_of_dicts"})

    def test_load_characters_broken_struct(self):
        (self.tmp / "analysis").mkdir(exist_ok=True)
        (self.tmp / "analysis" / "characters-[ネタバレあり].toml").write_text(
            'characters = "テーブルじゃない"\n', encoding="utf-8")
        with self.assertRaises(_lib.TomlSyntaxError):
            _lib.load_characters(self.tmp)

    def test_load_chapters_meta_broken(self):
        p = self.tmp / "chapters_meta.toml"
        p.write_text('chapters = "not a list"\n', encoding="utf-8")
        with self.assertRaises(_lib.TomlSyntaxError):
            _lib.load_chapters_meta(self.tmp)

    def test_emit_writes_parallel_intermediate(self):
        from qa_translation import emit
        from _lib import save_chapters_meta, save_paras
        (self.tmp / "output" / "_work").mkdir(parents=True)
        (self.tmp / "output" / "_work" / "ch001_b1_ja_numbered.md").write_text("ドラフト。", encoding="utf-8")
        save_paras(self.tmp, "ch001", {"paras": [{"n": 1, "text": "原一。"}, {"n": 2, "text": "原二。"}]})
        save_chapters_meta(self.tmp, {"chapters": [{"id": "ch001", "status": "pending", "blocks": [{"id": "b1", "status": "pending", "paras": [1, 2], "n_chars": 0}]}]})
        emit(self.tmp, "ch001", "b1", [(1, "訳一。"), (2, "訳二。")])
        ja = (self.tmp / "output" / "ch001" / "b1_ja.md").read_text(encoding="utf-8")
        self.assertIn("訳一。", ja)
        par = (self.tmp / "output" / "ch001" / "b1_parallel.md").read_text(encoding="utf-8")
        self.assertIn("> [P001] 原一。", par)
        self.assertIn("> [P002] 原二。", par)
        self.assertIn("訳二。", par)
        self.assertFalse((self.tmp / "output" / "_work" / "ch001_b1_ja_numbered.md").exists())


class TestAssemble(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.shutil = shutil

    def tearDown(self):
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup_work(self, with_age_report: bool) -> None:
        from _lib import save_chapters_meta, save_paras
        (self.tmp / "output" / "ch001").mkdir(parents=True)
        (self.tmp / "analysis").mkdir()
        save_chapters_meta(self.tmp, {"work": {"title": "x"}, "chapters": [
            {"id": "ch001", "title": "", "status": "emitted", "blocks": [
                {"id": "b1", "paras": [1, 2], "status": "emitted"}]}]})
        save_paras(self.tmp, "ch001", {"paras": [{"n": 1, "text": "原一。"}, {"n": 2, "text": "原二。"}]})
        (self.tmp / "analysis" / "titles.toml").write_text(
            'title_src = "x"\ntitle_ja = "x（改題）"\n\n'
            '[[chapters]]\nid = "ch001"\nsrc = "第一章"\nja = "第一章　始まり"\n', encoding="utf-8")
        (self.tmp / "output" / "ch001" / "b1_ja.md").write_text("訳一。\n\n訳二。\n", encoding="utf-8")
        (self.tmp / "output" / "ch001" / "b1_parallel.md").write_text(
            "> [P001] 原一。\n\n訳一。\n\n> [P002] 原二。\n\n訳二。\n", encoding="utf-8")
        if with_age_report:
            (self.tmp / "analysis" / "age_policy_report-[ネタバレあり].md").write_text(
                "# 年齢・設定変更通知\n", encoding="utf-8")

    def _run(self, *extra: str) -> None:
        import sys
        from unittest import mock
        from assemble import main
        with mock.patch.object(sys, "argv", ["assemble", str(self.tmp), "--keep-full", *extra]):
            main()

    def test_auto_parallel_when_age_report_exists(self):
        self._setup_work(True)
        self._run()
        self.assertTrue((self.tmp / "output" / "全文_対訳.md").exists())
        self.assertTrue((self.tmp / "output" / "ch001_parallel.md").exists())
        par = (self.tmp / "output" / "全文_対訳.md").read_text(encoding="utf-8")
        self.assertIn("> [P001] 原一。", par)
        toc = (self.tmp / "output" / "目次.md").read_text(encoding="utf-8")
        self.assertIn("ch001_parallel.md", toc)
        # 全ブロック確定済みなので purge が走る。対訳は残り、原文・中間物は消える
        self.assertFalse((self.tmp / "chapters").exists())
        self.assertTrue((self.tmp / "output" / "全文_対訳.md").exists())
        self.assertFalse((self.tmp / "output" / "ch001").exists())

    def test_no_parallel_without_age_report_or_flag(self):
        self._setup_work(False)
        self._run()
        self.assertTrue((self.tmp / "output" / "全文_訳文.md").exists())
        self.assertFalse((self.tmp / "output" / "全文_対訳.md").exists())

    def test_parallel_flag_forces_output(self):
        self._setup_work(False)
        self._run("--parallel")
        self.assertTrue((self.tmp / "output" / "全文_対訳.md").exists())

    def test_chapter_heading_paragraph_not_duplicated(self):
        # 本文先頭の章題段落と同文の自動見出しは二重化するので付けない
        from _lib import save_chapters_meta, save_paras
        (self.tmp / "output" / "ch001").mkdir(parents=True)
        (self.tmp / "output" / "ch002").mkdir()
        (self.tmp / "analysis").mkdir()
        save_chapters_meta(self.tmp, {"work": {"title": "x"}, "chapters": [
            {"id": "ch001", "title": "", "status": "emitted", "blocks": [
                {"id": "b1", "paras": [1, 1], "status": "emitted"}]},
            {"id": "ch002", "title": "第二章", "status": "emitted", "blocks": [
                {"id": "b1", "paras": [1, 2], "status": "emitted"}]}]})
        save_paras(self.tmp, "ch001", {"paras": [{"n": 1, "text": "前置き。"}]})
        save_paras(self.tmp, "ch002", {"paras": [{"n": 1, "text": "第二章"}, {"n": 2, "text": "本文。"}]})
        (self.tmp / "output" / "ch001" / "b1_ja.md").write_text("前置きの訳。\n", encoding="utf-8")
        (self.tmp / "output" / "ch002" / "b1_ja.md").write_text(
            "第二章　テスト章\n\n本文の訳。\n", encoding="utf-8")
        (self.tmp / "analysis" / "titles.toml").write_text(
            'title_src = "x"\ntitle_ja = "テスト作品"\n\n'
            '[[chapters]]\nid = "ch001"\nsrc = ""\nja = ""\n\n'
            '[[chapters]]\nid = "ch002"\nsrc = "第二章"\nja = "第二章　テスト章"\n',
            encoding="utf-8", newline="\n")
        self._run()
        ch2 = (self.tmp / "output" / "ch002_ja.md").read_text(encoding="utf-8")
        self.assertFalse(ch2.startswith("# "))
        self.assertEqual(ch2.count("第二章　テスト章"), 1)
        full = (self.tmp / "output" / "全文_訳文.md").read_text(encoding="utf-8")
        self.assertEqual(full.count("第二章　テスト章"), 1)


class TestUploadedImage(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.shutil = shutil

    def tearDown(self):
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rewrite_finds_id_in_filename(self):
        from ingest import rewrite_uploaded_images
        src_dir = self.tmp / "src"
        src_dir.mkdir()
        img = src_dir / "24780972-20956211-題.png"
        img.write_bytes(b"\x89PNG")
        work_images = self.tmp / "work" / "images"
        text = "前\n\n[uploadedimage:20956211]\n\n後"
        new, copied, missing = rewrite_uploaded_images(text, work_images, [src_dir])
        self.assertEqual(missing, [])
        self.assertIn("![](images/24780972-20956211-題.png)", new)
        self.assertNotIn("[uploadedimage:20956211]", new)
        self.assertTrue((work_images / "24780972-20956211-題.png").exists())
        self.assertTrue(copied)

    def test_rewrite_missing_kept(self):
        from ingest import rewrite_uploaded_images
        new, copied, missing = rewrite_uploaded_images(
            "[uploadedimage:20956211]", self.tmp / "images", [self.tmp]
        )
        self.assertEqual(new, "[uploadedimage:20956211]")
        self.assertEqual(missing, ["20956211"])
        self.assertEqual(copied, [])

    def test_rewrite_no_partial_id(self):
        from ingest import find_image_for_id
        d = self.tmp / "src"
        d.mkdir()
        (d / "x-2095621-y.png").write_bytes(b"x")
        self.assertIsNone(find_image_for_id("20956211", [d]))

    def test_relink_from_work_images(self):
        from ingest import relink_markdown_files
        work = self.tmp / "work"
        (work / "output").mkdir(parents=True)
        (work / "output" / "images").mkdir()
        (work / "output" / "images" / "20956216.png").write_bytes(b"x")
        md = work / "output" / "全文_訳文.md"
        md.write_text("本文\n\n[uploadedimage:20956216]\n", encoding="utf-8")
        still = relink_markdown_files(work)
        self.assertEqual(still, [])
        self.assertIn("![](images/20956216.png)", md.read_text(encoding="utf-8"))

    def test_retarget_fixes_legacy_prefix(self):
        from ingest import retarget_md_images
        text = "![a](../images/x.png) ![b](images/y.png)"
        self.assertEqual(retarget_md_images(text), "![a](images/x.png) ![b](images/y.png)")

    def test_purge_moves_old_images_to_output(self):
        (self.tmp / "manifest.toml").write_text("title = \"x\"\n", encoding="utf-8")
        (self.tmp / "images").mkdir()
        (self.tmp / "images" / "a.png").write_bytes(b"x")
        (self.tmp / "output").mkdir()
        (self.tmp / "output" / "全文_訳文.md").write_text("t", encoding="utf-8")
        _lib.purge_source(self.tmp)
        self.assertTrue((self.tmp / "output" / "images" / "a.png").exists())
        self.assertFalse((self.tmp / "images").exists())


if __name__ == "__main__":
    unittest.main()
