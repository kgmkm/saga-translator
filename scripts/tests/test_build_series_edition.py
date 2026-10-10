# -*- coding: utf-8 -*-
"""Tests for build_series_edition.py (stdlib only)."""
import os, re, shutil, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_series_edition as bse  # noqa: E402


def make_work(saga: Path, wid: str, title_ja=None, title_src=None, n_paras=4, with_scaffold=True, images=2):
    w = saga / wid
    (w / "output" / "images").mkdir(parents=True)
    (w / "analysis").mkdir()
    (w / "manifest.toml").write_text(
        f'source_path = "x"\nwork_id = "{wid}"\ntitle = "{title_src or ("原作" + wid)}"\nauthor = "作者"\n',
        encoding="utf-8")
    (w / "chapters_meta.toml").write_text(
        f'[work]\nwork_id = "{wid}"\ntitle = "{title_src or ("原作" + wid)}"\n', encoding="utf-8")
    if title_ja:
        (w / "analysis" / "titles.toml").write_text(
            f'# 作品タイトル。\n\ntitle_src = "{title_src or ""}"\ntitle_ja = "{title_ja}"\n', encoding="utf-8")
    body = []
    if with_scaffold:
        body += ["<!-- ch001 -->", "# ch001", ""]
    body += ["本文その1。" + wid, "", "「セリフ」", "", "本文その2。"]
    for i in range(images):
        body += ["", f"![img](images/{wid}-{i}.png)"]
    (w / "output" / "全文_訳文.md").write_text("\r\n".join(body) + "\r\n", encoding="utf-8")
    (w / "output" / "目次.md").write_text("# 目次\r\n\r\n- [ch001](ch001_ja.md)\r\n", encoding="utf-8")
    for i in range(images):
        (w / "output" / "images" / f"{wid}-{i}.png").write_bytes(b"\x89PNG" + bytes([i]))
    return w


def make_saga(root: Path, works=("11111111", "22222222"), progress=True, titles=True):
    saga = root / "_saga"
    saga.mkdir(parents=True)
    for i, wid in enumerate(works):
        make_work(saga, wid,
                  title_ja=f"第{i+1}話 テスト{i+1}" if titles else None,
                  title_src=f"原作第{i+1}話")
    if progress:
        lines = ["# 進捗", "", "| # | 作品ID | 原題 | 訳題 | ブロック | 状態 |", "|---|---|---|---|---|---|"]
        for i, wid in enumerate(works):
            ja = f"第{i+1}話 テスト{i+1}" if titles else "—"
            lines.append(f"| {i+1} | {wid} | 原作第{i+1}話 | {ja} | 8 | ✅ |")
        lines.append("| 9 | （未所持） | — | — | — | ⬜ |")
        (saga / "進捗.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return saga


class TestDiscovery(unittest.TestCase):
    def test_find_saga_direct_and_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root)
            self.assertEqual(bse.find_saga(root), saga)
            self.assertEqual(bse.find_saga(saga), saga)

    def test_missing_saga_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(bse.BuildError):
                bse.find_saga(Path(td))


class TestOrderAndTitles(unittest.TestCase):
    def test_order_from_progress(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root, works=("3333", "1111"))
            built, _ = bse.list_works(saga)
            prog = bse.parse_progress(saga)
            order, src = bse.resolve_order(built, None, prog)
            self.assertEqual(order, ["3333", "1111"])
            self.assertIn("進捗", src)

    def test_order_fallback_id_ascending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root, works=("3333", "1111"), progress=False)
            built, _ = bse.list_works(saga)
            order, src = bse.resolve_order(built, None, None)
            self.assertEqual(order, ["1111", "3333"])

    def test_explicit_works(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root, works=("3333", "1111"))
            built, _ = bse.list_works(saga)
            order, src = bse.resolve_order(built, ["1111", "3333"], None)
            self.assertEqual(order, ["1111", "3333"])
            self.assertEqual(src, "--works")

    def test_title_resolution_chain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root)
            prog = bse.parse_progress(saga)
            w = saga / "11111111"
            t, s = bse.resolve_title(w, "11111111", prog)
            self.assertEqual(t, "第1話 テスト1")
            self.assertEqual(s, "titles.toml")
            (w / "analysis" / "titles.toml").unlink()
            t, s = bse.resolve_title(w, "11111111", prog)
            self.assertEqual(s, "進捗.md 訳題")
            t, s = bse.resolve_title(w, "11111111", None)
            self.assertEqual(s, "原題（要確認）")
            self.assertEqual(t, "原作第1話")


class TestNormalize(unittest.TestCase):
    def test_scaffold_and_blank_collapse(self):
        raw = "<!-- ch001 -->\n# ch001\n\nA\n\n\n\nB\n"
        self.assertEqual(bse.normalize_body(raw), "A\n\nB\n")

    def test_seam_padding(self):
        raw = "テキスト\n---\n次\n"
        self.assertEqual(bse.normalize_body(raw), "テキスト\n\n---\n\n次\n")

    def test_heading_kept(self):
        raw = "# タイトル\n\n本文\n"
        self.assertEqual(bse.normalize_body(raw), "# タイトル\n\n本文\n")


class TestBuild(unittest.TestCase):
    def build(self, td, saga=None, **kw):
        root = Path(td)
        if saga is None:
            saga = make_saga(root, **kw)
        out = root / "out" / "シリーズ名"
        args = type("A", (), {
            "dest": str(out), "title": None, "note": "原作：テスト", "note_hon": None,
            "footer": "翻訳情報", "works": None, "check": False, "repair": False,
            "force": False, "eol": "crlf", "no_untranslated": False, "no_verify": False,
        })()
        built, unfinished = bse.list_works(saga)
        prog = bse.parse_progress(saga)
        order, osrc = bse.resolve_order(built, None, prog)
        infos = [{"wid": w, "work": built[w], "title": bse.resolve_title(built[w], w, prog)[0],
                  "title_src": bse.resolve_title(built[w], w, prog)[1]} for w in order]
        rc = bse.cmd_build(args, saga, built, unfinished, infos, osrc, prog)
        return rc, out, saga

    def test_build_full(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, saga = self.build(td)
            self.assertEqual(rc, 0)
            files = sorted(f for f in os.listdir(out) if os.path.isfile(out / f))
            self.assertEqual(files, ["01_第1話 テスト1.md", "02_第2話 テスト2.md", "合本.md", "目次.md"])
            for f in files:
                raw = (out / f).read_bytes()
                self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), f)
            p1 = (out / "01_第1話 テスト1.md").read_text(encoding="utf-8")
            self.assertTrue(p1.startswith("# 第1話 テスト1"))
            self.assertNotIn("<!--", p1)
            self.assertNotIn("# ch001", p1)
            self.assertEqual(len(os.listdir(out / "images")), 4)
            g = (out / "合本.md").read_text(encoding="utf-8").replace("\r\n", "\n")
            self.assertLess(g.index("# 第1話 テスト1"), g.index("# 第2話 テスト2"))
            self.assertNotIn("\n---\n---\n", g)
            self.assertEqual(bse.setext_hazards(g), [])
            self.assertEqual(len(bse.IMG_RE.findall(g)), 4)
            toc = (out / "目次.md").read_text(encoding="utf-8")
            for l in re.findall(r"\]\(([^)]+)\)", toc):
                self.assertTrue((out / l.replace("%20", " ")).exists(), l)
            self.assertIn("未翻訳", toc)
            self.assertIn("#9", toc)

    def test_missing_heading_prepended(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = root / "_saga"
            saga.mkdir(parents=True)
            make_work(saga, "11111111", title_ja="第1話 テスト1", with_scaffold=True)
            out = root / "out" / "S"
            args = type("A", (), {
                "dest": str(out), "title": None, "note": "", "note_hon": None,
                "footer": "", "works": None, "check": False, "repair": False,
                "force": False, "eol": "lf", "no_untranslated": True, "no_verify": False,
            })()
            built, un = bse.list_works(saga)
            infos = [{"wid": "11111111", "work": built["11111111"], "title": "第1話 テスト1",
                      "title_src": "titles.toml"}]
            rc = bse.cmd_build(args, saga, built, un, infos, "test", None)
            self.assertEqual(rc, 0)
            p = (out / "01_第1話 テスト1.md").read_text(encoding="utf-8")
            self.assertTrue(p.startswith("# 第1話 テスト1\n\n"), p[:40])

    def test_image_collision_aborts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root)
            for wid in ("11111111", "22222222"):
                imgdir = saga / wid / "output" / "images"
                for f in imgdir.iterdir():
                    f.unlink()
                (imgdir / "same.png").write_bytes(b"x")
                p = saga / wid / "output" / "全文_訳文.md"
                p.write_text(f"![a](images/same.png)\r\n", encoding="utf-8")
            with self.assertRaises(bse.BuildError) as cm:
                self.build(td, saga=saga)
            self.assertIn("衝突", str(cm.exception))

    def test_force_guard_rejects_drive_root(self):
        # ドライブルート直下は len(parts) < 3 で必ず BuildError。
        # Windows では Path("C:/").resolve() = Path("C:/")。POSIX では /tmp/foo が
        # resolve で深くならないことを利用し、固定でルート直下を使う。
        # Windows では C:/x は parts=3 で BuildError にならない（元テストの
        # 失敗理由）ため、ルート直下の Path.cwd().anchor の直下を使う。
        root = Path(Path.cwd().anchor)
        with self.assertRaises(bse.BuildError):
            bse.guard_destructive(root)

    def test_existing_dest_without_force_aborts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root)
            out = root / "out" / "S"
            out.mkdir(parents=True)
            (out / "junk.txt").write_text("x")
            args = type("A", (), {
                "dest": str(out), "title": None, "note": "", "note_hon": None,
                "footer": "", "works": None, "check": False, "repair": False,
                "force": False, "eol": "crlf", "no_untranslated": True, "no_verify": False,
            })()
            built, un = bse.list_works(saga)
            infos = [{"wid": "11111111", "work": built["11111111"], "title": "T", "title_src": "x"}]
            with self.assertRaises(bse.BuildError):
                bse.cmd_build(args, saga, built, un, infos, "t", None)


class TestCheck(unittest.TestCase):
    def test_check_reports_missing_titles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root, titles=False, progress=False)
            built, un = bse.list_works(saga)
            infos = []
            for wid in sorted(built):
                t, s = bse.resolve_title(built[wid], wid, None)
                infos.append({"wid": wid, "work": built[wid], "title": t, "title_src": s})
            rc = bse.cmd_check(saga, built, un, infos, "test", None)
            self.assertEqual(rc, 1)

    def test_check_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root)
            built, un = bse.list_works(saga)
            prog = bse.parse_progress(saga)
            infos = []
            for wid in sorted(built):
                t, s = bse.resolve_title(built[wid], wid, prog)
                infos.append({"wid": wid, "work": built[wid], "title": t, "title_src": s})
            rc = bse.cmd_check(saga, built, un, infos, "test", prog)
            self.assertEqual(rc, 0)


class TestChapterOnlyWorks(unittest.TestCase):
    """G25: 全文_訳文.md が無くても、全章の章ファイルが揃っていればビルド対象にする。"""

    def test_list_works_accepts_chapter_files_without_full(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root, works=("11111111",), progress=False)
            w = saga / "11111111"
            (w / "output" / "全文_訳文.md").unlink()
            (w / "output" / "ch001_ja.md").write_text(
                "# 第1話 テスト1\n\n本文。\n", encoding="utf-8")
            built, unfinished = bse.list_works(saga)
            self.assertIn("11111111", built)
            self.assertEqual(unfinished, [])

    def test_list_works_rejects_partial_chapter_files(self):
        # chapters_meta が2章を列挙しているのに1章しか無い部分出力は完成扱いしない
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = root / "_saga"
            saga.mkdir(parents=True)
            w = saga / "11111111"
            (w / "output").mkdir(parents=True)
            (w / "manifest.toml").write_text('title = "x"\n', encoding="utf-8")
            (w / "chapters_meta.toml").write_text(
                '[work]\ntitle = "x"\n\n[[chapters]]\nid = "ch001"\n\n[[chapters]]\nid = "ch002"\n',
                encoding="utf-8")
            (w / "output" / "ch001_ja.md").write_text("# 一章\n\n本文。\n", encoding="utf-8")
            built, unfinished = bse.list_works(saga)
            self.assertEqual(built, {})
            self.assertEqual(unfinished, ["11111111"])

    def test_build_from_chapter_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root, works=("11111111",), progress=False)
            w = saga / "11111111"
            (w / "output" / "全文_訳文.md").unlink()
            (w / "output" / "ch001_ja.md").write_text(
                "# 第1話 テスト1\n\n本文その1。11111111\n\n「セリフ」\n\n本文その2。\n"
                "\n![img](images/11111111-0.png)\n\n![img](images/11111111-1.png)\n",
                encoding="utf-8")
            out = root / "out" / "S"
            args = type("A", (), {
                "dest": str(out), "title": None, "note": "", "note_hon": None,
                "footer": "", "works": None, "check": False, "repair": False,
                "force": False, "eol": "crlf", "no_untranslated": True, "no_verify": False,
            })()
            built, un = bse.list_works(saga)
            infos = [{"wid": "11111111", "work": built["11111111"], "title": "第1話 テスト1",
                      "title_src": "titles.toml"}]
            rc = bse.cmd_build(args, saga, built, un, infos, "test", None)
            self.assertEqual(rc, 0)
            files = sorted(f for f in os.listdir(out) if os.path.isfile(out / f))
            self.assertEqual(files, ["01_第1話 テスト1.md", "合本.md", "目次.md"])


class TestHelpers(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(bse.sanitize_filename('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(bse.sanitize_filename("（前編）"), "（前編）")
        self.assertEqual(bse.sanitize_filename("(x) #1"), "（x） ＃1")

    def test_link_target(self):
        self.assertEqual(bse.link_target("01_a b.md"), "01_a%20b.md")

    def test_setext_hazards(self):
        self.assertEqual(bse.setext_hazards("a\n---\nb\n"), [2])
        self.assertEqual(bse.setext_hazards("a\n\n---\n\nb\n"), [])

    def test_repair_creates_titles_and_heading(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saga = make_saga(root, titles=False, progress=False)
            wid = "11111111"
            w = saga / wid
            infos = [{"wid": wid, "work": w, "title": "第1話 復元", "title_src": "進捗.md 訳題"}]
            bse.repair_sources(infos, None)
            self.assertTrue((w / "analysis" / "titles.toml").exists())
            text = (w / "output" / "全文_訳文.md").read_text(encoding="utf-8-sig")
            self.assertTrue(text.startswith("# 第1話 復元"), text[:30])
            bse.repair_sources(infos, None)
            text2 = (w / "output" / "全文_訳文.md").read_text(encoding="utf-8-sig")
            self.assertEqual(text, text2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
