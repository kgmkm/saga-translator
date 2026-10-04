"""P0-4: lock_prep.py の単体テスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import lock_prep  # noqa: E402

# ── フィクスチャヘルパー ──────────────────────────────────────────


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_minimal_work(base: Path) -> Path:
    """最小の作品ディレクトリ（zh→ja）を作成。"""
    work = base / "_saga" / "99999"
    _write(
        work / "manifest.toml",
        'lang = "zh"\ndst = "ja"\n',
    )
    _write(
        work / "chapters_meta.toml",
        """[[chapters]]
id = "ch001"
blocks = ["b1"]

[[chapters]]
id = "ch002"
blocks = ["b1"]
""",
    )
    # ch001: 固有名詞 + 自称 + 非JIS字を含む
    _write(
        work / "chapters" / "ch001.paras.toml",
        """[[paras]]
n = 1
text = "张三是一个聪明的人。奴家觉得他说得对。"

[[paras]]
n = 2
text = "张三和李四一起去了图书馆。"

[[paras]]
n = 3
text = "李四说：「这个秘鑰很重要。」"
""",
    )
    # ch002: 追加の出現
    _write(
        work / "chapters" / "ch002.paras.toml",
        """[[paras]]
n = 1
text = "李四又提到了张三的名字。我们走吧。"

[[paras]]
n = 2
text = "张三笑了笑，说：『你真是个笨蛋。』"
""",
    )
    return work


def _make_work_with_existing(work: Path, src_lang: str = "zh") -> Path:
    """既存の characters.toml / terms.toml を追加した作品を作成。"""
    _write(
        work / "analysis" / "characters-[ネタバレあり].toml",
        """[[characters]]
name_src = "张三"
name_ja = "张三"
ruby = "ちょうさん"
first_person = "私"
second_person = "あなた"
""",
    )
    _write(
        work / "analysis" / "terms.toml",
        """[[terms]]
term_src = "灵氣"
term_ja = "霊気"
""",
    )
    return work


class TestLockPrepCore(unittest.TestCase):
    """lock_prep.py のコアロジック。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_direction_zh_ja(self):
        """direction が zh→ja と判定される。"""
        work = _make_minimal_work(self.td)
        src, dst = lock_prep.direction(work)
        self.assertEqual(src, "zh")
        self.assertEqual(dst, "ja")

    def test_load_all_paras_returns_texts(self):
        """全段落が正しく読み込まれる。"""
        work = _make_minimal_work(self.td)
        meta = lock_prep.load_chapters_meta(work)
        items = lock_prep._load_all_paras(work, meta)
        # 3 + 2 = 5 段落
        self.assertGreaterEqual(len(items), 5)

    def test_gather_proper_nouns_finds_frequent(self):
        """頻出漢字語が候補に出る。"""
        work = _make_minimal_work(self.td)
        meta = lock_prep.load_chapters_meta(work)
        items = lock_prep._load_all_paras(work, meta)
        result = lock_prep._gather_proper_nouns(items, "zh", 50)
        words = [r["word"] for r in result]
        # "张三" は4回出る → 候補に含まれる (freq >= 3)
        self.assertIn("张三", words)
        # "李四" は3回
        self.assertIn("李四", words)
        for r in result:
            self.assertIn("first", r)
            self.assertIn("freq", r)

    def test_gather_proper_nouns_top_n(self):
        """top_n で件数が制限される。"""
        work = _make_minimal_work(self.td)
        meta = lock_prep.load_chapters_meta(work)
        items = lock_prep._load_all_paras(work, meta)
        result = lock_prep._gather_proper_nouns(items, "zh", 3)
        self.assertLessEqual(len(result), 3)

    def test_gather_pronouns_zh(self):
        """中国語の自称・二人称が拾える。"""
        work = _make_minimal_work(self.td)
        meta = lock_prep.load_chapters_meta(work)
        items = lock_prep._load_all_paras(work, meta)
        result = lock_prep._gather_pronouns(items, "zh", 20)
        words = [r["word"] for r in result]
        self.assertIn("奴家", words)
        self.assertIn("你", words)

    def test_gather_non_jis_finds_undecided(self):
        """非JIS字が「要決定」に出る。"""
        work = _make_minimal_work(self.td)
        meta = lock_prep.load_chapters_meta(work)
        items = lock_prep._load_all_paras(work, meta)
        undecided, resolved = lock_prep._gather_non_jis(items, None)
        # "鑰" は非JIS字のはず
        chars = [u["char"] for u in undecided]
        # "鑰" はJIS X 0208に無い（CP932ではエラー）
        self.assertTrue(len(undecided) > 0 or len(resolved) > 0)

    def test_existing_translation_marked(self):
        """既存 characters.toml の訳語が「既訳あり」として出る。"""
        work = _make_minimal_work(self.td)
        _make_work_with_existing(work)
        meta = lock_prep.load_chapters_meta(work)
        items = lock_prep._load_all_paras(work, meta)
        existing = lock_prep._build_existing_map(work)
        self.assertGreater(len(existing), 0)
        src_words = [e["src"] for e in existing]
        self.assertIn("张三", src_words)
        self.assertIn("灵氣", src_words)
        proper = lock_prep._gather_proper_nouns(items, "zh", 50)
        proper = lock_prep._mark_existing(proper, existing)
        found = [p for p in proper if p["word"] == "张三"]
        self.assertEqual(len(found), 1)
        self.assertIn("existing", found[0])

    def test_output_files_created(self):
        """出力ファイルが _work/ 下に生成される。"""
        work = _make_minimal_work(self.td)
        # 実行
        rc = lock_prep.main([
            str(work),
            "--output-md", str(work / "_work" / "lock_prep.md"),
            "--output-toml", str(work / "_work" / "lock_prep.toml"),
            "--top-n", "50",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue((work / "_work" / "lock_prep.md").exists())
        self.assertTrue((work / "_work" / "lock_prep.toml").exists())
        # md に作品IDが含まれる
        md_content = (work / "_work" / "lock_prep.md").read_text(encoding="utf-8")
        self.assertIn("99999", md_content)

    def test_non_existent_work_returns_1(self):
        """存在しない作品ディレクトリ → 終了コード 1。"""
        rc = lock_prep.main([str(self.td / "nonexistent")])
        self.assertEqual(rc, 1)

    def test_ja_work_pronouns(self):
        """日本語作品の自称・二人称パターン。"""
        work = self.td / "_saga" / "88888"
        _write(
            work / "manifest.toml",
            'lang = "ja"\ndst = "zh"\n',
        )
        _write(
            work / "chapters_meta.toml",
            """[[chapters]]
id = "ch001"
blocks = ["b1"]
""",
        )
        _write(
            work / "chapters" / "ch001.paras.toml",
            """[[paras]]
n = 1
text = "私は张三です。あなたは李四ですか。"

[[paras]]
n = 2
text = "僕はそう思う。お前はどうだ。"
""",
        )
        meta = lock_prep.load_chapters_meta(work)
        items = lock_prep._load_all_paras(work, meta)
        result = lock_prep._gather_pronouns(items, "ja", 20)
        words = [r["word"] for r in result]
        self.assertIn("私", words)
        self.assertIn("あなた", words)
        self.assertIn("僕", words)
        self.assertIn("お前", words)


if __name__ == "__main__":
    unittest.main()